# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2025 Nautech Systems Pty Ltd. All rights reserved.
#  https://nautechsystems.io
#
#  Licensed under the GNU Lesser General Public License Version 3.0 (the "License");
#  You may not use this file except in compliance with the License.
#  You may obtain a copy of the License at https://www.gnu.org/licenses/lgpl-3.0.en.html
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
# -------------------------------------------------------------------------------------------------
"""
Bybit execution client implementation.

This module provides a LiveExecutionClient that interfaces with Bybit's REST and
WebSocket APIs for order management and execution. The client uses Rust-based HTTP and
WebSocket clients exposed via PyO3 for performance.

"""

import asyncio
from typing import Any

from nautilus_trader.accounting.factory import AccountFactory
from nautilus_trader.adapters.bybit.config import BybitExecClientConfig
from nautilus_trader.adapters.bybit.constants import BYBIT_VENUE
from nautilus_trader.adapters.bybit.providers import BybitInstrumentProvider
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock
from nautilus_trader.common.component import MessageBus
from nautilus_trader.common.enums import LogColor
from nautilus_trader.common.enums import LogLevel
from nautilus_trader.core import nautilus_pyo3
from nautilus_trader.core.correctness import PyCondition
from nautilus_trader.core.datetime import ensure_pydatetime_utc
from nautilus_trader.core.nautilus_pyo3 import BybitAccountType
from nautilus_trader.core.nautilus_pyo3 import BybitProductType
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.messages import CancelAllOrders
from nautilus_trader.execution.messages import CancelOrder
from nautilus_trader.execution.messages import GenerateFillReports
from nautilus_trader.execution.messages import GenerateOrderStatusReport
from nautilus_trader.execution.messages import GenerateOrderStatusReports
from nautilus_trader.execution.messages import GeneratePositionStatusReports
from nautilus_trader.execution.messages import ModifyOrder
from nautilus_trader.execution.messages import QueryAccount
from nautilus_trader.execution.messages import SubmitOrder
from nautilus_trader.execution.messages import SubmitOrderList
from nautilus_trader.execution.reports import FillReport
from nautilus_trader.execution.reports import OrderStatusReport
from nautilus_trader.execution.reports import PositionStatusReport
from nautilus_trader.live.execution_client import LiveExecutionClient
from nautilus_trader.model.enums import AccountType
from nautilus_trader.model.enums import OmsType
from nautilus_trader.model.events import AccountState
from nautilus_trader.model.events import OrderCancelRejected
from nautilus_trader.model.events import OrderModifyRejected
from nautilus_trader.model.events import OrderRejected
from nautilus_trader.model.functions import order_side_to_pyo3
from nautilus_trader.model.functions import order_type_to_pyo3
from nautilus_trader.model.functions import time_in_force_to_pyo3
from nautilus_trader.model.identifiers import AccountId
from nautilus_trader.model.identifiers import ClientId
from nautilus_trader.model.orders import Order


