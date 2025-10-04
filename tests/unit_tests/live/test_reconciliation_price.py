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

from decimal import Decimal

import pytest

from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.reports import ExecutionMassStatus
from nautilus_trader.execution.reports import FillReport
from nautilus_trader.execution.reports import OrderStatusReport
from nautilus_trader.execution.reports import PositionStatusReport
from nautilus_trader.live.reconciliation import adjust_fills_for_partial_window
from nautilus_trader.live.reconciliation import calculate_reconciliation_price
from nautilus_trader.model.currencies import EUR
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.enums import LiquiditySide
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.enums import OrderStatus
from nautilus_trader.model.enums import OrderType
from nautilus_trader.model.enums import PositionSide
from nautilus_trader.model.enums import TimeInForce
from nautilus_trader.model.identifiers import AccountId
from nautilus_trader.model.identifiers import ClientId
from nautilus_trader.model.identifiers import ClientOrderId
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import Symbol
from nautilus_trader.model.identifiers import TradeId
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.identifiers import VenueOrderId
from nautilus_trader.model.instruments import CurrencyPair
from nautilus_trader.model.objects import Money
from nautilus_trader.model.objects import Price
from nautilus_trader.model.objects import Quantity


@pytest.fixture
def eurusd_instrument():
    """
    Create a test EUR/USD instrument for price precision testing.
    """
    return CurrencyPair(
        instrument_id=InstrumentId(Symbol("EUR/USD"), Venue("TEST")),
        raw_symbol=Symbol("EUR/USD"),
        base_currency=EUR,
        quote_currency=USD,
        price_precision=5,
        size_precision=2,
        price_increment=Price(1e-05, precision=5),
        size_increment=Quantity(0.01, precision=2),
        lot_size=Quantity(10000, precision=2),
        max_quantity=Quantity(1000000, precision=2),
        min_quantity=Quantity(0.01, precision=2),
        max_notional=None,
        min_notional=None,
        max_price=Price(10.0, precision=5),
        min_price=Price(0.0001, precision=5),
        margin_init=Decimal(0),
        margin_maint=Decimal(0),
        maker_fee=Decimal("0.0002"),
        taker_fee=Decimal("0.0002"),
        ts_event=0,
        ts_init=0,
    )


@pytest.mark.parametrize(
    "current_qty,current_avg_px,target_qty,target_avg_px,expected_price,description",
    [
        # Flat position scenarios
        (
            Decimal("0"),
            None,
            Decimal("100"),
            Decimal("1.25000"),
            Price(1.25000, precision=5),
            "Flat to long position",
        ),
        (
            Decimal("0"),
            None,
            Decimal("-100"),
            Decimal("1.25000"),
            Price(1.25000, precision=5),
            "Flat to short position",
        ),
        # Position increases
        (
            Decimal("100"),
            Decimal("1.20000"),
            Decimal("200"),
            Decimal("1.22000"),
            Price(1.24000, precision=5),
            "Long position increase",
        ),
        (
            Decimal("-100"),
            Decimal("1.30000"),
            Decimal("-200"),
            Decimal("1.28000"),
            Price(1.26000, precision=5),
            "Short position increase",
        ),
        # Position decreases
        (
            Decimal("200"),
            Decimal("1.20000"),
            Decimal("100"),
            Decimal("1.20000"),
            Price(1.20000, precision=5),
            "Long position decrease",
        ),
        # Position flips
        (
            Decimal("100"),
            Decimal("1.20000"),
            Decimal("-100"),
            Decimal("1.25000"),
            Price(1.25000, precision=5),
            "Long to short flip",
        ),
        (
            Decimal("-100"),
            Decimal("1.30000"),
            Decimal("100"),
            Decimal("1.25000"),
            Price(1.25000, precision=5),
            "Short to long flip",
        ),
        # Complex scenario
        (
            Decimal("150"),
            Decimal("1.23456"),
            Decimal("250"),
            Decimal("1.24567"),
            Price(1.26233, precision=5),
            "Complex partial fill scenario",
        ),
    ],
)
def test_reconciliation_price_calculations(
    eurusd_instrument,
    current_qty,
    current_avg_px,
    target_qty,
    target_avg_px,
    expected_price,
    description,
):
    """
    Test reconciliation price calculations for various scenarios.
    """
    result = calculate_reconciliation_price(
        current_position_qty=current_qty,
        current_position_avg_px=current_avg_px,
        target_position_qty=target_qty,
        target_position_avg_px=target_avg_px,
        instrument=eurusd_instrument,
    )

    assert result is not None, f"Failed for scenario: {description}"
    assert result == expected_price, f"Failed for scenario: {description}"


@pytest.mark.parametrize(
    "current_qty,current_avg_px,target_qty,target_avg_px,description",
    [
        # No target average price
        (
            Decimal("100"),
            Decimal("1.20000"),
            Decimal("200"),
            None,
            "No target avg price",
        ),
        # Zero target average price
        (
            Decimal("100"),
            Decimal("1.20000"),
            Decimal("200"),
            Decimal("0"),
            "Zero target avg price",
        ),
        # No quantity change
        (
            Decimal("100"),
            Decimal("1.20000"),
            Decimal("100"),
            Decimal("1.20000"),
            "No quantity change",
        ),
        # Negative price scenario
        (
            Decimal("100"),
            Decimal("2.00000"),
            Decimal("200"),
            Decimal("1.00000"),
            "Negative price calculation",
        ),
    ],
)
def test_reconciliation_price_returns_none(
    eurusd_instrument,
    current_qty,
    current_avg_px,
    target_qty,
    target_avg_px,
    description,
):
    """
    Test scenarios where reconciliation price calculation should return None.
    """
    result = calculate_reconciliation_price(
        current_position_qty=current_qty,
        current_position_avg_px=current_avg_px,
        target_position_qty=target_qty,
        target_position_avg_px=target_avg_px,
        instrument=eurusd_instrument,
    )

    assert result is None, f"Expected None for scenario: {description}"


def test_reconciliation_price_flat_position_logic(eurusd_instrument):
    """
    Test that flat position logic works correctly.
    """
    # When current position is flat, reconciliation price should equal target avg price
    result = calculate_reconciliation_price(
        current_position_qty=Decimal("0"),
        current_position_avg_px=None,
        target_position_qty=Decimal("100"),
        target_position_avg_px=Decimal("1.25000"),
        instrument=eurusd_instrument,
    )

    assert result == Price(1.25000, precision=5)


