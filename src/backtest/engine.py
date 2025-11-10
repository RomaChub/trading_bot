from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from src.config.params import ACCUMULATION_PARAMS, RISK_MANAGEMENT
from src.indicators.accumulation import detect_accumulation_zones, group_zones


class BacktestEngine:
	def __init__(self, df: pd.DataFrame, capital: float = 10000.0, params: dict = None):
		self.df = df
		self.capital = capital
		self.params = params or ACCUMULATION_PARAMS
		self.risk_per_trade = RISK_MANAGEMENT["risk_per_trade"]

	def get_all_zones(self) -> Tuple[pd.DataFrame, List[Dict]]:
		df_with_ind, acc_zones = detect_accumulation_zones(self.df, self.params)
		all_zones = group_zones(df_with_ind, acc_zones, self.params)
		return df_with_ind, all_zones

	def _calc_levels(self, zone: Dict) -> Optional[Dict]:
		p = self.params
		zone_start = zone["start"]
		zone_end = zone["end"]
		zone_high = zone["high"]
		zone_low = zone["low"]
		zone_mid = (zone_high + zone_low) / 2.0
		# Ищем пробой с закреплением: проверяем последнюю свечу зоны и все свечи после зоны
		# Это нужно, потому что пробойная свеча может быть включена в зону, если она соответствует условиям накопления
		
		# Сначала проверяем последнюю свечу зоны на пробой
		last_zone_candle = self.df[self.df.index == zone_end]
		if not last_zone_candle.empty:
			last_row = last_zone_candle.iloc[0]
			last_high = float(last_row["high"])
			last_low = float(last_row["low"])
			last_close = float(last_row["close"])
			
			# Проверяем, не пробивает ли последняя свеча зоны границы зоны
			# Если high последней свечи выше zone_high И close выше zone_high - это пробой LONG
			if last_high > zone_high and last_close > zone_high:
				breakout_candle = last_row
				direction = "LONG"
				entry_price = float(last_close)
				entry_time = last_row.name
			# Если low последней свечи ниже zone_low И close ниже zone_low - это пробой SHORT
			elif last_low < zone_low and last_close < zone_low:
				breakout_candle = last_row
				direction = "SHORT"
				entry_price = float(last_close)
				entry_time = last_row.name
			else:
				breakout_candle = None
				direction = None
		else:
			breakout_candle = None
			direction = None
		
		# Если пробой не найден в последней свече зоны, проверяем свечи после зоны
		if breakout_candle is None:
			after = self.df[self.df.index > zone_end]
			if after.empty:
				return None
			
			# Проверяем все свечи после зоны до конца данных
			# Логика пробоя как в лайве: ищем пробой с закреплением (high/low пробивает И close за пределами зоны)
			for _, row in after.iterrows():
				candle_high = float(row["high"])
				candle_low = float(row["low"])
				candle_close = float(row["close"])
				
				# Для LONG: high пробивает зону И close закрывается выше зоны (закрепление пробоя)
				if candle_high > zone_high and candle_close > zone_high:
					breakout_candle = row
					direction = "LONG"
					entry_price = float(candle_close)
					entry_time = row.name
					break
				# Для SHORT: low пробивает зону И close закрывается ниже зоны (закрепление пробоя)
				elif candle_low < zone_low and candle_close < zone_low:
					breakout_candle = row
					direction = "SHORT"
					entry_price = float(candle_close)
					entry_time = row.name
					break
			
			if breakout_candle is None:
				return None

		# Stop-loss method
		if p.get("sl_method", "mid") == "low":
			stop_loss = float(zone_low) if direction == "LONG" else float(zone_high)
		else:
			stop_loss = float(zone_mid)

		# Determine TP using nearest prior extreme satisfying min RR
		risk = abs(entry_price - stop_loss)
		rr_min = float(p.get("rr_ratio", 2.0))
		data_before = self.df[self.df.index < zone_start].tail(p.get("lookback_bars_for_tp", 50))
		if direction == "LONG":
			threshold_tp = entry_price + rr_min * risk
			candidate_highs = []
			if len(data_before) > 0:
				prior_highs = data_before["high"].values.tolist()
				candidate_highs = [float(h) for h in prior_highs if h >= threshold_tp]
			if candidate_highs:
				take_profit = min(candidate_highs)
			else:
				take_profit = threshold_tp
		else:
			threshold_tp = entry_price - rr_min * risk
			candidate_lows = []
			if len(data_before) > 0:
				prior_lows = data_before["low"].values.tolist()
				candidate_lows = [float(l) for l in prior_lows if l <= threshold_tp]
			if candidate_lows:
				take_profit = max(candidate_lows)
			else:
				take_profit = threshold_tp

		risk_amount = self.capital * float(self.risk_per_trade)
		risk_price = abs(entry_price - stop_loss)
		position_size = risk_amount / risk_price if risk_price > 0 else 0.0
		reward_price = abs(take_profit - entry_price)
		rr_ratio = reward_price / risk_price if risk_price > 0 else 0.0
		return {
			"direction": direction,
			"entry_price": entry_price,
			"entry_time": entry_time,
			"stop_loss": float(stop_loss),
			"take_profit": float(take_profit),
			"position_size": float(position_size),
			"risk_amount": float(risk_amount),
			"risk_price": float(risk_price),
			"reward_price": float(reward_price),
			"rr_ratio": float(rr_ratio),
			"breakout_candle_time": breakout_candle.name,
			"breakout_candle_close": float(breakout_candle["close"]),
			"zone_id": int(zone["zone_id"]),
		}

	def backtest_zone(self, zone: Dict) -> Optional[Dict]:
		trade = self._calc_levels(zone)
		if not trade:
			return None
		entry_time = trade["entry_time"]
		# Позиция открывается на close пробойной свечи
		entry_idx = self.df.index.get_loc(entry_time)
		# Начинаем проверку выхода (TP/SL) со следующей свечи после пробоя
		# (нельзя выйти из позиции в ту же свечу, в которую вошли)
		if entry_idx + 1 >= len(self.df):
			return None
		future = self.df.iloc[entry_idx + 1:]
		result = None
		exit_time = None
		exit_price = None
		exit_reason = None
		# Trailing settings
		use_trail = bool(self.params.get("use_trailing_stop", False))
		trail_rr = float(self.params.get("trailing_activate_rr", 1.0))
		trail_mode = self.params.get("trailing_mode", "bar_extremes")
		trail_buf_pct = float(self.params.get("trailing_buffer_pct", 0.0))
		trail_step_pct = float(self.params.get("trailing_step_pct", 0.5))
		current_stop = trade["stop_loss"]
		original_stop = current_stop
		risk = abs(trade["entry_price"] - trade["stop_loss"]) if use_trail else 0.0
		if trade["direction"] == "LONG":
			trail_threshold = trade["entry_price"] + trail_rr * risk
		else:
			trail_threshold = trade["entry_price"] - trail_rr * risk
		trailing_active = False
		last_step_applied = 0
		for idx, row in future.iterrows():
			# Activate trailing once RR threshold reached
			if use_trail and not trailing_active:
				if trade["direction"] == "LONG" and row["high"] >= trail_threshold:
					trailing_active = True
				elif trade["direction"] == "SHORT" and row["low"] <= trail_threshold:
					trailing_active = True
			# Update trailing stop if active
			if trailing_active:
				if trail_mode == "bar_extremes":
					if trade["direction"] == "LONG":
						buffer = row["low"] * trail_buf_pct / 100.0
						new_stop = float(row["low"]) - buffer
						current_stop = max(current_stop, new_stop)
					else:
						buffer = row["high"] * trail_buf_pct / 100.0
						new_stop = float(row["high"]) + buffer
						current_stop = min(current_stop, new_stop)
				elif trail_mode == "step" and trail_step_pct > 0:
					step_amount = trade["entry_price"] * (trail_step_pct / 100.0)
					if trade["direction"] == "LONG":
						progress = (row["high"] - trade["entry_price"]) / step_amount
						steps = int(progress) if progress > 0 else 0
						if steps > last_step_applied:
							target_stop = trade["entry_price"] + steps * step_amount
							buffer = target_stop * trail_buf_pct / 100.0
							current_stop = max(current_stop, target_stop - buffer)
							last_step_applied = steps
					else:
						progress = (trade["entry_price"] - row["low"]) / step_amount
						steps = int(progress) if progress > 0 else 0
						if steps > last_step_applied:
							target_stop = trade["entry_price"] - steps * step_amount
							buffer = abs(target_stop) * trail_buf_pct / 100.0
							current_stop = min(current_stop, target_stop + buffer)
							last_step_applied = steps
			# Evaluate exits
			if trade["direction"] == "LONG":
				if row["high"] >= trade["take_profit"]:
					result = "WIN"
					exit_time = idx
					exit_price = trade["take_profit"]
					exit_reason = "TP"
					break
				elif row["low"] <= current_stop:
					result = "LOSS"
					exit_time = idx
					exit_price = current_stop
					exit_reason = "TRAIL" if (use_trail and (current_stop != original_stop)) else "SL"
					break
			else:
				if row["low"] <= trade["take_profit"]:
					result = "WIN"
					exit_time = idx
					exit_price = trade["take_profit"]
					exit_reason = "TP"
					break
				elif row["high"] >= current_stop:
					result = "LOSS"
					exit_time = idx
					exit_price = current_stop
					exit_reason = "TRAIL" if (use_trail and (current_stop != original_stop)) else "SL"
					break
		if result is None:
			result = "OPEN"
			exit_time = future.index[-1]
			exit_price = float(future["close"].iloc[-1])
		if trade["direction"] == "LONG":
			pnl_pct = (exit_price - trade["entry_price"]) / trade["entry_price"] * 100.0
			pnl_usd = (exit_price - trade["entry_price"]) * trade["position_size"]
		else:
			pnl_pct = (trade["entry_price"] - exit_price) / trade["entry_price"] * 100.0
			pnl_usd = (trade["entry_price"] - exit_price) * trade["position_size"]
		trade.update({
			"result": result,
			"exit_time": exit_time,
			"exit_price": float(exit_price),
			"exit_reason": exit_reason,
			"pnl_pct": float(pnl_pct),
			"pnl_usd": float(pnl_usd),
			"duration": (exit_time - entry_time).total_seconds() / 60.0,
			"zone_score": float(zone.get("score_avg", 0.0)),
		})
		return trade

	def simulate_all(self) -> List[Dict]:
		_, zones = self.get_all_zones()
		trades: List[Dict] = []
		stats = {
			"total_zones": len(zones),
			"no_next_candle": 0,
			"successful_trades": 0
		}
		
		for z in zones:
			res = self.backtest_zone(z)
			if res:
				trades.append(res)
				stats["successful_trades"] += 1
			else:
				# Проверяем, почему зона не обработана
				zone_end = z["end"]
				after = self.df[self.df.index > zone_end]
				if after.empty:
					stats["no_next_candle"] += 1
		
		print(f"[Backtest Stats] Total zones: {stats['total_zones']}")
		print(f"  - Successful trades: {stats['successful_trades']}")
		print(f"  - No next candle (zones at end of data): {stats['no_next_candle']}")
		print(f"  - Zones without breakout: {stats['total_zones'] - stats['successful_trades'] - stats['no_next_candle']}")
		
		return trades

	@staticmethod
	def summarize(trades: List[Dict]) -> Dict:
		if not trades:
			return {"total_trades": 0}
		wins = [t for t in trades if t["result"] == "WIN"]
		losses = [t for t in trades if t["result"] == "LOSS"]
		breakeven = [t for t in trades if t["result"] == "BREAKEVEN"]
		trail_exits = [t for t in trades if t.get("exit_reason") == "TRAIL"]
		open_trades = [t for t in trades if t["result"] == "OPEN"]
		total_pnl = sum(t["pnl_usd"] for t in trades)
		avg_trade_duration_min = float(np.mean([t["duration"] for t in trades])) if trades else 0.0
		avg_score = float(np.mean([t.get("zone_score", 0.0) for t in trades])) if trades else 0.0
		min_score = float(np.min([t.get("zone_score", 0.0) for t in trades])) if trades else 0.0
		max_score = float(np.max([t.get("zone_score", 0.0) for t in trades])) if trades else 0.0
		return {
			"total_trades": len(trades),
			"wins": len(wins),
			"losses": len(losses),
			"breakeven": len(breakeven),
			"trailing_exits": len(trail_exits),
			"open": len(open_trades),
			"win_rate": (len(wins) / len(trades) * 100.0) if trades else 0.0,
			"total_pnl": total_pnl,
			"avg_pnl": (total_pnl / len(trades)) if trades else 0.0,
			"avg_duration_min": avg_trade_duration_min,
			"avg_score": avg_score,
			"min_score": min_score,
			"max_score": max_score,
		}

	@staticmethod
	def enhanced_statistics(trades: List[Dict], initial_capital: float) -> Dict:
		if not trades:
			return {}
		# Equity curve
		pnl_series = np.array([t["pnl_usd"] for t in trades], dtype=float)
		ret_series = np.array([t["pnl_pct"] for t in trades], dtype=float) / 100.0
		equity = initial_capital + np.cumsum(pnl_series)
		running_max = np.maximum.accumulate(equity)
		drawdowns = (equity - running_max)
		max_dd_abs = float(np.min(drawdowns)) if len(drawdowns) else 0.0
		max_dd_pct = float((max_dd_abs / running_max[np.argmin(drawdowns)]) * 100.0) if len(drawdowns) and running_max[np.argmin(drawdowns)] != 0 else 0.0
		# Sharpe (per-trade)
		mu = float(np.mean(ret_series)) if len(ret_series) else 0.0
		sigma = float(np.std(ret_series, ddof=1)) if len(ret_series) > 1 else 0.0
		sharpe_per_trade = (mu / sigma) if sigma > 0 else 0.0
		# Expectancy and profit factor
		wins = [t for t in trades if t["result"] == "WIN"]
		losses = [t for t in trades if t["result"] == "LOSS"]
		avg_win = float(np.mean([t["pnl_usd"] for t in wins])) if wins else 0.0
		avg_loss = float(np.mean([t["pnl_usd"] for t in losses])) if losses else 0.0
		total_win = float(np.sum([t["pnl_usd"] for t in wins])) if wins else 0.0
		total_loss = float(np.sum([abs(t["pnl_usd"]) for t in losses])) if losses else 0.0
		profit_factor = (total_win / total_loss) if total_loss > 0 else float("inf") if total_win > 0 else 0.0
		p_win = len(wins) / len(trades) if trades else 0.0
		p_loss = 1 - p_win
		expectancy = p_win * avg_win + p_loss * avg_loss  # avg_loss is negative
		# Consecutive sequences
		seq = [1 if t["result"] == "WIN" else 0 if t["result"] == "LOSS" else None for t in trades]
		max_cons_w, max_cons_l = 0, 0
		cur_w, cur_l = 0, 0
		for s in seq:
			if s == 1:
				cur_w += 1
				cur_l = 0
				max_cons_w = max(max_cons_w, cur_w)
			elif s == 0:
				cur_l += 1
				cur_w = 0
				max_cons_l = max(max_cons_l, cur_l)
		return {
			"equity_end": float(equity[-1]),
			"max_drawdown_usd": float(abs(max_dd_abs)),
			"max_drawdown_pct": float(abs(max_dd_pct)),
			"sharpe_per_trade": sharpe_per_trade,
			"expectancy_usd": expectancy,
			"profit_factor": profit_factor,
			"avg_win_usd": avg_win,
			"avg_loss_usd": avg_loss,
			"max_consecutive_wins": int(max_cons_w),
			"max_consecutive_losses": int(max_cons_l),
		}
