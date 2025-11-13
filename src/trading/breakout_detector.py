"""Breakout detection logic"""
import asyncio
import logging
from typing import List, Optional, Dict, Tuple
from datetime import datetime, timedelta
import pandas as pd
import pytz

from src.utils.time_utils import ensure_utc

logger = logging.getLogger(__name__)


class BreakoutDetector:
    """Detects breakouts from accumulation zones"""
    
    def __init__(self, symbol: str, exec_client, zone_max_age_hours: int = 48, trader=None):
        self.symbol = symbol
        self.exec_client = exec_client
        self.zone_max_age_hours = zone_max_age_hours
        self.trader = trader  # Reference to trader for current_zone_id
        self.traded_zones = set()  # Keep for backward compatibility
    
    def filter_active_zones(self, zones: List[Dict], current_price: float,
                          current_time: datetime) -> List[Dict]:
        """Filter zones that are currently active"""
        active = []
        max_age = timedelta(hours=self.zone_max_age_hours)
        
        for zone in zones or []:
            try:
                zone_end = ensure_utc(zone.get("end"))
            except Exception:
                continue
            
            # Skip zones that haven't ended yet
            if zone_end >= current_time:
                continue
            
            # Skip zones that are too old
            if current_time - zone_end > max_age:
                continue
            
            # Check if price is in zone
            zone_high = float(zone.get("high", 0.0))
            zone_low = float(zone.get("low", 0.0))
            
            if zone_low <= current_price <= zone_high:
                active.append(zone)
        
        return active
    
    def detect_breakout(self, zone: Dict, latest_candle: pd.Series,
                       current_time: datetime) -> Optional[str]:
        """
        Detect if a breakout occurred
        
        Returns:
            "LONG" for upward breakout, "SHORT" for downward breakout, None otherwise
        """
        zone_high = float(zone['high'])
        zone_low = float(zone['low'])
        zone_end = ensure_utc(zone.get('end'))
        zone_end_ts = pd.Timestamp(zone_end)
        
        candle_high = float(latest_candle['high'])
        candle_low = float(latest_candle['low'])
        candle_close = float(latest_candle['close'])
        candle_close_time = latest_candle['close_time']
        
        # Candle must close after zone ended
        if candle_close_time < zone_end_ts:
            return None
        
        # LONG breakout: low in zone AND close above zone
        if zone_low <= candle_low <= zone_high and candle_close > zone_high:
            return "LONG"
        
        # SHORT breakout: high in zone AND close below zone
        if zone_low <= candle_high <= zone_high and candle_close < zone_low:
            return "SHORT"
        
        return None
    
    def get_newest_untraded_zone(self, zones: List[Dict], 
                                current_time: datetime) -> Optional[Dict]:
        """Get the newest zone that hasn't been traded yet (not currently in use)"""
        max_age = timedelta(hours=self.zone_max_age_hours)
        candidate_zones = []
        
        for zone in zones:
            zone_id = zone.get('zone_id', -1)
            
            # Skip ONLY the zone with active position (allows re-entry after false breakout)
            if self.trader and self.trader.current_zone_id is not None:
                if zone_id == self.trader.current_zone_id:
                    continue
            
            zone_end = ensure_utc(zone.get('end'))
            
            # Skip zones that haven't ended
            if zone_end >= current_time:
                continue
            
            # Skip zones that are too old
            if current_time - zone_end > max_age:
                continue
            
            candidate_zones.append(zone)
        
        if not candidate_zones:
            return None
        
        # Return newest zone (latest end time)
        newest_zone = max(candidate_zones, key=lambda z: ensure_utc(z.get('end')))
        
        # Log if it's different from current active zone
        if self.trader and self.trader.current_zone_id:
            if newest_zone.get('zone_id') != self.trader.current_zone_id:
                logger.debug(f"[{self.symbol}] Мониторинг новой зоны #{newest_zone.get('zone_id')} (активная: #{self.trader.current_zone_id})")
        
        return newest_zone
    
    async def get_recent_klines(self, interval: str, limit: int = 20):
        """Fetch recent klines"""
        try:
            klines = await asyncio.wait_for(
                asyncio.to_thread(
                    self.exec_client.fetch_recent_klines,
                    self.symbol, interval, limit
                ),
                timeout=10.0
            )
            
            if not klines:
                return None
            
            # Convert to DataFrame
            df = pd.DataFrame(klines, columns=[
                'open_time', 'open', 'high', 'low', 'close', 'volume',
                'close_time', 'quote_volume', 'trades', 'taker_buy_base',
                'taker_buy_quote', 'ignore'
            ])
            
            df['open_time'] = pd.to_datetime(df['open_time'], unit='ms', utc=True)
            df['close_time'] = pd.to_datetime(df['close_time'], unit='ms', utc=True)
            df.set_index('open_time', inplace=True)
            df[['open', 'high', 'low', 'close', 'volume']] = \
                df[['open', 'high', 'low', 'close', 'volume']].astype(float)
            
            return df
            
        except asyncio.TimeoutError:
            logger.warning(f"[{self.symbol}] ⚠️ Timeout fetching klines")
            return None
        except Exception as e:
            logger.warning(f"[{self.symbol}] ⚠️ Error fetching klines: {e}")
            return None
    
    def get_closed_candles(self, df: pd.DataFrame, current_time: datetime) -> pd.DataFrame:
        """Filter candles that have closed"""
        return df[df['close_time'] < current_time]
    
    def mark_zone_traded(self, zone_id: int):
        """Mark zone as traded"""
        self.traded_zones.add(zone_id)
    
    def cleanup_old_zones(self, available_zone_ids: set):
        """Remove traded zones that no longer exist"""
        self.traded_zones = {zid for zid in self.traded_zones if zid in available_zone_ids}

