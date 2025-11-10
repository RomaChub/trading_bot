#!/bin/bash
set -e

echo "🚀 Starting trading bot deployment..."

# Check if Python is available
if ! command -v python3 &> /dev/null; then
    echo "❌ Python3 not found. Please install Python 3.11 or higher."
    exit 1
fi

# Display Python version
python3 --version

# Check if required environment variables are set
if [ -z "$BINANCE_API_KEY" ] || [ -z "$BINANCE_API_SECRET" ]; then
    echo "⚠️  Warning: BINANCE_API_KEY or BINANCE_API_SECRET not set"
fi

if [ -z "$TELEGRAM_BOT_TOKEN" ] || [ -z "$TELEGRAM_CHAT_ID" ]; then
    echo "⚠️  Warning: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set"
    echo "   Telegram notifications will be disabled"
fi

# Set default values for optional variables
export DEFAULT_SYMBOLS=${DEFAULT_SYMBOLS:-"BTCUSDT,ETHUSDT,BNBUSDT"}
export DEFAULT_INTERVAL=${DEFAULT_INTERVAL:-"5m"}
export RISK_PER_TRADE=${RISK_PER_TRADE:-"0.03"}
export LOG_LEVEL=${LOG_LEVEL:-"INFO"}

echo "📊 Configuration:"
echo "   Symbols: $DEFAULT_SYMBOLS"
echo "   Interval: $DEFAULT_INTERVAL"
echo "   Risk per trade: $RISK_PER_TRADE"
echo "   Log level: $LOG_LEVEL"

# Install dependencies if requirements.txt exists
if [ -f "requirements.txt" ]; then
    echo "📦 Installing dependencies from requirements.txt..."
    pip install --no-cache-dir -r requirements.txt
else
    echo "⚠️  Warning: requirements.txt not found"
fi

# Start the application
echo "✅ Starting trading bot..."
exec python3 -m src.main_live \
    --symbols "$DEFAULT_SYMBOLS" \
    --interval "$DEFAULT_INTERVAL" \
    --lookback_days 60 \
    --leverage 15 \
    --risk_per_trade "$RISK_PER_TRADE" \
    --allow_multiple_positions false \
    --dry_run false \
    --use_trailing_stop true \
    --trailing_mode step \
    --trailing_activate_rr 1.0 \
    --trailing_step_pct 0.5 \
    --trailing_buffer_pct 0.0 \
    --update_interval 5 \
    --data_refresh_interval 5 \
    --show_live_chart false

