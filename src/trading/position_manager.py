"""Position management logic"""
import asyncio
import logging
from typing import Optional, Dict
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class PositionInfo:
    """Position information"""
    direction: str
    entry_price: float
    quantity: float
    stop_loss: float
    take_profit: float
    zone_id: int


class PositionManager:
    """Manages trading positions"""
    
    def __init__(self, exec_client, symbol: str, dry_run: bool = False,
                 telegram_notifier=None, notification_sent_dict: Optional[Dict] = None):
        self.exec_client = exec_client
        self.symbol = symbol
        self.dry_run = dry_run
        self.telegram_notifier = telegram_notifier
        self.notification_sent_dict = notification_sent_dict or {}
        self.current_position: Optional[PositionInfo] = None
        self._last_has_position = False
    
    async def get_open_positions(self):
        """Get open positions for symbol"""
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self.exec_client.get_open_positions, self.symbol),
                timeout=5.0
            )
        except asyncio.TimeoutError:
            logger.warning(f"[{self.symbol}] ⚠️ Timeout getting positions")
            return []
        except Exception as e:
            logger.warning(f"[{self.symbol}] ⚠️ Error getting positions: {e}")
            return []
    
    async def has_position(self) -> bool:
        """Check if position exists"""
        positions = await self.get_open_positions()
        return any(abs(float(p.get("positionAmt", 0))) > 0 for p in positions)
    
    async def verify_position_closed(self, max_attempts: int = 3, delay: float = 1.0) -> bool:
        """
        Verify that position is really closed by checking multiple times.
        Returns True only if position is confirmed closed after all checks.
        
        Args:
            max_attempts: Number of verification attempts
            delay: Delay between attempts in seconds
        """
        logger.info(f"[{self.symbol}] 🔍 Проверка реального закрытия позиции (попыток: {max_attempts})...")
        
        for attempt in range(1, max_attempts + 1):
            has_pos = await self.has_position()
            
            # Get detailed position info for logging
            positions = await self.get_open_positions()
            if positions:
                pos_details = []
                for p in positions:
                    pos_details.append(f"{p.get('symbol', 'N/A')}: {p.get('positionAmt', 0)} @ ${p.get('entryPrice', 0):.2f}")
                logger.info(f"[{self.symbol}] Детали позиций: {', '.join(pos_details)}")
            
            if has_pos:
                logger.warning(f"[{self.symbol}] ⚠️ Позиция всё ещё открыта (попытка {attempt}/{max_attempts})")
                if attempt < max_attempts:
                    await asyncio.sleep(delay)
                    continue
                else:
                    logger.error(f"[{self.symbol}] ❌ Позиция не закрыта после {max_attempts} проверок!")
                    return False
            else:
                logger.info(f"[{self.symbol}] ✅ Позиция закрыта (попытка {attempt}/{max_attempts})")
                if attempt < max_attempts:
                    # Double-check after a short delay
                    await asyncio.sleep(delay)
                    continue
                else:
                    logger.info(f"[{self.symbol}] ✅ Позиция подтверждена закрытой после {max_attempts} проверок")
                    return True
        
        # If we get here, all checks passed
        return True
    
    async def check_position_closed(self) -> bool:
        """Check if position was closed and handle cleanup"""
        has_pos = await self.has_position()
        
        if self._last_has_position and not has_pos:
            # Position closed
            await self._handle_position_closed()
            self._last_has_position = False
            return True
        
        self._last_has_position = has_pos
        return False
    
    async def _handle_position_closed(self):
        """Handle position closure - cleanup and notify"""
        logger.info(f"[{self.symbol}] 🔄 Обнаружено возможное закрытие позиции. Проверяем...")
        
        # Verify that position is really closed before cleaning up
        is_closed = await self.verify_position_closed(max_attempts=5, delay=1.5)
        
        if not is_closed:
            logger.warning(f"[{self.symbol}] ⚠️ Позиция не закрыта! Пропускаем очистку стопов и тейков.")
            # Reset the flag to avoid false positives
            self._last_has_position = await self.has_position()
            return
        
        # Double-check one more time before sending notification
        await asyncio.sleep(0.5)
        final_check = await self.has_position()
        if final_check:
            logger.warning(f"[{self.symbol}] ⚠️ Позиция всё ещё открыта после финальной проверки! Пропускаем уведомление.")
            self._last_has_position = True
            return
        
        logger.info(f"[{self.symbol}] ✅ Позиция подтверждена закрытой. Очистка...")
        
        # Release zone for re-trading (allows re-entry after false breakouts)
        if hasattr(self, 'trader') and self.trader and self.trader.current_zone_id is not None:
            old_zone = self.trader.current_zone_id
            self.trader.current_zone_id = None
            logger.info(f"[{self.symbol}] 🔓 Зона #{old_zone} снова доступна для торговли")
        
        # Notify if we have position info
        # Check if notification was already sent by TrailingStopManager
        notification_sent = False
        if self.telegram_notifier and self.current_position:
            # Skip notification if it was already sent by TrailingStopManager
            if self.notification_sent_dict.get(self.symbol, False):
                logger.info(f"[{self.symbol}] ℹ️ Уведомление уже отправлено через TrailingStopManager, пропускаем")
                notification_sent = True
            else:
                # Final verification before sending notification
                await asyncio.sleep(0.5)
                last_check = await self.has_position()
                if last_check:
                    logger.error(f"[{self.symbol}] ❌ Позиция открыта перед отправкой уведомления! Отменяем уведомление и НЕ удаляем ордера.")
                    self._last_has_position = True
                    return
                
                try:
                    current_price = await self._get_current_price()
                    
                    if self.current_position.direction == "LONG":
                        pnl = (current_price - self.current_position.entry_price) * self.current_position.quantity
                    else:
                        pnl = (self.current_position.entry_price - current_price) * self.current_position.quantity
                    
                    self.telegram_notifier.notify_position_closed(
                        symbol=self.symbol,
                        direction=self.current_position.direction,
                        entry_price=self.current_position.entry_price,
                        exit_price=current_price,
                        quantity=self.current_position.quantity,
                        pnl=pnl,
                        by_trailing=False,
                        reason="Take Profit" if pnl > 0 else "Stop Loss"
                    )
                    self.notification_sent_dict[self.symbol] = True
                    notification_sent = True
                    logger.info(f"[{self.symbol}] ✅ Position close notification sent")
                    
                    # Verify position is still closed after notification
                    await asyncio.sleep(1.0)
                    post_notification_check = await self.has_position()
                    if post_notification_check:
                        logger.error(f"[{self.symbol}] ❌ КРИТИЧЕСКАЯ ОШИБКА: Позиция открыта ПОСЛЕ отправки уведомления! Позиция не была закрыта. НЕ удаляем ордера.")
                        self._last_has_position = True
                        return
                except Exception as e:
                    logger.warning(f"[{self.symbol}] ⚠️ Failed to send notification: {e}")
        
        # Cancel orders ONLY after all checks passed and notification sent (if needed)
        # This ensures we don't remove protection orders if position is still open
        if notification_sent or not (self.telegram_notifier and self.current_position):
            # Final check before canceling orders
            await asyncio.sleep(0.5)
            final_order_check = await self.has_position()
            if final_order_check:
                logger.error(f"[{self.symbol}] ❌ Позиция открыта перед удалением ордеров! НЕ удаляем ордера.")
                self._last_has_position = True
                return
            
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(self.exec_client.cancel_all_conditional_orders, self.symbol),
                    timeout=5.0
                )
                logger.info(f"[{self.symbol}] ✅ Условные ордера (стопы/тейки) удалены")
            except Exception as e:
                logger.warning(f"[{self.symbol}] ⚠️ Error cancelling orders: {e}")
        else:
            logger.warning(f"[{self.symbol}] ⚠️ Уведомление не отправлено, пропускаем удаление ордеров для безопасности")
        
        self.current_position = None
    
    async def _get_current_price(self) -> float:
        """Get current market price with caching"""
        try:
            price = await asyncio.wait_for(
                asyncio.to_thread(self.exec_client.get_ticker_price, self.symbol, use_cache=True),
                timeout=3.0
            )
            return price if price is not None else 0.0
        except Exception as e:
            logger.warning(f"[{self.symbol}] ⚠️ Error getting price: {e}")
            return 0.0
    
    async def open_position(self, direction: str, entry_price: float, 
                          quantity: float, stop_loss: float, 
                          take_profit: float, zone_id: int) -> bool:
        """Open a new position"""
        
        # Determine sides
        if direction == "LONG":
            open_func = self.exec_client.open_long
            sl_side = "SELL"
            tp_side = "SELL"
        else:
            open_func = self.exec_client.open_short
            sl_side = "BUY"
            tp_side = "BUY"
        
        try:
            # Open position
            open_resp = await asyncio.wait_for(
                asyncio.to_thread(open_func, self.symbol, quantity),
                timeout=10.0
            )
            
            if self.dry_run:
                logger.info(f"[{self.symbol}] DRY RUN: Position would be opened")
            else:
                logger.info(f"[{self.symbol}] ✅ Position opened: {direction} {quantity} @ ${entry_price:.2f}")
            
            # Place stop loss
            await asyncio.wait_for(
                asyncio.to_thread(
                    self.exec_client.place_stop_loss,
                    self.symbol, sl_side, quantity, stop_loss
                ),
                timeout=10.0
            )
            
            # Place take profit
            await asyncio.wait_for(
                asyncio.to_thread(
                    self.exec_client.place_take_profit,
                    self.symbol, tp_side, quantity, take_profit
                ),
                timeout=10.0
            )
            
            # Store position info
            self.current_position = PositionInfo(
                direction=direction,
                entry_price=entry_price,
                quantity=quantity,
                stop_loss=stop_loss,
                take_profit=take_profit,
                zone_id=zone_id
            )
            
            # Reset notification flag for new position
            self.notification_sent_dict[self.symbol] = False
            
            # Notify Telegram
            if self.telegram_notifier:
                self.telegram_notifier.notify_position_opened(
                    symbol=self.symbol,
                    direction=direction,
                    entry_price=entry_price,
                    quantity=quantity,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    zone_id=zone_id
                )
            
            return True
            
        except asyncio.TimeoutError:
            logger.warning(f"[{self.symbol}] ⚠️ Timeout opening position")
            return False
        except Exception as e:
            logger.error(f"[{self.symbol}] ❌ Error opening position: {e}")
            return False
    
    async def validate_margin(self, entry_price: float, quantity: float, 
                            leverage: int) -> bool:
        """Validate sufficient margin for position"""
        notional_value = entry_price * quantity
        required_margin = notional_value / leverage
        
        if self.dry_run:
            return True
        
        try:
            available = await asyncio.wait_for(
                asyncio.to_thread(self.exec_client.get_available_margin, self.symbol),
                timeout=5.0
            )
            
            if available <= 0:
                logger.error(f"[{self.symbol}] ❌ No available margin")
                await self._send_margin_error(0, required_margin)
                return False
            
            if required_margin > available:
                logger.error(f"[{self.symbol}] ❌ Insufficient margin. Required: ${required_margin:.2f}, Available: ${available:.2f}")
                await self._send_margin_error(available, required_margin)
                return False
            
            return True
            
        except asyncio.TimeoutError:
            logger.warning(f"[{self.symbol}] ⚠️ Timeout checking margin, assuming sufficient")
            return True
        except Exception as e:
            logger.warning(f"[{self.symbol}] ⚠️ Error checking margin: {e}")
            return True
    
    async def _send_margin_error(self, available: float, required: float):
        """Send margin error notification"""
        if not self.telegram_notifier or not self.telegram_notifier.chat_id:
            return
        
        try:
            self.telegram_notifier.send_message(
                self.telegram_notifier.chat_id,
                f"⚠️ [{self.symbol}] Недостаточно маржина\n"
                f"Требуется: ${required:.2f} USDT\n"
                f"Доступно: ${available:.2f} USDT"
            )
        except Exception as e:
            logger.warning(f"[{self.symbol}] ⚠️ Failed to send margin error: {e}")