def test_reconciliation_price_precision_handling(eurusd_instrument):
    """
    Test that price precision is handled correctly by the instrument.
    """
    # Test with high precision input that should be rounded
    result = calculate_reconciliation_price(
        current_position_qty=Decimal("100"),
        current_position_avg_px=Decimal("1.123456789"),
        target_position_qty=Decimal("200"),
        target_position_avg_px=Decimal("1.234567890"),
        instrument=eurusd_instrument,
    )

    assert result is not None
    # Should be rounded to instrument precision (5 decimal places)
    assert result.precision == 5
    assert str(result) == "1.34568"  # Expected calculation result rounded to 5 decimals


def test_reconciliation_price_zero_quantity_difference_after_precision():
    """
    Test handling when quantity difference rounds to zero after precision.
    """
    # This scenario is mainly handled at the engine level, but we can test
    # that the function handles very small differences correctly
    instrument = CurrencyPair(
        instrument_id=InstrumentId(Symbol("EUR/USD"), Venue("TEST")),
        raw_symbol=Symbol("EUR/USD"),
        base_currency=EUR,
        quote_currency=USD,
        price_precision=5,
        size_precision=0,  # No decimal places for quantity
        price_increment=Price(1e-05, precision=5),
        size_increment=Quantity(1, precision=0),
        lot_size=Quantity(1, precision=0),
        max_quantity=Quantity(1000000, precision=0),
        min_quantity=Quantity(1, precision=0),
        max_notional=None,
        min_notional=None,
        max_price=Price(10.0, precision=5),
        min_price=Price(0.0001, precision=5),
        margin_init=Decimal(0),
        margin_maint=Decimal(0),
        maker_fee=Decimal("0.0002"),
        taker_fee=Decimal("0.0002"),
        ts_event=0,
        ts_init=0,
    )

    # Very small difference that would round to zero at instrument precision
    result = calculate_reconciliation_price(
        current_position_qty=Decimal("100.4"),
        current_position_avg_px=Decimal("1.20000"),
        target_position_qty=Decimal("100.6"),
        target_position_avg_px=Decimal("1.21000"),
        instrument=instrument,
    )

    # Should still calculate since we're working with the raw decimals
    assert result is not None


# ================================================================================================
# Tests for adjust_fills_for_partial_window
# ================================================================================================


@pytest.fixture
def ethusdt_instrument():
    """
    Create a test ETH/USDT instrument for testing.
    """
    from nautilus_trader.model.currencies import ETH
    from nautilus_trader.model.currencies import USDT

    return CurrencyPair(
        instrument_id=InstrumentId(Symbol("ETHUSDT"), Venue("TEST")),
        raw_symbol=Symbol("ETHUSDT"),
        base_currency=ETH,
        quote_currency=USDT,
        price_precision=2,
        size_precision=2,
        price_increment=Price(0.01, precision=2),
        size_increment=Quantity(0.01, precision=2),
        lot_size=Quantity(0.01, precision=2),
        max_quantity=Quantity(1000, precision=2),
        min_quantity=Quantity(0.01, precision=2),
        max_notional=None,
        min_notional=None,
        max_price=Price(100000, precision=2),
        min_price=Price(0.01, precision=2),
        margin_init=Decimal(0),
        margin_maint=Decimal(0),
        maker_fee=Decimal("0.0005"),
        taker_fee=Decimal("0.0005"),
        ts_event=0,
        ts_init=0,
    )


def test_adjust_fills_no_position_report(ethusdt_instrument):
    """
    Test adjust_fills_for_partial_window returns unchanged when no position report.
    """
    mass_status = ExecutionMassStatus(
        client_id=ClientId("TEST"),
        account_id=AccountId("TEST-ACCOUNT"),
        venue=Venue("TEST"),
        report_id=UUID4(),
        ts_init=0,
    )

    # Add a fill but no position report
    venue_order_id = VenueOrderId("ORDER-001")
    fill = FillReport(
        account_id=AccountId("TEST-ACCOUNT"),
        instrument_id=ethusdt_instrument.id,
        venue_order_id=venue_order_id,
        trade_id=TradeId("TRADE-001"),
        order_side=OrderSide.BUY,
        last_qty=Quantity.from_str("0.01"),
        last_px=Price.from_str("4000.00"),
        commission=Money(0, ethusdt_instrument.quote_currency),
        liquidity_side=LiquiditySide.TAKER,
        report_id=UUID4(),
        ts_event=1000,
        ts_init=0,
    )
    mass_status._fill_reports[venue_order_id] = [fill]

    adjusted_orders, result = adjust_fills_for_partial_window(
        mass_status,
        ethusdt_instrument,
    )

    # Should return unchanged
    assert result == mass_status.fill_reports


def test_adjust_fills_complete_lifecycle_no_adjustment(ethusdt_instrument):
    """
    Test adjust_fills_for_partial_window when lifecycle is complete (starts from FLAT).
    """
    mass_status = ExecutionMassStatus(
        client_id=ClientId("TEST"),
        account_id=AccountId("TEST-ACCOUNT"),
        venue=Venue("TEST"),
        report_id=UUID4(),
        ts_init=0,
    )

    # Create fills that form a complete lifecycle: FLAT -> LONG 0.02 @ 4100
    fills_sequence = [
        (1000, OrderSide.BUY, "0.01", "4100.00", "ORDER-001"),
        (2000, OrderSide.BUY, "0.01", "4100.00", "ORDER-002"),
    ]

    for ts, side, qty, price, order_id in fills_sequence:
        venue_order_id = VenueOrderId(order_id)
        fill = FillReport(
            account_id=AccountId("TEST-ACCOUNT"),
            instrument_id=ethusdt_instrument.id,
            venue_order_id=venue_order_id,
            trade_id=TradeId(f"TRADE-{order_id}"),
            order_side=side,
            last_qty=Quantity.from_str(qty),
            last_px=Price.from_str(price),
            commission=Money(0, ethusdt_instrument.quote_currency),
            liquidity_side=LiquiditySide.TAKER,
            report_id=UUID4(),
            ts_event=ts,
            ts_init=0,
        )
        mass_status._fill_reports[venue_order_id] = [fill]

        order_report = OrderStatusReport(
            account_id=AccountId("TEST-ACCOUNT"),
            instrument_id=ethusdt_instrument.id,
            client_order_id=ClientOrderId(f"C-{order_id}"),
            venue_order_id=venue_order_id,
            order_side=side,
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.IOC,
            order_status=OrderStatus.FILLED,
            quantity=Quantity.from_str(qty),
            filled_qty=Quantity.from_str(qty),
            report_id=UUID4(),
            ts_accepted=ts,
            ts_last=ts,
            ts_init=0,
        )
        mass_status._order_reports[venue_order_id] = order_report

    # Position report showing final state
    position_report = PositionStatusReport(
        account_id=AccountId("TEST-ACCOUNT"),
        instrument_id=ethusdt_instrument.id,
        position_side=PositionSide.LONG,
        quantity=Quantity.from_str("0.02"),
        report_id=UUID4(),
        ts_last=3000,
        ts_init=0,
        avg_px_open=Decimal("4100.00"),
    )
    mass_status._position_reports[ethusdt_instrument.id] = [position_report]

    adjusted_orders, result = adjust_fills_for_partial_window(
        mass_status,
        ethusdt_instrument,
    )

    # Should return unchanged (avg_px matches)
    assert len(result) == len(mass_status.fill_reports)


