#!/bin/bash
# Start the OWN-LIKE-BOT Telegram bot
cd "$(dirname "$0")/bot"
echo "Starting FF Like Telegram Bot..."
echo "IMPORTANT: Edit bot/app.py first to set API_URL, BOT_TOKEN, and ADMIN_IDS"
python3 app.py