// -------------------------------------------------------------------------------------------------
//  Copyright (C) 2015-2025 Nautech Systems Pty Ltd. All rights reserved.
//  https://nautechsystems.io
//
//  Licensed under the GNU Lesser General Public License Version 3.0 (the "License");
//  You may not use this file except in compliance with the License.
//  You may obtain a copy of the License at https://www.gnu.org/licenses/lgpl-3.0.en.html
//
//  Unless required by applicable law or agreed to in writing, software
//  distributed under the License is distributed on an "AS IS" BASIS,
//  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
//  See the License for the specific language governing permissions and
//  limitations under the License.
// -------------------------------------------------------------------------------------------------

//! Python bindings for reconciliation functions.

use std::collections::HashMap;

use nautilus_core::{UnixNanos, python::to_pyvalue_err};
use nautilus_model::{
    enums::{LiquiditySide, OrderSide, OrderStatus, OrderType, PositionSideSpecified, TimeInForce},
    identifiers::{AccountId, InstrumentId, TradeId, VenueOrderId},
    instruments::{Instrument, InstrumentAny},
    python::instruments::pyobject_to_instrument_any,
    reports::{ExecutionMassStatus, FillReport, OrderStatusReport},
    types::{Money, Price, Quantity},
};
use pyo3::{
    IntoPyObjectExt,
    prelude::*,
    types::{PyDict, PyList, PyTuple},
};
use rust_decimal::Decimal;
use ustr::Ustr;

use crate::reconciliation::calculations::{
    FillAdjustmentResult, FillSnapshot, VenuePositionSnapshot, adjust_fills_for_partial_window,
    calculate_reconciliation_price,
};

const DEFAULT_TOLERANCE: Decimal = Decimal::from_parts(1, 0, 0, false, 4); // 0.0001

