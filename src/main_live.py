import argparse
import time
import warnings
import threading
import os
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
import pytz
import pandas as pd
from requests.exceptions import ConnectionError
import matplotlib
matplotlib.use('Agg')  # Use non-interactive backend
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

from src.config.settings import DEFAULT_SYMBOL, DEFAULT_INTERVAL
from src.config.params import RISK_MANAGEMENT, ACCUMULATION_PARAMS
from src.data.binance_data import BinanceDataLoader
from src.backtest.engine import BacktestEngine
from src.execution.binance_client import BinanceFuturesExecutor
from src.plotting.live_chart import LiveChart
from src.notifications.telegram_bot import TelegramNotifier

warnings.filterwarnings('ignore')


def manage_trailing_stop(exec_client, symbol, interval, update_interval, direction, entry_price, 
                         initial_stop, take_profit, position_qty, trail_activate_rr, trail_mode,
                         trail_step_pct, trail_buffer_pct, dry_run, telegram_notifier=None, trailing_status_dict=None):
	"""Manage trailing stop for an open position"""
	current_stop = initial_stop
	trail_rr = float(trail_activate_rr)
	risk = abs(entry_price - initial_stop)
	
	if direction == "LONG":
		trail_threshold = entry_price + trail_rr * risk
	else:
		trail_threshold = entry_price - trail_rr * risk
	
	trailing_active = False
	last_step_applied = 0
	last_log_time = 0  # For periodic logging
	
	print(f"[Trailing] Started for {direction} position. Activation threshold: ${trail_threshold:.2f}")
	print(f"[Trailing] Entry: ${entry_price:.2f}, Initial stop: ${initial_stop:.2f}, Risk: ${risk:.2f}")
	
	# Используем ThreadPoolExecutor для неблокирующих API вызовов в трейлинг стопе
	trailing_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix=f"{symbol}_trailing")
	executor_shutdown = False
	
	def _check_executor_alive():
		"""Проверяет, что executor еще работает"""
		if executor_shutdown:
			return False
		try:
			# Пробуем проверить состояние executor через приватный атрибут
			# Это безопасно, так как мы обрабатываем исключения
			if hasattr(trailing_executor, '_shutdown') and trailing_executor._shutdown:
				return False
		except (AttributeError, RuntimeError, Exception):
			# Executor может быть закрыт или недоступен
			return False
		return True
	
	try:
		while True:
			try:
				# Check if executor is still valid
				if not _check_executor_alive():
					print("[Trailing] ⚠️ Executor is shut down, stopping trailing stop management")
					break
				
				# Check if position still exists (неблокирующий вызов)
				def _get_positions_trailing():
					try:
						return exec_client.get_open_positions(symbol)
					except Exception as e:
						print(f"[Trailing] ⚠️ Error getting positions: {e}")
						return []
				
				try:
					positions_future = trailing_executor.submit(_get_positions_trailing)
				except RuntimeError as e:
					error_msg = str(e).lower()
					if "cannot schedule new futures after shutdown" in error_msg or "shutdown" in error_msg:
						print(f"[Trailing] ⚠️ Executor was shut down, stopping trailing stop management. Error: {e}")
						executor_shutdown = True
						break
					# Другие RuntimeError - логируем и продолжаем
					print(f"[Trailing] ⚠️ RuntimeError submitting task: {e}")
					import traceback
					traceback.print_exc()
					time.sleep(update_interval)
					continue
				except Exception as e:
					# Любые другие исключения при submit - логируем и продолжаем
					print(f"[Trailing] ⚠️ Unexpected error submitting task: {e}")
					import traceback
					traceback.print_exc()
					time.sleep(update_interval)
					continue
				
				try:
					positions = positions_future.result(timeout=5)
					has_position = any(abs(float(p.get("positionAmt", 0))) > 0 for p in positions)
				except FutureTimeoutError:
					print("[Trailing] ⚠️ Timeout getting positions, assuming position still exists")
					has_position = True  # Предполагаем что позиция еще есть
				
				if not has_position:
					print("[Trailing] Position closed. Cleaning up conditional orders...")
					# Cancel all conditional orders (stop loss and take profit) when position closes (неблокирующий)
					def _cleanup_trailing():
						try:
							exec_client.cancel_all_conditional_orders(symbol)
						except Exception as e:
							print(f"[Trailing] ⚠️ Error cancelling orders: {e}")
					
					try:
						if _check_executor_alive():
							cleanup_future = trailing_executor.submit(_cleanup_trailing)
							# Не ждем завершения - просто запускаем
						else:
							print("[Trailing] ⚠️ Executor was shut down, skipping cleanup")
					except RuntimeError as e:
						error_msg = str(e).lower()
						if "cannot schedule new futures after shutdown" in error_msg or "shutdown" in error_msg:
							print("[Trailing] ⚠️ Executor was shut down, skipping cleanup")
						else:
							print(f"[Trailing] ⚠️ Error submitting cleanup task: {e}")
					except Exception as e:
						print(f"[Trailing] ⚠️ Unexpected error submitting cleanup: {e}")
					print("[Trailing] Stopping trailing stop management.")
					break
				
				# Fetch latest candles (неблокирующий вызов)
				def _get_klines_trailing():
					try:
						return exec_client.fetch_recent_klines(symbol, interval, limit=2)
					except Exception as e:
						print(f"[Trailing] ⚠️ Error fetching klines: {e}")
						return None
				
				try:
					klines_future = trailing_executor.submit(_get_klines_trailing)
				except RuntimeError as e:
					error_msg = str(e).lower()
					if "cannot schedule new futures after shutdown" in error_msg or "shutdown" in error_msg:
						print(f"[Trailing] ⚠️ Executor was shut down, stopping trailing stop management. Error: {e}")
						executor_shutdown = True
						break
					# Другие RuntimeError - логируем и продолжаем
					print(f"[Trailing] ⚠️ RuntimeError submitting klines task: {e}")
					import traceback
					traceback.print_exc()
					time.sleep(update_interval)
					continue
				except Exception as e:
					# Любые другие исключения при submit - логируем и продолжаем
					print(f"[Trailing] ⚠️ Unexpected error submitting klines task: {e}")
					import traceback
					traceback.print_exc()
					time.sleep(update_interval)
					continue
				
				try:
					kl = klines_future.result(timeout=5)
					if not kl:
						time.sleep(update_interval)
						continue
				except FutureTimeoutError:
					print("[Trailing] ⚠️ Timeout fetching klines, skipping this iteration")
					time.sleep(update_interval)
					continue
				
				last = kl[-1]
				high = float(last[2])
				low = float(last[3])
				
				# Activate trailing only after reaching RR threshold
				if not trailing_active:
					# Log progress towards activation (every 30 seconds)
					now = time.time()
					if now - last_log_time > 30:
						if direction == "LONG":
							progress_pct = ((high - entry_price) / (trail_threshold - entry_price) * 100) if trail_threshold > entry_price else 0
							print(f"[Trailing] Waiting for activation: High=${high:.2f}, Threshold=${trail_threshold:.2f}, Progress={progress_pct:.1f}%")
						else:  # SHORT
							progress_pct = ((entry_price - low) / (entry_price - trail_threshold) * 100) if entry_price > trail_threshold else 0
							print(f"[Trailing] Waiting for activation: Low=${low:.2f}, Threshold=${trail_threshold:.2f}, Progress={progress_pct:.1f}%")
						last_log_time = now
				
					if direction == "LONG" and high >= trail_threshold:
						trailing_active = True
						current_price = float(last[4])  # close price
						print(f"[Trailing] ✅ Activated! Price reached ${high:.2f} (threshold: ${trail_threshold:.2f}, RR: {trail_rr:.2f})")
						# Update trailing status FIRST (before Telegram notification)
						if trailing_status_dict is not None:
							trailing_status_dict[symbol] = True
						# Notify Telegram about trailing activation (non-blocking, with error handling)
						if telegram_notifier:
							try:
								telegram_notifier.notify_trailing_activated(
									symbol=symbol,
									direction=direction,
									entry_price=entry_price,
									current_price=current_price,
									stop_price=current_stop,
									rr_ratio=trail_rr
								)
							except Exception as e:
								print(f"[Trailing] ⚠️ Failed to send Telegram notification: {e}")
								# Continue execution - don't let Telegram errors stop trailing
						# Initialize last_step_applied when trailing activates (for step mode)
						if trail_mode == "step" and trail_step_pct > 0:
							step_amount = entry_price * (trail_step_pct / 100.0)
							progress = (high - entry_price) / step_amount
							last_step_applied = int(progress) if progress > 0 else 0
							print(f"[Trailing] Initialized step counter: {last_step_applied} steps from entry")
					elif direction == "SHORT" and low <= trail_threshold:
						trailing_active = True
						current_price = float(last[4])  # close price
						print(f"[Trailing] ✅ Activated! Price reached ${low:.2f} (threshold: ${trail_threshold:.2f}, RR: {trail_rr:.2f})")
						# Update trailing status FIRST (before Telegram notification)
						if trailing_status_dict is not None:
							trailing_status_dict[symbol] = True
						# Notify Telegram about trailing activation (non-blocking, with error handling)
						if telegram_notifier:
							try:
								telegram_notifier.notify_trailing_activated(
									symbol=symbol,
									direction=direction,
									entry_price=entry_price,
									current_price=current_price,
									stop_price=current_stop,
									rr_ratio=trail_rr
								)
							except Exception as e:
								print(f"[Trailing] ⚠️ Failed to send Telegram notification: {e}")
								# Continue execution - don't let Telegram errors stop trailing
						# Initialize last_step_applied when trailing activates (for step mode)
						if trail_mode == "step" and trail_step_pct > 0:
							step_amount = entry_price * (trail_step_pct / 100.0)
							progress = (entry_price - low) / step_amount
							last_step_applied = int(progress) if progress > 0 else 0
							print(f"[Trailing] Initialized step counter: {last_step_applied} steps from entry")
				
				# Update trailing stop ONLY if trailing is active (after RR threshold reached)
				new_stop = current_stop
				if trailing_active:
					# Log that trailing is active and we're checking for updates (every 60 seconds)
					now = time.time()
					if now - last_log_time > 60:
						print(f"[Trailing] Active and monitoring for stop updates (Current: ${current_stop:.2f}, Price: ${float(last[4]):.2f})")
						last_log_time = now
					if trail_mode == "bar_extremes":
						if direction == "LONG":
							# For LONG: stop should be below the low, with optional buffer
							buffer = low * trail_buffer_pct / 100.0 if trail_buffer_pct > 0 else 0
							proposed_stop = low - buffer
							# Only move stop up (higher is better for LONG)
							new_stop = max(current_stop, proposed_stop)
							print(f"[Trailing] LONG bar_extremes: Low=${low:.2f}, Buffer=${buffer:.2f}, Proposed=${proposed_stop:.2f}, Current=${current_stop:.2f}, New=${new_stop:.2f}")
						else:  # SHORT
							# For SHORT: stop should be above the high, with optional buffer
							buffer = high * trail_buffer_pct / 100.0 if trail_buffer_pct > 0 else 0
							proposed_stop = high + buffer
							# Only move stop down (lower is better for SHORT)
							new_stop = min(current_stop, proposed_stop)
							print(f"[Trailing] SHORT bar_extremes: High=${high:.2f}, Buffer=${buffer:.2f}, Proposed=${proposed_stop:.2f}, Current=${current_stop:.2f}, New=${new_stop:.2f}")
					elif trail_mode == "step" and trail_step_pct > 0:
						step_amount = entry_price * (trail_step_pct / 100.0)
						if direction == "LONG":
							progress = (high - entry_price) / step_amount
							steps = int(progress) if progress > 0 else 0
							if steps > last_step_applied:
								# For LONG: stop moves up from initial_stop, not from entry_price
								target_stop = initial_stop + steps * step_amount
								buffer = target_stop * trail_buffer_pct / 100.0 if trail_buffer_pct > 0 else 0
								proposed_stop = target_stop - buffer
								# Only move stop up (higher is better for LONG), but ensure it's below current price
								new_stop = max(current_stop, proposed_stop)
								# Ensure stop is below current price (safety check)
								current_market_price = float(last[4])  # close price
								if new_stop >= current_market_price:
									new_stop = current_market_price * 0.999  # Set stop slightly below current price
								last_step_applied = steps
								print(f"[Trailing] LONG step: Steps={steps}, Target=${target_stop:.2f}, Buffer=${buffer:.2f}, Proposed=${proposed_stop:.2f}, Current=${current_stop:.2f}, New=${new_stop:.2f}")
						else:  # SHORT
							progress = (entry_price - low) / step_amount
							steps = int(progress) if progress > 0 else 0
							if steps > last_step_applied:
								# For SHORT: stop moves down from initial_stop, not from entry_price
								target_stop = initial_stop - steps * step_amount
								buffer = abs(target_stop) * trail_buffer_pct / 100.0 if trail_buffer_pct > 0 else 0
								proposed_stop = target_stop + buffer
								# Only move stop down (lower is better for SHORT), but ensure it's above current price
								new_stop = min(current_stop, proposed_stop)
								# Ensure stop is above current price (safety check)
								current_market_price = float(last[4])  # close price
								if new_stop <= current_market_price:
									new_stop = current_market_price * 1.001  # Set stop slightly above current price
								last_step_applied = steps
								print(f"[Trailing] SHORT step: Steps={steps}, Target=${target_stop:.2f}, Buffer=${buffer:.2f}, Proposed=${proposed_stop:.2f}, Current=${current_stop:.2f}, New=${new_stop:.2f}")
				
				# Update stop loss if changed significantly
				stop_change = abs(new_stop - current_stop)
				if stop_change > 0.01:  # Only update if change is significant
					old_stop = current_stop
					current_stop = new_stop
					sl_side = "SELL" if direction == "LONG" else "BUY"
					try:
						# Get current price for validation
						current_market_price = float(last[4])  # close price from last candle
						print(f"[Trailing] Updating stop: ${old_stop:.2f} -> ${current_stop:.2f} (change: ${stop_change:.2f})")
						
						# Неблокирующий вызов для обновления стопа
						def _update_stop():
							try:
								return exec_client.replace_stop_loss(
									symbol, side=sl_side, quantity=position_qty, 
									new_stop=current_stop, current_price=current_market_price
								)
							except Exception as e:
								raise e  # Пробрасываем исключение для обработки выше
						
						try:
							if not _check_executor_alive():
								print("[Trailing] ⚠️ Executor was shut down, cannot update stop")
								executor_shutdown = True
								break
							stop_future = trailing_executor.submit(_update_stop)
						except RuntimeError as e:
							error_msg = str(e).lower()
							if "cannot schedule new futures after shutdown" in error_msg or "shutdown" in error_msg:
								print(f"[Trailing] ⚠️ Executor was shut down, cannot update stop. Error: {e}")
								executor_shutdown = True
								break
							# Другие RuntimeError - логируем и продолжаем
							print(f"[Trailing] ⚠️ RuntimeError submitting stop update: {e}")
							import traceback
							traceback.print_exc()
							current_stop = old_stop
							time.sleep(update_interval)
							continue
						except Exception as e:
							# Любые другие исключения при submit - логируем и продолжаем
							print(f"[Trailing] ⚠️ Unexpected error submitting stop update: {e}")
							import traceback
							traceback.print_exc()
							current_stop = old_stop
							time.sleep(update_interval)
							continue
						
						try:
							resp = stop_future.result(timeout=10)  # Таймаут 10 секунд для обновления стопа
							if dry_run:
								print(f"[Trailing] ✅ DRY RUN: Stop would be updated to ${current_stop:.2f}")
							else:
								print(f"[Trailing] ✅ Stop updated to ${current_stop:.2f} | Response: {resp}")
						except FutureTimeoutError:
							print("[Trailing] ⚠️ Timeout updating stop, reverting to old stop")
							current_stop = old_stop
							continue
					except ValueError as e:
						# Stop too close to price - skip this update and revert
						current_stop = old_stop
						print(f"[Trailing] ⚠️ Skipping stop update (too close): {e}")
					except Exception as e:
						# Revert on error
						current_stop = old_stop
						print(f"[Trailing] ⚠️ Error updating stop: {e}")
						import traceback
						traceback.print_exc()
				else:
					# Log when trailing is active but no update needed
					if trailing_active:
						print(f"[Trailing] Active but no update needed. Current stop: ${current_stop:.2f}, Proposed: ${new_stop:.2f}, Change: ${stop_change:.4f}")
				
				time.sleep(update_interval)
				
			except KeyboardInterrupt:
				print("[Trailing] Stopped by user")
				break
			except RuntimeError as e:
				# Handle executor shutdown errors
				error_msg = str(e).lower()
				if "cannot schedule new futures after shutdown" in error_msg or "shutdown" in error_msg:
					print(f"[Trailing] ⚠️ Executor was shut down, stopping trailing stop management. Error: {e}")
					executor_shutdown = True
					break
				else:
					print(f"[Trailing] ⚠️ Runtime error in trailing stop loop: {e}")
					import traceback
					traceback.print_exc()
					# Продолжаем работу - не останавливаем трейлинг из-за ошибок
					time.sleep(update_interval)
			except Exception as e:
				print(f"[Trailing] ⚠️ Error in trailing stop loop: {e}")
				import traceback
				traceback.print_exc()
				# Continue execution - don't stop trailing on errors
				time.sleep(update_interval)
	finally:
		# Properly shutdown the executor
		try:
			if not executor_shutdown and _check_executor_alive():
				print("[Trailing] Shutting down executor...")
				# Используем wait=False чтобы не блокировать, но даем время на завершение
				trailing_executor.shutdown(wait=False)
			else:
				# If already shut down, just ensure cleanup
				try:
					if hasattr(trailing_executor, '_shutdown') and not trailing_executor._shutdown:
						trailing_executor.shutdown(wait=False)
				except (AttributeError, RuntimeError, Exception):
					pass
		except Exception as e:
			print(f"[Trailing] ⚠️ Error shutting down executor: {e}")
			import traceback
			traceback.print_exc()
		print("[Trailing] Trailing stop management stopped")