def test_adjust_fills_incomplete_lifecycle_adds_synthetic_fill(ethusdt_instrument):
    """
    Test adjust_fills_for_partial_window adds synthetic fill for incomplete lifecycle.
    """
    mass_status = ExecutionMassStatus(
        client_id=ClientId("TEST"),
        account_id=AccountId("TEST-ACCOUNT"),
        venue=Venue("TEST"),
        report_id=UUID4(),
        ts_init=0,
    )

    # Simulate incomplete lifecycle: window starts mid-position
    # Real scenario: had 0.02 LONG @ 4000 before window, then added 0.02 @ 4200
    # Window only sees the +0.02 @ 4200 fill
    # Final state: 0.04 LONG @ 4100 (venue report)

    venue_order_id = VenueOrderId("ORDER-001")
    fill = FillReport(
        account_id=AccountId("TEST-ACCOUNT"),
        instrument_id=ethusdt_instrument.id,
        venue_order_id=venue_order_id,
        trade_id=TradeId("TRADE-001"),
        order_side=OrderSide.BUY,
        last_qty=Quantity.from_str("0.02"),
        last_px=Price.from_str("4200.00"),
        commission=Money(0, ethusdt_instrument.quote_currency),
        liquidity_side=LiquiditySide.TAKER,
        report_id=UUID4(),
        ts_event=2000,
        ts_init=0,
    )
    mass_status._fill_reports[venue_order_id] = [fill]

    order_report = OrderStatusReport(
        account_id=AccountId("TEST-ACCOUNT"),
        instrument_id=ethusdt_instrument.id,
        client_order_id=ClientOrderId("C-ORDER-001"),
        venue_order_id=venue_order_id,
        order_side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.IOC,
        order_status=OrderStatus.FILLED,
        quantity=Quantity.from_str("0.02"),
        filled_qty=Quantity.from_str("0.02"),
        report_id=UUID4(),
        ts_accepted=2000,
        ts_last=2000,
        ts_init=0,
    )
    mass_status._order_reports[venue_order_id] = order_report

    # Position report showing final state
    position_report = PositionStatusReport(
        account_id=AccountId("TEST-ACCOUNT"),
        instrument_id=ethusdt_instrument.id,
        position_side=PositionSide.LONG,
        quantity=Quantity.from_str("0.04"),
        report_id=UUID4(),
        ts_last=3000,
        ts_init=0,
        avg_px_open=Decimal("4100.00"),  # Correct venue avg_px
    )
    mass_status._position_reports[ethusdt_instrument.id] = [position_report]

    adjusted_orders, result = adjust_fills_for_partial_window(
        mass_status,
        ethusdt_instrument,
    )

    # Should have added synthetic fill with its own venue_order_id
    assert len(result) == 2  # Synthetic order + original order
    assert venue_order_id in result  # Original order still present

    # Find the synthetic venue_order_id
    synthetic_venue_order_ids = [vid for vid in result.keys() if str(vid).startswith("S-")]
    assert len(synthetic_venue_order_ids) == 1
    synthetic_venue_order_id = synthetic_venue_order_ids[0]

    # Synthetic order should have 1 fill
    synthetic_fills = result[synthetic_venue_order_id]
    assert len(synthetic_fills) == 1

    # Verify synthetic fill properties
    synthetic_fill = synthetic_fills[0]
    assert synthetic_fill.trade_id.value.startswith("S-")
    assert synthetic_fill.account_id == AccountId("TEST-ACCOUNT")
    assert synthetic_fill.last_qty == Quantity.from_str("0.02")
    # Working backwards: 0.04 @ 4100 = (0.02 @ X) + (0.02 @ 4200)
    # 164 = 0.02X + 84
    # X = 4000
    assert synthetic_fill.last_px == Price.from_str("4000.00")

    # Original order should still have its fill
    assert len(result[venue_order_id]) == 1


def test_adjust_fills_with_zero_crossings(ethusdt_instrument):
    """
    Test adjust_fills_for_partial_window handles multiple lifecycles correctly.
    """
    mass_status = ExecutionMassStatus(
        client_id=ClientId("TEST"),
        account_id=AccountId("TEST-ACCOUNT"),
        venue=Venue("TEST"),
        report_id=UUID4(),
        ts_init=0,
    )

    # Lifecycle 1: LONG 0.02 @ 4100 -> FLAT (zero-crossing)
    # Lifecycle 2: LONG 0.03 @ 4200 (current)
    fills_sequence = [
        (1000, OrderSide.BUY, "0.02", "4100.00", "ORDER-001"),
        (2000, OrderSide.SELL, "0.02", "4150.00", "ORDER-002"),  # Zero-crossing
        (3000, OrderSide.BUY, "0.03", "4200.00", "ORDER-003"),  # Current lifecycle
    ]

    for ts, side, qty, price, order_id in fills_sequence:
        venue_order_id = VenueOrderId(order_id)
        fill = FillReport(
            account_id=AccountId("TEST-ACCOUNT"),
            instrument_id=ethusdt_instrument.id,
            venue_order_id=venue_order_id,
            trade_id=TradeId(f"TRADE-{order_id}"),
            order_side=side,
            last_qty=Quantity.from_str(qty),
            last_px=Price.from_str(price),
            commission=Money(0, ethusdt_instrument.quote_currency),
            liquidity_side=LiquiditySide.TAKER,
            report_id=UUID4(),
            ts_event=ts,
            ts_init=0,
        )
        mass_status._fill_reports[venue_order_id] = [fill]

        order_report = OrderStatusReport(
            account_id=AccountId("TEST-ACCOUNT"),
            instrument_id=ethusdt_instrument.id,
            client_order_id=ClientOrderId(f"C-{order_id}"),
            venue_order_id=venue_order_id,
            order_side=side,
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.IOC,
            order_status=OrderStatus.FILLED,
            quantity=Quantity.from_str(qty),
            filled_qty=Quantity.from_str(qty),
            report_id=UUID4(),
            ts_accepted=ts,
            ts_last=ts,
            ts_init=0,
        )
        mass_status._order_reports[venue_order_id] = order_report

    # Position report showing final state
    position_report = PositionStatusReport(
        account_id=AccountId("TEST-ACCOUNT"),
        instrument_id=ethusdt_instrument.id,
        position_side=PositionSide.LONG,
        quantity=Quantity.from_str("0.03"),
        report_id=UUID4(),
        ts_last=4000,
        ts_init=0,
        avg_px_open=Decimal("4200.00"),
    )
    mass_status._position_reports[ethusdt_instrument.id] = [position_report]

    adjusted_orders, result = adjust_fills_for_partial_window(
        mass_status,
        ethusdt_instrument,
    )

    # Should only return fills from current lifecycle (after last zero-crossing)
    # Old lifecycles (ORDER-001, ORDER-002) should be filtered out
    assert len(result) == 1
    assert VenueOrderId("ORDER-003") in result
    assert VenueOrderId("ORDER-001") not in result
    assert VenueOrderId("ORDER-002") not in result


