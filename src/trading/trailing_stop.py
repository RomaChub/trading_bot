"""Trailing stop management with async support"""
import asyncio
import logging
from datetime import datetime
from typing import Optional, Dict
import pytz

logger = logging.getLogger(__name__)


class TrailingStopManager:
    """Manages trailing stop for an open position"""
    
    def __init__(self, exec_client, symbol: str, interval: str, 
                 direction: str, entry_price: float, initial_stop: float,
                 take_profit: float, position_qty: float, 
                 trail_activate_rr: float, trail_mode: str,
                 trail_step_pct: float, trail_buffer_pct: float,
                 dry_run: bool = False, telegram_notifier=None,
                 trailing_status_dict: Optional[Dict] = None,
                 notification_sent_dict: Optional[Dict] = None):
        
        self.exec_client = exec_client
        self.symbol = symbol
        self.interval = interval
        self.direction = direction
        self.entry_price = entry_price
        self.initial_stop = initial_stop
        self.take_profit = take_profit
        self.position_qty = position_qty
        self.trail_activate_rr = float(trail_activate_rr)
        self.trail_mode = trail_mode
        self.trail_step_pct = trail_step_pct
        self.trail_buffer_pct = trail_buffer_pct
        self.dry_run = dry_run
        self.telegram_notifier = telegram_notifier
        self.trailing_status_dict = trailing_status_dict or {}
        self.notification_sent_dict = notification_sent_dict or {}
        
        self.current_stop = initial_stop
        self.risk = abs(entry_price - initial_stop)
        self.trailing_active = False
        self.last_step_applied = 0
        self.last_log_time = 0
        self._stopped = False
        
        # Calculate activation threshold
        if direction == "LONG":
            self.trail_threshold = entry_price + self.trail_activate_rr * self.risk
        else:
            self.trail_threshold = entry_price - self.trail_activate_rr * self.risk
        
        logger.info(f"[Trailing] Started for {direction} position")
        logger.info(f"[Trailing] Entry: ${entry_price:.2f}, Stop: ${initial_stop:.2f}, Risk: ${self.risk:.2f}")
        logger.info(f"[Trailing] Activation threshold: ${self.trail_threshold:.2f}")
    
    async def check_position_exists(self) -> bool:
        """Check if position still exists"""
        try:
            positions = await asyncio.wait_for(
                asyncio.to_thread(self.exec_client.get_open_positions, self.symbol),
                timeout=5.0
            )
            return any(abs(float(p.get("positionAmt", 0))) > 0 for p in positions)
        except asyncio.TimeoutError:
            logger.warning("[Trailing] ⚠️ Timeout checking position, assuming exists")
            return True
        except Exception as e:
            logger.warning(f"[Trailing] ⚠️ Error checking position: {e}")
            return True
    
    async def verify_position_closed(self, max_attempts: int = 3, delay: float = 1.0) -> bool:
        """
        Verify that position is really closed by checking multiple times.
        Returns True only if position is confirmed closed after all checks.
        
        Args:
            max_attempts: Number of verification attempts
            delay: Delay between attempts in seconds
        """
        logger.info(f"[Trailing] 🔍 Проверка реального закрытия позиции (попыток: {max_attempts})...")
        
        for attempt in range(1, max_attempts + 1):
            has_pos = await self.check_position_exists()
            
            if has_pos:
                logger.warning(f"[Trailing] ⚠️ Позиция всё ещё открыта (попытка {attempt}/{max_attempts})")
                if attempt < max_attempts:
                    await asyncio.sleep(delay)
                    continue
                else:
                    logger.error(f"[Trailing] ❌ Позиция не закрыта после {max_attempts} проверок!")
                    return False
            else:
                logger.info(f"[Trailing] ✅ Позиция закрыта (попытка {attempt}/{max_attempts})")
                if attempt < max_attempts:
                    # Double-check after a short delay
                    await asyncio.sleep(delay)
                    continue
                else:
                    logger.info(f"[Trailing] ✅ Позиция подтверждена закрытой после {max_attempts} проверок")
                    return True
        
        # If we get here, all checks passed
        return True
    
    async def get_latest_kline(self):
        """Fetch latest candle data"""
        try:
            klines = await asyncio.wait_for(
                asyncio.to_thread(self.exec_client.fetch_recent_klines, 
                                 self.symbol, self.interval, 2),
                timeout=5.0
            )
            return klines[-1] if klines else None
        except asyncio.TimeoutError:
            logger.warning("[Trailing] ⚠️ Timeout fetching klines")
            return None
        except Exception as e:
            logger.warning(f"[Trailing] ⚠️ Error fetching klines: {e}")
            return None
    
    def calculate_new_stop(self, candle_high: float, candle_low: float) -> float:
        """Calculate new stop loss based on price movement"""
        if not self.trailing_active:
            return self.current_stop
        
        new_stop = self.current_stop
        
        if self.trail_mode == "bar_extremes":
            new_stop = self._calculate_bar_extreme_stop(candle_high, candle_low)
        elif self.trail_mode == "step" and self.trail_step_pct > 0:
            new_stop = self._calculate_step_stop(candle_high, candle_low)
        
        return new_stop
    
    def _calculate_bar_extreme_stop(self, high: float, low: float) -> float:
        """Calculate stop based on bar extremes"""
        if self.direction == "LONG":
            buffer = low * self.trail_buffer_pct / 100.0
            proposed = low - buffer
            return max(self.current_stop, proposed)
        else:  # SHORT
            buffer = high * self.trail_buffer_pct / 100.0
            proposed = high + buffer
            return min(self.current_stop, proposed)
    
    def _calculate_step_stop(self, high: float, low: float) -> float:
        """Calculate stop based on step progression"""
        step_amount = self.entry_price * (self.trail_step_pct / 100.0)
        
        if self.direction == "LONG":
            progress = (high - self.entry_price) / step_amount
            steps = int(progress) if progress > 0 else 0
            
            if steps > self.last_step_applied:
                target_stop = self.initial_stop + steps * step_amount
                buffer = target_stop * self.trail_buffer_pct / 100.0
                proposed = target_stop - buffer
                self.last_step_applied = steps
                return max(self.current_stop, proposed)
        else:  # SHORT
            progress = (self.entry_price - low) / step_amount
            steps = int(progress) if progress > 0 else 0
            
            if steps > self.last_step_applied:
                target_stop = self.initial_stop - steps * step_amount
                buffer = abs(target_stop) * self.trail_buffer_pct / 100.0
                proposed = target_stop + buffer
                self.last_step_applied = steps
                return min(self.current_stop, proposed)
        
        return self.current_stop
    
    def check_activation(self, high: float, low: float, close: float) -> bool:
        """Check if trailing should be activated"""
        if self.trailing_active:
            return False
        
        activated = False
        if self.direction == "LONG" and high >= self.trail_threshold:
            activated = True
        elif self.direction == "SHORT" and low <= self.trail_threshold:
            activated = True
        
        if activated:
            self.trailing_active = True
            self.trailing_status_dict[self.symbol] = True
            
            logger.info(f"[Trailing] ✅ Activated! Price: ${high if self.direction == 'LONG' else low:.2f}")
            
            # Initialize step counter
            if self.trail_mode == "step" and self.trail_step_pct > 0:
                step_amount = self.entry_price * (self.trail_step_pct / 100.0)
                if self.direction == "LONG":
                    progress = (high - self.entry_price) / step_amount
                else:
                    progress = (self.entry_price - low) / step_amount
                self.last_step_applied = int(progress) if progress > 0 else 0
            
            # Notify Telegram
            if self.telegram_notifier:
                try:
                    self.telegram_notifier.notify_trailing_activated(
                        symbol=self.symbol,
                        direction=self.direction,
                        entry_price=self.entry_price,
                        current_price=close,
                        stop_price=self.current_stop,
                        rr_ratio=self.trail_activate_rr
                    )
                except Exception as e:
                    logger.warning(f"[Trailing] ⚠️ Failed to send notification: {e}")
        
        return activated
    
    async def update_stop_loss(self, new_stop: float, current_price: float) -> bool:
        """Update stop loss order"""
        stop_change = abs(new_stop - self.current_stop)
        if stop_change <= 0.01:
            return False
        
        old_stop = self.current_stop
        sl_side = "SELL" if self.direction == "LONG" else "BUY"
        
        try:
            await asyncio.wait_for(
                asyncio.to_thread(
                    self.exec_client.replace_stop_loss,
                    self.symbol, sl_side, self.position_qty, new_stop, current_price
                ),
                timeout=10.0
            )
            
            self.current_stop = new_stop
            logger.info(f"[Trailing] ✅ Stop updated: ${old_stop:.2f} -> ${new_stop:.2f}")
            return True
            
        except asyncio.TimeoutError:
            logger.warning("[Trailing] ⚠️ Timeout updating stop")
            return False
        except ValueError as e:
            logger.warning(f"[Trailing] ⚠️ Stop too close to price: {e}")
            return False
        except Exception as e:
            logger.warning(f"[Trailing] ⚠️ Error updating stop: {e}")
            return False
    
    async def handle_position_closed(self):
        """Handle position closure - cleanup and notify"""
        logger.info("[Trailing] Обнаружено возможное закрытие позиции. Проверяем...")
        
        # Verify that position is really closed before cleaning up
        is_closed = await self.verify_position_closed(max_attempts=3, delay=1.0)
        
        if not is_closed:
            logger.warning("[Trailing] ⚠️ Позиция не закрыта! Пропускаем очистку стопов и тейков.")
            return
        
        logger.info("[Trailing] ✅ Позиция подтверждена закрытой. Очистка...")
        
        # Get exit price
        exit_price = await self._get_exit_price()
        
        # Calculate PnL
        if self.direction == "LONG":
            pnl = (exit_price - self.entry_price) * self.position_qty
        else:
            pnl = (self.entry_price - exit_price) * self.position_qty
        
        # Determine reason
        by_trailing = self.trailing_status_dict.get(self.symbol, False)
        reason = "Trailing Stop" if by_trailing else ("Take Profit" if pnl > 0 else "Stop Loss")
        
        # Send notification
        # Check if notification was already sent by PositionManager
        if self.telegram_notifier:
            if self.notification_sent_dict.get(self.symbol, False):
                logger.info("[Trailing] ℹ️ Уведомление уже отправлено через PositionManager, пропускаем")
            else:
                try:
                    self.telegram_notifier.notify_position_closed(
                        symbol=self.symbol,
                        direction=self.direction,
                        entry_price=self.entry_price,
                        exit_price=exit_price,
                        quantity=self.position_qty,
                        pnl=pnl,
                        by_trailing=by_trailing,
                        reason=reason
                    )
                    self.notification_sent_dict[self.symbol] = True
                    logger.info(f"[Trailing] ✅ Notification sent (Exit: ${exit_price:.2f}, P&L: ${pnl:.2f})")
                except Exception as e:
                    logger.warning(f"[Trailing] ⚠️ Failed to send notification: {e}")
        
        # Cleanup orders only after confirming position is closed
        try:
            await asyncio.wait_for(
                asyncio.to_thread(self.exec_client.cancel_all_conditional_orders, self.symbol),
                timeout=5.0
            )
            logger.info("[Trailing] ✅ Условные ордера (стопы/тейки) удалены")
        except Exception as e:
            logger.warning(f"[Trailing] ⚠️ Error cancelling orders: {e}")
    
    async def _get_exit_price(self) -> float:
        """Get current price as exit price with caching"""
        try:
            price = await asyncio.wait_for(
                asyncio.to_thread(self.exec_client.get_ticker_price, self.symbol, use_cache=True),
                timeout=3.0
            )
            return price if price is not None else self.entry_price
        except Exception as e:
            logger.warning(f"[Trailing] ⚠️ Error getting exit price: {e}")
            return self.entry_price
    
    async def run(self, update_interval: int = 15):
        """Main trailing stop loop (default interval increased to reduce API calls)"""
        try:
            while not self._stopped:
                # Check if position exists
                if not await self.check_position_exists():
                    await self.handle_position_closed()
                    break
                
                # Get latest candle (with rate limiting)
                candle = await self.get_latest_kline()
                if not candle:
                    await asyncio.sleep(update_interval)
                    continue
                
                high = float(candle[2])
                low = float(candle[3])
                close = float(candle[4])
                
                # Check activation
                self.check_activation(high, low, close)
                
                # Calculate and update stop if needed
                new_stop = self.calculate_new_stop(high, low)
                if self.trailing_active:
                    await self.update_stop_loss(new_stop, close)
                
            await asyncio.sleep(update_interval)
            
        except asyncio.CancelledError:
            logger.info("[Trailing] Stopped by cancellation")
        except Exception as e:
            logger.error(f"[Trailing] ⚠️ Error in main loop: {e}")
        finally:
            logger.info("[Trailing] Trailing stop management stopped")
    
    def stop(self):
        """Stop trailing stop management"""
        self._stopped = True

