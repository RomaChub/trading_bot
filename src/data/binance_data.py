import os
import time
import pickle
from datetime import datetime, timedelta
from typing import Optional, Tuple

import pandas as pd
from binance.client import Client
from requests.exceptions import ConnectionError

from src.config.settings import DATA_CACHE_DIR


class BinanceDataLoader:
	def __init__(self, symbol: str, interval: str, lookback_days: int, api_key: str = "", api_secret: str = ""):
		self.symbol = symbol
		self.interval = interval
		self.lookback_days = lookback_days
		self.client = Client(api_key, api_secret)
		self.df: Optional[pd.DataFrame] = None

	@property
	def cache_path(self) -> str:
		return os.path.join(DATA_CACHE_DIR, f"{self.symbol}_{self.interval}_data.pkl")

	def save(self) -> None:
		if self.df is None:
			return
		with open(self.cache_path, "wb") as f:
			pickle.dump(self.df, f)
		print(f"💾 Saved data to {self.cache_path} ({len(self.df)} candles)")

	def load(self) -> Optional[pd.DataFrame]:
		if os.path.exists(self.cache_path):
			with open(self.cache_path, "rb") as f:
				self.df = pickle.load(f)
			self.df = self._ensure_utc_index(self.df)
			print(f"📂 Loaded cached data: {len(self.df)} candles from {self.df.index.min()} to {self.df.index.max()}")
			return self.df
		print("❌ No cache found, will fetch fresh data...")
		return None

	def fetch_with_simple_pagination(self, pause_sec: float = 0.2) -> pd.DataFrame:
		print(f"⬇️ Fetching klines for {self.symbol} {self.interval} over {self.lookback_days} days...")
		all_klines = []
		end_date = datetime.now()
		start_date = end_date - timedelta(days=self.lookback_days)
		current_start = start_date
		chunk_days = 30

		while current_start < end_date:
			current_end = min(current_start + timedelta(days=chunk_days), end_date)
			print(f"  Chunk: {current_start:%Y-%m-%d %H:%M} -> {current_end:%Y-%m-%d %H:%M}")
			try:
				klines = self.client.get_historical_klines(
					self.symbol,
					self.interval,
					current_start.strftime("%d %b, %Y %H:%M:%S"),
					current_end.strftime("%d %b, %Y %H:%M:%S"),
				)
				print(f"    received: {len(klines) if klines else 0}")
				if klines:
					all_klines.extend(klines)
				time.sleep(pause_sec)
			except Exception as e:
				print(f"    error: {e}")
				current_start = current_end
				continue
			current_start = current_end

		if not all_klines:
			raise RuntimeError("No klines were fetched. Check network/symbol/interval.")

		columns = [
			"timestamp",
			"open",
			"high",
			"low",
			"close",
			"volume",
			"close_time",
			"quote_asset_volume",
			"number_of_trades",
			"taker_buy_base_asset_volume",
			"taker_buy_quote_asset_volume",
			"ignore",
		]
		df = pd.DataFrame(all_klines, columns=columns)
		for col in ["open", "high", "low", "close", "volume"]:
			df[col] = pd.to_numeric(df[col])
		df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
		df.set_index("timestamp", inplace=True)
		df = df[~df.index.duplicated(keep="first")].sort_index()
		self.df = self._ensure_utc_index(df)
		print(f"✅ Fetched {len(self.df)} candles: {df.index.min()} -> {df.index.max()}")
		self.save()
		return df

	def _klines_to_dataframe(self, klines) -> pd.DataFrame:
		columns = [
			"open_time",
			"open",
			"high",
			"low",
			"close",
			"volume",
			"close_time",
			"quote_volume",
			"trades",
			"taker_buy_base",
			"taker_buy_quote",
			"ignore",
		]
		df = pd.DataFrame(klines, columns=columns)
		if df.empty:
			return df
		df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
		df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
		for col in ["open", "high", "low", "close", "volume", "taker_buy_base", "taker_buy_quote", "quote_volume"]:
			df[col] = pd.to_numeric(df[col], errors="coerce")
		df = df.set_index("open_time")
		df.index.name = "timestamp"
		df = df[~df.index.duplicated(keep="last")].sort_index()
		return self._ensure_utc_index(df)

	def _trim_to_lookback(self, df: pd.DataFrame) -> pd.DataFrame:
		if df is None or df.empty:
			return df
		now_utc = pd.Timestamp.utcnow()
		if now_utc.tzinfo is None:
			now_utc = now_utc.tz_localize("UTC")
		else:
			now_utc = now_utc.tz_convert("UTC")
		cutoff = now_utc - pd.Timedelta(days=self.lookback_days)
		return df[df.index >= cutoff]

	def _ensure_utc_index(self, df: pd.DataFrame) -> pd.DataFrame:
		if df is None or df.empty:
			return df
		if df.index.tz is None:
			df.index = df.index.tz_localize("UTC")
		else:
			df.index = df.index.tz_convert("UTC")
		return df

	def refresh_live_data(self, limit: int = 1500) -> Tuple[pd.DataFrame, bool]:
		"""Fetch latest futures klines and keep dataframe up to lookback window."""
		for attempt in range(3):
			try:
				klines = self.client.futures_klines(symbol=self.symbol, interval=self.interval, limit=limit)
				break
			except (ConnectionError, Exception) as e:
				if attempt < 2:
					wait_time = (attempt + 1) * 2
					print(f"[LIVE DATA] ⚠️ Error fetching klines (attempt {attempt + 1}/3): {e}")
					print(f"[LIVE DATA] Retrying in {wait_time} seconds...")
					import time
					time.sleep(wait_time)
				else:
					print(f"[LIVE DATA] ❌ Failed to fetch latest klines after 3 attempts: {e}")
					return self.df if self.df is not None else pd.DataFrame(), False
		df_live = self._klines_to_dataframe(klines)
		if df_live.empty:
			return self.df if self.df is not None else pd.DataFrame(), False
		previous_last = None
		if self.df is not None and not self.df.empty:
			previous_last = self.df.index.max()
			combined = pd.concat([self.df, df_live])
			combined = combined[~combined.index.duplicated(keep="last")].sort_index()
		else:
			combined = df_live
		combined = self._trim_to_lookback(combined)
		updated_last = combined.index.max() if not combined.empty else None
		self.df = combined
		updated = updated_last is not None and updated_last != previous_last
		return self.df, updated