/// Python wrapper for adjust_fills_for_partial_window.
///
/// Takes ExecutionMassStatus and Instrument, performs all adjustment logic in Rust,
/// and returns tuple of (order_reports, fill_reports) ready for reconciliation.
///
/// # Returns
///
/// Tuple of (Dict[VenueOrderId, OrderStatusReport], Dict[VenueOrderId, List[FillReport]])
///
/// # Errors
///
/// This function returns an error if:
/// - The instrument conversion fails.
/// - Any decimal or tolerance parsing fails.
#[pyfunction(name = "adjust_fills_for_partial_window")]
#[pyo3(signature = (mass_status, instrument, tolerance=None))]
pub fn py_adjust_fills_for_partial_window(
    py: Python<'_>,
    mass_status: &Bound<'_, PyAny>,
    instrument: Py<PyAny>,
    tolerance: Option<String>,
) -> PyResult<Py<PyTuple>> {
    let instrument_any = pyobject_to_instrument_any(py, instrument)?;
    let instrument_id = instrument_any.id();
    let mass_status_obj: ExecutionMassStatus = mass_status.extract()?;
    let account_id = mass_status_obj.account_id;

    let all_position_reports = mass_status_obj.position_reports();

    let position_reports = match all_position_reports.get(&instrument_id) {
        Some(reports) if !reports.is_empty() => reports,
        _ => {
            // No position report - return all orders and fills for this instrument unchanged
            let all_orders = mass_status_obj.order_reports();
            let all_fills = mass_status_obj.fill_reports();

            let orders_dict = PyDict::new(py);
            let fills_dict = PyDict::new(py);

            // Add all orders for this instrument
            for (venue_order_id, order) in all_orders.iter() {
                if order.instrument_id == instrument_id {
                    orders_dict
                        .set_item(venue_order_id.to_string(), order.clone().into_py_any(py)?)?;
                }
            }

            // Add all fills for this instrument
            for (venue_order_id, fills) in all_fills.iter() {
                if let Some(first_fill) = fills.first()
                    && first_fill.instrument_id == instrument_id
                {
                    let fills_list = PyList::empty(py);
                    for fill in fills {
                        let py_fill = Py::new(py, fill.clone())?;
                        fills_list.append(py_fill)?;
                    }
                    fills_dict.set_item(venue_order_id.to_string(), fills_list)?;
                }
            }

            return Ok(PyTuple::new(
                py,
                [orders_dict.into_py_any(py)?, fills_dict.into_py_any(py)?],
            )?
            .into());
        }
    };

    let position_report = &position_reports[0];

    let venue_side = match position_report.position_side {
        PositionSideSpecified::Long => OrderSide::Buy,
        PositionSideSpecified::Short => OrderSide::Sell,
        PositionSideSpecified::Flat => OrderSide::Buy, // Default to Buy for flat
    };

    let venue_position = VenuePositionSnapshot {
        side: venue_side,
        qty: position_report.quantity.into(),
        avg_px: position_report.avg_px_open.unwrap_or(Decimal::ZERO),
    };

    // Extract fills for this instrument and convert to FillSnapshot
    let mut fill_snapshots = Vec::new();
    let mut fill_map: HashMap<VenueOrderId, Vec<FillReport>> = HashMap::new();
    let mut order_map: HashMap<VenueOrderId, OrderStatusReport> = HashMap::new();

    // Seed order_map with ALL orders for this instrument (including those without fills)
    for (venue_order_id, order) in mass_status_obj.order_reports() {
        if order.instrument_id == instrument_id {
            order_map.insert(venue_order_id, order.clone());
        }
    }

    for (venue_order_id, fill_reports) in mass_status_obj.fill_reports() {
        for fill in fill_reports {
            if fill.instrument_id == instrument_id {
                // Prefer order report side, fallback to fill's order_side
                let side = mass_status_obj
                    .order_reports()
                    .get(&venue_order_id)
                    .map(|order| order.order_side)
                    .unwrap_or(fill.order_side);

                fill_snapshots.push(FillSnapshot::new(
                    fill.ts_event.as_u64(),
                    side,
                    fill.last_qty.into(),
                    fill.last_px.into(),
                    venue_order_id,
                ));

                // Store original fills
                fill_map
                    .entry(venue_order_id)
                    .or_default()
                    .push(fill.clone());
            }
        }
    }

    if fill_snapshots.is_empty() {
        // Return original orders and fills if no fills found
        return py_tuple_from_reports(py, &order_map, &fill_map);
    }

    // Parse tolerance
    let tol = if let Some(tol_str) = tolerance {
        Decimal::from_str_exact(&tol_str).map_err(to_pyvalue_err)?
    } else {
        DEFAULT_TOLERANCE
    };

    let result =
        adjust_fills_for_partial_window(&fill_snapshots, &venue_position, &instrument_any, tol);

    // Handle the result and create adjusted order and fill reports
    let (adjusted_orders, adjusted_fills) = match result {
        FillAdjustmentResult::NoAdjustment => {
            // Return original orders and fills
            (order_map, fill_map)
        }
        FillAdjustmentResult::AddSyntheticOpening {
            synthetic_fill,
            existing_fills: _,
        } => {
            // Create synthetic venue_order_id
            let synthetic_venue_order_id = create_synthetic_venue_order_id(synthetic_fill.ts_event);

            // Create synthetic order and fill
            let synthetic_order = create_synthetic_order_report(
                &synthetic_fill,
                account_id,
                instrument_id,
                &instrument_any,
                synthetic_venue_order_id,
            )?;
            let synthetic_fill_report = create_synthetic_fill_report(
                &synthetic_fill,
                account_id,
                instrument_id,
                &instrument_any,
                synthetic_venue_order_id,
            )?;

            let mut adjusted_fills = fill_map;
            adjusted_fills
                .entry(synthetic_venue_order_id)
                .or_default()
                .insert(0, synthetic_fill_report);

            let mut adjusted_orders = order_map;
            adjusted_orders.insert(synthetic_venue_order_id, synthetic_order);

            (adjusted_orders, adjusted_fills)
        }
        FillAdjustmentResult::ReplaceCurrentLifecycle {
            synthetic_fill,
            first_venue_order_id: _,
        } => {
            // Create synthetic venue_order_id
            let synthetic_venue_order_id = create_synthetic_venue_order_id(synthetic_fill.ts_event);

            // Create synthetic order and fill
            let synthetic_order = create_synthetic_order_report(
                &synthetic_fill,
                account_id,
                instrument_id,
                &instrument_any,
                synthetic_venue_order_id,
            )?;
            let synthetic_fill_report = create_synthetic_fill_report(
                &synthetic_fill,
                account_id,
                instrument_id,
                &instrument_any,
                synthetic_venue_order_id,
            )?;

            // Return ONLY the synthetic order and fill
            let mut adjusted_orders = HashMap::new();
            adjusted_orders.insert(synthetic_venue_order_id, synthetic_order);

            let mut adjusted_fills = HashMap::new();
            adjusted_fills.insert(synthetic_venue_order_id, vec![synthetic_fill_report]);

            (adjusted_orders, adjusted_fills)
        }
        FillAdjustmentResult::FilterToCurrentLifecycle {
            last_zero_crossing_ts,
            current_lifecycle_fills: _,
        } => {
            // Filter fills and orders to only those AFTER last zero-crossing
            let mut result_fills = HashMap::new();
            let mut result_orders = HashMap::new();

            for (venue_order_id, fills) in fill_map {
                let filtered: Vec<FillReport> = fills
                    .into_iter()
                    .filter(|f| f.ts_event.as_u64() > last_zero_crossing_ts)
                    .collect();
                if !filtered.is_empty() {
                    result_fills.insert(venue_order_id, filtered);
                    // Keep order report if fills were kept
                    if let Some(order) = order_map.get(&venue_order_id) {
                        result_orders.insert(venue_order_id, order.clone());
                    }
                }
            }
            (result_orders, result_fills)
        }
    };

    py_tuple_from_reports(py, &adjusted_orders, &adjusted_fills)
}