def test_adjust_fills_current_lifecycle_mismatch_creates_synthetic(ethusdt_instrument):
    """
    Test that synthetic fill is created when current lifecycle fills don't match venue
    position.

    This is the most critical scenario from the live trading fix:
    - Multiple zero-crossings detected
    - Current lifecycle fills are filtered correctly
    - But simulating them produces wrong qty/avg_px
    - Function should replace ALL current lifecycle fills with single synthetic fill
    - Function should also filter order_reports to prevent inferred fills

    """
    mass_status = ExecutionMassStatus(
        client_id=ClientId("TEST"),
        account_id=AccountId("TEST-ACCOUNT"),
        venue=Venue("TEST"),
        report_id=UUID4(),
        ts_init=0,
    )

    # Lifecycle 1: LONG 0.05 @ 4000 -> FLAT (zero-crossing)
    # Lifecycle 2 (current): Multiple fills that DON'T produce correct position
    #   - BUY 0.05 @ 4000.00 (ORDER-004)
    #   - BUY 0.05 @ 4100.00 (ORDER-005)
    #   - Simulated result: 0.10 @ 4050.00
    # Venue reports: 0.05 @ 4142.04 (MISMATCH - both qty AND avg_px!)
    fills_sequence = [
        (1000, OrderSide.BUY, "0.05", "4000.00", "ORDER-001"),
        (2000, OrderSide.SELL, "0.05", "4050.00", "ORDER-002"),  # Zero-crossing
        (3000, OrderSide.BUY, "0.05", "4000.00", "ORDER-004"),  # Current lifecycle
        (4000, OrderSide.BUY, "0.05", "4100.00", "ORDER-005"),  # Current lifecycle
    ]

    for ts, side, qty, price, order_id in fills_sequence:
        venue_order_id = VenueOrderId(order_id)
        fill = FillReport(
            account_id=AccountId("TEST-ACCOUNT"),
            instrument_id=ethusdt_instrument.id,
            venue_order_id=venue_order_id,
            trade_id=TradeId(f"TRADE-{order_id}"),
            order_side=side,
            last_qty=Quantity.from_str(qty),
            last_px=Price.from_str(price),
            commission=Money(0, ethusdt_instrument.quote_currency),
            liquidity_side=LiquiditySide.TAKER,
            report_id=UUID4(),
            ts_event=ts,
            ts_init=0,
        )
        mass_status._fill_reports[venue_order_id] = [fill]

        order_report = OrderStatusReport(
            account_id=AccountId("TEST-ACCOUNT"),
            instrument_id=ethusdt_instrument.id,
            client_order_id=ClientOrderId(f"C-{order_id}"),
            venue_order_id=venue_order_id,
            order_side=side,
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.IOC,
            order_status=OrderStatus.FILLED,
            quantity=Quantity.from_str(qty),
            filled_qty=Quantity.from_str(qty),
            report_id=UUID4(),
            ts_accepted=ts,
            ts_last=ts,
            ts_init=0,
        )
        mass_status._order_reports[venue_order_id] = order_report

    # Position report showing venue's actual state
    position_report = PositionStatusReport(
        account_id=AccountId("TEST-ACCOUNT"),
        instrument_id=ethusdt_instrument.id,
        position_side=PositionSide.LONG,
        quantity=Quantity.from_str("0.05"),
        report_id=UUID4(),
        ts_last=5000,
        ts_init=0,
        avg_px_open=Decimal("4142.04"),  # Different from what fills would produce
    )
    mass_status._position_reports[ethusdt_instrument.id] = [position_report]

    adjusted_orders, result = adjust_fills_for_partial_window(
        mass_status,
        ethusdt_instrument,
    )

    # Should replace current lifecycle fills with single synthetic fill
    assert len(result) == 1

    # Should have a synthetic venue_order_id (not ORDER-004)
    synthetic_venue_order_ids = [vid for vid in result.keys() if str(vid).startswith("S-")]
    assert len(synthetic_venue_order_ids) == 1
    synthetic_venue_order_id = synthetic_venue_order_ids[0]

    # Verify it's the only order in result
    assert synthetic_venue_order_id in result

    fills = result[synthetic_venue_order_id]
    assert len(fills) == 1

    synthetic_fill = fills[0]
    assert synthetic_fill.trade_id.value.startswith("S-")
    assert synthetic_fill.account_id == AccountId("TEST-ACCOUNT")
    assert synthetic_fill.last_qty == Quantity.from_str("0.05")
    assert synthetic_fill.last_px == Price.from_str("4142.04")  # Matches venue
    assert synthetic_fill.order_side == OrderSide.BUY
    assert synthetic_fill.commission == Money(0, ethusdt_instrument.quote_currency)