def _ensure_utc(dt: datetime) -> datetime:
	if isinstance(dt, pd.Timestamp):
		if dt.tz is None:
			dt = dt.tz_localize('UTC')
		else:
			dt = dt.tz_convert('UTC')
		return dt.to_pydatetime()
	if isinstance(dt, datetime):
		if dt.tzinfo is None:
			return dt.replace(tzinfo=pytz.UTC)
		return dt.astimezone(pytz.UTC)
	parsed = pd.Timestamp(dt)
	if parsed.tz is None:
		parsed = parsed.tz_localize('UTC')
	else:
		parsed = parsed.tz_convert('UTC')
	return parsed.to_pydatetime()


def filter_active_zones(zones, current_price: float, current_time: datetime, max_zone_age_hours: int):
	active = []
	max_zone_age = timedelta(hours=max_zone_age_hours)
	for zone in zones or []:
		try:
			zone_end = _ensure_utc(zone.get("end"))
		except Exception:
			continue
		if zone_end >= current_time:
			continue
		if current_time - zone_end > max_zone_age:
			continue
		zone_high = float(zone.get("high", 0.0))
		zone_low = float(zone.get("low", 0.0))
		if zone_low <= current_price <= zone_high:
			active.append(zone)
	return active


def compute_zones(df: pd.DataFrame, capital: float) -> list:
	if df is None or df.empty:
		return []
	engine = BacktestEngine(df, capital=capital)
	_, zones = engine.get_all_zones()
	return zones or []


