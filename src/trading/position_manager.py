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
                 telegram_notifier=None):
        self.exec_client = exec_client
        self.symbol = symbol
        self.dry_run = dry_run
        self.telegram_notifier = telegram_notifier
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
        logger.info(f"[{self.symbol}] 🔄 Position closed. Cleaning up...")
        
        # Cancel orders
        try:
            await asyncio.wait_for(
                asyncio.to_thread(self.exec_client.cancel_all_conditional_orders, self.symbol),
                timeout=5.0
            )
        except Exception as e:
            logger.warning(f"[{self.symbol}] ⚠️ Error cancelling orders: {e}")
        
        # Notify if we have position info
        if self.telegram_notifier and self.current_position:
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
                logger.info(f"[{self.symbol}] ✅ Position close notification sent")
            except Exception as e:
                logger.warning(f"[{self.symbol}] ⚠️ Failed to send notification: {e}")
        
        self.current_position = None
    
    async def _get_current_price(self) -> float:
        """Get current market price"""
        try:
            ticker = await asyncio.wait_for(
                asyncio.to_thread(self.exec_client.client.futures_symbol_ticker, symbol=self.symbol),
                timeout=3.0
            )
            return float(ticker['price'])
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