class BybitExecutionClient(LiveExecutionClient):
    """
    Execution client for Bybit exchange.

    Provides order management and execution via Bybit's REST and WebSocket APIs.
    Supports both HTTP and WebSocket-based order submission.

    Parameters
    ----------
    loop : asyncio.AbstractEventLoop
        The event loop for the client.
    client : nautilus_pyo3.BybitHttpClient
        The Bybit HTTP client.
    msgbus : MessageBus
        The message bus for the client.
    cache : Cache
        The cache for the client.
    clock : LiveClock
        The clock for the client.
    instrument_provider : BybitInstrumentProvider
        The instrument provider.
    config : BybitExecClientConfig
        The configuration for the client.
    name : str, optional
        The custom client ID.

    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        client: nautilus_pyo3.BybitHttpClient,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
        instrument_provider: BybitInstrumentProvider,
        config: BybitExecClientConfig,
        name: str | None,
    ) -> None:
        PyCondition.not_empty(config.product_types, "config.product_types")
        assert config.product_types is not None  # Type narrowing for mypy

        # Determine account type based on product types
        if set(config.product_types) == {BybitProductType.SPOT}:
            self._account_type = AccountType.CASH
            # Bybit SPOT accounts support margin trading (borrowing)
            AccountFactory.register_cash_borrowing(BYBIT_VENUE.value)
        else:
            # UTA (Unified Trading Account) for derivatives or mixed products
            self._account_type = AccountType.MARGIN

        super().__init__(
            loop=loop,
            client_id=ClientId(name or BYBIT_VENUE.value),
            venue=BYBIT_VENUE,
            oms_type=OmsType.NETTING,
            instrument_provider=instrument_provider,
            account_type=self._account_type,
            base_currency=None,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
        )

        # Configuration
        self._config = config
        self._product_types = list(config.product_types)
        self._log.info(
            f"config.product_types={[str(p) for p in config.product_types]}",
            LogColor.BLUE,
        )
        self._log.info(f"{config.testnet=}", LogColor.BLUE)
        self._log.info(f"{config.use_gtd=}", LogColor.BLUE)
        self._log.info(f"{config.use_ws_trade_api=}", LogColor.BLUE)
        self._log.info(f"{config.ws_trade_timeout_secs=}", LogColor.BLUE)

        # Set account ID
        account_id = AccountId(f"{name or BYBIT_VENUE.value}-UNIFIED")
        self._set_account_id(account_id)

        # Create pyo3 account ID for Rust HTTP client
        self.pyo3_account_id = nautilus_pyo3.AccountId(account_id.value)

        # HTTP API
        self._http_client = client
        self._log.info(f"REST API key {self._http_client.api_key}", LogColor.BLUE)

        # WebSocket API - environment setup
        environment = (
            nautilus_pyo3.BybitEnvironment.TESTNET
            if config.testnet
            else nautilus_pyo3.BybitEnvironment.MAINNET
        )

        # WebSocket API - Private channel
        self._ws_private_client = nautilus_pyo3.BybitWebSocketClient.new_private(
            environment=environment,
            api_key=config.api_key or "",
            api_secret=config.api_secret or "",
            url=config.base_url_ws_private,
            heartbeat=20,
        )
        self._ws_client_futures: set[asyncio.Future] = set()

        # WebSocket API - Trade channel (optional)
        self._use_ws_trade_api = config.use_ws_trade_api
        if self._use_ws_trade_api:
            self._ws_trade_client: nautilus_pyo3.BybitWebSocketClient | None = (
                nautilus_pyo3.BybitWebSocketClient.new_trade(
                    environment=environment,
                    api_key=config.api_key or "",
                    api_secret=config.api_secret or "",
                    url=config.base_url_ws_trade,
                    heartbeat=20,
                )
            )
        else:
            self._ws_trade_client = None

    @property
    def bybit_instrument_provider(self) -> BybitInstrumentProvider:
        return self._instrument_provider  # type: ignore

    async def _connect(self) -> None:
        await self._instrument_provider.initialize()
        await self._cache_instruments()
        await self._update_account_state()

        # Connect to private WebSocket
        await self._ws_private_client.connect(callback=self._handle_msg)

        # Wait for connection to be established
        await self._ws_private_client.wait_until_active(timeout_secs=10.0)
        self._log.info("Connected to private WebSocket", LogColor.BLUE)

        # Subscribe to private channels
        try:
            await self._ws_private_client.subscribe_orders()
            await self._ws_private_client.subscribe_executions()
            await self._ws_private_client.subscribe_positions()
            await self._ws_private_client.subscribe_wallet()
        except Exception as e:
            self._log.error(f"Failed to subscribe to private channels: {e}")
            raise

        # Connect to trade WebSocket if enabled
        if self._use_ws_trade_api and self._ws_trade_client:
            await self._ws_trade_client.connect(callback=self._handle_msg)
            await self._ws_trade_client.wait_until_active(timeout_secs=10.0)
            self._log.info("Connected to trade WebSocket", LogColor.BLUE)

    async def _disconnect(self) -> None:
        # Close private WebSocket
        if await self._ws_private_client.is_active():
            self._log.info("Disconnecting private websocket")
            await self._ws_private_client.close()

        # Close trade WebSocket
        if (
            self._use_ws_trade_api
            and self._ws_trade_client
            and await self._ws_trade_client.is_active()
        ):
            self._log.info("Disconnecting trade websocket")
            await self._ws_trade_client.close()

        # Cancel any pending futures
        for future in self._ws_client_futures:
            if not future.done():
                future.cancel()

        if self._ws_client_futures:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*self._ws_client_futures, return_exceptions=True),
                    timeout=2.0,
                )
            except TimeoutError:
                self._log.warning("Timeout while waiting for websockets shutdown to complete")

        self._ws_client_futures.clear()

    async def _cache_instruments(self) -> None:
        # Ensures instrument definitions are available for correct
        # price and size precisions when parsing responses
        instruments_pyo3 = self.bybit_instrument_provider.instruments_pyo3()
        for inst in instruments_pyo3:
            self._http_client.add_instrument(inst)

        self._log.debug("Cached instruments", LogColor.MAGENTA)

    async def _update_account_state(self) -> None:
        try:
            # Determine account type
            if self._account_type == AccountType.CASH:
                account_type = BybitAccountType.UNIFIED  # Spot uses unified account
            else:
                account_type = BybitAccountType.UNIFIED

            pyo3_account_state = await self._http_client.request_account_state(
                account_type=account_type,
                account_id=self.pyo3_account_id,
            )
            account_state = AccountState.from_dict(pyo3_account_state.to_dict())

            self.generate_account_state(
                balances=account_state.balances,
                margins=account_state.margins,
                reported=True,
                ts_event=self._clock.timestamp_ns(),
            )
        except Exception as e:
            self._log.error(f"Failed to update account state: {e}")

    # -- EXECUTION REPORTS ------------------------------------------------------------------------

    async def generate_order_status_reports(
        self,
        command: GenerateOrderStatusReports,
    ) -> list[OrderStatusReport]:
        self._log.debug(
            f"Requesting OrderStatusReports "
            f"{repr(command.instrument_id) if command.instrument_id else ''}"
            "...",
        )

        pyo3_reports: list[nautilus_pyo3.OrderStatusReport] = []
        reports: list[OrderStatusReport] = []

        try:
            for product_type in self._product_types:
                pyo3_instrument_id = None
                if command.instrument_id:
                    pyo3_instrument_id = nautilus_pyo3.InstrumentId.from_str(
                        command.instrument_id.value,
                    )

                response = await self._http_client.request_order_status_reports(
                    product_type=product_type,
                    instrument_id=pyo3_instrument_id,
                    open_only=command.open_only,
                )
                pyo3_reports.extend(response)

            for pyo3_report in pyo3_reports:
                report = OrderStatusReport.from_pyo3(pyo3_report)
                self._log.debug(f"Received {report}", LogColor.MAGENTA)
                reports.append(report)
        except ValueError as exc:
            if "request canceled" in str(exc).lower():
                self._log.debug("OrderStatusReports request cancelled during shutdown")
            else:
                self._log.exception("Failed to generate OrderStatusReports", exc)
        except Exception as e:
            self._log.exception("Failed to generate OrderStatusReports", e)

        len_reports = len(reports)
        plural = "" if len_reports == 1 else "s"
        receipt_log = f"Received {len(reports)} OrderStatusReport{plural}"

        if command.log_receipt_level == LogLevel.INFO:
            self._log.info(receipt_log)
        else:
            self._log.debug(receipt_log)

        return reports

    async def generate_order_status_report(
        self,
        command: GenerateOrderStatusReport,
    ) -> OrderStatusReport | None:
        self._log.debug(
            f"Requesting OrderStatusReport for {command.client_order_id!r}...",
        )

        try:
            # Determine product type (simplified - use first)
            product_type = (
                self._product_types[0] if self._product_types else BybitProductType.LINEAR
            )

            pyo3_instrument_id = nautilus_pyo3.InstrumentId.from_str(command.instrument_id.value)
            pyo3_client_order_id = (
                nautilus_pyo3.ClientOrderId(command.client_order_id.value)
                if command.client_order_id
                else None
            )
            pyo3_venue_order_id = (
                nautilus_pyo3.VenueOrderId(command.venue_order_id.value)
                if command.venue_order_id
                else None
            )

            pyo3_report = await self._http_client.query_order(
                product_type=product_type,
                instrument_id=pyo3_instrument_id,
                account_id=self.pyo3_account_id,
                client_order_id=pyo3_client_order_id,
                venue_order_id=pyo3_venue_order_id,
            )

            if pyo3_report is None:
                self._log.warning(f"No order status report found for {command.client_order_id!r}")
                return None

            report = OrderStatusReport.from_pyo3(pyo3_report)
            self._log.debug(f"Received {report}", LogColor.MAGENTA)
            return report
        except Exception as e:
            self._log.exception(
                f"Failed to generate OrderStatusReport for {command.client_order_id!r}",
                e,
            )
            return None

    async def generate_fill_reports(
        self,
        command: GenerateFillReports,
    ) -> list[FillReport]:
        self._log.debug(
            f"Requesting FillReports "
            f"{repr(command.instrument_id) if command.instrument_id else ''}"
            "...",
        )

        pyo3_reports: list[nautilus_pyo3.FillReport] = []
        reports: list[FillReport] = []

        try:
            for product_type in self._product_types:
                pyo3_instrument_id = None
                if command.instrument_id:
                    pyo3_instrument_id = nautilus_pyo3.InstrumentId.from_str(
                        command.instrument_id.value,
                    )

                start_ms = None
                end_ms = None
                if command.start:
                    start_dt = ensure_pydatetime_utc(command.start)
                    if start_dt:
                        start_ms = int(start_dt.timestamp() * 1000)
                if command.end:
                    end_dt = ensure_pydatetime_utc(command.end)
                    if end_dt:
                        end_ms = int(end_dt.timestamp() * 1000)

                response = await self._http_client.request_fill_reports(
                    product_type=product_type,
                    instrument_id=pyo3_instrument_id,
                    start=start_ms,
                    end=end_ms,
                )
                pyo3_reports.extend(response)

            for pyo3_report in pyo3_reports:
                report = FillReport.from_pyo3(pyo3_report)
                self._log.debug(f"Received {report}", LogColor.MAGENTA)
                reports.append(report)
        except Exception as e:
            self._log.exception("Failed to generate FillReports", e)

        len_reports = len(reports)
        plural = "" if len_reports == 1 else "s"
        self._log.info(f"Received {len(reports)} FillReport{plural}")

        return reports

    async def generate_position_status_reports(
        self,
        command: GeneratePositionStatusReports,
    ) -> list[PositionStatusReport]:
        self._log.debug("Requesting PositionStatusReports...")

        pyo3_reports: list[nautilus_pyo3.PositionStatusReport] = []
        reports: list[PositionStatusReport] = []

        try:
            for product_type in self._product_types:
                pyo3_instrument_id = None
                if command.instrument_id:
                    pyo3_instrument_id = nautilus_pyo3.InstrumentId.from_str(
                        command.instrument_id.value,
                    )

                response = await self._http_client.request_position_status_reports(
                    product_type=product_type,
                    instrument_id=pyo3_instrument_id,
                )
                pyo3_reports.extend(response)

            for pyo3_report in pyo3_reports:
                report = PositionStatusReport.from_pyo3(pyo3_report)
                self._log.debug(f"Received {report}", LogColor.MAGENTA)
                reports.append(report)
        except Exception as e:
            self._log.exception("Failed to generate PositionStatusReports", e)

        len_reports = len(reports)
        plural = "" if len_reports == 1 else "s"
        self._log.info(f"Received {len(reports)} PositionStatusReport{plural}")

        return reports

    # -- COMMAND HANDLERS -------------------------------------------------------------------------

    async def _query_account(self, _command: QueryAccount) -> None:
        await self._update_account_state()

    async def _submit_order(self, command: SubmitOrder) -> None:
        order = command.order

        if order.is_closed:
            self._log.warning(f"Cannot submit already closed order: {order}")
            return

        # Generate OrderSubmitted event
        self.generate_order_submitted(
            strategy_id=order.strategy_id,
            instrument_id=order.instrument_id,
            client_order_id=order.client_order_id,
            ts_event=self._clock.timestamp_ns(),
        )

        # Convert to PyO3 types
        pyo3_instrument_id = nautilus_pyo3.InstrumentId.from_str(order.instrument_id.value)
        pyo3_client_order_id = nautilus_pyo3.ClientOrderId(order.client_order_id.value)
        pyo3_order_side = order_side_to_pyo3(order.side)
        pyo3_order_type = order_type_to_pyo3(order.order_type)
        pyo3_quantity = nautilus_pyo3.Quantity.from_str(str(order.quantity))
        pyo3_price = nautilus_pyo3.Price.from_str(str(order.price)) if order.has_price else None
        pyo3_time_in_force = (
            time_in_force_to_pyo3(order.time_in_force) if order.time_in_force else None
        )

        # Determine product type (simplified - use first)
        product_type = self._product_types[0] if self._product_types else BybitProductType.LINEAR

        try:
            if self._use_ws_trade_api and self._ws_trade_client:
                # Submit via WebSocket
                await self._ws_trade_client.submit_order(
                    product_type=product_type,
                    instrument_id=pyo3_instrument_id,
                    client_order_id=pyo3_client_order_id,
                    order_side=pyo3_order_side,
                    order_type=pyo3_order_type,
                    quantity=pyo3_quantity,
                    time_in_force=pyo3_time_in_force,
                    price=pyo3_price,
                    reduce_only=order.is_reduce_only if hasattr(order, "is_reduce_only") else False,
                )
            else:
                # Submit via HTTP
                await self._http_client.submit_order(
                    product_type=product_type,
                    instrument_id=pyo3_instrument_id,
                    client_order_id=pyo3_client_order_id,
                    order_side=pyo3_order_side,
                    order_type=pyo3_order_type,
                    quantity=pyo3_quantity,
                    time_in_force=pyo3_time_in_force or nautilus_pyo3.TimeInForce.GTC,
                    price=pyo3_price,
                    reduce_only=order.is_reduce_only if hasattr(order, "is_reduce_only") else False,
                )
        except Exception as e:
            self._log.error(f"Failed to submit order {order.client_order_id}: {e}")
            self.generate_order_rejected(
                strategy_id=order.strategy_id,
                instrument_id=order.instrument_id,
                client_order_id=order.client_order_id,
                reason=str(e),
                ts_event=self._clock.timestamp_ns(),
            )

    async def _submit_order_list(self, command: SubmitOrderList) -> None:
        # Submit orders one by one (batch submission can be added later)
        for order in command.order_list.orders:
            submit_order = SubmitOrder(
                trader_id=command.trader_id,
                strategy_id=command.strategy_id,
                order=order,
                command_id=UUID4(),
                ts_init=self._clock.timestamp_ns(),
            )
            await self._submit_order(submit_order)

    async def _modify_order(self, command: ModifyOrder) -> None:
        order: Order | None = self._cache.order(command.client_order_id)
        if order is None:
            self._log.error(f"{command.client_order_id!r} not found in cache")
            return

        if order.is_closed:
            self._log.warning(
                f"`ModifyOrder` command for {command.client_order_id!r} when order already {order.status_string()} "
                "(will not send to exchange)",
            )
            return

        # Convert to PyO3 types
        pyo3_instrument_id = nautilus_pyo3.InstrumentId.from_str(command.instrument_id.value)
        pyo3_client_order_id = (
            nautilus_pyo3.ClientOrderId(command.client_order_id.value)
            if command.client_order_id
            else None
        )
        pyo3_venue_order_id = (
            nautilus_pyo3.VenueOrderId(command.venue_order_id.value)
            if command.venue_order_id
            else None
        )
        pyo3_quantity = (
            nautilus_pyo3.Quantity.from_str(str(command.quantity)) if command.quantity else None
        )
        pyo3_price = nautilus_pyo3.Price.from_str(str(command.price)) if command.price else None

        # Determine product type
        product_type = self._product_types[0] if self._product_types else BybitProductType.LINEAR

        try:
            if self._use_ws_trade_api and self._ws_trade_client:
                # Modify via WebSocket
                await self._ws_trade_client.modify_order(
                    product_type=product_type,
                    instrument_id=pyo3_instrument_id,
                    venue_order_id=pyo3_venue_order_id,
                    client_order_id=pyo3_client_order_id,
                    quantity=pyo3_quantity,
                    price=pyo3_price,
                )
            else:
                # Modify via HTTP
                await self._http_client.modify_order(
                    product_type=product_type,
                    instrument_id=pyo3_instrument_id,
                    client_order_id=pyo3_client_order_id,
                    venue_order_id=pyo3_venue_order_id,
                    quantity=pyo3_quantity,
                    price=pyo3_price,
                )
        except Exception as e:
            self._log.error(f"Failed to modify order {command.client_order_id}: {e}")
            self.generate_order_modify_rejected(
                strategy_id=order.strategy_id,
                instrument_id=order.instrument_id,
                client_order_id=order.client_order_id,
                venue_order_id=order.venue_order_id,
                reason=str(e),
                ts_event=self._clock.timestamp_ns(),
            )

    async def _cancel_order(self, command: CancelOrder) -> None:
        order: Order | None = self._cache.order(command.client_order_id)
        if order is None:
            self._log.error(f"{command.client_order_id!r} not found in cache")
            return

        if order.is_closed:
            self._log.warning(
                f"`CancelOrder` command for {command.client_order_id!r} when order already {order.status_string()} "
                "(will not send to exchange)",
            )
            return

        # Convert to PyO3 types
        pyo3_instrument_id = nautilus_pyo3.InstrumentId.from_str(command.instrument_id.value)
        pyo3_client_order_id = (
            nautilus_pyo3.ClientOrderId(command.client_order_id.value)
            if command.client_order_id
            else None
        )
        pyo3_venue_order_id = (
            nautilus_pyo3.VenueOrderId(command.venue_order_id.value)
            if command.venue_order_id
            else None
        )

        # Determine product type
        product_type = self._product_types[0] if self._product_types else BybitProductType.LINEAR

        try:
            if self._use_ws_trade_api and self._ws_trade_client:
                # Cancel via WebSocket
                await self._ws_trade_client.cancel_order(
                    product_type=product_type,
                    instrument_id=pyo3_instrument_id,
                    venue_order_id=pyo3_venue_order_id,
                    client_order_id=pyo3_client_order_id,
                )
            else:
                # Cancel via HTTP
                await self._http_client.cancel_order(
                    product_type=product_type,
                    instrument_id=pyo3_instrument_id,
                    client_order_id=pyo3_client_order_id,
                    venue_order_id=pyo3_venue_order_id,
                )
        except Exception as e:
            self._log.error(f"Failed to cancel order {command.client_order_id}: {e}")
            self.generate_order_cancel_rejected(
                strategy_id=order.strategy_id,
                instrument_id=order.instrument_id,
                client_order_id=order.client_order_id,
                venue_order_id=order.venue_order_id,
                reason=str(e),
                ts_event=self._clock.timestamp_ns(),
            )

    async def _cancel_all_orders(self, command: CancelAllOrders) -> None:
        # Convert to PyO3 types
        pyo3_instrument_id = nautilus_pyo3.InstrumentId.from_str(command.instrument_id.value)

        # Determine product type
        product_type = self._product_types[0] if self._product_types else BybitProductType.LINEAR

        try:
            reports = await self._http_client.cancel_all_orders(
                product_type=product_type,
                instrument_id=pyo3_instrument_id,
                account_id=self.pyo3_account_id,
            )

            for pyo3_report in reports:
                report = OrderStatusReport.from_pyo3(pyo3_report)
                self._log.debug(f"Cancelled order: {report}", LogColor.MAGENTA)
        except Exception as e:
            self._log.error(f"Failed to cancel all orders for {command.instrument_id}: {e}")

    # -- MESSAGE HANDLERS -------------------------------------------------------------------------

    def _handle_msg(self, msg: Any) -> None:
        """
        Handle messages from WebSocket.
        """
        if isinstance(msg, nautilus_pyo3.BybitWebSocketError):
            self._log.error(f"WebSocket error: {msg}")
        elif isinstance(msg, nautilus_pyo3.AccountState):
            self._handle_account_state(msg)
        elif isinstance(msg, nautilus_pyo3.OrderRejected):
            self._handle_order_rejected_pyo3(msg)
        elif isinstance(msg, nautilus_pyo3.OrderCancelRejected):
            self._handle_order_cancel_rejected_pyo3(msg)
        elif isinstance(msg, nautilus_pyo3.OrderModifyRejected):
            self._handle_order_modify_rejected_pyo3(msg)
        elif isinstance(msg, nautilus_pyo3.OrderStatusReport):
            self._handle_order_status_report_pyo3(msg)
        elif isinstance(msg, nautilus_pyo3.FillReport):
            self._handle_fill_report_pyo3(msg)
        elif isinstance(msg, nautilus_pyo3.PositionStatusReport):
            self._handle_position_status_report_pyo3(msg)
        elif isinstance(msg, str):
            # Raw JSON message - log for debugging
            self._log.debug(f"Received raw message: {msg[:200]}")
        else:
            self._log.debug(f"Received unhandled message type: {type(msg)}")

    def _handle_account_state(self, msg: nautilus_pyo3.AccountState) -> None:
        account_state = AccountState.from_pyo3(msg)
        self.generate_account_state(
            balances=account_state.balances,
            margins=account_state.margins,
            reported=True,
            ts_event=account_state.ts_event,
        )

    def _handle_order_rejected_pyo3(self, msg: nautilus_pyo3.OrderRejected) -> None:
        event = OrderRejected.from_pyo3(msg)
        self._send_order_event(event)

    def _handle_order_cancel_rejected_pyo3(self, msg: nautilus_pyo3.OrderCancelRejected) -> None:
        event = OrderCancelRejected.from_pyo3(msg)
        self._send_order_event(event)

    def _handle_order_modify_rejected_pyo3(self, msg: nautilus_pyo3.OrderModifyRejected) -> None:
        event = OrderModifyRejected.from_pyo3(msg)
        self._send_order_event(event)

    def _handle_order_status_report_pyo3(self, msg: nautilus_pyo3.OrderStatusReport) -> None:
        report = OrderStatusReport.from_pyo3(msg)
        self._log.debug(f"Received {report}", LogColor.MAGENTA)
        self._send_order_status_report(report)

    def _handle_fill_report_pyo3(self, msg: nautilus_pyo3.FillReport) -> None:
        report = FillReport.from_pyo3(msg)
        self._log.debug(f"Received {report}", LogColor.MAGENTA)
        self.generate_fill_report(report)

    def _handle_position_status_report_pyo3(self, msg: nautilus_pyo3.PositionStatusReport) -> None:
        report = PositionStatusReport.from_pyo3(msg)
        self._log.debug(f"Received {report}", LogColor.MAGENTA)
        self.generate_position_status_report(report)
