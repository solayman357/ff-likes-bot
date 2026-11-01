#!/bin/bash
cd /root/OWN-LIKE-BOT/bot
export BOT_TOKEN="8640349698:AAHFP8dgzMB4GmAry9F1uATS-No2lD8PCzk"
export ADMIN_IDS="7176002628"
export API_URL="http://127.0.0.1:5000"
nohup python3 app.py > /tmp/bot.log 2>&1 &
echo "Bot PID: $!"
