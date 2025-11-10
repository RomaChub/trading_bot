ACCUMULATION_PARAMS = {
	"atr_window": 10,
	"bb_window": 15,
	"bb_dev": 2.0,
	"adx_window": 30,
	"accumulation_period": 25,
	"min_zone_size": 3,
	"min_accumulation_score": 3,  # Minimum score to enter trade (filter zones with score < this value)
	"atr_threshold": 0.3,  # quantile for atr_pct
	"bb_width_threshold": 0.3,  # quantile for bb_width
	"adx_threshold": 30,
	"bb_position_low": 0.3,
	"bb_position_high": 0.7,
	"range_threshold": 0.3,  # quantile for range_pct
	"sl_method": "low",
	"rr_ratio": 4,
	"lookback_bars_for_tp": 1000,
	"volume_sma_window": 20,
	"volume_window": 20,
	# Breakeven settings (disabled)
	"use_breakeven": False,
	"breakeven_rr": 1.0,
	# Trailing stop settings
	"use_trailing_stop": True,
	"trailing_activate_rr": 1.0,
	"trailing_mode": "step",  # step | bar_extremes | none
	"trailing_step_pct": 0.01,   # move stop every +0.5% step beyond entry
	"trailing_buffer_pct": 0,
}

RISK_MANAGEMENT = {
	"risk_per_trade": 0.03
}