def generate_trade_chart(df: pd.DataFrame, zone: dict, breakout_candle_idx: pd.Timestamp, 
                         entry_price: float, direction: str, symbol: str, zone_id: int) -> str:
	"""
	Generate a chart showing accumulation zone, breakout point, and entry.
	
	Returns:
		Path to saved chart file
	"""
	try:
		# Create charts directory if it doesn't exist
		charts_dir = os.path.join("charts", symbol)
		os.makedirs(charts_dir, exist_ok=True)
		
		# Get zone boundaries
		zone_start = _ensure_utc(zone.get('start'))
		zone_end = _ensure_utc(zone.get('end'))
		zone_high = float(zone.get('high', 0))
		zone_low = float(zone.get('low', 0))
		
		# Get data around the zone (before and after)
		zone_start_ts = pd.Timestamp(zone_start)
		zone_end_ts = pd.Timestamp(zone_end)
		
		# Get 50 candles before zone start and 20 candles after breakout
		zone_start_idx = df.index.get_indexer([zone_start_ts], method='nearest')[0]
		start_idx = max(0, zone_start_idx - 50)
		end_idx = min(len(df), df.index.get_indexer([breakout_candle_idx], method='nearest')[0] + 20)
		
		chart_df = df.iloc[start_idx:end_idx].copy()
		
		# Create figure
		fig, ax = plt.subplots(figsize=(16, 10))
		
		# Plot candlesticks (simplified version)
		# Draw wicks
		for idx, row in chart_df.iterrows():
			ax.plot([idx, idx], [row['low'], row['high']], color='black', linewidth=0.8, alpha=0.6)
		
		# Draw bodies
		for idx, row in chart_df.iterrows():
			color = '#26a69a' if row['close'] >= row['open'] else '#ef5350'  # Green/Red
			body_low = min(row['open'], row['close'])
			body_high = max(row['open'], row['close'])
			ax.plot([idx, idx], [body_low, body_high], color=color, linewidth=4)
		
		# Highlight accumulation zone
		zone_mask = (chart_df.index >= zone_start_ts) & (chart_df.index <= zone_end_ts)
		zone_data = chart_df[zone_mask]
		if not zone_data.empty:
			ax.fill_between(
				zone_data.index,
				zone_low,
				zone_high,
				alpha=0.3,
				color='yellow',
				label=f'Accumulation Zone (${zone_low:.2f} - ${zone_high:.2f})'
			)
		
		# Mark zone boundaries
		ax.axhline(y=zone_high, color='orange', linestyle='--', linewidth=2, alpha=0.7, label='Zone High')
		ax.axhline(y=zone_low, color='orange', linestyle='--', linewidth=2, alpha=0.7, label='Zone Low')
		
		# Mark breakout candle
		if breakout_candle_idx in chart_df.index:
			breakout_row = chart_df.loc[breakout_candle_idx]
			ax.scatter(
				breakout_candle_idx,
				breakout_row['high'] if direction == "LONG" else breakout_row['low'],
				color='red',
				s=200,
				marker='*',
				zorder=5,
				label=f'Breakout ({direction})'
			)
		
		# Mark entry point
		entry_time = chart_df.index[-1] if len(chart_df) > 0 else breakout_candle_idx
		ax.scatter(
			entry_time,
			entry_price,
			color='blue',
			s=200,
			marker='o',
			zorder=5,
			label=f'Entry @ ${entry_price:.2f}'
		)
		
		# Formatting
		ax.set_title(
			f'{symbol} - Zone {zone_id} Breakout ({direction})\n'
			f'Zone: ${zone_low:.2f} - ${zone_high:.2f} | Entry: ${entry_price:.2f}',
			fontsize=14,
			fontweight='bold'
		)
		ax.set_xlabel('Time', fontsize=12)
		ax.set_ylabel('Price (USDT)', fontsize=12)
		ax.legend(loc='best', fontsize=10)
		ax.grid(True, alpha=0.3)
		
		# Format x-axis dates
		ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m-%d %H:%M'))
		ax.xaxis.set_major_locator(mdates.HourLocator(interval=6))
		plt.xticks(rotation=45)
		
		plt.tight_layout()
		
		# Save chart
		timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
		filename = f"{symbol}_Zone{zone_id}_{direction}_{timestamp}.png"
		filepath = os.path.join(charts_dir, filename)
		plt.savefig(filepath, dpi=150, bbox_inches='tight')
		plt.close(fig)
		
		return filepath
	except Exception as e:
		print(f"⚠️ Error generating chart: {e}")
		import traceback
		traceback.print_exc()
		return ""


def _get_interval_seconds(interval: str) -> int:
	"""Convert interval string to seconds."""
	unit = interval[-1].lower() if interval else 'm'
	try:
		value = int(interval[:-1]) if interval[:-1] else 1
	except ValueError:
		value = 1
	
	if unit == 'm':
		return value * 60
	elif unit == 'h':
		return value * 3600
	elif unit == 'd':
		return value * 86400
	elif unit == 'w':
		return value * 604800
	else:
		return 60  # Default to 1 minute


