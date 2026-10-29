#!/bin/bash
cd /root/OWN-LIKE-BOT/api
python3 app.py > /tmp/api.log 2>&1 &
echo "API PID: $!"

cd /root/OWN-LIKE-BOT/bot
BOT_TOKEN="8640349698:AAHFP8dgzMB4GmAry9F1uATS-No2lD8PCzk" ADMIN_IDS="7176002628" API_URL="http://127.0.0.1:5000" python3 app.py > /tmp/bot.log 2>&1 &
echo "Bot PID: $!"
echo "Both services starting..."
sleep 2
cat /tmp/api.log | head -5
cat /tmp/bot.log | head -5