def test_adjust_fills_oldest_lifecycle_incomplete_adds_synthetic(ethusdt_instrument):
    """
    Test synthetic fill added when single incomplete lifecycle doesn't match venue.

    Scenario:
    - Single lifecycle (no zero-crossings)
    - Window incomplete: missing some fills from start
    - Current fills don't produce correct position
    - Function should add synthetic fill to match venue

    """
    mass_status = ExecutionMassStatus(
        client_id=ClientId("TEST"),
        account_id=AccountId("TEST-ACCOUNT"),
        venue=Venue("TEST"),
        report_id=UUID4(),
        ts_init=0,
    )

    # Single incomplete lifecycle: Window misses opening fills
    # Real scenario: had 0.04 LONG before window, then +0.02 LONG
    # Window only sees: BUY 0.02 @ 4200 (incomplete)
    # Venue reports: 0.06 LONG @ 4150 (MISMATCH!)
    fills_sequence = [
        (2000, OrderSide.BUY, "0.02", "4200.00", "ORDER-002"),
    ]

    for ts, side, qty, price, order_id in fills_sequence:
        venue_order_id = VenueOrderId(order_id)
        fill = FillReport(
            account_id=AccountId("TEST-ACCOUNT"),
            instrument_id=ethusdt_instrument.id,
            venue_order_id=venue_order_id,
            trade_id=TradeId(f"TRADE-{order_id}"),
            order_side=side,
            last_qty=Quantity.from_str(qty),
            last_px=Price.from_str(price),
            commission=Money(0, ethusdt_instrument.quote_currency),
            liquidity_side=LiquiditySide.TAKER,
            report_id=UUID4(),
            ts_event=ts,
            ts_init=0,
        )
        mass_status._fill_reports[venue_order_id] = [fill]

        order_report = OrderStatusReport(
            account_id=AccountId("TEST-ACCOUNT"),
            instrument_id=ethusdt_instrument.id,
            client_order_id=ClientOrderId(f"C-{order_id}"),
            venue_order_id=venue_order_id,
            order_side=side,
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.IOC,
            order_status=OrderStatus.FILLED,
            quantity=Quantity.from_str(qty),
            filled_qty=Quantity.from_str(qty),
            report_id=UUID4(),
            ts_accepted=ts,
            ts_last=ts,
            ts_init=0,
        )
        mass_status._order_reports[venue_order_id] = order_report

    # Position report showing venue's actual state
    position_report = PositionStatusReport(
        account_id=AccountId("TEST-ACCOUNT"),
        instrument_id=ethusdt_instrument.id,
        position_side=PositionSide.LONG,
        quantity=Quantity.from_str("0.06"),
        report_id=UUID4(),
        ts_last=3000,
        ts_init=0,
        avg_px_open=Decimal("4150.00"),
    )
    mass_status._position_reports[ethusdt_instrument.id] = [position_report]

    adjusted_orders, result = adjust_fills_for_partial_window(
        mass_status,
        ethusdt_instrument,
    )

    # Should have added synthetic opening fill with its own venue_order_id
    assert len(result) == 2  # Synthetic order + original order
    assert VenueOrderId("ORDER-002") in result  # Original order still present

    # Find the synthetic venue_order_id
    synthetic_venue_order_ids = [vid for vid in result.keys() if str(vid).startswith("S-")]
    assert len(synthetic_venue_order_ids) == 1
    synthetic_venue_order_id = synthetic_venue_order_ids[0]

    # Synthetic order should have 1 fill
    synthetic_fills = result[synthetic_venue_order_id]
    assert len(synthetic_fills) == 1

    # Verify synthetic fill properties
    synthetic_fill = synthetic_fills[0]
    assert synthetic_fill.trade_id.value.startswith("S-")
    assert synthetic_fill.account_id == AccountId("TEST-ACCOUNT")
    assert synthetic_fill.order_side == OrderSide.BUY
    # Working backwards: 0.06 @ 4150 = (0.04 @ X) + (0.02 @ 4200)
    # 249 = 0.04X + 84
    # X = 4125
    assert synthetic_fill.last_qty == Quantity.from_str("0.04")

    # Original order should still have its fill
    assert len(result[VenueOrderId("ORDER-002")]) == 1


def test_adjust_fills_short_position(ethusdt_instrument):
    """
    Test adjust_fills_for_partial_window handles SHORT positions correctly.
    """
    mass_status = ExecutionMassStatus(
        client_id=ClientId("TEST"),
        account_id=AccountId("TEST-ACCOUNT"),
        venue=Venue("TEST"),
        report_id=UUID4(),
        ts_init=0,
    )

    # Incomplete lifecycle: window misses opening SHORT position
    # Real position: -0.05 @ 4100
    # Window only sees: SELL 0.02 @ 4120 (additional short)
    venue_order_id = VenueOrderId("ORDER-001")
    fill = FillReport(
        account_id=AccountId("TEST-ACCOUNT"),
        instrument_id=ethusdt_instrument.id,
        venue_order_id=venue_order_id,
        trade_id=TradeId("TRADE-001"),
        order_side=OrderSide.SELL,
        last_qty=Quantity.from_str("0.02"),
        last_px=Price.from_str("4120.00"),
        commission=Money(0, ethusdt_instrument.quote_currency),
        liquidity_side=LiquiditySide.TAKER,
        report_id=UUID4(),
        ts_event=1000,
        ts_init=0,
    )
    mass_status._fill_reports[venue_order_id] = [fill]

    order_report = OrderStatusReport(
        account_id=AccountId("TEST-ACCOUNT"),
        instrument_id=ethusdt_instrument.id,
        client_order_id=ClientOrderId("C-ORDER-001"),
        venue_order_id=venue_order_id,
        order_side=OrderSide.SELL,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.IOC,
        order_status=OrderStatus.FILLED,
        quantity=Quantity.from_str("0.02"),
        filled_qty=Quantity.from_str("0.02"),
        report_id=UUID4(),
        ts_accepted=1000,
        ts_last=1000,
        ts_init=0,
    )
    mass_status._order_reports[venue_order_id] = order_report

    # Position report showing SHORT position
    position_report = PositionStatusReport(
        account_id=AccountId("TEST-ACCOUNT"),
        instrument_id=ethusdt_instrument.id,
        position_side=PositionSide.SHORT,
        quantity=Quantity.from_str("0.05"),  # Positive quantity for SHORT
        report_id=UUID4(),
        ts_last=2000,
        ts_init=0,
        avg_px_open=Decimal("4100.00"),
    )
    mass_status._position_reports[ethusdt_instrument.id] = [position_report]

    adjusted_orders, result = adjust_fills_for_partial_window(
        mass_status,
        ethusdt_instrument,
    )

    # Should add synthetic opening SHORT fill with its own venue_order_id
    assert len(result) == 2  # Synthetic order + original order
    assert venue_order_id in result  # Original order still present

    # Find the synthetic venue_order_id
    synthetic_venue_order_ids = [vid for vid in result.keys() if str(vid).startswith("S-")]
    assert len(synthetic_venue_order_ids) == 1
    synthetic_venue_order_id = synthetic_venue_order_ids[0]

    # Synthetic order should have 1 fill
    synthetic_fills = result[synthetic_venue_order_id]
    assert len(synthetic_fills) == 1

    # Verify synthetic fill properties
    synthetic_fill = synthetic_fills[0]
    assert synthetic_fill.trade_id.value.startswith("S-")
    assert synthetic_fill.order_side == OrderSide.SELL
    # Working backwards: -0.05 @ 4100 = (-0.03 @ X) + (-0.02 @ 4120)
    # -205 = -0.03X + -82.4
    # X = 4086.67
    assert synthetic_fill.last_qty == Quantity.from_str("0.03")

    # Original order should still have its fill
    assert len(result[venue_order_id]) == 1


