"""Main trading orchestrator"""
import asyncio
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
from src.utils.time_utils import estimate_candles_needed
from src.config.params import ACCUMULATION_PARAMS


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
        self.breakout_detector = BreakoutDetector(
            symbol, exec_client, args.zone_max_age_hours
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
    
    async def initialize(self):
        """Initialize trader - load data, check positions, etc."""
        print(f"\n{'='*60}")
        print(f"🚀 Starting trading for {self.symbol}")
        print(f"{'='*60}\n")
        
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
        
        print(f"[{self.symbol}] 🔄 Starting real-time monitoring...")
    
    async def _check_existing_positions(self):
        """Check for existing open positions"""
        positions = await self.position_manager.get_open_positions()
        if positions:
            print(f"[{self.symbol}] ⚠️ Found {len(positions)} open position(s)")
            for pos in positions:
                print(f"  - {pos['symbol']}: {pos['positionAmt']} @ ${float(pos['entryPrice']):.2f}")
    
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
            
            print(f"[{self.symbol}] ✅ Leverage set to {leverage}x")
        except Exception as e:
            print(f"[{self.symbol}] ⚠️ Error setting up trading: {e}")
    
    async def _load_data(self):
        """Load historical and live data"""
        df = await asyncio.to_thread(self.loader.load)
        if df is None:
            df = await asyncio.to_thread(self.loader.fetch_with_simple_pagination)
        
        # Refresh with live data
        live_limit = estimate_candles_needed(
            self.args.interval, self.args.lookback_days
        )
        live_df, _ = await asyncio.to_thread(
            self.loader.refresh_live_data, live_limit
        )
        
        if live_df is not None and not live_df.empty:
            self.loader.df = live_df
        
        # Compute zones
        self.zones = await self._compute_zones()
        
        if self.zones:
            print(f"[{self.symbol}] 📈 Detected {len(self.zones)} accumulation zone(s)")
        else:
            print(f"[{self.symbol}] ⚠️ No accumulation zones detected yet")
    
    async def _compute_zones(self):
        """Compute accumulation zones"""
        if self.loader.df is None or self.loader.df.empty:
            return []
        
        def _compute():
            engine = BacktestEngine(self.loader.df, capital=self.total_balance)
            _, zones = engine.get_all_zones()
            return zones or []
        
        return await asyncio.to_thread(_compute)
    
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
            print(f"[{self.symbol}] ⚠️ Error getting price: {e}")
            return 0.0
    
    async def run(self):
        """Main trading loop"""
        self._running = True
        update_interval = self.args.update_interval
        data_refresh_interval = max(1, self.args.data_refresh_interval)
        last_data_refresh = 0
        
        try:
            while self._running:
                current_time = datetime.now(pytz.UTC)
                
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
                
                # Refresh data periodically
                import time
                now = time.time()
                if now - last_data_refresh >= data_refresh_interval:
                    await self._refresh_data()
                    last_data_refresh = now
                
                # Update live chart
                if self.live_chart:
                    self.live_chart.update_data(self.loader.df, self.zones, current_price)
                
                await asyncio.sleep(update_interval)
                
        except asyncio.CancelledError:
            print(f"[{self.symbol}] ⏹️ Trading stopped")
        except Exception as e:
            print(f"[{self.symbol}] ❌ Error in trading loop: {e}")
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
        
        # Detect breakout
        direction = self.breakout_detector.detect_breakout(
            zone, latest_candle, current_time
        )
        
        if direction:
            await self._handle_breakout(zone, direction)
    
    async def _handle_breakout(self, zone: dict, direction: str):
        """Handle detected breakout - open position"""
        zone_id = zone.get('zone_id', -1)
        zone_high = float(zone['high'])
        zone_low = float(zone['low'])
        
        print(f"[{self.symbol}] 🚨 BREAKOUT DETECTED! Zone {zone_id} | {direction}")
        print(f"   Zone: ${zone_low:.2f} - ${zone_high:.2f}")
        
        # Mark as traded
        self.breakout_detector.mark_zone_traded(zone_id)
        
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
            print(f"[{self.symbol}] ❌ minNotional not satisfied")
            return
        
        position_qty = float(rv["qty"])
        
        # Validate margin
        leverage = self.args.leverage if self.args.leverage else 15
        has_margin = await self.position_manager.validate_margin(
            entry_price, position_qty, leverage
        )
        
        if not has_margin:
            print(f"[{self.symbol}] ⚠️ Insufficient margin, skipping")
            return
        
        # Open position
        success = await self.position_manager.open_position(
            direction, entry_price, position_qty,
            stop_loss, take_profit, zone_id
        )
        
        if not success:
            return
        
        # Update chart
        if self.live_chart:
            try:
                entry_time = pd.Timestamp(datetime.now(pytz.UTC))
                self.live_chart.add_entry_point(
                    entry_time, entry_price, direction,
                    zone_id, stop_loss, take_profit
                )
            except Exception as e:
                print(f"[{self.symbol}] ⚠️ Failed to update chart: {e}")
        
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
        print(f"[{self.symbol}] 🔄 Trailing stop started")
    
    async def _refresh_data(self):
        """Refresh live data and recompute zones"""
        live_limit = estimate_candles_needed(
            self.args.interval, self.args.lookback_days
        )
        
        live_df, updated = await asyncio.to_thread(
            self.loader.refresh_live_data, live_limit
        )
        
        if updated or not self.zones:
            self.zones = await self._compute_zones()
            
            # Cleanup traded zones
            available_ids = {z.get("zone_id", i) for i, z in enumerate(self.zones)}
            self.breakout_detector.cleanup_old_zones(available_ids)
    
    async def cleanup(self):
        """Cleanup resources"""
        print(f"[{self.symbol}] Cleaning up...")
        
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
                print(f"[{self.symbol}] ⚠️ Error stopping chart: {e}")
        
        print(f"[{self.symbol}] ✅ Cleanup complete")
    
    def stop(self):
        """Stop trading"""
        self._running = False

