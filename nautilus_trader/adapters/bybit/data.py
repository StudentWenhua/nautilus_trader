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

import asyncio

from nautilus_trader.adapters.bybit.config import BybitDataClientConfig
from nautilus_trader.adapters.bybit.constants import BYBIT_VENUE
from nautilus_trader.adapters.bybit.providers import BybitInstrumentProvider
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock
from nautilus_trader.common.component import MessageBus
from nautilus_trader.common.enums import LogColor
from nautilus_trader.core import nautilus_pyo3
from nautilus_trader.core.correctness import PyCondition
from nautilus_trader.data.messages import SubscribeBars
from nautilus_trader.data.messages import SubscribeFundingRates
from nautilus_trader.data.messages import SubscribeOrderBook
from nautilus_trader.data.messages import SubscribeQuoteTicks
from nautilus_trader.data.messages import SubscribeTradeTicks
from nautilus_trader.data.messages import UnsubscribeBars
from nautilus_trader.data.messages import UnsubscribeFundingRates
from nautilus_trader.data.messages import UnsubscribeOrderBook
from nautilus_trader.data.messages import UnsubscribeQuoteTicks
from nautilus_trader.data.messages import UnsubscribeTradeTicks
from nautilus_trader.live.cancellation import DEFAULT_FUTURE_CANCELLATION_TIMEOUT
from nautilus_trader.live.cancellation import cancel_tasks_with_timeout
from nautilus_trader.live.data_client import LiveMarketDataClient
from nautilus_trader.model.data import capsule_to_data
from nautilus_trader.model.enums import BookType
from nautilus_trader.model.enums import book_type_to_str
from nautilus_trader.model.identifiers import ClientId