def estimate_live_limit(interval: str, lookback_days: int, max_limit: int = 1500) -> int:
	unit = interval[-1].lower() if interval else 'm'
	try:
		value = int(interval[:-1]) if interval[:-1] else 1
	except ValueError:
		value = 1
	if unit == 'm':
		minutes_per_candle = value
	elif unit == 'h':
		minutes_per_candle = value * 60
	elif unit == 'd':
		minutes_per_candle = value * 60 * 24
	elif unit == 'w':
		minutes_per_candle = value * 60 * 24 * 7
	else:
		minutes_per_candle = max(1, value)
	total_minutes = lookback_days * 24 * 60
	if minutes_per_candle <= 0:
		return max_limit
	needed = int(total_minutes / minutes_per_candle) + 5
	return int(max(10, min(max_limit, needed)))


def trade_symbol(symbol: str, args, exec_client, total_balance, use_trailing, dry_run, chart_port=None, telegram_notifier=None):
	"""Trade a single symbol - main trading loop for one pair"""
	print(f"\n{'='*60}")
	print(f"🚀 Starting trading for {symbol}")
	print(f"{'='*60}\n")
	
	# Initialize live chart if enabled
	live_chart = None
	if args.show_live_chart.lower() == "true" and chart_port is not None:
		live_chart = LiveChart(symbol=symbol, update_interval=args.update_interval, port=chart_port)
	
	# Track position info for notifications (initialize early so it's available for existing positions)
	last_position_info = None  # Store: {direction, entry_price, quantity, trailing_activated}
	
	# Check for existing positions
	existing_positions = exec_client.get_open_positions(symbol)
	if existing_positions:
		print(f"[{symbol}] ⚠️ Found {len(existing_positions)} open position(s):")
		for pos in existing_positions:
			print(f"  - {pos['symbol']}: {pos['positionAmt']} @ ${pos['entryPrice']:.2f} (P&L: ${pos['unRealizedProfit']:.2f})")
			# Initialize last_position_info for existing position
			position_amt = float(pos.get('positionAmt', 0))
			if abs(position_amt) > 0:
				direction = "LONG" if position_amt > 0 else "SHORT"
				entry_price = float(pos.get('entryPrice', 0))
				quantity = abs(position_amt)
				# Store position info for notifications when it closes
				last_position_info = {
					"direction": direction,
					"entry_price": entry_price,
					"quantity": quantity
				}
				print(f"[{symbol}] ✅ Initialized last_position_info for existing {direction} position: {quantity} @ ${entry_price:.2f}")
	
	# Get available margin
	available_margin = exec_client.get_available_margin(symbol)
	if available_margin <= 0:
		print(f"[{symbol}] ❌ Error: No available margin. Available: ${available_margin:.2f}")
		return
	
	print(f"[{symbol}] Balance: Total: ${total_balance:.2f} USDT | Available margin: ${available_margin:.2f} USDT")
	
	# Load historical data to detect accumulation zones
	loader = BinanceDataLoader(symbol, args.interval, args.lookback_days)
	df = loader.load()
	if df is None:
		df = loader.fetch_with_simple_pagination()
	live_limit = estimate_live_limit(args.interval, args.lookback_days)
	live_df, _ = loader.refresh_live_data(limit=live_limit)
	if live_df is not None and not live_df.empty:
		df = live_df
	else:
		loader.df = df
	
	zones = compute_zones(loader.df, total_balance)
	if zones:
		print(f"[{symbol}] 📈 Detected {len(zones)} accumulation zone(s) from historical data.")
	else:
		print(f"[{symbol}] ⚠️ No accumulation zones detected yet. Waiting for live data updates...")
	
	current_time = datetime.now(pytz.UTC)
	ticker = exec_client.client.futures_symbol_ticker(symbol=symbol)
	current_price = float(ticker['price'])
	
	# Initialize live chart with initial data
	if live_chart:
		live_chart.update_data(df=loader.df, zones=zones, current_price=current_price)
		live_chart.start()
	active_zones = filter_active_zones(zones, current_price, current_time, args.zone_max_age_hours)
	
	if active_zones:
		print(f"[{symbol}] 📊 Monitoring {len(active_zones)} active accumulation zone(s) for breakouts...")
		print(f"  Current price: ${current_price:.2f}")
	else:
		print(f"[{symbol}] No active zones to monitor right now. Monitoring will continue as data refreshes.")
		print(f"  - Current price: ${current_price:.2f}")
	
	# Set margin type and leverage
	exec_client.set_margin_type(symbol, isolated=True)
	fixed_leverage = args.leverage if args.leverage is not None else 15
	leverage_resp = exec_client.set_leverage(symbol, fixed_leverage)
	if not dry_run:
		resp_leverage = leverage_resp.get("leverage") or leverage_resp.get("leverageBracket")
		if resp_leverage and resp_leverage != fixed_leverage:
			print(f"[{symbol}] ⚠️ WARNING: Leverage may not have been set correctly. Expected: {fixed_leverage}x, Got: {resp_leverage}")
	
	print(f"[{symbol}] 🔄 Starting real-time breakout monitoring (updates every {args.update_interval}s)...")
	
	# Track which zones we've already traded
	traded_zones = set()
	data_refresh_interval = max(1, args.data_refresh_interval)
	last_data_refresh = time.time() - data_refresh_interval
	last_zone_count = len(zones)
	last_active_zone_ids = {zone.get("zone_id", idx) for idx, zone in enumerate(active_zones)}
	
	# Track open positions for cleanup and notifications
	initial_positions = exec_client.get_open_positions(symbol)
	last_has_position = any(abs(float(p.get("positionAmt", 0))) > 0 for p in initial_positions)
	# Shared dict to track trailing activation status (key: symbol, value: bool)
	trailing_status = {}  # Will be shared with manage_trailing_stop via closure
	
	# Real-time monitoring loop
	# Используем ThreadPoolExecutor для неблокирующих API вызовов
	executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix=f"{symbol}_api")
	
	while True:
		try:
			current_time = datetime.now(pytz.UTC)
			
			# Неблокирующий вызов для получения цены
			def _get_ticker():
				try:
					return exec_client.client.futures_symbol_ticker(symbol=symbol)
				except Exception as e:
					print(f"[{symbol}] ⚠️ Error getting ticker: {e}")
					return None
			
			ticker_future = executor.submit(_get_ticker)
			try:
				ticker = ticker_future.result(timeout=5)  # Таймаут 5 секунд
				if ticker is None:
					time.sleep(args.update_interval)
					continue
				current_price = float(ticker['price'])
			except FutureTimeoutError:
				print(f"[{symbol}] ⚠️ Timeout getting ticker, skipping this iteration")
				time.sleep(args.update_interval)
				continue
			
			# Неблокирующий вызов для проверки позиций
			def _get_positions():
				try:
					return exec_client.get_open_positions(symbol)
				except Exception as e:
					print(f"[{symbol}] ⚠️ Error getting positions: {e}")
					return []
			
			positions_future = executor.submit(_get_positions)
			try:
				positions = positions_future.result(timeout=5)  # Таймаут 5 секунд
				has_position = any(abs(float(p.get("positionAmt", 0))) > 0 for p in positions)
			except FutureTimeoutError:
				print(f"[{symbol}] ⚠️ Timeout getting positions, using last known state")
				has_position = last_has_position  # Используем последнее известное состояние
			if last_has_position and not has_position:
				# Position was closed - cleanup and notify
				print(f"[{symbol}] 🔄 Position closed. Cleaning up orders and chart...")
				
				# Неблокирующая очистка ордеров
				def _cleanup_orders():
					try:
						exec_client.cancel_all_conditional_orders(symbol)
					except Exception as e:
						print(f"[{symbol}] ⚠️ Error cancelling orders: {e}")
				
				cleanup_future = executor.submit(_cleanup_orders)
				# Не ждем завершения - продолжаем работу
				if live_chart:
					live_chart.remove_entry_points()
				print(f"[{symbol}] ✅ Cleanup initiated")
				
				# Notify Telegram about closed position - ALWAYS send notification
				if telegram_notifier:
					# Try to get position info from last_position_info first
					if last_position_info:
						entry_price = last_position_info.get("entry_price", 0)
						quantity = last_position_info.get("quantity", 0)
						direction = last_position_info.get("direction", "LONG")
						exit_price = current_price
						
						# Calculate PnL
						if direction == "LONG":
							pnl = (exit_price - entry_price) * quantity
						else:  # SHORT
							pnl = (entry_price - exit_price) * quantity
					else:
						# If last_position_info is not available, try to get from recent trades
						# or use default values
						print(f"[{symbol}] ⚠️ last_position_info not available, trying to get from API...")
						try:
							# Try to get information from the last closed position
							# Get recent account trades to find the closed position
							def _get_recent_trades():
								try:
									if not dry_run:
										# Get last 10 trades for this symbol
										trades = exec_client.client.futures_account_trades(symbol=symbol, limit=10)
										return trades
									return []
								except Exception as e:
									print(f"[{symbol}] ⚠️ Error getting recent trades: {e}")
									return []
							
							trades_future = executor.submit(_get_recent_trades)
							try:
								recent_trades = trades_future.result(timeout=5)
								if recent_trades and len(recent_trades) > 0:
									# Find the most recent trade that closed a position
									# For simplicity, use the last trade
									last_trade = recent_trades[-1]
									entry_price = float(last_trade.get("price", 0))
									quantity = abs(float(last_trade.get("qty", 0)))
									# Determine direction from position side or trade side
									side = last_trade.get("side", "BUY")
									direction = "LONG" if side == "BUY" else "SHORT"
									exit_price = current_price
									
									# Calculate PnL (approximate)
									if direction == "LONG":
										pnl = (exit_price - entry_price) * quantity
									else:  # SHORT
										pnl = (entry_price - exit_price) * quantity
									
									print(f"[{symbol}] ✅ Got position info from recent trades: {direction} {quantity} @ ${entry_price:.2f}")
								else:
									# Fallback: use current price and estimate
									print(f"[{symbol}] ⚠️ No recent trades found, using current price as estimate")
									entry_price = current_price
									quantity = 0
									direction = "LONG"
									exit_price = current_price
									pnl = 0
							except FutureTimeoutError:
								print(f"[{symbol}] ⚠️ Timeout getting recent trades, using current price as estimate")
								entry_price = current_price
								quantity = 0
								direction = "LONG"
								exit_price = current_price
								pnl = 0
						except Exception as e:
							print(f"[{symbol}] ⚠️ Error getting position info: {e}")
							# Fallback: use current price
							entry_price = current_price
							quantity = 0
							direction = "LONG"
							exit_price = current_price
							pnl = 0
					
					# Determine if closed by trailing (if trailing was activated)
					by_trailing = trailing_status.get(symbol, False)
					reason = "Take Profit" if pnl > 0 else "Stop Loss"
					if by_trailing:
						reason = "Trailing Stop"
					# Clear trailing status
					if symbol in trailing_status:
						del trailing_status[symbol]
					
					# Always send notification (even if some info is missing)
					try:
						telegram_notifier.notify_position_closed(
							symbol=symbol,
							direction=direction,
							entry_price=entry_price,
							exit_price=exit_price,
							quantity=quantity,
							pnl=pnl,
							by_trailing=by_trailing,
							reason=reason
						)
						print(f"[{symbol}] ✅ Position close notification sent to Telegram")
					except Exception as e:
						print(f"[{symbol}] ⚠️ Failed to send position close notification: {e}")
						import traceback
						traceback.print_exc()
					
					last_position_info = None
			last_has_position = has_position
			
			# Get recent candles to check for breakout BEFORE updating data and recalculating zones
			# Неблокирующий вызов для получения свечей
			def _get_klines():
				try:
					return exec_client.fetch_recent_klines(symbol, args.interval, limit=20)
				except Exception as e:
					print(f"[{symbol}] ⚠️ Error fetching klines: {e}")
					return None
			
			klines_future = executor.submit(_get_klines)
			try:
				kl = klines_future.result(timeout=10)  # Таймаут 10 секунд для свечей
				if not kl:
					time.sleep(args.update_interval)
					continue
			except FutureTimeoutError:
				print(f"[{symbol}] ⚠️ Timeout fetching klines, skipping this iteration")
				time.sleep(args.update_interval)
				continue

			kl_df = pd.DataFrame(kl, columns=['open_time', 'open', 'high', 'low', 'close', 'volume',
			                                 'close_time', 'quote_volume', 'trades', 'taker_buy_base',
			                                 'taker_buy_quote', 'ignore'])
			kl_df['open_time'] = pd.to_datetime(kl_df['open_time'], unit='ms', utc=True)
			kl_df['close_time'] = pd.to_datetime(kl_df['close_time'], unit='ms', utc=True)
			kl_df.set_index('open_time', inplace=True)
			kl_df[['open', 'high', 'low', 'close', 'volume']] = kl_df[['open', 'high', 'low', 'close', 'volume']].astype(float)
			
			# Get the most recent closed candle's close price
			# Используем уже преобразованный close_time (не преобразуем снова)
			closed_candles = kl_df[kl_df['close_time'] < current_time]
			if not closed_candles.empty:
				latest_closed_price = float(closed_candles.iloc[-1]['close'])
				latest_closed_candle = closed_candles.iloc[-1]
			else:
				latest_closed_price = current_price
				latest_closed_candle = None
			
			# ПЕРЕД обновлением данных и пересчетом зон: проверяем пробой только в САМОЙ НОВОЙ зоне
			# ВАЖНО: Берем самую новую зону из ВСЕХ зон, а не только из активных
			# Это нужно, чтобы отслеживать пробой даже если текущая цена уже вышла из зоны
			if zones and latest_closed_candle is not None:
				# Фильтруем зоны: исключаем уже торгованные и слишком старые
				max_zone_age = timedelta(hours=args.zone_max_age_hours)
				candidate_zones = []
				for zone in zones:
					zone_id = zone.get('zone_id', -1)
					if zone_id in traded_zones:
						continue
					zone_end_dt = _ensure_utc(zone.get('end'))
					if zone_end_dt >= current_time:
						continue  # Зона еще не закончилась
					if current_time - zone_end_dt > max_zone_age:
						continue  # Зона слишком старая
					candidate_zones.append(zone)
				
				if not candidate_zones:
					# Нет подходящих зон для отслеживания пробоя
					pass
				else:
					# Находим самую новую зону (с самым поздним end)
					newest_zone = max(candidate_zones, key=lambda z: _ensure_utc(z.get('end')))
					
					breakout_found = False
					zone = newest_zone
					zone_id = zone.get('zone_id', -1)
					
					zone_high = float(zone['high'])
					zone_low = float(zone['low'])
					zone_end_dt = _ensure_utc(zone.get('end'))
					zone_end_ts = pd.Timestamp(zone_end_dt)
					
					# Отладочное сообщение для отслеживания
					print(f"[{symbol}] [DEBUG] Monitoring newest zone {zone_id} for breakout (ended: {zone_end_dt}, range: ${zone_low:.2f}-${zone_high:.2f})")
					
					# Проверяем последнюю закрытую свечу на пробой
					candle_high = float(latest_closed_candle['high'])
					candle_low = float(latest_closed_candle['low'])
					candle_close = float(latest_closed_candle['close'])
					candle_open_time = latest_closed_candle.name  # open_time свечи
					candle_close_time = latest_closed_candle['close_time']  # close_time свечи
					
					# ВАЖНО: Проверяем, что свеча закрылась ПОСЛЕ окончания зоны
					# Используем close_time свечи, а не open_time
					if candle_close_time >= zone_end_ts:
						# Проверяем пробой с закреплением
						# Для LONG: часть свечи должна быть в зоне (low в зоне), а закрыться выше зоны
						# Для SHORT: часть свечи должна быть в зоне (high в зоне), а закрыться ниже зоны
						# LONG пробой: low свечи в зоне И close выше зоны
						if zone_low <= candle_low <= zone_high and candle_close > zone_high:
							# LONG пробой - открываем позицию
							print(f"[{symbol}] 🚨 BREAKOUT DETECTED! Zone {zone_id} | LONG")
							print(f"   Candle closed: {candle_close_time} | Low=${candle_low:.2f} in zone [${zone_low:.2f}-${zone_high:.2f}], Close=${candle_close:.2f} > Zone High=${zone_high:.2f}")
							print(f"   Zone: ${zone_low:.2f} - ${zone_high:.2f} | Current: ${current_price:.2f}")
							
							# Проверяем, есть ли уже открытая позиция по этой паре
							positions = exec_client.get_open_positions(symbol)
							has_position = any(abs(float(p.get("positionAmt", 0))) > 0 for p in positions)
							if has_position:
								print(f"[{symbol}] ⚠️ Position already exists. Skipping new position opening.")
								print(f"   Only one position per symbol is allowed.")
								breakout_found = True
								traded_zones.add(zone_id)
								break
							
							# Открываем позицию
							direction = "LONG"
							entry_price = current_price
							stop_loss = zone_low
							open_side = exec_client.open_long
							sl_side = "SELL"
							tp_side = "SELL"
							
							risk = abs(entry_price - stop_loss)
							rr_min = float(ACCUMULATION_PARAMS.get("rr_ratio", 3.0))
							take_profit = entry_price + rr_min * risk
							
							risk_amount = total_balance * args.risk_per_trade
							risk_price = risk or 1e-8
							position_qty_by_risk = max(risk_amount / risk_price, 0.0)
							
							min_position_value = 105.0
							min_position_qty = min_position_value / entry_price
							position_qty = max(position_qty_by_risk, min_position_qty)
							
							rv = exec_client.round_and_validate(symbol, entry_price, position_qty)
							if not rv["valid"]:
								print(f"[{symbol}] ❌ ERROR: minNotional not satisfied. Qty: {rv['qty']}, Price: {rv['price']}")
								breakout_found = True
								traded_zones.add(zone_id)
								break
							position_qty = float(rv["qty"])
							
							notional_value = entry_price * position_qty
							required_margin = notional_value / fixed_leverage
							current_available_margin = available_margin
							if not dry_run:
								# Неблокирующий вызов для получения маржина с таймаутом
								def _get_margin():
									try:
										return exec_client.get_available_margin(symbol)
									except Exception as e:
										print(f"[{symbol}] ⚠️ Error getting margin: {e}")
										return available_margin  # Используем последнее известное значение
								
								margin_future = executor.submit(_get_margin)
								try:
									current_available_margin = margin_future.result(timeout=5)  # Таймаут 5 секунд
									available_margin = current_available_margin
								except FutureTimeoutError:
									print(f"[{symbol}] ⚠️ Timeout getting margin, using last known value: ${available_margin:.2f}")
									current_available_margin = available_margin  # Используем последнее известное значение
							if not dry_run and current_available_margin <= 0:
								print(f"[{symbol}] ❌ ERROR: No available margin. Latest fetched value: ${current_available_margin:.2f}")
								# Уведомление в Telegram (неблокирующее)
								if telegram_notifier and telegram_notifier.chat_id:
									try:
										telegram_notifier.send_message(
											telegram_notifier.chat_id,
											f"⚠️ [{symbol}] Недостаточно маржина для открытия позиции\n"
											f"Доступно: ${current_available_margin:.2f} USDT"
										)
									except Exception as e:
										print(f"[{symbol}] ⚠️ Failed to send Telegram notification: {e}")
								breakout_found = True
								traded_zones.add(zone_id)
								break
							if not dry_run and required_margin > current_available_margin:
								print(f"[{symbol}] ❌ ERROR: Insufficient margin. Required: ${required_margin:.2f}, Available: ${current_available_margin:.2f}")
								# Уведомление в Telegram (неблокирующее)
								if telegram_notifier and telegram_notifier.chat_id:
									try:
										telegram_notifier.send_message(
											telegram_notifier.chat_id,
											f"⚠️ [{symbol}] Недостаточно маржина для открытия позиции\n"
											f"Требуется: ${required_margin:.2f} USDT\n"
											f"Доступно: ${current_available_margin:.2f} USDT"
										)
									except Exception as e:
										print(f"[{symbol}] ⚠️ Failed to send Telegram notification: {e}")
								breakout_found = True
								traded_zones.add(zone_id)
								break
							
							# Open position (неблокирующая операция)
							print(f"[{symbol}] [DEBUG] Opening {direction} position: {position_qty} @ ${entry_price:.2f}")
							
							def _open_position_and_orders():
								try:
									open_resp = open_side(symbol, quantity=position_qty)
									if dry_run:
										print(f"[{symbol}] [DEBUG] DRY RUN: Position would be opened")
									else:
										print(f"[{symbol}] [DEBUG] Position opened response: {open_resp}")
									
									# Place stop loss and take profit
									print(f"[{symbol}] [DEBUG] Placing stop loss: {sl_side} {position_qty} @ ${stop_loss:.2f}")
									sl_resp = exec_client.place_stop_loss(symbol, side=sl_side, quantity=position_qty, stop_price=stop_loss)
									if not dry_run:
										print(f"[{symbol}] [DEBUG] Stop loss placed: {sl_resp}")
									
									print(f"[{symbol}] [DEBUG] Placing take profit: {tp_side} {position_qty} @ ${take_profit:.2f}")
									tp_resp = exec_client.place_take_profit(symbol, side=tp_side, quantity=position_qty, tp_price=take_profit)
									if not dry_run:
										print(f"[{symbol}] [DEBUG] Take profit placed: {tp_resp}")
									
									print(f"[{symbol}] ✅ Position opened: {direction} {position_qty} @ ${entry_price:.2f}")
									return True
								except Exception as e:
									print(f"[{symbol}] ❌ ERROR opening position: {e}")
									import traceback
									traceback.print_exc()
									return False
							
							# Запускаем в отдельном потоке, но ждем результат (с таймаутом)
							position_future = executor.submit(_open_position_and_orders)
							try:
								success = position_future.result(timeout=15)  # Таймаут 15 секунд для открытия позиции
								if not success:
									print(f"[{symbol}] ⚠️ Position opening failed")
									breakout_found = True
									traded_zones.add(zone_id)
									break
							except FutureTimeoutError:
								print(f"[{symbol}] ⚠️ Timeout opening position, but continuing...")
								# Продолжаем - позиция может открыться в фоне
							
							# Store position info for notifications
							last_position_info = {
								"direction": direction,
								"entry_price": entry_price,
								"quantity": position_qty
							}
							
							# Notify Telegram about opened position (уже неблокирующее)
							if telegram_notifier:
								telegram_notifier.notify_position_opened(
									symbol=symbol,
									direction=direction,
									entry_price=entry_price,
									quantity=position_qty,
									stop_loss=stop_loss,
									take_profit=take_profit,
									zone_id=zone_id
								)
							
							# Add entry point to live chart
							if live_chart:
								try:
									entry_time = pd.Timestamp(candle_close_time) if isinstance(candle_close_time, (pd.Timestamp, datetime)) else datetime.now(pytz.UTC)
									live_chart.add_entry_point(
										entry_time=entry_time,
										entry_price=entry_price,
										direction=direction,
										zone_id=zone_id,
										stop_loss=stop_loss,
										take_profit=take_profit
									)
									# Обновляем график после добавления точки входа, чтобы он не зависал
									live_chart.update_data(df=loader.df, zones=zones, current_price=current_price)
								except Exception as e:
									print(f"[{symbol}] ⚠️ Failed to add entry point to chart: {e}")
							
							# Start trailing stop management in separate thread if enabled
							if use_trailing:
								trailing_thread = threading.Thread(
									target=manage_trailing_stop,
									args=(
										exec_client, symbol, args.interval, args.update_interval,
										direction, entry_price, stop_loss, take_profit, position_qty,
										args.trailing_activate_rr, args.trailing_mode,
										args.trailing_step_pct, args.trailing_buffer_pct, dry_run, telegram_notifier, trailing_status
									),
									daemon=True
								)
								trailing_thread.start()
								print(f"[{symbol}] 🔄 Trailing stop management started for {direction} position")
							
							breakout_found = True
							traded_zones.add(zone_id)
							break
						# SHORT пробой: high свечи в зоне И close ниже зоны
						elif zone_low <= candle_high <= zone_high and candle_close < zone_low:
							# SHORT пробой - открываем позицию (аналогично LONG, но для SHORT)
							print(f"[{symbol}] 🚨 BREAKOUT DETECTED! Zone {zone_id} | SHORT")
							print(f"   Candle closed: {candle_close_time} | High=${candle_high:.2f} in zone [${zone_low:.2f}-${zone_high:.2f}], Close=${candle_close:.2f} < Zone Low=${zone_low:.2f}")
							print(f"   Zone: ${zone_low:.2f} - ${zone_high:.2f} | Current: ${current_price:.2f}")
							
							# Проверяем, есть ли уже открытая позиция по этой паре
							positions = exec_client.get_open_positions(symbol)
							has_position = any(abs(float(p.get("positionAmt", 0))) > 0 for p in positions)
							if has_position:
								print(f"[{symbol}] ⚠️ Position already exists. Skipping new position opening.")
								print(f"   Only one position per symbol is allowed.")
								breakout_found = True
								traded_zones.add(zone_id)
								break
							
							# Открываем позицию
							direction = "SHORT"
							entry_price = current_price
							stop_loss = zone_high
							open_side = exec_client.open_short
							sl_side = "BUY"
							tp_side = "BUY"
							
							risk = abs(entry_price - stop_loss)
							rr_min = float(ACCUMULATION_PARAMS.get("rr_ratio", 3.0))
							take_profit = entry_price - rr_min * risk
							
							risk_amount = total_balance * args.risk_per_trade
							risk_price = risk or 1e-8
							position_qty_by_risk = max(risk_amount / risk_price, 0.0)
							
							min_position_value = 105.0
							min_position_qty = min_position_value / entry_price
							position_qty = max(position_qty_by_risk, min_position_qty)
							
							rv = exec_client.round_and_validate(symbol, entry_price, position_qty)
							if not rv["valid"]:
								print(f"[{symbol}] ❌ ERROR: minNotional not satisfied. Qty: {rv['qty']}, Price: {rv['price']}")
								breakout_found = True
								traded_zones.add(zone_id)
								break
							position_qty = float(rv["qty"])
							
							notional_value = entry_price * position_qty
							required_margin = notional_value / fixed_leverage
							current_available_margin = available_margin
							if not dry_run:
								# Неблокирующий вызов для получения маржина с таймаутом
								def _get_margin():
									try:
										return exec_client.get_available_margin(symbol)
									except Exception as e:
										print(f"[{symbol}] ⚠️ Error getting margin: {e}")
										return available_margin  # Используем последнее известное значение
								
								margin_future = executor.submit(_get_margin)
								try:
									current_available_margin = margin_future.result(timeout=5)  # Таймаут 5 секунд
									available_margin = current_available_margin
								except FutureTimeoutError:
									print(f"[{symbol}] ⚠️ Timeout getting margin, using last known value: ${available_margin:.2f}")
									current_available_margin = available_margin  # Используем последнее известное значение
							if not dry_run and current_available_margin <= 0:
								print(f"[{symbol}] ❌ ERROR: No available margin. Latest fetched value: ${current_available_margin:.2f}")
								# Уведомление в Telegram (неблокирующее)
								if telegram_notifier and telegram_notifier.chat_id:
									try:
										telegram_notifier.send_message(
											telegram_notifier.chat_id,
											f"⚠️ [{symbol}] Недостаточно маржина для открытия позиции\n"
											f"Доступно: ${current_available_margin:.2f} USDT"
										)
									except Exception as e:
										print(f"[{symbol}] ⚠️ Failed to send Telegram notification: {e}")
								breakout_found = True
								traded_zones.add(zone_id)
								break
							if not dry_run and required_margin > current_available_margin:
								print(f"[{symbol}] ❌ ERROR: Insufficient margin. Required: ${required_margin:.2f}, Available: ${current_available_margin:.2f}")
								# Уведомление в Telegram (неблокирующее)
								if telegram_notifier and telegram_notifier.chat_id:
									try:
										telegram_notifier.send_message(
											telegram_notifier.chat_id,
											f"⚠️ [{symbol}] Недостаточно маржина для открытия позиции\n"
											f"Требуется: ${required_margin:.2f} USDT\n"
											f"Доступно: ${current_available_margin:.2f} USDT"
										)
									except Exception as e:
										print(f"[{symbol}] ⚠️ Failed to send Telegram notification: {e}")
								breakout_found = True
								traded_zones.add(zone_id)
								break
							
							# Open position (неблокирующая операция)
							print(f"[{symbol}] [DEBUG] Opening {direction} position: {position_qty} @ ${entry_price:.2f}")
							
							def _open_position_and_orders_short():
								try:
									open_resp = open_side(symbol, quantity=position_qty)
									if dry_run:
										print(f"[{symbol}] [DEBUG] DRY RUN: Position would be opened")
									else:
										print(f"[{symbol}] [DEBUG] Position opened response: {open_resp}")
									
									# Place stop loss and take profit
									print(f"[{symbol}] [DEBUG] Placing stop loss: {sl_side} {position_qty} @ ${stop_loss:.2f}")
									sl_resp = exec_client.place_stop_loss(symbol, side=sl_side, quantity=position_qty, stop_price=stop_loss)
									if not dry_run:
										print(f"[{symbol}] [DEBUG] Stop loss placed: {sl_resp}")
									
									print(f"[{symbol}] [DEBUG] Placing take profit: {tp_side} {position_qty} @ ${take_profit:.2f}")
									tp_resp = exec_client.place_take_profit(symbol, side=tp_side, quantity=position_qty, tp_price=take_profit)
									if not dry_run:
										print(f"[{symbol}] [DEBUG] Take profit placed: {tp_resp}")
									
									print(f"[{symbol}] ✅ Position opened: {direction} {position_qty} @ ${entry_price:.2f}")
									return True
								except Exception as e:
									print(f"[{symbol}] ❌ ERROR opening position: {e}")
									import traceback
									traceback.print_exc()
									return False
							
							# Запускаем в отдельном потоке, но ждем результат (с таймаутом)
							position_future = executor.submit(_open_position_and_orders_short)
							try:
								success = position_future.result(timeout=15)  # Таймаут 15 секунд для открытия позиции
								if not success:
									print(f"[{symbol}] ⚠️ Position opening failed")
									breakout_found = True
									traded_zones.add(zone_id)
									break
							except FutureTimeoutError:
								print(f"[{symbol}] ⚠️ Timeout opening position, but continuing...")
								# Продолжаем - позиция может открыться в фоне
							
							# Store position info for notifications
							last_position_info = {
								"direction": direction,
								"entry_price": entry_price,
								"quantity": position_qty
							}
							
							# Notify Telegram about opened position (уже неблокирующее)
							if telegram_notifier:
								telegram_notifier.notify_position_opened(
									symbol=symbol,
									direction=direction,
									entry_price=entry_price,
									quantity=position_qty,
									stop_loss=stop_loss,
									take_profit=take_profit,
									zone_id=zone_id
								)
							
							# Add entry point to live chart
							if live_chart:
								try:
									entry_time = pd.Timestamp(candle_close_time) if isinstance(candle_close_time, (pd.Timestamp, datetime)) else datetime.now(pytz.UTC)
									live_chart.add_entry_point(
										entry_time=entry_time,
										entry_price=entry_price,
										direction=direction,
										zone_id=zone_id,
										stop_loss=stop_loss,
										take_profit=take_profit
									)
									# Обновляем график после добавления точки входа, чтобы он не зависал
									live_chart.update_data(df=loader.df, zones=zones, current_price=current_price)
								except Exception as e:
									print(f"[{symbol}] ⚠️ Failed to add entry point to chart: {e}")
							
							# Start trailing stop management in separate thread if enabled
							if use_trailing:
								trailing_thread = threading.Thread(
									target=manage_trailing_stop,
									args=(
										exec_client, symbol, args.interval, args.update_interval,
										direction, entry_price, stop_loss, take_profit, position_qty,
										args.trailing_activate_rr, args.trailing_mode,
										args.trailing_step_pct, args.trailing_buffer_pct, dry_run, telegram_notifier, trailing_status
									),
									daemon=True
								)
								trailing_thread.start()
								print(f"[{symbol}] 🔄 Trailing stop management started for {direction} position")
							
							breakout_found = True
							traded_zones.add(zone_id)
							break
						elif zone_low <= candle_close <= zone_high:
							# Цена вернулась в зону - продолжаем отслеживать
							print(f"[{symbol}] [DEBUG] Zone {zone_id}: Price returned to zone (${candle_close:.2f}), continuing to monitor")
			
			# Теперь обновляем данные и пересчитываем зоны
			now = time.time()
			if now - last_data_refresh >= data_refresh_interval:
				live_df, updated = loader.refresh_live_data(limit=live_limit)
				last_data_refresh = now
				if updated or not zones:
					zones = compute_zones(loader.df, total_balance)
					available_zone_ids = {zone.get("zone_id", idx) for idx, zone in enumerate(zones)}
					traded_zones = {zone_id for zone_id in traded_zones if zone_id in available_zone_ids}
					if len(zones) != last_zone_count:
						if zones:
							print(f"[{symbol}] 📈 Updated accumulation zones: {len(zones)} total.")
						else:
							print(f"[{symbol}] ⚠️ No accumulation zones detected. Waiting for accumulation to form...")
						last_zone_count = len(zones)
				
				# Always update live chart with latest data
				if live_chart:
					live_chart.update_data(df=loader.df, zones=zones, current_price=current_price)
			
			# Update live chart with current price and latest data
			if live_chart:
				live_chart.update_data(df=loader.df, zones=zones, current_price=current_price)
			
			# Filter zones: price must be WITHIN zone (waiting for breakout)
			active_zones = filter_active_zones(zones, latest_closed_price, current_time, args.zone_max_age_hours)
			active_zones = [zone for zone in active_zones if zone.get('zone_id', -1) not in traded_zones]
			
			# Additional check: verify price hasn't already broken out with confirmation
			truly_active_zones = []
			for zone in active_zones:
				zone_id = zone.get('zone_id', -1)
				zone_high = float(zone.get('high', 0))
				zone_low = float(zone.get('low', 0))
				zone_end_dt = _ensure_utc(zone.get('end'))
				zone_end_ts = pd.Timestamp(zone_end_dt)
				
				candles_after_zone = closed_candles[closed_candles.index > zone_end_ts]
				if candles_after_zone.empty:
					continue
				
				has_confirmed_breakout = False
				for _, row in candles_after_zone.iterrows():
					candle_high = float(row['high'])
					candle_low = float(row['low'])
					candle_close = float(row['close'])
					
					if (candle_high > zone_high and candle_close > zone_high) or \
					   (candle_low < zone_low and candle_close < zone_low):
						has_confirmed_breakout = True
						break
				
				if has_confirmed_breakout:
					print(f"[{symbol}] [DEBUG] Zone {zone_id}: Confirmed breakout already occurred, skipping")
					traded_zones.add(zone_id)
					continue
				
				if zone_low <= latest_closed_price <= zone_high:
					recent_closes = closed_candles.tail(3)['close'].values if len(closed_candles) >= 3 else closed_candles['close'].values
					if len(recent_closes) > 0:
						prices_in_zone = [zone_low <= float(p) <= zone_high for p in recent_closes]
						if any(prices_in_zone):
							truly_active_zones.append(zone)
			
			active_zones = truly_active_zones
			active_zone_ids = {zone.get("zone_id", idx) for idx, zone in enumerate(active_zones)}
			if active_zone_ids != last_active_zone_ids:
				if active_zones:
					print(f"[{symbol}] 📊 Active zones updated: {len(active_zones)} in play | Price: ${latest_closed_price:.2f}")
				else:
					print(f"[{symbol}] No active zones to monitor right now. Waiting for accumulation to form...")
				last_active_zone_ids = active_zone_ids
			
			if not active_zones:
				time.sleep(args.update_interval)
				continue
			
			time.sleep(args.update_interval)
			
		except KeyboardInterrupt:
			print(f"\n[{symbol}] ⏹️ Monitoring stopped by user")
			if live_chart:
				live_chart.stop()
			break
		except (ConnectionError) as e:
			print(f"[{symbol}] ⚠️ Connection error in monitoring loop: {e}")
			print("Retrying in 5 seconds...")
			time.sleep(5)
			continue
		except Exception as e:
			print(f"[{symbol}] ❌ ERROR in monitoring loop: {e}")
			import traceback
			traceback.print_exc()
			time.sleep(args.update_interval)


