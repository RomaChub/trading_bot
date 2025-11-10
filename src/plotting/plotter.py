from typing import Dict

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd


def plot_zone_with_trade(df: pd.DataFrame, zone: Dict, trade: Dict, window_hours: int = 8) -> None:
	zone_start = zone["start"]
	zone_end = zone["end"]
	start_window = zone_start - pd.Timedelta(hours=window_hours // 2)
	end_window = max(zone_end + pd.Timedelta(hours=window_hours // 2), trade["entry_time"] + pd.Timedelta(hours=2))
	plot_data = df.loc[start_window:end_window]
	if len(plot_data) < 10:
		return
	fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(15, 10), gridspec_kw={"height_ratios": [3, 1]})
	date_format = mdates.DateFormatter('%m-%d %H:%M')
	ax1.xaxis.set_major_formatter(date_format)
	ax2.xaxis.set_major_formatter(date_format)
	width = 0.0004 * window_hours
	for idx, row in plot_data.iterrows():
		open_price = row["open"]
		high_price = row["high"]
		low_price = row["low"]
		close_price = row["close"]
		volume = row["volume"]
		color = 'green' if close_price >= open_price else 'red'
		body_bottom = min(open_price, close_price)
		body_top = max(open_price, close_price)
		body_height = body_top - body_bottom
		if body_height > 0:
			rect = plt.Rectangle((mdates.date2num(idx) - width / 2, body_bottom), width, body_height, facecolor=color, alpha=0.7, edgecolor='black')
			ax1.add_patch(rect)
		ax1.plot([mdates.date2num(idx), mdates.date2num(idx)], [low_price, high_price], color='black', linewidth=0.5)
		ax2.bar(mdates.date2num(idx), volume, width=width * 0.8, color=color, alpha=0.7)
	ax1.axvspan(zone_start, zone_end, alpha=0.3, color='yellow', label='Accumulation Zone')
	ax1.axhline(y=zone["high"], color='red', linestyle='--', linewidth=1, label=f'Zone High: {zone["high"]:.2f}')
	ax1.axhline(y=zone["low"], color='blue', linestyle='--', linewidth=1, label=f'Zone Low: {zone["low"]:.2f}')
	ax1.axvline(x=trade["entry_time"], color='purple', linestyle='-', linewidth=2, label='Entry')
	ax1.scatter(trade["entry_time"], trade["entry_price"], color='purple', s=100, zorder=5)
	ax1.axhline(y=trade["stop_loss"], color='red', linestyle='-', linewidth=2, label=f'Stop: {trade["stop_loss"]:.2f}')
	ax1.axhline(y=trade["take_profit"], color='green', linestyle='-', linewidth=2, label=f'TP: {trade["take_profit"]:.2f}')
	ax1.axvline(x=trade["breakout_candle_time"], color='orange', linestyle='--', linewidth=1, label='Breakout')
	if trade["direction"] == 'LONG':
		ax1.axhspan(trade["entry_price"], trade["take_profit"], alpha=0.1, color='green')
		ax1.axhspan(trade["stop_loss"], trade["entry_price"], alpha=0.1, color='red')
	else:
		ax1.axhspan(trade["take_profit"], trade["entry_price"], alpha=0.1, color='green')
		ax1.axhspan(trade["entry_price"], trade["stop_loss"], alpha=0.1, color='red')
	ax1.set_title(f'Accumulation Zone (Score: {zone["score_avg"]:.2f})')
	ax1.set_ylabel('Price')
	ax1.legend()
	ax1.grid(True, alpha=0.3)
	ax2.set_ylabel('Volume')
	ax2.set_xlabel('Time')
	ax2.grid(True, alpha=0.3)
	fig.autofmt_xdate()
	plt.tight_layout()
	plt.show()