def test_adjust_fills_flat_position_after_zero_crossings(ethusdt_instrument):
    """
    Test when venue position is FLAT after zero-crossings.
    """
    mass_status = ExecutionMassStatus(
        client_id=ClientId("TEST"),
        account_id=AccountId("TEST-ACCOUNT"),
        venue=Venue("TEST"),
        report_id=UUID4(),
        ts_init=0,
    )

    # Lifecycle 1: LONG -> FLAT
    # Lifecycle 2: LONG -> FLAT (current)
    fills_sequence = [
        (1000, OrderSide.BUY, "0.02", "4100.00", "ORDER-001"),
        (2000, OrderSide.SELL, "0.02", "4150.00", "ORDER-002"),  # Zero-crossing
        (3000, OrderSide.BUY, "0.03", "4200.00", "ORDER-003"),
        (4000, OrderSide.SELL, "0.03", "4250.00", "ORDER-004"),  # Zero-crossing
    ]

    for ts, side, qty, price, order_id in fills_sequence:
        venue_order_id = VenueOrderId(order_id)
        fill = FillReport(
            account_id=AccountId("TEST-ACCOUNT"),
            instrument_id=ethusdt_instrument.id,
            venue_order_id=venue_order_id,
            trade_id=TradeId(f"TRADE-{order_id}"),
            order_side=side,
            last_qty=Quantity.from_str(qty),
            last_px=Price.from_str(price),
            commission=Money(0, ethusdt_instrument.quote_currency),
            liquidity_side=LiquiditySide.TAKER,
            report_id=UUID4(),
            ts_event=ts,
            ts_init=0,
        )
        mass_status._fill_reports[venue_order_id] = [fill]

        order_report = OrderStatusReport(
            account_id=AccountId("TEST-ACCOUNT"),
            instrument_id=ethusdt_instrument.id,
            client_order_id=ClientOrderId(f"C-{order_id}"),
            venue_order_id=venue_order_id,
            order_side=side,
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.IOC,
            order_status=OrderStatus.FILLED,
            quantity=Quantity.from_str(qty),
            filled_qty=Quantity.from_str(qty),
            report_id=UUID4(),
            ts_accepted=ts,
            ts_last=ts,
            ts_init=0,
        )
        mass_status._order_reports[venue_order_id] = order_report

    # Position report showing FLAT
    position_report = PositionStatusReport(
        account_id=AccountId("TEST-ACCOUNT"),
        instrument_id=ethusdt_instrument.id,
        position_side=PositionSide.FLAT,
        quantity=Quantity.from_str("0.00"),
        report_id=UUID4(),
        ts_last=5000,
        ts_init=0,
    )
    mass_status._position_reports[ethusdt_instrument.id] = [position_report]

    adjusted_orders, result = adjust_fills_for_partial_window(
        mass_status,
        ethusdt_instrument,
    )

    # When venue position is FLAT after zero-crossings,
    # function returns all fills unchanged (early return at line 341-343)
    assert len(result) == 4
    assert VenueOrderId("ORDER-001") in result
    assert VenueOrderId("ORDER-002") in result
    assert VenueOrderId("ORDER-003") in result
    assert VenueOrderId("ORDER-004") in result


def test_adjust_fills_position_flip_in_current_lifecycle(ethusdt_instrument):
    """
    Test current lifecycle with position flip (LONG -> SHORT).
    """
    mass_status = ExecutionMassStatus(
        client_id=ClientId("TEST"),
        account_id=AccountId("TEST-ACCOUNT"),
        venue=Venue("TEST"),
        report_id=UUID4(),
        ts_init=0,
    )

    # Lifecycle 1: LONG -> FLAT
    # Lifecycle 2 (current): LONG -> FLIP -> SHORT
    fills_sequence = [
        (1000, OrderSide.BUY, "0.02", "4000.00", "ORDER-001"),
        (2000, OrderSide.SELL, "0.02", "4050.00", "ORDER-002"),  # Zero-crossing
        (3000, OrderSide.BUY, "0.03", "4100.00", "ORDER-003"),  # Current: LONG
        (4000, OrderSide.SELL, "0.05", "4150.00", "ORDER-004"),  # Current: Flip to SHORT
    ]

    for ts, side, qty, price, order_id in fills_sequence:
        venue_order_id = VenueOrderId(order_id)
        fill = FillReport(
            account_id=AccountId("TEST-ACCOUNT"),
            instrument_id=ethusdt_instrument.id,
            venue_order_id=venue_order_id,
            trade_id=TradeId(f"TRADE-{order_id}"),
            order_side=side,
            last_qty=Quantity.from_str(qty),
            last_px=Price.from_str(price),
            commission=Money(0, ethusdt_instrument.quote_currency),
            liquidity_side=LiquiditySide.TAKER,
            report_id=UUID4(),
            ts_event=ts,
            ts_init=0,
        )
        mass_status._fill_reports[venue_order_id] = [fill]

        order_report = OrderStatusReport(
            account_id=AccountId("TEST-ACCOUNT"),
            instrument_id=ethusdt_instrument.id,
            client_order_id=ClientOrderId(f"C-{order_id}"),
            venue_order_id=venue_order_id,
            order_side=side,
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.IOC,
            order_status=OrderStatus.FILLED,
            quantity=Quantity.from_str(qty),
            filled_qty=Quantity.from_str(qty),
            report_id=UUID4(),
            ts_accepted=ts,
            ts_last=ts,
            ts_init=0,
        )
        mass_status._order_reports[venue_order_id] = order_report

    # Position report showing SHORT
    position_report = PositionStatusReport(
        account_id=AccountId("TEST-ACCOUNT"),
        instrument_id=ethusdt_instrument.id,
        position_side=PositionSide.SHORT,
        quantity=Quantity.from_str("0.02"),
        report_id=UUID4(),
        ts_last=5000,
        ts_init=0,
        avg_px_open=Decimal("4150.00"),  # Flip price
    )
    mass_status._position_reports[ethusdt_instrument.id] = [position_report]

    adjusted_orders, result = adjust_fills_for_partial_window(
        mass_status,
        ethusdt_instrument,
    )

    # Should keep current lifecycle fills (ORDER-003, ORDER-004)
    assert len(result) == 2
    assert VenueOrderId("ORDER-003") in result
    assert VenueOrderId("ORDER-004") in result
    assert VenueOrderId("ORDER-001") not in result
    assert VenueOrderId("ORDER-002") not in result


