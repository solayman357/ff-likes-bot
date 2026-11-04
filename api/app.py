from flask import Flask, request, jsonify
import asyncio
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
import binascii
import aiohttp
import requests
import json
import os
import like_pb2
import like_count_pb2
import uid_generator_pb2
import threading
import urllib3
import time
import base64
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

# -------------------------------
# API KEY CONFIGURATION
# -------------------------------
API_KEY = "Solayman"
WRONG_KEY_RESPONSE = {
    "error": "Unauthorized",
    "credits": "@DCFsolayman"
}
# -------------------------------

TOKENS_PER_FILE = 111
LIKES_PER_TOKEN_PER_DAY = 150
LIKES_PER_USER_REQUEST = 300
TOKEN_FILES = ["token_bd_1.json", "token_bd_2.json"]
ACCOUNT_JSON_SOURCES = ["accounts20_part2.json", "sol.lost_account.json"]

# JWT provider endpoints (comma separated in the JWT_API_URLS env var)
JWT_API_URLS = [
    u.strip()
    for u in os.getenv("JWT_API_URLS", "https://kawsarxjwt.lovable.app/api/public/token").split(",")
    if u.strip()
]

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# -------------------------------
# AUTO JWT REFRESH CONFIGURATION
# -------------------------------
JWT_REFRESH_MINUTES = 240  # Refresh tokens every 4 hours
AUTO_REFRESH_ENABLED = True  # Set to False to disable auto refresh

