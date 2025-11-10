import argparse
import warnings

import numpy as np

from src.config.settings import DEFAULT_SYMBOL, DEFAULT_INTERVAL
from src.data.binance_data import BinanceDataLoader
from src.backtest.engine import BacktestEngine
from src.plotting.plotter import plot_zone_with_trade

warnings.filterwarnings('ignore')


def main():
	parser = argparse.ArgumentParser()
	parser.add_argument("--symbol", default=DEFAULT_SYMBOL)
	parser.add_argument("--interval", default=DEFAULT_INTERVAL)
	parser.add_argument("--lookback_days", type=int, default=360)
	parser.add_argument("--capital", type=float, default=10000.0)
	parser.add_argument("--show_plots", type=int, default=1)
	args = parser.parse_args()

	print(f"[Backtest] symbol={args.symbol} interval={args.interval} lookback_days={args.lookback_days}")
	loader = BinanceDataLoader(args.symbol, args.interval, args.lookback_days)
	df = loader.load()
	if df is None:
		df = loader.fetch_with_simple_pagination()
	print(f"[Data] candles={len(df)} range=({df.index.min()} -> {df.index.max()})")

	engine = BacktestEngine(df, capital=args.capital)
	df_ind, zones = engine.get_all_zones()
	print(f"[Zones] detected={len(zones)}")
	print(f"[Config] min_accumulation_score={engine.params.get('min_accumulation_score', 3)}")
	if not zones:
		print("No zones detected. Try loosening thresholds or increasing lookback.")
		return

	trades = engine.simulate_all()
	print(f"[Trades] simulated={len(trades)}")
	if not trades:
		print("No trades detected.")
		return
	stats = engine.summarize(trades)
	enh = engine.enhanced_statistics(trades, initial_capital=args.capital)
	print(f"[Stats] {stats}")
	print(f"[Enhanced] {enh}")

	# Plot a few examples
	plot_zones = zones if len(zones) <= args.show_plots else [zones[0], zones[-1]]
	if len(zones) > 2 and len(plot_zones) < args.show_plots:
		middle_count = args.show_plots - len(plot_zones)
		indices = np.random.choice(range(1, len(zones) - 1), middle_count, replace=False)
		for i in indices:
			plot_zones.append(zones[i])
	for z in plot_zones:
		trade = engine.backtest_zone(z)
		if trade:
			plot_zone_with_trade(df, z, trade)


if __name__ == "__main__":
	main()
