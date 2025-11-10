from typing import List, Dict, Tuple

import numpy as np
import pandas as pd
import ta

from src.config.params import ACCUMULATION_PARAMS


def compute_indicators(df: pd.DataFrame, params: dict = None) -> pd.DataFrame:
	p = params or ACCUMULATION_PARAMS
	df = df.copy()
	
	# Параметры индикаторов (как в предоставленном коде)
	atr_window = p.get("atr_window", 14)
	bb_window = p.get("bb_window", 20)
	bb_dev = p.get("bb_dev", 2.0)
	adx_window = p.get("adx_window", 14)
	accumulation_period = p.get("accumulation_period", 20)
	volume_window = p.get("volume_window", p.get("volume_sma_window", 20))
	
	# Индикаторы (точно как в предоставленном коде)
	df["atr"] = ta.volatility.AverageTrueRange(df["high"], df["low"], df["close"], window=atr_window).average_true_range()
	df["atr_pct"] = df["atr"] / df["close"]
	
	bb = ta.volatility.BollingerBands(df["close"], window=bb_window, window_dev=bb_dev)
	df["bb_width"] = (bb.bollinger_hband() - bb.bollinger_lband()) / df["close"]
	df["bb_position"] = (df["close"] - bb.bollinger_lband()) / (bb.bollinger_hband() - bb.bollinger_lband())
	
	df["adx"] = ta.trend.ADXIndicator(df["high"], df["low"], df["close"], window=adx_window).adx()
	
	df["volume_sma"] = df["volume"].rolling(volume_window).mean()
	df["volume_ratio"] = df["volume"] / df["volume_sma"]
	
	df["high_roll"] = df["high"].rolling(accumulation_period).max()
	df["low_roll"] = df["low"].rolling(accumulation_period).min()
	df["range_pct"] = (df["high_roll"] - df["low_roll"]) / df["close"]
	
	df["obv"] = ta.volume.OnBalanceVolumeIndicator(df["close"], df["volume"]).on_balance_volume()
	df["obv_trend"] = df["obv"].diff(accumulation_period) > 0
	
	return df


def detect_accumulation_zones(df: pd.DataFrame, params: dict = None) -> Tuple[pd.DataFrame, pd.DataFrame]:
	p = params or ACCUMULATION_PARAMS
	df = compute_indicators(df, p)
	
	# Условия для накопления с порогами (точно как в предоставленном коде)
	atr_threshold = p.get("atr_threshold", 0.2)
	bb_width_threshold = p.get("bb_width_threshold", 0.2)
	adx_threshold = p.get("adx_threshold", 25)
	bb_position_low = p.get("bb_position_low", 0.3)
	bb_position_high = p.get("bb_position_high", 0.7)
	range_threshold = p.get("range_threshold", 0.3)
	
	# Условия (точно как в предоставленном коде)
	conditions = []
	conditions.append(df["atr_pct"] < df["atr_pct"].quantile(atr_threshold))
	conditions.append(df["bb_width"] < df["bb_width"].quantile(bb_width_threshold))
	conditions.append(df["adx"] < adx_threshold)
	conditions.append((df["bb_position"] > bb_position_low) & (df["bb_position"] < bb_position_high))
	conditions.append(df["range_pct"] < df["range_pct"].quantile(range_threshold))
	conditions.append(df["obv_trend"] == True)
	
	df["accumulation_score"] = sum(conditions)
	
	min_score = p.get("min_accumulation_score", 4)
	accumulation_mask = df["accumulation_score"] >= min_score
	accumulation_zones = df[accumulation_mask].copy()
	
	if accumulation_zones.empty:
		return df, accumulation_zones
	
	accumulation_zones["time_diff"] = accumulation_zones.index.to_series().diff() > pd.Timedelta(minutes=30)
	accumulation_zones["zone_group"] = accumulation_zones["time_diff"].cumsum()
	
	return df, accumulation_zones


def group_zones(df: pd.DataFrame, acc_zones: pd.DataFrame, params: dict = None) -> List[Dict]:
	p = params or ACCUMULATION_PARAMS
	if acc_zones.empty:
		return []
	
	min_zone_size = p.get("min_zone_size", 5)
	all_zones = []
	
	zone_groups = acc_zones.groupby("zone_group")
	
	for zone_id, zone_data in zone_groups:
		# Проверка минимального размера зоны (как в предоставленном коде)
		if len(zone_data) < min_zone_size:
			continue
		
		# Формат зоны (как в предоставленном коде - БЕЗ исключения пробоев)
		# Исключение пробоев было добавлено позже и может быть слишком агрессивным
		# Вернемся к оригинальной логике из предоставленного кода
		zone_start = zone_data.index[0]
		zone_end = zone_data.index[-1]
		
		zone_high_final = float(zone_data["high"].max())
		zone_low_final = float(zone_data["low"].min())
		score_avg_final = float(zone_data["accumulation_score"].mean())
		
		all_zones.append({
			"zone_id": int(zone_id),
			"start": zone_start,
			"end": zone_end,
			"size": len(zone_data),
			"score_avg": score_avg_final,
			"high": zone_high_final,
			"low": zone_low_final,
			"data": zone_data,
		})
	return all_zones
