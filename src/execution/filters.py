from typing import Dict, Any

from binance.client import Client
from decimal import Decimal, getcontext

getcontext().prec = 28


def get_symbol_filters(client: Client, symbol: str) -> Dict[str, Any]:
	info = client.futures_exchange_info()
	for s in info.get("symbols", []):
		if s.get("symbol") == symbol:
			filters = {f["filterType"]: f for f in s.get("filters", [])}
			return {
				"LOT_SIZE": filters.get("LOT_SIZE", {}),
				"PRICE_FILTER": filters.get("PRICE_FILTER", {}),
				"MIN_NOTIONAL": filters.get("MIN_NOTIONAL", {}),
				"bracket": s,
			}
	return {"LOT_SIZE": {}, "PRICE_FILTER": {}, "MIN_NOTIONAL": {}, "bracket": {}}


def _round_step(value: float, step: float) -> float:
	if not step or step == 0:
		return value
	value_dec = Decimal(str(value))
	step_dec = Decimal(str(step))
	# Use quantize to ensure value stays within allowed precision, rounding down
	quantized = (value_dec // step_dec) * step_dec
	return float(quantized)


def round_quantity(symbol_filters: Dict[str, Any], qty: float) -> float:
	step = float(symbol_filters.get("LOT_SIZE", {}).get("stepSize", 0) or 0)
	min_qty = float(symbol_filters.get("LOT_SIZE", {}).get("minQty", 0) or 0)
	if step:
		qty = _round_step(qty, step)
	if min_qty and qty < min_qty:
		qty = min_qty
	return max(qty, 0.0)


def round_price(symbol_filters: Dict[str, Any], price: float) -> float:
	tick = float(symbol_filters.get("PRICE_FILTER", {}).get("tickSize", 0) or 0)
	if tick:
		price = _round_step(price, tick)
	return price


def _get_precision_from_step(step_value: str) -> int:
	try:
		step_decimal = Decimal(step_value)
		return max(-step_decimal.as_tuple().exponent, 0)
	except Exception:
		return 8


def _format_decimal(value: float, precision: int) -> str:
	formatted = f"{value:.{precision}f}"
	if "." in formatted:
		formatted = formatted.rstrip("0").rstrip(".")
	return formatted or "0"


def format_quantity_str(symbol_filters: Dict[str, Any], qty: float) -> str:
	step = symbol_filters.get("LOT_SIZE", {}).get("stepSize", "0")
	precision = _get_precision_from_step(step)
	return _format_decimal(qty, precision)


def format_price_str(symbol_filters: Dict[str, Any], price: float) -> str:
	tick = symbol_filters.get("PRICE_FILTER", {}).get("tickSize", "0")
	precision = _get_precision_from_step(tick)
	return _format_decimal(price, precision)


def validate_notional(symbol_filters: Dict[str, Any], price: float, qty: float) -> bool:
	min_notional = float(symbol_filters.get("MIN_NOTIONAL", {}).get("notional", 0) or 0)
	return (price * qty) >= min_notional if min_notional else True
