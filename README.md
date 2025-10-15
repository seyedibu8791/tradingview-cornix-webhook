# TradingView to Cornix Webhook

Automated webhook server that captures TradingView strategy signals and formats them for Cornix trading bot on Telegram.

## Features
- ✅ Captures entry and exit signals from TradingView
- ✅ Formats messages in Cornix format
- ✅ Handles pump/dump trailing stop logic accurately
- ✅ Sends alerts to Telegram automatically

## Deployment
This project is configured for easy deployment on Render using the render.yaml file.

## Environment Variables Required
- `TELEGRAM_BOT_TOKEN` - Your Telegram bot token from @BotFather
- `TELEGRAM_CHAT_ID` - Your Telegram chat/group ID

## Endpoints
- `POST /webhook` - Receives TradingView alerts
- `GET /health` - Health check
- `GET /trades` - View active trades
