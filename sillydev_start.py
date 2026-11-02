#!/usr/bin/env python3
"""
Combined entry point for Silly Development deployment.
Runs the Flask API in a background thread and the Telegram bot in the main thread.
Both share the same container (256 MB RAM, 512 MB disk on SillyDev free tier).
"""

import os
import sys
import threading
import time
import importlib.util

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Add both api and bot directories to sys.path so protobuf imports resolve
sys.path.insert(0, os.path.join(BASE_DIR, "api"))
sys.path.insert(0, os.path.join(BASE_DIR, "bot"))

# --- Load Flask API from api/app.py ---
api_spec = importlib.util.spec_from_file_location(
    "ff_api_app", os.path.join(BASE_DIR, "api", "app.py")
)
api_module = importlib.util.module_from_spec(api_spec)
api_spec.loader.exec_module(api_module)
flask_app = api_module.app


# --- Load bot main from bot/app.py ---
bot_spec = importlib.util.spec_from_file_location(
    "ff_bot_app", os.path.join(BASE_DIR, "bot", "app.py")
)
bot_module = importlib.util.module_from_spec(bot_spec)
bot_spec.loader.exec_module(bot_module)
bot_main_func = bot_module.main


def run_flask():
    """Run the Flask API in a background daemon thread."""
    port = int(os.environ.get("PORT", 5000))
    flask_app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


def main():
    """Start Flask API in background, then run Telegram bot polling in main thread."""
    port = int(os.environ.get("PORT", 5000))

    # Point the bot at the local Flask API (same container)
    os.environ["API_URL"] = f"http://127.0.0.1:{port}"

    # Start Flask API in a daemon thread
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    # Give Flask a moment to bind the port
    time.sleep(2)

    # Run the Telegram bot in the main thread (blocking - long polling)
    bot_main_func()


if __name__ == "__main__":
    main()
