"""Main trading orchestrator"""
import asyncio
import logging
from typing import Optional
from datetime import datetime
import pytz
import pandas as pd

from src.trading.position_manager import PositionManager
from src.trading.trailing_stop import TrailingStopManager
from src.trading.breakout_detector import BreakoutDetector
from src.data.binance_data import BinanceDataLoader
from src.backtest.engine import BacktestEngine
from src.plotting.live_chart import LiveChart
from src.utils.time_utils import estimate_candles_needed, ensure_utc
from src.config.params import ACCUMULATION_PARAMS

logger = logging.getLogger(__name__)


class SymbolTrader:
    """Manages trading for a single symbol"""
    
    def __init__(self, symbol: str, args, exec_client, total_balance: float,
                 dry_run: bool = False, chart_port: Optional[int] = None,
                 telegram_notifier=None):
        
        self.symbol = symbol
        self.args = args
        self.exec_client = exec_client
        self.total_balance = total_balance
        self.dry_run = dry_run
        self.telegram_notifier = telegram_notifier
        
        # Initialize components
        self.position_manager = PositionManager(
            exec_client, symbol, dry_run, telegram_notifier
        )
        self.position_manager.trader = self  # Link to trader for zone management
        self.breakout_detector = BreakoutDetector(
            symbol, exec_client, args.zone_max_age_hours, self
        )
        
        # Live chart
        self.live_chart = None
        if args.show_live_chart.lower() == "true" and chart_port:
            self.live_chart = LiveChart(
                symbol=symbol,
                update_interval=args.update_interval,
                port=chart_port
            )
        
        # Data loader
        self.loader = BinanceDataLoader(
            symbol, args.interval, args.lookback_days
        )
        
        # State
        self.zones = []
        self.trailing_task = None
        self.trailing_status = {}
        self.notification_sent = {}
        self._running = False
        self.current_zone_id = None  # ID зоны текущей открытой позиции
    
    async def initialize(self):
        """Initialize trader - load data, check positions, etc."""
        logger.info(f"\n{'='*60}")
        logger.info(f"🚀 Starting trading for {self.symbol}")
        logger.info(f"{'='*60}\n")
        
        # Check existing positions
        await self._check_existing_positions()
        
        # Setup margin and leverage
        await self._setup_trading()
        
        # Load historical data
        await self._load_data()
        
        # Start live chart
        if self.live_chart:
            current_price = await self._get_current_price()
            self.live_chart.update_data(self.loader.df, self.zones, current_price)
            self.live_chart.start()
        
        logger.info(f"[{self.symbol}] 🔄 Starting real-time monitoring...")
    
    async def _check_existing_positions(self):
        """Check for existing open positions"""
        positions = await self.position_manager.get_open_positions()
        if positions:
            logger.warning(f"[{self.symbol}] ⚠️ Found {len(positions)} open position(s)")
            for pos in positions:
                logger.info(f"  - {pos['symbol']}: {pos['positionAmt']} @ ${float(pos['entryPrice']):.2f}")
    
    async def _setup_trading(self):
        """Setup margin type and leverage"""
        try:
            await asyncio.to_thread(
                self.exec_client.set_margin_type, self.symbol, isolated=True
            )
            
            leverage = self.args.leverage if self.args.leverage else 15
            await asyncio.to_thread(
                self.exec_client.set_leverage, self.symbol, leverage
            )
            
            logger.info(f"[{self.symbol}] ✅ Leverage set to {leverage}x")
        except Exception as e:
            logger.warning(f"[{self.symbol}] ⚠️ Error setting up trading: {e}")
    
    async def _load_data(self):
        """Load historical and live data"""
        logger.info(f"[{self.symbol}] 📥 Загрузка исторических данных...")
        df = await asyncio.to_thread(self.loader.load)
        if df is None:
            logger.info(f"[{self.symbol}] 📥 Кэш не найден, загружаю с биржи...")
            df = await asyncio.to_thread(self.loader.fetch_with_simple_pagination)
        else:
            logger.info(f"[{self.symbol}] ✅ Загружено из кэша: {len(df)} свечей")
        
        # Refresh with live data
        live_limit = estimate_candles_needed(
            self.args.interval, self.args.lookback_days
        )
        logger.info(f"[{self.symbol}] 🔄 Обновление актуальными данными...")
        live_df, _ = await asyncio.to_thread(
            self.loader.refresh_live_data, live_limit
        )
        
        if live_df is not None and not live_df.empty:
            self.loader.df = live_df
            logger.info(f"[{self.symbol}] ✅ Данные обновлены: {len(live_df)} свечей")
        
        # Compute zones
        logger.info(f"[{self.symbol}] 🔍 Начинаю поиск зон накопления...")
        self.zones = await self._compute_zones()
        
        if self.zones:
            logger.info(f"[{self.symbol}] 📈 Detected {len(self.zones)} accumulation zone(s)")
        else:
            logger.warning(f"[{self.symbol}] ⚠️ No accumulation zones detected yet")
    
    async def _compute_zones(self):
        """Compute accumulation zones"""
        if self.loader.df is None or self.loader.df.empty:
            logger.warning(f"[{self.symbol}] ⚠️ Нет данных для вычисления зон")
            return []
        
        def _compute():
            engine = BacktestEngine(self.loader.df, capital=self.total_balance)
            _, zones = engine.get_all_zones()
            return zones or []
        
        zones = await asyncio.to_thread(_compute)
        logger.info(f"[{self.symbol}] 📍 Обнаружено зон накопления: {len(zones)}")
        
        if zones:
            # Log details of the newest zone only
            newest_zone = max(zones, key=lambda z: ensure_utc(z.get('end')))
            zone_id = newest_zone.get('zone_id', -1)
            zone_high = newest_zone.get('high', 0)
            zone_low = newest_zone.get('low', 0)
            zone_end = newest_zone.get('end')
            logger.info(f"[{self.symbol}]    Последняя зона #{zone_id}: ${zone_low:.2f} - ${zone_high:.2f} | Окончание: {zone_end}")
        
        return zones
    
    async def _get_current_price(self) -> float:
        """Get current market price"""
        try:
            ticker = await asyncio.wait_for(
                asyncio.to_thread(
                    self.exec_client.client.futures_symbol_ticker,
                    symbol=self.symbol
                ),
                timeout=5.0
            )
            return float(ticker['price'])
        except Exception as e:
            logger.warning(f"[{self.symbol}] ⚠️ Error getting price: {e}")
            return 0.0
    
    async def run(self):
        """Main trading loop"""
        self._running = True
        update_interval = self.args.update_interval
        data_refresh_interval = max(1, self.args.data_refresh_interval)
        last_data_refresh = 0
        iteration = 0
        
        logger.info(f"[{self.symbol}] 🔄 Главный цикл запущен (интервал обновления: {update_interval}с, обновление данных: {data_refresh_interval}с)")
        
        try:
            while self._running:
                iteration += 1
                current_time = datetime.now(pytz.UTC)
                
                # Log every 10th iteration to avoid spam
                if iteration % 10 == 0:
                    logger.info(f"[{self.symbol}] 🔁 Цикл #{iteration} | Время: {current_time.strftime('%H:%M:%S')}")
                
                # Check if position closed
                await self.position_manager.check_position_closed()
                
                # Get current price
                current_price = await self._get_current_price()
                if current_price == 0:
                    await asyncio.sleep(update_interval)
                    continue
                
                # Check for breakouts
                has_position = await self.position_manager.has_position()
                if not has_position:
                    await self._check_breakouts(current_time)
                else:
                    if iteration % 10 == 0:
                        zone_info = f" (зона #{self.current_zone_id})" if self.current_zone_id else ""
                        logger.info(f"[{self.symbol}] 📊 Позиция открыта{zone_info}, ожидание выхода...")
                
                # Refresh data periodically
                import time
                now = time.time()
                if now - last_data_refresh >= data_refresh_interval:
                    logger.info(f"[{self.symbol}] 🔄 Обновление данных и пересчет зон...")
                    await self._refresh_data()
                    last_data_refresh = now
                
                # Update live chart
                if self.live_chart:
                    self.live_chart.update_data(self.loader.df, self.zones, current_price)
                
                await asyncio.sleep(update_interval)
                
        except asyncio.CancelledError:
            logger.info(f"[{self.symbol}] ⏹️ Trading stopped")
        except Exception as e:
            logger.error(f"[{self.symbol}] ❌ Error in trading loop: {e}")
            import traceback
            traceback.print_exc()
        finally:
            await self.cleanup()
    
    async def _check_breakouts(self, current_time: datetime):
        """Check for breakouts and open positions"""
        if not self.zones:
            return
        
        # Get recent klines
        klines_df = await self.breakout_detector.get_recent_klines(
            self.args.interval, limit=20
        )
        if klines_df is None or klines_df.empty:
            return
        
        # Get closed candles
        closed_candles = self.breakout_detector.get_closed_candles(
            klines_df, current_time
        )
        if closed_candles.empty:
            return
        
        latest_candle = closed_candles.iloc[-1]
        
        # Get newest untraded zone
        zone = self.breakout_detector.get_newest_untraded_zone(
            self.zones, current_time
        )
        if not zone:
            return
        
        zone_id = zone.get('zone_id', -1)
        zone_high = float(zone['high'])
        zone_low = float(zone['low'])
        candle_close = float(latest_candle['close'])
        
        # Detect breakout
        direction = self.breakout_detector.detect_breakout(
            zone, latest_candle, current_time
        )
        
        if direction:
            await self._handle_breakout(zone, direction)
        else:
            # Log zone monitoring status every 50 checks
            if not hasattr(self, '_breakout_check_count'):
                self._breakout_check_count = 0
            self._breakout_check_count += 1
            
            if self._breakout_check_count % 50 == 0:
                logger.info(f"[{self.symbol}] 👀 Мониторинг зоны #{zone_id} | Цена: ${candle_close:.2f} | Диапазон: ${zone_low:.2f}-${zone_high:.2f}")
    
    async def _handle_breakout(self, zone: dict, direction: str):
        """Handle detected breakout - open position"""
        zone_id = zone.get('zone_id', -1)
        zone_high = float(zone['high'])
        zone_low = float(zone['low'])
        
        logger.info(f"[{self.symbol}] 🚨 BREAKOUT DETECTED! Zone {zone_id} | {direction}")
        logger.info(f"   Zone: ${zone_low:.2f} - ${zone_high:.2f}")
        
        # Calculate position parameters
        current_price = await self._get_current_price()
        
        if direction == "LONG":
            entry_price = current_price
            stop_loss = zone_low
        else:
            entry_price = current_price
            stop_loss = zone_high
        
        risk = abs(entry_price - stop_loss)
        rr_min = float(ACCUMULATION_PARAMS.get("rr_ratio", 3.0))
        
        if direction == "LONG":
            take_profit = entry_price + rr_min * risk
        else:
            take_profit = entry_price - rr_min * risk
        
        # Calculate position size
        risk_amount = self.total_balance * self.args.risk_per_trade
        position_qty = max(risk_amount / risk, 105.0 / entry_price)
        
        # Validate and round
        rv = await asyncio.to_thread(
            self.exec_client.round_and_validate,
            self.symbol, entry_price, position_qty
        )
        
        if not rv["valid"]:
            logger.warning(f"[{self.symbol}] ❌ minNotional not satisfied")
            return
        
        position_qty = float(rv["qty"])
        
        # Validate margin
        leverage = self.args.leverage if self.args.leverage else 15
        has_margin = await self.position_manager.validate_margin(
            entry_price, position_qty, leverage
        )
        
        if not has_margin:
            logger.warning(f"[{self.symbol}] ⚠️ Insufficient margin, skipping")
            return
        
        # Open position
        success = await self.position_manager.open_position(
            direction, entry_price, position_qty,
            stop_loss, take_profit, zone_id
        )
        
        if not success:
            return
        
        # Mark zone as active (locked for current position)
        self.current_zone_id = zone_id
        logger.info(f"[{self.symbol}] 📍 Активная зона для позиции: #{zone_id}")
        
        # Update chart
        if self.live_chart:
            try:
                entry_time = pd.Timestamp(datetime.now(pytz.UTC))
                self.live_chart.add_entry_point(
                    entry_time, entry_price, direction,
                    zone_id, stop_loss, take_profit
                )
            except Exception as e:
                logger.warning(f"[{self.symbol}] ⚠️ Failed to update chart: {e}")
        
        # Start trailing stop
        if self.args.use_trailing_stop.lower() == "true":
            await self._start_trailing_stop(
                direction, entry_price, stop_loss,
                take_profit, position_qty
            )
    
    async def _start_trailing_stop(self, direction: str, entry_price: float,
                                   stop_loss: float, take_profit: float,
                                   position_qty: float):
        """Start trailing stop management"""
        trailing_manager = TrailingStopManager(
            self.exec_client, self.symbol, self.args.interval,
            direction, entry_price, stop_loss, take_profit, position_qty,
            self.args.trailing_activate_rr, self.args.trailing_mode,
            self.args.trailing_step_pct, self.args.trailing_buffer_pct,
            self.dry_run, self.telegram_notifier,
            self.trailing_status, self.notification_sent
        )
        
        self.trailing_task = asyncio.create_task(
            trailing_manager.run(self.args.update_interval)
        )
        logger.info(f"[{self.symbol}] 🔄 Trailing stop started")
    
    async def _refresh_data(self):
        """Refresh live data and recompute zones"""
        live_limit = estimate_candles_needed(
            self.args.interval, self.args.lookback_days
        )
        
        logger.info(f"[{self.symbol}] 📊 Загрузка свежих данных (лимит свечей: {live_limit})...")
        
        live_df, updated = await asyncio.to_thread(
            self.loader.refresh_live_data, live_limit
        )
        
        if updated:
            logger.info(f"[{self.symbol}] ✅ Данные обновлены, пересчитываю зоны...")
            old_zone_count = len(self.zones)
            self.zones = await self._compute_zones()
            
            # Check if active zone still exists
            if self.current_zone_id is not None:
                available_ids = {z.get("zone_id", i) for i, z in enumerate(self.zones)}
                if self.current_zone_id not in available_ids:
                    logger.warning(f"[{self.symbol}] ⚠️ Активная зона #{self.current_zone_id} исчезла после обновления данных")
        elif not self.zones:
            logger.info(f"[{self.symbol}] 📊 Новых данных нет, но зоны отсутствуют, пересчитываю...")
            self.zones = await self._compute_zones()
        else:
            logger.info(f"[{self.symbol}] ✓ Данные актуальны (зон: {len(self.zones)})")
    
    async def cleanup(self):
        """Cleanup resources"""
        logger.info(f"[{self.symbol}] Cleaning up...")
        
        if self.trailing_task and not self.trailing_task.done():
            self.trailing_task.cancel()
            try:
                await self.trailing_task
            except asyncio.CancelledError:
                pass
        
        if self.live_chart:
            try:
                self.live_chart.stop()
            except Exception as e:
                logger.warning(f"[{self.symbol}] ⚠️ Error stopping chart: {e}")
        
        logger.info(f"[{self.symbol}] ✅ Cleanup complete")
    
    def stop(self):
        """Stop trading"""
        self._running = False

