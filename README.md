# OWN-LIKE-BOT

Free Fire Like Bot system — Flask API backend + Telegram bot frontend.

## Project Structure

```
OWN-LIKE-BOT/
├── api/                     # Flask API backend (sends likes to Garena)
│   ├── app.py               # Main Flask application
│   ├── wsgi.py              # WSGI entry point
│   ├── index.py             # Vercel serverless entry point
│   ├── vercel.json          # Vercel deployment config
│   ├── requirements.txt     # Python dependencies (API)
│   ├── token_bd.json        # BD region Garena tokens (14 tokens)
│   ├── token_ind.json       # IND region Garena tokens (54 tokens)
│   ├── like_pb2.py          # Protobuf: like message
│   ├── uid_generator_pb2.py # Protobuf: UID generator
│   ├── visit_count_pb2.py   # Protobuf: player info response
│   └── README.md            # Original README
├── bot/                     # Telegram bot frontend
│   ├── app.py               # Main Telegram bot
│   └── requirements.txt     # Python dependencies (bot)
├── data/
│   └── guests/
│       └── guest_id.json    # 2 guest accounts (uid + password)
└── README.md                # This file
```

## Quick Start

### 1. Install Dependencies

```bash
# API backend
cd /root/OWN-LIKE-BOT/api
pip3 install --break-system-packages -r requirements.txt

# Telegram bot
cd /root/OWN-LIKE-BOT/bot
pip3 install --break-system-packages -r requirements.txt
```

### 2. Run the API

```bash
cd /root/OWN-LIKE-BOT/api
python3 app.py
```

API endpoints:
- `GET /like?key=ZEXXY&uid=1234567890&region=BD` — Send likes
- `GET /remain` — Check remaining daily likes

### 3. Configure and Run the Telegram Bot

Edit `/root/OWN-LIKE-BOT/bot/app.py`:
- Set `API_URL` to your deployed API endpoint
- Set `BOT_TOKEN` to your Telegram bot token
- Set `ADMIN_IDS` to your Telegram user ID(s)

```bash
cd /root/OWN-LIKE-BOT/bot
python3 app.py
```

## Current Status

### Working ✅
- All Python files compile without errors
- All dependencies install successfully
- Flask API imports and routes register correctly
- `/remain` endpoint works
- API key validation works
- Telegram bot imports and compiles correctly
- Token files are valid JSON (14 BD + 54 IND tokens)

### Blocker ❌
**Garena tokens are expired.** The stored JWT tokens in `token_bd.json` and `token_ind.json` expired approximately 165 days ago. When the `/like` endpoint is called, Garena returns:

```
token is expired by 3974h22m52s
```

Fresh token generation via `MajorLogin` returns `SignError1` — the request reaches Garena's servers but the encryption/signature format doesn't match the current OB55 client version. This requires reverse-engineering Garena's updated login protocol.

**To unblock:** You need fresh, unexpired Garena guest tokens. Options:
1. Obtain tokens from a working Free Fire guest account (login via the game app, extract the JWT)
2. Contact the API owner at TG: @arafat_codex for new tokens
3. Fix the MajorLogin signature for OB55 (requires reverse engineering)

## Files Modified from Original ZIP

| File | Change |
|------|--------|
| `api/app.py` | Fixed relative paths to absolute paths using `BASE_DIR` so token files load regardless of working directory |
| `bot/app.py` | Fixed `ADMIN_IDS` placeholder syntax (was invalid Python, now valid empty list with comment) |

## Original ZIP

Preserved at `/root/OWN-LIKE-BOT.zip` (not modified).
