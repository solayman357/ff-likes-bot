#!/bin/bash
# Start the OWN-LIKE-BOT API server
cd "$(dirname "$0")/api"
echo "Starting FF Like API on http://127.0.0.1:5000"
python3 app.py