def test_adjust_fills_multi_instrument_preserves_all_fills(eurusd_instrument, ethusdt_instrument):
    """
    Test that adjusting fills for multiple instruments preserves fills for all
    instruments.
    """
    # Create mass status with fills for TWO instruments
    account_id = AccountId("TEST-001")
    client_id = ClientId("TEST")
    venue = Venue("TEST")

    # EURUSD fills
    eurusd_venue_order_id1 = VenueOrderId("EURUSD-ORDER1")
    eurusd_venue_order_id2 = VenueOrderId("EURUSD-ORDER2")

    eurusd_fill1 = FillReport(
        account_id=account_id,
        instrument_id=eurusd_instrument.id,
        venue_order_id=eurusd_venue_order_id1,
        trade_id=TradeId("EURUSD-1"),
        order_side=OrderSide.BUY,
        last_qty=Quantity.from_str("0.01"),
        last_px=Price.from_str("1.1000"),
        commission=Money(0.0, EUR),
        liquidity_side=LiquiditySide.TAKER,
        report_id=UUID4(),
        ts_event=1000,
        ts_init=1000,
    )

    eurusd_fill2 = FillReport(
        account_id=account_id,
        instrument_id=eurusd_instrument.id,
        venue_order_id=eurusd_venue_order_id2,
        trade_id=TradeId("EURUSD-2"),
        order_side=OrderSide.BUY,
        last_qty=Quantity.from_str("0.01"),
        last_px=Price.from_str("1.1000"),
        commission=Money(0.0, EUR),
        liquidity_side=LiquiditySide.TAKER,
        report_id=UUID4(),
        ts_event=2000,
        ts_init=2000,
    )

    eurusd_order_report1 = OrderStatusReport(
        account_id=account_id,
        instrument_id=eurusd_instrument.id,
        venue_order_id=eurusd_venue_order_id1,
        order_side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        order_status=OrderStatus.FILLED,
        quantity=Quantity.from_str("0.01"),
        filled_qty=Quantity.from_str("0.01"),
        report_id=UUID4(),
        ts_accepted=1000,
        ts_last=1000,
        ts_init=1000,
    )

    eurusd_order_report2 = OrderStatusReport(
        account_id=account_id,
        instrument_id=eurusd_instrument.id,
        venue_order_id=eurusd_venue_order_id2,
        order_side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        order_status=OrderStatus.FILLED,
        quantity=Quantity.from_str("0.01"),
        filled_qty=Quantity.from_str("0.01"),
        report_id=UUID4(),
        ts_accepted=2000,
        ts_last=2000,
        ts_init=2000,
    )

    eurusd_position_report = PositionStatusReport(
        account_id=account_id,
        instrument_id=eurusd_instrument.id,
        position_side=PositionSide.LONG,
        quantity=Quantity.from_str("0.02"),
        avg_px_open=Decimal("1.1000"),
        ts_last=2000,
        ts_init=2000,
        report_id=UUID4(),
    )

    # ETHUSDT fills
    ethusdt_venue_order_id1 = VenueOrderId("ETHUSDT-ORDER1")
    ethusdt_venue_order_id2 = VenueOrderId("ETHUSDT-ORDER2")

    ethusdt_fill1 = FillReport(
        account_id=account_id,
        instrument_id=ethusdt_instrument.id,
        venue_order_id=ethusdt_venue_order_id1,
        trade_id=TradeId("ETHUSDT-1"),
        order_side=OrderSide.BUY,
        last_qty=Quantity.from_str("0.05"),
        last_px=Price.from_str("4000.00"),
        commission=Money(0.0, USD),
        liquidity_side=LiquiditySide.TAKER,
        report_id=UUID4(),
        ts_event=1500,
        ts_init=1500,
    )

    ethusdt_fill2 = FillReport(
        account_id=account_id,
        instrument_id=ethusdt_instrument.id,
        venue_order_id=ethusdt_venue_order_id2,
        trade_id=TradeId("ETHUSDT-2"),
        order_side=OrderSide.BUY,
        last_qty=Quantity.from_str("0.05"),
        last_px=Price.from_str("4000.00"),
        commission=Money(0.0, USD),
        liquidity_side=LiquiditySide.TAKER,
        report_id=UUID4(),
        ts_event=2500,
        ts_init=2500,
    )

    ethusdt_order_report1 = OrderStatusReport(
        account_id=account_id,
        instrument_id=ethusdt_instrument.id,
        venue_order_id=ethusdt_venue_order_id1,
        order_side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        order_status=OrderStatus.FILLED,
        quantity=Quantity.from_str("0.05"),
        filled_qty=Quantity.from_str("0.05"),
        report_id=UUID4(),
        ts_accepted=1500,
        ts_last=1500,
        ts_init=1500,
    )

    ethusdt_order_report2 = OrderStatusReport(
        account_id=account_id,
        instrument_id=ethusdt_instrument.id,
        venue_order_id=ethusdt_venue_order_id2,
        order_side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        order_status=OrderStatus.FILLED,
        quantity=Quantity.from_str("0.05"),
        filled_qty=Quantity.from_str("0.05"),
        report_id=UUID4(),
        ts_accepted=2500,
        ts_last=2500,
        ts_init=2500,
    )

    ethusdt_position_report = PositionStatusReport(
        account_id=account_id,
        instrument_id=ethusdt_instrument.id,
        position_side=PositionSide.LONG,
        quantity=Quantity.from_str("0.10"),
        avg_px_open=Decimal("4000.00"),
        ts_last=2500,
        ts_init=2500,
        report_id=UUID4(),
    )

    # Create mass status
    mass_status = ExecutionMassStatus(
        client_id=client_id,
        account_id=account_id,
        venue=venue,
        report_id=UUID4(),
        ts_init=0,
    )

    mass_status._order_reports = {
        eurusd_venue_order_id1: eurusd_order_report1,
        eurusd_venue_order_id2: eurusd_order_report2,
        ethusdt_venue_order_id1: ethusdt_order_report1,
        ethusdt_venue_order_id2: ethusdt_order_report2,
    }

    mass_status._fill_reports = {
        eurusd_venue_order_id1: [eurusd_fill1],
        eurusd_venue_order_id2: [eurusd_fill2],
        ethusdt_venue_order_id1: [ethusdt_fill1],
        ethusdt_venue_order_id2: [ethusdt_fill2],
    }

    mass_status._position_reports = {
        eurusd_instrument.id: [eurusd_position_report],
        ethusdt_instrument.id: [ethusdt_position_report],
    }

    # Simulate the execution engine loop that processes multiple instruments
    final_fills = dict(mass_status._fill_reports)

    for instrument_id in mass_status.position_reports.keys():
        instrument = (
            eurusd_instrument if instrument_id == eurusd_instrument.id else ethusdt_instrument
        )
        adjusted_orders_for_instrument, adjusted_fills_for_instrument = (
            adjust_fills_for_partial_window(mass_status, instrument)
        )

        # Remove old fills for this instrument
        for venue_order_id in list(final_fills.keys()):
            fills = final_fills[venue_order_id]
            if fills and fills[0].instrument_id == instrument_id:
                del final_fills[venue_order_id]

        # Add adjusted fills for this instrument
        final_fills.update(adjusted_fills_for_instrument)

    # Update mass status
    mass_status._fill_reports = final_fills

    # Both instruments should retain their fills
    assert len(mass_status._fill_reports) == 4
    assert eurusd_venue_order_id1 in mass_status._fill_reports
    assert eurusd_venue_order_id2 in mass_status._fill_reports
    assert ethusdt_venue_order_id1 in mass_status._fill_reports
    assert ethusdt_venue_order_id2 in mass_status._fill_reports

    # Verify each instrument has correct fills
    eurusd_fills = [
        fill
        for fills in mass_status._fill_reports.values()
        for fill in fills
        if fill.instrument_id == eurusd_instrument.id
    ]
    ethusdt_fills = [
        fill
        for fills in mass_status._fill_reports.values()
        for fill in fills
        if fill.instrument_id == ethusdt_instrument.id
    ]

    assert len(eurusd_fills) == 2
    assert len(ethusdt_fills) == 2