/// Create a synthetic VenueOrderId from a timestamp.
fn create_synthetic_venue_order_id(ts_event: u64) -> VenueOrderId {
    let uuid_str = Ustr::from(&ts_event.to_string());
    let venue_order_id_value = format!(
        "SYNTH-{}",
        &uuid_str.as_str()[..std::cmp::min(29, uuid_str.len())]
    );
    VenueOrderId::new(&venue_order_id_value)
}

/// Create a synthetic OrderStatusReport from a FillSnapshot.
fn create_synthetic_order_report(
    fill: &FillSnapshot,
    account_id: AccountId,
    instrument_id: InstrumentId,
    instrument: &InstrumentAny,
    venue_order_id: VenueOrderId,
) -> PyResult<OrderStatusReport> {
    let qty_f64 = fill
        .qty
        .to_string()
        .parse::<f64>()
        .map_err(to_pyvalue_err)?;
    let order_qty = Quantity::new(qty_f64, instrument.size_precision());

    Ok(OrderStatusReport::new(
        account_id,
        instrument_id,
        None, // client_order_id
        venue_order_id,
        fill.side,
        OrderType::Market,
        TimeInForce::Gtc,
        OrderStatus::Filled,
        order_qty,
        order_qty, // filled_qty = order_qty (fully filled)
        UnixNanos::from(fill.ts_event),
        UnixNanos::from(fill.ts_event),
        UnixNanos::from(fill.ts_event),
        None, // report_id
    ))
}

/// Create a synthetic FillReport from a FillSnapshot.
fn create_synthetic_fill_report(
    fill: &FillSnapshot,
    account_id: AccountId,
    instrument_id: InstrumentId,
    instrument: &InstrumentAny,
    venue_order_id: VenueOrderId,
) -> PyResult<FillReport> {
    let uuid_str = Ustr::from(&fill.ts_event.to_string());
    let trade_id_value = format!(
        "SYNTH-{}",
        &uuid_str.as_str()[..std::cmp::min(29, uuid_str.len())]
    );
    let trade_id = TradeId::new(&trade_id_value);

    let qty_f64 = fill
        .qty
        .to_string()
        .parse::<f64>()
        .map_err(to_pyvalue_err)?;
    let px_f64 = fill.px.to_string().parse::<f64>().map_err(to_pyvalue_err)?;

    Ok(FillReport::new(
        account_id,
        instrument_id,
        venue_order_id,
        trade_id,
        fill.side,
        Quantity::new(qty_f64, instrument.size_precision()),
        Price::new(px_f64, instrument.price_precision()),
        Money::new(0.0, instrument.quote_currency()),
        LiquiditySide::NoLiquiditySide,
        None, // client_order_id
        None, // venue_position_id
        fill.ts_event.into(),
        fill.ts_event.into(),
        None, // report_id
    ))
}

/// Convert HashMaps of orders and fills to Python tuple of dicts.
fn py_tuple_from_reports(
    py: Python<'_>,
    order_map: &HashMap<VenueOrderId, OrderStatusReport>,
    fill_map: &HashMap<VenueOrderId, Vec<FillReport>>,
) -> PyResult<Py<PyTuple>> {
    // Create order reports dict
    let orders_dict = PyDict::new(py);
    for (venue_order_id, order) in order_map {
        orders_dict.set_item(venue_order_id.to_string(), order.clone().into_py_any(py)?)?;
    }

    // Create fill reports dict
    let fills_dict = PyDict::new(py);
    for (venue_order_id, fills) in fill_map {
        let fills_list: Result<Vec<_>, _> =
            fills.iter().map(|f| f.clone().into_py_any(py)).collect();
        fills_dict.set_item(venue_order_id.to_string(), fills_list?)?;
    }

    Ok(PyTuple::new(
        py,
        [orders_dict.into_py_any(py)?, fills_dict.into_py_any(py)?],
    )?
    .into())
}

/// Calculate the price needed for a reconciliation order to achieve target position.
///
/// This is a pure function that calculates what price a fill would need to have
/// to move from the current position state to the target position state with the
/// correct average price.
///
/// # Parameters
///
/// * `current_position_qty` - The current signed position quantity (positive for long, negative for short)
/// * `current_position_avg_px` - The current position average price (can be None for flat position)
/// * `target_position_qty` - The target signed position quantity
/// * `target_position_avg_px` - The target position average price
///
/// # Returns
///
/// Returns `Some(Decimal)` if a valid reconciliation price can be calculated, `None` otherwise.
///
/// # Notes
///
/// The function calculates the reconciliation price using the formula:
/// `(target_qty * target_avg_px) = (current_qty * current_avg_px) + (qty_diff * reconciliation_px)`
#[pyfunction(name = "calculate_reconciliation_price")]
#[pyo3(signature = (current_position_qty, current_position_avg_px, target_position_qty, target_position_avg_px))]
pub fn py_calculate_reconciliation_price(
    current_position_qty: Decimal,
    current_position_avg_px: Option<Decimal>,
    target_position_qty: Decimal,
    target_position_avg_px: Option<Decimal>,
) -> Option<Decimal> {
    calculate_reconciliation_price(
        current_position_qty,
        current_position_avg_px,
        target_position_qty,
        target_position_avg_px,
    )
}
