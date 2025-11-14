"""
Refactored live trading script with async support and clean architecture
"""
import argparse
import asyncio
import logging
import warnings
from typing import List

from src.config.settings import DEFAULT_SYMBOL, DEFAULT_INTERVAL
from src.config.params import RISK_MANAGEMENT, ACCUMULATION_PARAMS
from src.execution.binance_client import BinanceFuturesExecutor
from src.notifications.telegram_bot import TelegramNotifier
from src.trading.trader import SymbolTrader

warnings.filterwarnings('ignore')

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


async def trade_symbols(symbols: List[str], args, exec_client,
                       total_balance: float, dry_run: bool,
                       telegram_notifier):
    """Trade multiple symbols concurrently"""
    
    # Create traders for each symbol
    traders = []
    for idx, symbol in enumerate(symbols):
        chart_port = 8050 + idx if args.show_live_chart.lower() == "true" else None
        
        trader = SymbolTrader(
            symbol=symbol,
            args=args,
            exec_client=exec_client,
            total_balance=total_balance,
            dry_run=dry_run,
            chart_port=chart_port,
            telegram_notifier=telegram_notifier
        )
        
        await trader.initialize()
        traders.append(trader)
        
        logger.info(f"✅ Started trading for {symbol}" + 
                   (f" (chart on port {chart_port})" if chart_port else ""))
    
    # Run all traders concurrently
    try:
        await asyncio.gather(*[trader.run() for trader in traders])
    except KeyboardInterrupt:
        logger.info("\n⏹️ Stopping all traders...")
        for trader in traders:
            trader.stop()
        
        # Wait for cleanup
        await asyncio.gather(
            *[trader.cleanup() for trader in traders],
            return_exceptions=True
        )


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(
        description="Multi-symbol futures trading bot with accumulation zone breakouts"
    )
    
    # Symbol configuration
    parser.add_argument(
        "--symbol",
        default=DEFAULT_SYMBOL,
        help="Single symbol (deprecated, use --symbols)"
    )
    parser.add_argument(
        "--symbols",
        default="BTCUSDT,ETHUSDT,BNBUSDT",
        help="Comma-separated list of symbols"
    )
    parser.add_argument("--interval", default=DEFAULT_INTERVAL)
    parser.add_argument(
        "--lookback_days",
        type=int,
        default=5,
        help="Days of historical data"
    )
    
    # Zone configuration
    parser.add_argument(
        "--zone_max_age_hours",
        type=int,
        default=48,
        help="Maximum age of zones to monitor"
    )
    
    # Risk management
    parser.add_argument(
        "--leverage",
        type=int,
        default=15,
        help="Trading leverage"
    )
    parser.add_argument(
        "--risk_per_trade",
        type=float,
        default=RISK_MANAGEMENT["risk_per_trade"],
        help="Risk per trade as fraction of balance"
    )
    
    # Trailing stop configuration
    parser.add_argument(
        "--use_trailing_stop",
        type=str,
        default=str(ACCUMULATION_PARAMS.get("use_trailing_stop", True)).lower(),
        help="Enable trailing stop"
    )
    parser.add_argument(
        "--trailing_mode",
        default=ACCUMULATION_PARAMS.get("trailing_mode", "step"),
        choices=["bar_extremes", "step"],
        help="Trailing stop mode"
    )
    parser.add_argument(
        "--trailing_activate_rr",
        type=float,
        default=ACCUMULATION_PARAMS.get("trailing_activate_rr", 1.0),
        help="Risk/reward ratio to activate trailing"
    )
    parser.add_argument(
        "--trailing_step_pct",
        type=float,
        default=ACCUMULATION_PARAMS.get("trailing_step_pct", 0.5),
        help="Trailing step percentage"
    )
    parser.add_argument(
        "--trailing_buffer_pct",
        type=float,
        default=ACCUMULATION_PARAMS.get("trailing_buffer_pct", 0.0),
        help="Trailing buffer percentage"
    )
    
    # Update intervals
    parser.add_argument(
        "--update_interval",
        type=int,
        default=10,
        help="Seconds between updates"
    )
    parser.add_argument(
        "--data_refresh_interval",
        type=int,
        default=10,
        help="Seconds between data refreshes"
    )
    
    # Misc
    parser.add_argument(
        "--dry_run",
        type=str,
        default="false",
        help="Enable dry run mode"
    )
    parser.add_argument(
        "--show_live_chart",
        type=str,
        default="true",
        help="Show live chart"
    )
    parser.add_argument(
        "--allow_multiple_positions",
        type=str,
        default="true",
        help="Allow multiple positions (currently not used)"
    )
    
    args = parser.parse_args()
    
    # Parse settings
    dry_run = args.dry_run.lower() == "true"
    
    # Initialize clients
    exec_client = BinanceFuturesExecutor(dry_run=dry_run)
    telegram_notifier = TelegramNotifier()
    
    # Determine symbols to trade
    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(',')]
    else:
        symbols = [args.symbol.upper()]
    
    # Print startup info
    logger.info(f"\n{'='*60}")
    logger.info(f"🚀 Multi-Symbol Trading Bot")
    logger.info(f"📊 Symbols: {', '.join(symbols)}")
    logger.info(f"{'='*60}\n")
    
    # Get balance
    total_balance = exec_client.get_available_balance("USDT")
    logger.info(f"[Global] Total balance: ${total_balance:.2f} USDT")
    
    # Check position mode
    current_mode = exec_client.get_position_mode()
    logger.info(f"[Global] Position mode: {current_mode}")
    
    if current_mode == "Hedge":
        logger.warning("⚠️ Hedge Mode detected")
    else:
        logger.info("✅ One-way Mode - optimal")
    
    # Run trading
    try:
        asyncio.run(
            trade_symbols(
                symbols, args, exec_client,
                total_balance, dry_run, telegram_notifier
            )
        )
    except KeyboardInterrupt:
        logger.info("\n⏹️ Shutdown complete")


if __name__ == "__main__":
    main()

