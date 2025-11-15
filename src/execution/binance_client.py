from typing import Any, Dict, Optional, List
import math
import time

from binance.client import Client
from requests.exceptions import ReadTimeout, ConnectionError

from src.config.settings import BINANCE_API_KEY, BINANCE_API_SECRET
from src.execution.filters import (
	get_symbol_filters,
	round_quantity,
	round_price,
	validate_notional,
	format_quantity_str,
	format_price_str,
)
from src.utils.rate_limiter import get_rate_limiter


class BinanceFuturesExecutor:
	def __init__(self, api_key: str = None, api_secret: str = None, dry_run: bool = True):
		self.api_key = api_key or BINANCE_API_KEY
		self.api_secret = api_secret or BINANCE_API_SECRET
		self.dry_run = dry_run
		self._filters_cache: Dict[str, Dict[str, Any]] = {}  # Cache for symbol filters
		self._ticker_cache: Dict[str, tuple] = {}  # Cache for ticker: {symbol: (price, timestamp)}
		self._ticker_cache_ttl = 1.0  # Cache ticker for 1 second
		self._rate_limiter = get_rate_limiter()
		
		# Debug: Check if API keys are loaded
		if not self.api_key or not self.api_secret:
			print(f"[DEBUG] ⚠️ API keys not loaded! api_key: {bool(self.api_key)}, api_secret: {bool(self.api_secret)}")
		else:
			print(f"[DEBUG] ✅ API keys loaded. Key length: {len(self.api_key)}, Secret length: {len(self.api_secret)}")
		
		# Always use mainnet (no testnet parameter) - exactly like check_balance.py
		print("[DEBUG] Creating Binance Client (mainnet)...")
		# Set longer timeout (30 seconds) to avoid ReadTimeout errors
		requests_params = {
			'timeout': 30  # 30 seconds timeout for both connect and read
		}
		self.client = Client(self.api_key, self.api_secret, requests_params=requests_params)
		print("[DEBUG] ✅ Client created successfully with 30s timeout")

	def set_leverage(self, symbol: str, leverage: int) -> Dict[str, Any]:
		if self.dry_run:
			return {"dry_run": True, "leverage": leverage}
		return self.client.futures_change_leverage(symbol=symbol, leverage=leverage)

	def set_margin_type(self, symbol: str, isolated: bool) -> Dict[str, Any]:
		if self.dry_run:
			return {"dry_run": True, "isolated": isolated}
		margin_type = "ISOLATED" if isolated else "CROSSED"
		try:
			return self.client.futures_change_margin_type(symbol=symbol, marginType=margin_type)
		except Exception as e:
			return {"ok": True, "message": str(e)}

	def get_position_mode(self) -> str:
		"""Get current position mode: 'Hedge' or 'One-way'"""
		if self.dry_run:
			return "One-way"
		try:
			result = self.client.futures_get_position_mode()
			# Returns: {'dualSidePosition': True/False}
			# True = Hedge Mode, False = One-way Mode
			return "Hedge" if result.get('dualSidePosition', False) else "One-way"
		except Exception as e:
			print(f"[WARNING] Could not get position mode: {e}")
			return "One-way"  # Default assumption

	def set_position_mode(self, hedge_mode: bool = False) -> Dict[str, Any]:
		"""
		Set position mode.
		
		Args:
			hedge_mode: If True, enables Hedge Mode (allows both LONG and SHORT positions).
						If False, enables One-way Mode (only one position direction at a time).
		"""
		if self.dry_run:
			return {"dry_run": True, "hedge_mode": hedge_mode}
		try:
			# Note: This is a global setting, not per-symbol
			result = self.client.futures_change_position_mode(dualSidePosition=hedge_mode)
			mode_name = "Hedge" if hedge_mode else "One-way"
			print(f"[INFO] Position mode set to: {mode_name}")
			return result
		except Exception as e:
			print(f"[WARNING] Could not set position mode: {e}")
			return {"ok": False, "error": str(e)}

	def get_symbol_filters(self, symbol: str) -> Dict[str, Any]:
		"""Get symbol filters with caching and retry logic."""
		if symbol in self._filters_cache:
			return self._filters_cache[symbol]
		
		for attempt in range(3):
			try:
				filters = get_symbol_filters(self.client, symbol)
				self._filters_cache[symbol] = filters
				return filters
			except (ConnectionError, ReadTimeout) as e:
				if attempt < 2:
					wait_time = (attempt + 1) * 2
					print(f"[WARNING] Connection error fetching filters (attempt {attempt + 1}/3): {e}")
					print(f"[WARNING] Retrying in {wait_time} seconds...")
					time.sleep(wait_time)
				else:
					print(f"[ERROR] Failed to fetch filters after 3 attempts: {e}")
					raise
			except Exception as e:
				print(f"[ERROR] Unexpected error fetching filters: {e}")
				raise

	def get_available_balance(self, asset: str = "USDT") -> float:
		try:
			print(f"[DEBUG] get_available_balance: Starting for asset {asset}")
			
			# Use futures_account() exactly like check_balance.py does
			print("[DEBUG] Calling client.futures_account()...")
			account_info = self.client.futures_account()
			
			if asset == "USDT":
				# Direct access like check_balance.py (not .get())
				total_wallet = account_info['totalWalletBalance']
				print(f"[DEBUG] totalWalletBalance (raw): {total_wallet} (type: {type(total_wallet)})")
				wallet_float = float(total_wallet)
				print(f"[DEBUG] ✅ Returning totalWalletBalance: {wallet_float}")
				return wallet_float
			
			# Fallback to futures_account_balance() for other assets
			print("[DEBUG] Fallback: Trying futures_account_balance()...")
			balances = self.client.futures_account_balance()
			for b in balances:
				if b['asset'] == asset:
					balance_val = float(b['balance'])
					print(f"[DEBUG] ✅ Returning {asset} balance: {balance_val}")
					return balance_val
			
			print(f"[DEBUG] ❌ No balance found for {asset}")
		except KeyError as e:
			print(f"[DEBUG] ❌ KeyError in account_info: {e}")
			print(f"[DEBUG] Available keys: {list(account_info.keys()) if 'account_info' in locals() else 'N/A'}")
			import traceback
			traceback.print_exc()
		except Exception as e:
			print(f"[DEBUG] ❌ get_available_balance error: {e}")
			import traceback
			traceback.print_exc()
		return 0.0

	def get_open_positions(self, symbol: str = None) -> List[Dict[str, Any]]:
		if self.dry_run:
			return []
		try:
			positions = self.client.futures_position_information(symbol=symbol) if symbol else self.client.futures_position_information()
			open_positions = []
			for pos in positions:
				qty = float(pos.get("positionAmt", 0.0))
				if abs(qty) > 0:
					open_positions.append({
						"symbol": pos.get("symbol"),
						"positionAmt": qty,
						"entryPrice": float(pos.get("entryPrice", 0)),
						"unRealizedProfit": float(pos.get("unRealizedProfit", 0)),
						"leverage": int(pos.get("leverage", 1)),
						"isolated": pos.get("isolated", False),
					})
			return open_positions
		except Exception:
			return []

	def get_available_margin(self, symbol: str) -> float:
		if self.dry_run:
			print("[DEBUG] ⚠️ DRY RUN mode - returning 0.0")
			return 0.0
		try:
			print(f"[DEBUG] get_available_margin: Starting for symbol {symbol}")
			print(f"[DEBUG] dry_run={self.dry_run}")
			
			# Use futures_account() exactly like check_balance.py does
			print("[DEBUG] Calling client.futures_account()...")
			account_info = self.client.futures_account()
			print(f"[DEBUG] Account info type: {type(account_info)}")
			print(f"[DEBUG] Account info keys: {list(account_info.keys())[:15]}")
			
			# Direct access like check_balance.py (not .get())
			available_balance = account_info['availableBalance']
			print(f"[DEBUG] availableBalance (raw): {available_balance} (type: {type(available_balance)})")
			
			available = float(available_balance)
			print(f"[DEBUG] ✅ availableBalance converted: {available}")
			
			if available > 0:
				print(f"[DEBUG] ✅ Returning availableBalance: {available}")
				return available
			else:
				print(f"[DEBUG] ⚠️ availableBalance is 0 or negative: {available}")
				# Fallback to futures_account_balance() like check_balance.py
				print("[DEBUG] Fallback: Trying futures_account_balance()...")
				futures_balance = self.client.futures_account_balance()
				for balance in futures_balance:
					if balance['asset'] == 'USDT':
						wallet_balance = float(balance['balance'])
						print(f"[DEBUG] USDT balance from futures_account_balance(): {wallet_balance}")
						if wallet_balance > 0:
							return wallet_balance
			
			print("[DEBUG] ❌ No available margin found")
		except KeyError as e:
			print(f"[DEBUG] ❌ KeyError in account_info: {e}")
			print(f"[DEBUG] Available keys: {list(account_info.keys()) if 'account_info' in locals() else 'N/A'}")
			import traceback
			traceback.print_exc()
		except Exception as e:
			print(f"[DEBUG] ❌ get_available_margin error: {e}")
			import traceback
			traceback.print_exc()
		return 0.0

	def compute_dynamic_leverage(self, capital_available: float, entry_price: float, qty: float, symbol: str, max_leverage_cap: int = 125) -> int:
		notional = entry_price * qty
		if capital_available <= 0:
			return 1
		min_leverage = math.ceil(notional / capital_available) if notional > 0 else 1
		try:
			info = self.client.futures_leverage_bracket(symbol=symbol)
			if info and isinstance(info, list):
				brackets = info[0].get("brackets", [])
				if brackets:
					max_leverage = max(int(b.get("initialLeverage", 1)) for b in brackets)
					return int(max(1, min(min_leverage, max_leverage)))
		except Exception:
			pass
		return int(max(1, min(min_leverage, max_leverage_cap)))

	def round_and_validate(self, symbol: str, price: float, qty: float) -> Dict[str, Any]:
		filters = self.get_symbol_filters(symbol)
		rounded_price = round_price(filters, price)
		rounded_qty = round_quantity(filters, qty)
		is_valid = validate_notional(filters, rounded_price, rounded_qty)
		price_str = format_price_str(filters, rounded_price)
		qty_str = format_quantity_str(filters, rounded_qty)
		return {
			"price": rounded_price,
			"price_str": price_str,
			"qty": rounded_qty,
			"qty_str": qty_str,
			"valid": is_valid,
		}

	def open_long(self, symbol: str, quantity: float, reduce_only: bool = False) -> Dict[str, Any]:
		if self.dry_run:
			return {"dry_run": True, "side": "BUY", "symbol": symbol, "qty": quantity}
		filters = self.get_symbol_filters(symbol)
		rounded_qty = round_quantity(filters, quantity)
		qty_str = format_quantity_str(filters, rounded_qty)
		
		# Check if we need positionSide (Hedge Mode)
		position_mode = self.get_position_mode()
		order_params = {
			"symbol": symbol,
			"side": "BUY",
			"type": "MARKET",
			"quantity": qty_str,
		}
		# Only add reduceOnly if it's True (for closing positions)
		if reduce_only:
			order_params["reduceOnly"] = True
		if position_mode == "Hedge":
			order_params["positionSide"] = "LONG"
		
		return self.client.futures_create_order(**order_params)

	def open_short(self, symbol: str, quantity: float, reduce_only: bool = False) -> Dict[str, Any]:
		if self.dry_run:
			return {"dry_run": True, "side": "SELL", "symbol": symbol, "qty": quantity}
		filters = self.get_symbol_filters(symbol)
		rounded_qty = round_quantity(filters, quantity)
		qty_str = format_quantity_str(filters, rounded_qty)
		
		# Check if we need positionSide (Hedge Mode)
		position_mode = self.get_position_mode()
		order_params = {
			"symbol": symbol,
			"side": "SELL",
			"type": "MARKET",
			"quantity": qty_str,
		}
		# Only add reduceOnly if it's True (for closing positions)
		if reduce_only:
			order_params["reduceOnly"] = True
		if position_mode == "Hedge":
			order_params["positionSide"] = "SHORT"
		
		return self.client.futures_create_order(**order_params)

	def close_position(self, symbol: str) -> Dict[str, Any]:
		if self.dry_run:
			return {"dry_run": True, "action": "close_position", "symbol": symbol}
		positions = self.client.futures_position_information(symbol=symbol)
		resp = {"responses": []}
		position_mode = self.get_position_mode()
		
		for pos in positions:
			qty = float(pos.get("positionAmt", 0.0))
			if qty == 0:
				continue
			
			order_params = {
				"symbol": symbol,
				"type": "MARKET",
				"quantity": str(abs(qty)),
				"reduceOnly": True,
			}
			
			if qty > 0:
				order_params["side"] = "SELL"
				if position_mode == "Hedge":
					order_params["positionSide"] = "LONG"
			else:
				order_params["side"] = "BUY"
				if position_mode == "Hedge":
					order_params["positionSide"] = "SHORT"
			
			order = self.client.futures_create_order(**order_params)
			resp["responses"].append(order)
		return resp

	def place_stop_loss(self, symbol: str, side: str, quantity: float, stop_price: float) -> Dict[str, Any]:
		if self.dry_run:
			return {"dry_run": True, "action": "stop", "symbol": symbol, "side": side, "qty": quantity, "stop": stop_price}
		# Round stop price according to exchange filters (tickSize)
		filters = self.get_symbol_filters(symbol)
		rounded_stop = round_price(filters, stop_price)
		# For STOP_MARKET: use closePosition=True to close entire position, or quantity with closePosition=False
		# We use quantity to match the exact position size
		qty_str = format_quantity_str(filters, quantity)
		stop_str = format_price_str(filters, rounded_stop)
		
		# Check if we need positionSide (Hedge Mode)
		position_mode = self.get_position_mode()
		order_params = {
			"symbol": symbol,
			"side": side,
			"type": "STOP_MARKET",
			"stopPrice": stop_str,
			"closePosition": False,  # Set to True if you want to close entire position without specifying quantity
			"quantity": qty_str,
		}
		if position_mode == "Hedge":
			# For stop loss: if side is "SELL", position is LONG; if side is "BUY", position is SHORT
			order_params["positionSide"] = "LONG" if side == "SELL" else "SHORT"
		
		return self.client.futures_create_order(**order_params)

	def place_take_profit(self, symbol: str, side: str, quantity: float, tp_price: float) -> Dict[str, Any]:
		if self.dry_run:
			return {"dry_run": True, "action": "tp", "symbol": symbol, "side": side, "qty": quantity, "tp": tp_price}
		# Round TP price according to exchange filters (tickSize)
		filters = self.get_symbol_filters(symbol)
		rounded_tp = round_price(filters, tp_price)
		# For TAKE_PROFIT_MARKET: use closePosition=True to close entire position, or quantity with closePosition=False
		# We use quantity to match the exact position size
		qty_str = format_quantity_str(filters, quantity)
		tp_str = format_price_str(filters, rounded_tp)
		
		# Check if we need positionSide (Hedge Mode)
		position_mode = self.get_position_mode()
		order_params = {
			"symbol": symbol,
			"side": side,
			"type": "TAKE_PROFIT_MARKET",
			"stopPrice": tp_str,
			"closePosition": False,  # Set to True if you want to close entire position without specifying quantity
			"quantity": qty_str,
		}
		if position_mode == "Hedge":
			# For take profit: if side is "SELL", position is LONG; if side is "BUY", position is SHORT
			order_params["positionSide"] = "LONG" if side == "SELL" else "SHORT"
		
		return self.client.futures_create_order(**order_params)

	def get_open_orders(self, symbol: str) -> List[Dict[str, Any]]:
		if self.dry_run:
			return []
		return self.client.futures_get_open_orders(symbol=symbol)

	def cancel_order(self, symbol: str, order_id: int) -> Dict[str, Any]:
		if self.dry_run:
			return {"dry_run": True, "action": "cancel", "order_id": order_id}
		return self.client.futures_cancel_order(symbol=symbol, orderId=order_id)

	def cancel_stop_orders(self, symbol: str) -> None:
		if self.dry_run:
			return
		orders = self.get_open_orders(symbol)
		for o in orders:
			otype = o.get("type")
			if otype in ("STOP", "STOP_MARKET"):
				try:
					self.cancel_order(symbol, int(o.get("orderId")))
				except Exception:
					pass
	
	def cancel_all_conditional_orders(self, symbol: str) -> None:
		"""Cancel all conditional orders (stop loss and take profit) for a symbol"""
		if self.dry_run:
			print(f"[Cleanup] DRY RUN: Would cancel all conditional orders for {symbol}")
			return
		
		try:
			orders = self.get_open_orders(symbol)
			if not orders:
				print(f"[Cleanup] No open orders found for {symbol}")
				return
			
			print(f"[Cleanup] Found {len(orders)} open order(s) for {symbol}")
			cancelled = 0
			failed = 0
			
			for o in orders:
				otype = o.get("type", "")
				order_id = o.get("orderId")
				order_status = o.get("status", "")
				
				# Log all orders for debugging
				print(f"[Cleanup] Order {order_id}: type={otype}, status={order_status}")
				
				# Cancel all conditional order types
				# Include all possible conditional order types
				if otype in ("STOP", "STOP_MARKET", "TAKE_PROFIT", "TAKE_PROFIT_MARKET", 
				            "STOP_LOSS", "STOP_LOSS_LIMIT", "TAKE_PROFIT_LIMIT"):
					try:
						self.cancel_order(symbol, int(order_id))
						cancelled += 1
						print(f"[Cleanup] ✅ Cancelled {otype} order {order_id}")
					except Exception as e:
						failed += 1
						# Check if order was already filled or cancelled
						error_msg = str(e).lower()
						if "does not exist" in error_msg or "not found" in error_msg or "already" in error_msg:
							print(f"[Cleanup] Order {order_id} already cancelled/filled (OK)")
						else:
							print(f"[Cleanup] ⚠️ Failed to cancel order {order_id} ({otype}): {e}")
			
			if cancelled > 0:
				print(f"[Cleanup] ✅ Successfully cancelled {cancelled} conditional order(s) for {symbol}")
			elif failed > 0:
				print(f"[Cleanup] ⚠️ Attempted to cancel {failed} order(s), but all were already cancelled/filled")
			else:
				print(f"[Cleanup] No conditional orders found to cancel for {symbol}")
		except Exception as e:
			print(f"[Cleanup] ❌ Error getting/cancelling orders for {symbol}: {e}")
			import traceback
			traceback.print_exc()

	def replace_stop_loss(self, symbol: str, side: str, quantity: float, new_stop: float, current_price: float = None) -> Dict[str, Any]:
		"""
		Replace stop loss with validation to prevent immediate trigger.
		
		Args:
			symbol: Trading symbol
			side: Order side ("BUY" or "SELL")
			quantity: Position quantity
			new_stop: New stop loss price
			current_price: Current market price (optional, for validation)
		"""
		# Round stop price according to exchange filters (tickSize)
		filters = self.get_symbol_filters(symbol)
		new_stop = round_price(filters, new_stop)
		
		# Validate that stop won't trigger immediately
		if current_price is not None:
			tick_size = float(filters.get("PRICE_FILTER", {}).get("tickSize", 0) or 0)
			min_distance = max(tick_size * 2, current_price * 0.0001)  # At least 0.01% or 2 ticks
			
			if side == "SELL":  # LONG position - stop below price
				if new_stop >= current_price - min_distance:
					raise ValueError(f"Stop loss too close to current price. Current: ${current_price:.2f}, Stop: ${new_stop:.2f}, Min distance: ${min_distance:.2f}")
			else:  # SHORT position - stop above price
				if new_stop <= current_price + min_distance:
					raise ValueError(f"Stop loss too close to current price. Current: ${current_price:.2f}, Stop: ${new_stop:.2f}, Min distance: ${min_distance:.2f}")
		
		# Cancel existing stop orders and place a new one
		self.cancel_stop_orders(symbol)
		return self.place_stop_loss(symbol, side=side, quantity=quantity, stop_price=new_stop)

	def get_ticker_price(self, symbol: str, use_cache: bool = True) -> Optional[float]:
		"""
		Get current ticker price with caching and rate limiting.
		
		Args:
			symbol: Trading symbol
			use_cache: Whether to use cached price (default: True)
		
		Returns:
			Current price or None if error
		"""
		current_time = time.time()
		
		# Check cache
		if use_cache and symbol in self._ticker_cache:
			cached_price, cache_time = self._ticker_cache[symbol]
			if current_time - cache_time < self._ticker_cache_ttl:
				return cached_price
		
		# Rate limit before making request
		if not self.dry_run:
			self._rate_limiter.wait_if_needed()
		
		try:
			ticker = self.client.futures_symbol_ticker(symbol=symbol)
			price = float(ticker['price'])
			
			# Update cache
			self._ticker_cache[symbol] = (price, current_time)
			return price
		except Exception as e:
			print(f"[ERROR] Error getting ticker price for {symbol}: {e}")
			# Return cached price if available, even if expired
			if symbol in self._ticker_cache:
				return self._ticker_cache[symbol][0]
			return None
	
	def fetch_recent_klines(self, symbol: str, interval: str, limit: int = 2, max_retries: int = 3):
		"""
		Fetch recent klines with retry logic for timeout errors and rate limiting.
		
		Args:
			symbol: Trading symbol
			interval: Kline interval
			limit: Number of klines to fetch
			max_retries: Maximum number of retry attempts
		
		Returns:
			List of klines or None if all retries failed
		"""
		for attempt in range(max_retries):
			try:
				# Rate limit before making request
				if not self.dry_run:
					self._rate_limiter.wait_if_needed()
				
				return self.client.futures_klines(symbol=symbol, interval=interval, limit=limit)
			except (ReadTimeout, ConnectionError) as e:
				if attempt < max_retries - 1:
					wait_time = (attempt + 1) * 2  # Exponential backoff: 2s, 4s, 6s
					print(f"[WARNING] Timeout/Connection error fetching klines (attempt {attempt + 1}/{max_retries}): {e}")
					print(f"[WARNING] Retrying in {wait_time} seconds...")
					time.sleep(wait_time)
				else:
					print(f"[ERROR] Failed to fetch klines after {max_retries} attempts: {e}")
					return None
			except Exception as e:
				# Check if it's a rate limit error
				error_str = str(e)
				if "Too many requests" in error_str or "-1003" in error_str:
					# Rate limit exceeded - wait longer
					wait_time = 5.0  # Wait 5 seconds for rate limit
					print(f"[RATE LIMIT] Rate limit exceeded, waiting {wait_time} seconds...")
					time.sleep(wait_time)
					if attempt < max_retries - 1:
						continue
				print(f"[ERROR] Unexpected error fetching klines: {e}")
				return None
		return None