def main():
	parser = argparse.ArgumentParser()
	parser.add_argument("--symbol", default=DEFAULT_SYMBOL, help="Single symbol to trade (deprecated, use --symbols)")
	parser.add_argument("--symbols", default="BTCUSDT,ETHUSDT,BNBUSDT", help="Comma-separated list of symbols to trade (e.g., BTCUSDT,BNBUSDT,ETHUSDT). Default: BTCUSDT,ETHUSDT,BNBUSDT")
	parser.add_argument("--interval", default=DEFAULT_INTERVAL)
	parser.add_argument("--lookback_days", type=int, default=5, help="Days of historical data for zone detection (default: 7)")
	parser.add_argument("--zone_max_age_hours", type=int, default=48, help="Maximum age of zones to monitor in hours (default: 48)")
	parser.add_argument("--leverage", type=int, default=15, help="Fixed leverage (default: 15)")
	parser.add_argument("--risk_per_trade", type=float, default=RISK_MANAGEMENT["risk_per_trade"])
	parser.add_argument("--allow_multiple_positions", type=str, default="true", help="Allow opening new position if one already exists")
	parser.add_argument("--dry_run", type=str, default="false", help="Enable dry run mode (no real trades)")
	# Trailing options
	parser.add_argument("--use_trailing_stop", type=str, default=str(ACCUMULATION_PARAMS.get("use_trailing_stop", True)).lower())
	parser.add_argument("--trailing_mode", default=ACCUMULATION_PARAMS.get("trailing_mode", "step"))
	parser.add_argument("--trailing_activate_rr", type=float, default=ACCUMULATION_PARAMS.get("trailing_activate_rr", 1.0))
	parser.add_argument("--trailing_step_pct", type=float, default=ACCUMULATION_PARAMS.get("trailing_step_pct", 0.5))
	parser.add_argument("--trailing_buffer_pct", type=float, default=ACCUMULATION_PARAMS.get("trailing_buffer_pct", 0.0))
	parser.add_argument("--update_interval", type=int, default=5)
	parser.add_argument("--data_refresh_interval", type=int, default=5, help="Seconds between live data refreshes for zone detection")
	parser.add_argument("--show_live_chart", type=str, default="true", help="Show live chart with accumulation zones and entry points")
	args = parser.parse_args()

	dry_run = args.dry_run.lower() == "true"
	use_trailing = args.use_trailing_stop.lower() == "true"
	allow_multiple = args.allow_multiple_positions.lower() == "true"
	show_live_chart = args.show_live_chart.lower() == "true"
	exec_client = BinanceFuturesExecutor(dry_run=dry_run)
	
	# Initialize Telegram notifier
	telegram_notifier = TelegramNotifier()
	
	# Determine symbols to trade
	if args.symbols:
		symbols = [s.strip().upper() for s in args.symbols.split(',')]
	else:
		symbols = [args.symbol.upper()]
	
	print(f"\n{'='*60}")
	print(f"🚀 Starting multi-symbol trading")
	print(f"📊 Symbols: {', '.join(symbols)}")
	print(f"{'='*60}\n")
	
	# Get total balance once
	total_balance = exec_client.get_available_balance("USDT")
	print(f"[Global] Total balance: ${total_balance:.2f} USDT")
	
	# Check position mode
	current_mode = exec_client.get_position_mode()
	print(f"[Global] Position mode: {current_mode}")
	if current_mode == "Hedge":
		print("⚠️ Hedge Mode detected. Strategy will work with positionSide parameter.")
	else:
		print("✅ One-way Mode - optimal for this strategy")
	
	# Start trading for each symbol in separate threads
	threads = []
	for idx, symbol in enumerate(symbols):
		# Use different ports for each symbol's chart (8050, 8051, 8052, etc.)
		chart_port = 8050 + idx if show_live_chart else None
		thread = threading.Thread(
			target=trade_symbol,
			args=(symbol, args, exec_client, total_balance, use_trailing, dry_run, chart_port, telegram_notifier),
			daemon=True
		)
		thread.start()
		threads.append(thread)
		print(f"✅ Started trading thread for {symbol}" + (f" (chart on port {chart_port})" if show_live_chart else ""))
	
	# Wait for all threads
	try:
		for thread in threads:
			thread.join()
	except KeyboardInterrupt:
		print("\n⏹️ Stopping all trading threads...")
		print("Press Ctrl+C again to force exit.")


if __name__ == "__main__":
	main()