def test_adjust_fills_missing_order_reports_uses_fill_side(ethusdt_instrument):
    """
    Test that fills without order reports still use fill.order_side.

    Scenario:
    - Incomplete lookback window: some fills have order reports, some don't
    - Fills without order reports should still be included using their order_side
    - System should add synthetic opening fill to match venue position

    """
    mass_status = ExecutionMassStatus(
        client_id=ClientId("TEST"),
        account_id=AccountId("TEST-ACCOUNT"),
        venue=Venue("TEST"),
        report_id=UUID4(),
        ts_init=0,
    )

    # Simulate incomplete lookback: First fill has NO order report (missing from window)
    # Second fill has order report (within window)
    fills_sequence = [
        (1000, OrderSide.BUY, "0.02", "4000.00", "ORDER-001", False),  # No order report
        (2000, OrderSide.BUY, "0.02", "4200.00", "ORDER-002", True),  # Has order report
    ]

    for ts, side, qty, price, order_id, has_order_report in fills_sequence:
        venue_order_id = VenueOrderId(order_id)

        fill = FillReport(
            account_id=AccountId("TEST-ACCOUNT"),
            instrument_id=ethusdt_instrument.id,
            venue_order_id=venue_order_id,
            trade_id=TradeId(f"TRADE-{order_id}"),
            order_side=side,
            last_qty=Quantity.from_str(qty),
            last_px=Price.from_str(price),
            commission=Money(0, ethusdt_instrument.quote_currency),
            liquidity_side=LiquiditySide.TAKER,
            report_id=UUID4(),
            ts_event=ts,
            ts_init=0,
        )
        mass_status._fill_reports[venue_order_id] = [fill]

        # Only add order report if within lookback window
        if has_order_report:
            order_report = OrderStatusReport(
                account_id=AccountId("TEST-ACCOUNT"),
                instrument_id=ethusdt_instrument.id,
                client_order_id=ClientOrderId(f"C-{order_id}"),
                venue_order_id=venue_order_id,
                order_side=side,
                order_type=OrderType.MARKET,
                time_in_force=TimeInForce.IOC,
                order_status=OrderStatus.FILLED,
                quantity=Quantity.from_str(qty),
                filled_qty=Quantity.from_str(qty),
                report_id=UUID4(),
                ts_accepted=ts,
                ts_last=ts,
                ts_init=0,
            )
            mass_status._order_reports[venue_order_id] = order_report

    # Position report showing venue's actual state
    # If we only used fills with order reports, we'd get: 0.02 @ 4200
    # But venue shows: 0.04 @ 4142.042 (needs both fills + adjustment)
    position_report = PositionStatusReport(
        account_id=AccountId("TEST-ACCOUNT"),
        instrument_id=ethusdt_instrument.id,
        position_side=PositionSide.LONG,
        quantity=Quantity.from_str("0.04"),
        report_id=UUID4(),
        ts_last=3000,
        ts_init=0,
        avg_px_open=Decimal("4142.042"),
    )
    mass_status._position_reports[ethusdt_instrument.id] = [position_report]

    adjusted_orders, result = adjust_fills_for_partial_window(
        mass_status,
        ethusdt_instrument,
    )

    # Should include fills from both orders
    assert VenueOrderId("ORDER-001") in result or VenueOrderId("ORDER-002") in result

    # Collect all fills
    all_fills = []
    for fills in result.values():
        all_fills.extend(fills)

    # Both fills should be included (one with order report, one without)
    assert len(all_fills) == 2

    # Verify both orders are present
    assert VenueOrderId("ORDER-001") in result
    assert VenueOrderId("ORDER-002") in result

    # Verify fills from ORDER-001 (no order report) are included using fill.order_side
    order_001_fills = result[VenueOrderId("ORDER-001")]
    assert len(order_001_fills) == 1
    assert order_001_fills[0].order_side == OrderSide.BUY