class BybitDataClient(LiveMarketDataClient):
    """
    Provides a data client for the Bybit centralized crypto exchange.

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
    config : BybitDataClientConfig
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
        config: BybitDataClientConfig,
        name: str | None,
    ) -> None:
        PyCondition.not_empty(config.product_types, "config.product_types")
        assert config.product_types is not None  # Type narrowing for mypy
        super().__init__(
            loop=loop,
            client_id=ClientId(name or BYBIT_VENUE.value),
            venue=BYBIT_VENUE,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=instrument_provider,
        )

        self._instrument_provider: BybitInstrumentProvider = instrument_provider

        # Configuration
        self._config = config
        self._log.info(
            f"config.product_types={[str(p) for p in config.product_types]}",
            LogColor.BLUE,
        )

        # HTTP API
        self._http_client = client
        self._log.info(f"REST API key {self._http_client.api_key}", LogColor.BLUE)

        # WebSocket API - create clients for each product type (public endpoints)
        self._ws_clients: dict[
            nautilus_pyo3.BybitProductType,
            nautilus_pyo3.BybitWebSocketClient,
        ] = {}
        environment = (
            nautilus_pyo3.BybitEnvironment.TESTNET
            if config.testnet
            else nautilus_pyo3.BybitEnvironment.MAINNET
        )

        for product_type in config.product_types:
            ws_client = nautilus_pyo3.BybitWebSocketClient.new_public(
                product_type=product_type,
                environment=environment,
                url=config.base_url_http,
                heartbeat=None,
            )
            self._ws_clients[product_type] = ws_client

        self._ws_client_futures: set[asyncio.Future] = set()

    @property
    def instrument_provider(self) -> BybitInstrumentProvider:
        return self._instrument_provider

    async def _connect(self) -> None:
        await self._instrument_provider.initialize()
        self._cache_instruments()
        self._send_all_instruments_to_data_engine()

        # Connect all websocket clients
        for product_type, ws_client in self._ws_clients.items():
            await ws_client.connect(callback=self._handle_msg)
            await ws_client.wait_until_active(timeout_secs=10.0)
            self._log.info(f"Connected to {product_type.name} websocket", LogColor.BLUE)

    async def _disconnect(self) -> None:
        self._http_client.cancel_all_requests()

        # Delay to allow websocket to send any unsubscribe messages
        await asyncio.sleep(1.0)

        # Shutdown all websocket clients
        for product_type, ws_client in self._ws_clients.items():
            self._log.info(f"Disconnecting {product_type.name} websocket")
            await ws_client.close()
            self._log.info(f"Disconnected from {product_type.name} websocket", LogColor.BLUE)

        # Cancel any pending futures
        await cancel_tasks_with_timeout(
            self._ws_client_futures,
            self._log,
            timeout_secs=DEFAULT_FUTURE_CANCELLATION_TIMEOUT,
        )

        self._ws_client_futures.clear()

    def _cache_instruments(self) -> None:
        # Ensures instrument definitions are available for correct
        # price and size precisions when parsing responses
        instruments_pyo3 = self.instrument_provider.instruments_pyo3()
        for inst in instruments_pyo3:
            self._http_client.add_instrument(inst)
            # Also add instruments to all websocket clients
            for ws_client in self._ws_clients.values():
                ws_client.add_instrument(inst)  # type: ignore[attr-defined]

        self._log.debug("Cached instruments", LogColor.MAGENTA)

    def _send_all_instruments_to_data_engine(self) -> None:
        for currency in self._instrument_provider.currencies().values():
            self._cache.add_currency(currency)

        for instrument in self._instrument_provider.get_all().values():
            self._handle_data(instrument)

    def _get_ws_client_for_instrument(
        self,
        instrument_id: nautilus_pyo3.InstrumentId,
    ) -> nautilus_pyo3.BybitWebSocketClient:
        """
        Get the appropriate websocket client for an instrument.
        """
        # For now, use the first available client
        # TODO: Map instrument_id to correct product type
        return next(iter(self._ws_clients.values()))

    def _bar_spec_to_bybit_interval(self, bar_spec) -> str:
        """
        Convert a Nautilus bar spec to a Bybit kline interval string.
        """
        # Bybit supported intervals: 1 3 5 15 30 60 120 240 360 720 D W M
        # Map (aggregation, step) to Bybit interval
        interval_map = {
            (4, 1): "1",  # MINUTE: 1
            (4, 3): "3",  # MINUTE: 3
            (4, 5): "5",  # MINUTE: 5
            (4, 15): "15",  # MINUTE: 15
            (4, 30): "30",  # MINUTE: 30
            (5, 1): "60",  # HOUR: 1
            (5, 2): "120",  # HOUR: 2
            (5, 4): "240",  # HOUR: 4
            (5, 6): "360",  # HOUR: 6
            (5, 12): "720",  # HOUR: 12
            (6, 1): "D",  # DAY: 1
            (7, 1): "W",  # WEEK: 1
            (8, 1): "M",  # MONTH: 1
        }

        key = (bar_spec.aggregation, bar_spec.step)
        interval = interval_map.get(key)

        if interval is None:
            self._log.warning(
                f"Unsupported bar spec {bar_spec}, defaulting to 1 minute",
            )
            return "1"

        return interval

    async def _subscribe_order_book_deltas(self, command: SubscribeOrderBook) -> None:
        if command.book_type != BookType.L2_MBP:
            self._log.warning(
                f"Book type {book_type_to_str(command.book_type)} not supported by Bybit, skipping subscription",
            )
            return

        pyo3_instrument_id = nautilus_pyo3.InstrumentId.from_str(command.instrument_id.value)
        ws_client = self._get_ws_client_for_instrument(pyo3_instrument_id)

        depth = command.depth if command.depth != 0 else 50
        symbol = command.instrument_id.symbol.value

        await ws_client.subscribe_orderbook(symbol, depth)

    async def _subscribe_order_book_snapshots(self, command: SubscribeOrderBook) -> None:
        # Bybit doesn't differentiate between snapshots and deltas at subscription level
        await self._subscribe_order_book_deltas(command)

    async def _subscribe_quote_ticks(self, command: SubscribeQuoteTicks) -> None:
        pyo3_instrument_id = nautilus_pyo3.InstrumentId.from_str(command.instrument_id.value)
        ws_client = self._get_ws_client_for_instrument(pyo3_instrument_id)
        symbol = command.instrument_id.symbol.value

        await ws_client.subscribe_ticker(symbol)

    async def _subscribe_trade_ticks(self, command: SubscribeTradeTicks) -> None:
        pyo3_instrument_id = nautilus_pyo3.InstrumentId.from_str(command.instrument_id.value)
        ws_client = self._get_ws_client_for_instrument(pyo3_instrument_id)
        symbol = command.instrument_id.symbol.value

        await ws_client.subscribe_trades(symbol)

    async def _subscribe_bars(self, command: SubscribeBars) -> None:
        pyo3_instrument_id = nautilus_pyo3.InstrumentId.from_str(
            command.bar_type.instrument_id.value,
        )
        ws_client = self._get_ws_client_for_instrument(pyo3_instrument_id)
        symbol = command.bar_type.instrument_id.symbol.value

        # Convert bar spec to Bybit kline interval
        interval = self._bar_spec_to_bybit_interval(command.bar_type.spec)
        await ws_client.subscribe_klines(symbol, interval)

    async def _subscribe_funding_rates(self, command: SubscribeFundingRates) -> None:
        # Bybit doesn't have a separate funding rate subscription
        # Funding rate data comes through ticker subscriptions for perpetual instruments
        pyo3_instrument_id = nautilus_pyo3.InstrumentId.from_str(command.instrument_id.value)
        ws_client = self._get_ws_client_for_instrument(pyo3_instrument_id)
        symbol = command.instrument_id.symbol.value

        # Subscribe to ticker which includes funding rate updates
        await ws_client.subscribe_ticker(symbol)

    async def _unsubscribe_order_book_deltas(self, command: UnsubscribeOrderBook) -> None:
        pyo3_instrument_id = nautilus_pyo3.InstrumentId.from_str(command.instrument_id.value)
        ws_client = self._get_ws_client_for_instrument(pyo3_instrument_id)

        depth = command.depth if command.depth != 0 else 50
        symbol = command.instrument_id.symbol.value

        await ws_client.unsubscribe_orderbook(symbol, depth)

    async def _unsubscribe_order_book_snapshots(self, command: UnsubscribeOrderBook) -> None:
        await self._unsubscribe_order_book_deltas(command)

    async def _unsubscribe_quote_ticks(self, command: UnsubscribeQuoteTicks) -> None:
        pyo3_instrument_id = nautilus_pyo3.InstrumentId.from_str(command.instrument_id.value)
        ws_client = self._get_ws_client_for_instrument(pyo3_instrument_id)
        symbol = command.instrument_id.symbol.value

        await ws_client.unsubscribe_ticker(symbol)

    async def _unsubscribe_trade_ticks(self, command: UnsubscribeTradeTicks) -> None:
        pyo3_instrument_id = nautilus_pyo3.InstrumentId.from_str(command.instrument_id.value)
        ws_client = self._get_ws_client_for_instrument(pyo3_instrument_id)
        symbol = command.instrument_id.symbol.value

        await ws_client.unsubscribe_trades(symbol)

    async def _unsubscribe_bars(self, command: UnsubscribeBars) -> None:
        pyo3_instrument_id = nautilus_pyo3.InstrumentId.from_str(
            command.bar_type.instrument_id.value,
        )
        ws_client = self._get_ws_client_for_instrument(pyo3_instrument_id)
        symbol = command.bar_type.instrument_id.symbol.value

        # Convert bar spec to Bybit kline interval
        interval = self._bar_spec_to_bybit_interval(command.bar_type.spec)
        await ws_client.unsubscribe_klines(symbol, interval)

    async def _unsubscribe_funding_rates(self, command: UnsubscribeFundingRates) -> None:
        # Bybit doesn't have a separate funding rate subscription
        # Unsubscribe from ticker which includes funding rate updates
        pyo3_instrument_id = nautilus_pyo3.InstrumentId.from_str(command.instrument_id.value)
        ws_client = self._get_ws_client_for_instrument(pyo3_instrument_id)
        symbol = command.instrument_id.symbol.value

        await ws_client.unsubscribe_ticker(symbol)

    def _handle_msg(self, raw: object) -> None:
        """
        Handle websocket message from Rust client.
        """
        try:
            # Handle pycapsule data from Rust (market data)
            if nautilus_pyo3.is_pycapsule(raw):
                # The capsule will fall out of scope at the end of this method,
                # and eventually be garbage collected. The contained pointer
                # to `Data` is still owned and managed by Rust.
                data = capsule_to_data(raw)
                self._handle_data(data)
                return

            # Handle JSON messages (auth, subscription responses, raw/unhandled messages)
            msg_str = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
            if msg_str:
                self._log.debug(f"WebSocket message: {msg_str}")
                # These are likely auth/subscription confirmations or raw/unhandled messages
                # Log them for debugging

        except Exception as e:
            self._log.error(f"Error handling websocket message: {e}")