def fetch_jwt_from_api(uid, password, api_url):
    """Fetch fresh JWT token from JWT API"""
    try:
        encoded_pwd = urllib.parse.quote(password, safe='')
        url = f"{api_url}?uid={uid}&password={encoded_pwd}"
        resp = requests.get(url, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            token = None
            if isinstance(data, dict):
                if 'token' in data:
                    token = data['token']
                elif 'access_token' in data:
                    token = data['access_token']
                elif 'data' in data and isinstance(data['data'], dict):
                    if 'token' in data['data']:
                        token = data['data']['token']
            if token:
                return {"token": token}
    except Exception as e:
        print(f"JWT fetch error for {uid}: {e}")
    return None

REFRESH_STATE = {
    "running": False,
    "last_run": None,
    "last_fetched": 0,
    "last_total": 0,
    "last_error": None,
}
REFRESH_LOCK = threading.Lock()


def _load_accounts_for_refresh():
    """All accounts from the txt files + the JSON sources shipped in the repo."""
    accounts = []
    for fname in ["account_bd.txt", "account_ind.txt"]:
        if os.path.exists(fname):
            with open(fname, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    if ':' in line:
                        parts = line.split(':', 1)
                        uid = parts[0].strip()
                        pwd = parts[1].strip()
                        if uid and pwd and uid.isdigit():
                            accounts.append({"uid": uid, "password": pwd})

    for jname in ACCOUNT_JSON_SOURCES:
        if not os.path.exists(jname):
            continue
        try:
            with open(jname, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            print(f"⚠️ {jname} read error: {e}")
            continue
        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                uid = str(item.get("uid", "")).strip()
                pwd = str(item.get("password", "")).strip()
                if uid and pwd and uid.isdigit():
                    accounts.append({"uid": uid, "password": pwd})

    seen = set()
    unique = []
    for a in accounts:
        k = (a["uid"], a["password"])
        if k not in seen:
            seen.add(k)
            unique.append(a)
    return unique


def _account_id_of(token):
    try:
        p = token.split('.')[1]
        p += '=' * (-len(p) % 4)
        return json.loads(base64.urlsafe_b64decode(p)).get('account_id')
    except Exception:
        return None


def merge_tokens_into_bank(new_tokens):
    """Drop expired entries, replace the token of known accounts, add new ones.

    Returns (total, added, refreshed, removed_expired).
    """
    try:
        with open("token_bd_1.json", "r") as f:
            existing = json.load(f)
    except Exception:
        existing = []

    valid = [e for e in existing if isinstance(e, dict) and is_token_fresh(e.get("token"))]
    removed = len(existing) - len(valid)

    seen_tokens = set()
    by_account = {}
    for entry in valid:
        tok = entry.get("token")
        seen_tokens.add(tok)
        aid = _account_id_of(tok)
        if aid is not None:
            by_account[aid] = entry

    added = refreshed = 0
    for entry in new_tokens:
        tok = entry.get("token")
        if not tok or not is_token_fresh(tok) or tok in seen_tokens:
            continue
        aid = _account_id_of(tok)
        if aid is not None and aid in by_account:
            old = by_account[aid]
            seen_tokens.discard(old.get("token"))
            old["token"] = tok
            seen_tokens.add(tok)
            refreshed += 1
        else:
            valid.append(entry)
            seen_tokens.add(tok)
            if aid is not None:
                by_account[aid] = entry
            added += 1

    valid = [e for e in valid if is_token_fresh(e.get("token"))]

    with open("token_bd_1.json", "w") as f:
        json.dump(valid, f, indent=2)

    return len(valid), added, refreshed, removed


def _fetch_one_account(acc):
    for api_url in JWT_API_URLS:
        token_dict = fetch_jwt_from_api(acc["uid"], acc["password"], api_url)
        if token_dict:
            return token_dict
    return None


def refresh_all_tokens():
    """Fetch fresh JWTs for every account and rebuild the bank.

    Runs in chunks so partial results are saved quickly (important on
    Render's free tier, which can sleep mid-refresh).
    """
    print("🔄 Starting auto JWT refresh...")
    if not REFRESH_LOCK.acquire(blocking=False):
        print("ℹ️ Refresh already running - skipped")
        return

    REFRESH_STATE["running"] = True
    REFRESH_STATE["last_error"] = None
    try:
        accounts = _load_accounts_for_refresh()
        if not accounts:
            print("❌ No accounts found for JWT refresh")
            REFRESH_STATE["last_error"] = "no accounts"
            return

        print(f"📊 {len(accounts)} accounts for JWT refresh")
        total_fetched = 0
        total_bank = 0
        CHUNK = 100

        for start in range(0, len(accounts), CHUNK):
            chunk = accounts[start:start + CHUNK]
            with ThreadPoolExecutor(max_workers=10) as ex:
                results = list(ex.map(_fetch_one_account, chunk))
            batch = [r for r in results if r]
            total_fetched += len(batch)
            if batch:
                total_bank, added, refreshed, removed = merge_tokens_into_bank(batch)
                print(f"  chunk {start // CHUNK + 1}: fetched={len(batch)} bank={total_bank} "
                      f"added={added} refreshed={refreshed} removed_expired={removed}")

        REFRESH_STATE["last_fetched"] = total_fetched
        REFRESH_STATE["last_total"] = total_bank
        print(f"✅ Auto JWT refresh done: fetched {total_fetched}, bank now {total_bank}")
    except Exception as e:
        REFRESH_STATE["last_error"] = str(e)
        print(f"❌ Auto refresh error: {e}")
    finally:
        REFRESH_STATE["running"] = False
        REFRESH_STATE["last_run"] = time.time()
        REFRESH_LOCK.release()

def auto_refresh_job():
    """Background job to refresh JWT tokens periodically"""
    while True:
        try:
            refresh_all_tokens()
        except Exception as e:
            print(f"❌ Auto refresh error: {e}")
        time.sleep(JWT_REFRESH_MINUTES * 60)  # Wait before next refresh

def start_auto_refresh():
    """Start the auto refresh background thread"""
    if AUTO_REFRESH_ENABLED:
        thread = threading.Thread(target=auto_refresh_job, daemon=True)
        thread.start()
        print("🔄 Auto JWT refresh thread started (every {} min)".format(JWT_REFRESH_MINUTES))
    else:
        print("⚠️ Auto JWT refresh is disabled")

# -------------------------------
# STATE (Vercel is stateless → /tmp ব্যবহার)
# -------------------------------
STATE_FILE = "/tmp/token_usage_state.json"
state_lock = threading.Lock()


def _today_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def load_state():
    default = {
        "date": _today_utc(),
        "usage": {},
        "file_pointer": 0,
        "file_used": {}
    }
    if not os.path.exists(STATE_FILE):
        return default
    try:
        with open(STATE_FILE, "r") as f:
            state = json.load(f)
        if state.get("date") != _today_utc():
            print("New UTC day -> resetting limits")
            return default
        state.setdefault("usage", {})
        state.setdefault("file_pointer", 0)
        state.setdefault("file_used", {})
        return state
    except Exception as e:
        print(f"State load error: {e}")
        return default


def save_state(state):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception as e:
        print(f"State save error: {e}")


def jwt_expiry(token):
    """Return the JWT exp (unix seconds), or 0 if it cannot be parsed."""
    try:
        if not isinstance(token, str) or token.count('.') != 2:
            return 0
        payload = token.split('.')[1]
        payload += '=' * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return int(claims.get('exp', 0))
    except Exception:
        return 0


def is_token_fresh(token, skew=60):
    """True only if the JWT is well formed and not expired."""
    return jwt_expiry(token) > time.time() + skew


def load_token_file(file_name):
    try:
        with open(file_name, "r") as f:
            tokens = json.load(f)
        if isinstance(tokens, list) and all(isinstance(t, dict) and "token" in t for t in tokens):
            fresh = [t for t in tokens if is_token_fresh(t.get("token"))]
            expired = len(tokens) - len(fresh)
            if expired:
                print(f"{file_name}: skipped {expired} expired tokens")
            return fresh
        print(f"Warning: {file_name} invalid format.")
        return []
    except FileNotFoundError:
        print(f"Warning: {file_name} not found.")
        return []
    except json.JSONDecodeError:
        print(f"Warning: {file_name} invalid JSON.")
        return []


def load_all_tokens():
    all_tokens = []
    for fname in TOKEN_FILES:
        tokens = load_token_file(fname)
        print(f"Loaded {len(tokens)} tokens from {fname}")
        for t in tokens:
            all_tokens.append((fname, t))
    return all_tokens


def encrypt_message(plaintext):
    key = b'Yg&tc%DEuh6%Zc^8'
    iv = b'6oyZDr22E3ychjM%'
    cipher = AES.new(key, AES.MODE_CBC, iv)
    padded = pad(plaintext, AES.block_size)
    return binascii.hexlify(cipher.encrypt(padded)).decode('utf-8')


def create_protobuf_message(user_id, region):
    message = like_pb2.like()
    message.uid = int(user_id)
    message.region = region
    return message.SerializeToString()


def create_protobuf_for_profile_check(uid):
    message = uid_generator_pb2.uid_generator()
    message.krishna_ = int(uid)
    message.teamXdarks = 1
    return message.SerializeToString()


def enc_profile_check_payload(uid):
    return encrypt_message(create_protobuf_for_profile_check(uid))


async def send_single_like_request(encrypted_like_payload, token_dict, url):
    edata = bytes.fromhex(encrypted_like_payload)
    token_value = token_dict.get("token", "")
    if not token_value:
        return 999
    headers = {
        'User-Agent': "Dalvik/2.1.0 (Linux; U; Android 9; ASUS_Z01QD Build/PI)",
        'Connection': "Keep-Alive",
        'Accept-Encoding': "gzip",
        'Authorization': f"Bearer {token_value}",
        'Content-Type': "application/x-www-form-urlencoded",
        'Expect': "100-continue",
        'X-Unity-Version': "2018.4.11f1",
        'X-GA': "v1 1",
        'ReleaseVersion': "OB55"
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=edata, headers=headers,
                                    timeout=aiohttp.ClientTimeout(total=10)) as response:
                return response.status
    except asyncio.TimeoutError:
        return 998
    except Exception:
        return 997


def _pb_read_varint(buf, i):
    shift = 0
    val = 0
    while True:
        b = buf[i]
        i += 1
        val |= (b & 0x7f) << shift
        if not b & 0x80:
            return val, i
        shift += 7


def _pb_fields(buf):
    """Minimal protobuf field walker -> list of (field_number, value)."""
    i = 0
    out = []
    while i < len(buf):
        try:
            key, i = _pb_read_varint(buf, i)
        except Exception:
            break
        fnum, wt = key >> 3, key & 7
        try:
            if wt == 0:
                v, i = _pb_read_varint(buf, i)
                out.append((fnum, v))
            elif wt == 2:
                ln, i = _pb_read_varint(buf, i)
                v = buf[i:i + ln]
                i += ln
                out.append((fnum, v))
            elif wt == 5:
                i += 4
            elif wt == 1:
                i += 8
            else:
                break
        except Exception:
            break
    return out


def parse_profile_extras(binary_data):
    """Extract Level / Exp / Region from the raw AccountInfo protobuf.

    Garena's AccountInfo has no 'Level' field in our .proto copy, but the
    wire format carries it: field 6 = Level, field 7 = Exp, field 5 = Region.
    """
    extras = {"level": 0, "exp": 0, "region": None}
    try:
        top = _pb_fields(binary_data)
        account_info = None
        for fnum, val in top:
            if fnum == 1 and isinstance(val, bytes):
                account_info = val
                break
        if account_info is None:
            return extras
        for fnum, val in _pb_fields(account_info):
            if fnum == 6 and isinstance(val, int):
                extras["level"] = val
            elif fnum == 7 and isinstance(val, int):
                extras["exp"] = val
            elif fnum == 5 and isinstance(val, bytes):
                try:
                    extras["region"] = val.decode("utf-8")
                except Exception:
                    pass
    except Exception as e:
        print(f"profile extras parse error: {e}")
    return extras


def make_profile_check_request(encrypted_profile_payload, server_name, token_dict):
    """Returns (Info | None, raw_bytes | b'')."""
    token_value = token_dict.get("token", "")
    if not token_value:
        return None, b""
    if server_name == "IND":
        url = "https://client.ind.freefiremobile.com/GetPlayerPersonalShow"
    else:
        url = "https://clientbp.ppmainecoonghj.com/GetPlayerPersonalShow"

    edata = bytes.fromhex(encrypted_profile_payload)
    headers = {
        'User-Agent': "Dalvik/2.1.0 (Linux; U; Android 9; ASUS_Z01QD Build/PI)",
        'Connection': "Keep-Alive",
        'Accept-Encoding': "gzip",
        'Authorization': f"Bearer {token_value}",
        'Content-Type': "application/x-www-form-urlencoded",
        'Expect': "100-continue",
        'X-Unity-Version': "2018.4.11f1",
        'X-GA': "v1 1",
        'ReleaseVersion': "OB55"
    }
    try:
        response = requests.post(url, data=edata, headers=headers, verify=False, timeout=10)
        response.raise_for_status()
        return decode_protobuf_profile_info(response.content), response.content
    except Exception as e:
        print(f"Profile check error: {e}")
        return None, b""


def decode_protobuf_profile_info(binary_data):
    try:
        items = like_count_pb2.Info()
        items.ParseFromString(binary_data)
        return items
    except Exception as e:
        print(f"Decode error: {e}")
        return None


def get_like_api_url(server_name):
    if server_name == "IND":
        return "https://client.ind.freefiremobile.com/LikeProfile"
    elif server_name in {"BR", "US", "SAC", "NA"}:
        return "https://client.us.freefiremobile.com/LikeProfile"
    else:
        return "https://clientbp.ppmainecoonghj.com/LikeProfile"


def select_tokens_for_likes(all_tokens, required_count, state):
    usage = state["usage"]
    selected = []
    pointer = state.get("file_pointer", 0)

    file_order = list(range(len(TOKEN_FILES)))
    file_order = file_order[pointer:] + file_order[:pointer]

    for file_idx in file_order:
        if len(selected) >= required_count:
            break
        fname = TOKEN_FILES[file_idx]
        tokens_in_file = [t for (f, t) in all_tokens if f == fname]
        for token_dict in tokens_in_file:
            if len(selected) >= required_count:
                break
            tok = token_dict["token"]
            used = usage.get(tok, 0)
            if used < LIKES_PER_TOKEN_PER_DAY:
                selected.append((fname, token_dict, used))
        state["file_pointer"] = (file_idx + 1) % len(TOKEN_FILES)

    return selected


def get_next_profile_token(all_tokens, state):
    usage = state["usage"]
    for fname, token_dict in all_tokens:
        tok = token_dict["token"]
        if usage.get(tok, 0) < LIKES_PER_TOKEN_PER_DAY:
            return token_dict
    return all_tokens[0][1] if all_tokens else None


app = Flask(__name__)


@app.route('/', methods=['GET'])
def home():
    return jsonify({
        "api": "Free Fire Like API",
        "status": "online",
        "endpoints": {
            "/like": "?key=YOUR_KEY&uid=UID&server_name=BD",
            "/token_info": "?key=YOUR_KEY"
        },
        "credits": "@DCFsolayman"
    })


@app.route('/like', methods=['GET'])
def handle_requests():
    key = request.args.get('key')
    if key != API_KEY:
        return jsonify(WRONG_KEY_RESPONSE), 401

    uid_param = request.args.get("uid")
    server_name_param = request.args.get("server_name", "BD").upper()

    if not uid_param:
        return jsonify({"error": "UID is required", "credits": "@DCFsolayman"}), 400

    all_tokens = load_all_tokens()
    if not all_tokens:
        return jsonify({
            "error": "No tokens loaded from token_bd_1.json or token_bd_2.json",
            "credits": "@DCFsolayman"
        }), 500

    with state_lock:
        state = load_state()
        usage = state["usage"]

    profile_token = get_next_profile_token(all_tokens, state)
    if not profile_token:
        return jsonify({"error": "No token for profile check."}), 500

    enc_uid = enc_profile_check_payload(uid_param)
    before_info, before_raw = make_profile_check_request(enc_uid, server_name_param, profile_token)
    before_likes = int(before_info.AccountInfo.Likes) if (before_info and hasattr(before_info, 'AccountInfo')) else 0
    print(f"UID {uid_param}: before = {before_likes}")

    with state_lock:
        state = load_state()
        selected = select_tokens_for_likes(all_tokens, LIKES_PER_USER_REQUEST, state)

    if not selected:
        return jsonify({
            "LikesGivenByAPI": 0,
            "LikesafterCommand": before_likes,
            "LikesbeforeCommand": before_likes,
            "PlayerNickname": "N/A",
            "UID": int(uid_param),
            "status": 2,
            "message": "All tokens reached daily limit. Try again tomorrow (UTC).",
            "credits": "@DCFsolayman"
        })

    like_proto = create_protobuf_message(uid_param, server_name_param)
    enc_like_payload = encrypt_message(like_proto)
    like_url = get_like_api_url(server_name_param)

    async def send_all():
        tasks = [
            send_single_like_request(enc_like_payload, td, like_url)
            for (_f, td, _u) in selected
        ]
        return await asyncio.gather(*tasks, return_exceptions=True)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        results = loop.run_until_complete(send_all())
    finally:
        loop.close()

    success_count = 0
    with state_lock:
        state = load_state()
        usage = state["usage"]
        for (_f, td, _u), res in zip(selected, results):
            if isinstance(res, int) and res == 200:
                tok = td["token"]
                usage[tok] = usage.get(tok, 0) + 1
                success_count += 1
        save_state(state)

    after_info, after_raw = make_profile_check_request(enc_uid, server_name_param, profile_token)
    after_likes = int(after_info.AccountInfo.Likes) if (after_info and hasattr(after_info, 'AccountInfo')) else before_likes
    nickname = "N/A"
    actual_uid = int(uid_param)
    player_level = 0
    if after_info and hasattr(after_info, 'AccountInfo'):
        actual_uid = int(after_info.AccountInfo.UID)
        if after_info.AccountInfo.PlayerNickname:
            nickname = str(after_info.AccountInfo.PlayerNickname)
    # Level / Exp are not in our .proto, read them from the raw wire format
    extras = parse_profile_extras(after_raw or before_raw)
    if extras.get("level"):
        player_level = int(extras["level"])

    increment = after_likes - before_likes
    status = 1 if increment > 0 else 2

    with state_lock:
        state = load_state()
        usage = state["usage"]
    total_used = sum(usage.values())
    total_cap = len(all_tokens) * LIKES_PER_TOKEN_PER_DAY

    return jsonify({
        "LikesGivenByAPI": increment,
        "LikesafterCommand": after_likes,
        "LikesbeforeCommand": before_likes,
        "PlayerNickname": nickname,
        "UID": actual_uid,
        "Level": player_level,
        "status": status,
        "TokensUsedThisRequest": len(selected),
        "SuccessfulSends": success_count,
        "DailyUsage": {
            "used": total_used,
            "capacity": total_cap,
            "remaining": total_cap - total_used
        },
        "credits": "@DCFsolayman"
    })


@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({"status": "ok", "service": "ff-likes-api"})


@app.route('/refresh', methods=['GET'])
def manual_refresh():
    """Trigger a token refresh in the background. /refresh?key=..."""
    if request.args.get('key') != API_KEY:
        return jsonify(WRONG_KEY_RESPONSE), 401
    if REFRESH_STATE["running"]:
        return jsonify({"status": "already_running"})
    threading.Thread(target=refresh_all_tokens, daemon=True).start()
    return jsonify({"status": "started"})


@app.route('/refresh_status', methods=['GET'])
def refresh_status():
    if request.args.get('key') != API_KEY:
        return jsonify(WRONG_KEY_RESPONSE), 401

    all_tokens = load_all_tokens()
    last_run = REFRESH_STATE["last_run"]
    next_run = None
    if last_run:
        next_run = last_run + JWT_REFRESH_MINUTES * 60

    def fmt(ts):
        return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(ts)) if ts else None

    return jsonify({
        "valid_tokens_in_bank": len(all_tokens),
        "auto_refresh_enabled": AUTO_REFRESH_ENABLED,
        "interval_minutes": JWT_REFRESH_MINUTES,
        "refresh_running": REFRESH_STATE["running"],
        "last_refresh": fmt(last_run),
        "next_refresh_if_awake": fmt(next_run),
        "last_fetched": REFRESH_STATE["last_fetched"],
        "last_error": REFRESH_STATE["last_error"],
        "credits": "@DCFsolayman"
    })


@app.route('/remain', methods=['GET'])
def remain_info():
    key = request.args.get('key')
    if key != API_KEY:
        return jsonify(WRONG_KEY_RESPONSE), 401

    with state_lock:
        state = load_state()
        usage = state["usage"]

    all_tokens = load_all_tokens()
    total_used = sum(usage.values())
    total_cap = len(all_tokens) * LIKES_PER_TOKEN_PER_DAY

    return jsonify({
        "daily_limit": total_cap,
        "remaining": max(total_cap - total_used, 0),
        "used": total_used,
        "reset_info": "4:00 AM UTC"
    })


@app.route('/token_info', methods=['GET'])
def token_info():
    key = request.args.get('key')
    if key != API_KEY:
        return jsonify(WRONG_KEY_RESPONSE), 401

    all_tokens = load_all_tokens()
    with state_lock:
        state = load_state()
        usage = state["usage"]

    file_stats = {}
    for fname in TOKEN_FILES:
        tokens = [t for (f, t) in all_tokens if f == fname]
        used = sum(usage.get(t["token"], 0) for t in tokens)
        cap = len(tokens) * LIKES_PER_TOKEN_PER_DAY
        file_stats[fname] = {
            "token_count": len(tokens),
            "used_today": used,
            "capacity_today": cap,
            "remaining_today": cap - used
        }

    total_used = sum(usage.values())
    total_cap = len(all_tokens) * LIKES_PER_TOKEN_PER_DAY

    return jsonify({
        "date_utc": state.get("date"),
        "files": file_stats,
        "total": {
            "tokens": len(all_tokens),
            "used_today": total_used,
            "capacity_today": total_cap,
            "remaining_today": total_cap - total_used
        },
        "config": {
            "LIKES_PER_TOKEN_PER_DAY": LIKES_PER_TOKEN_PER_DAY,
            "LIKES_PER_USER_REQUEST": LIKES_PER_USER_REQUEST
        },
        "credits": "@DCFsolayman"
    })


# Render runs this module through gunicorn (app:app), so __main__ never runs.
# Start the refresh thread at import time; DISABLE_AUTO_REFRESH=1 turns it off.
if os.getenv("DISABLE_AUTO_REFRESH") != "1":
    try:
        start_auto_refresh()
    except Exception as _e:
        print(f"auto refresh start failed: {_e}")


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))