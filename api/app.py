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

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

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


def load_token_file(file_name):
    try:
        with open(file_name, "r") as f:
            tokens = json.load(f)
        if isinstance(tokens, list) and all(isinstance(t, dict) and "token" in t for t in tokens):
            return tokens
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


def make_profile_check_request(encrypted_profile_payload, server_name, token_dict):
    token_value = token_dict.get("token", "")
    if not token_value:
        return None
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
        return decode_protobuf_profile_info(response.content)
    except Exception as e:
        print(f"Profile check error: {e}")
        return None


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
        "credits": "@HAFSAN_KUN"
    })


@app.route('/like', methods=['GET'])
def handle_requests():
    key = request.args.get('key')
    if key != API_KEY:
        return jsonify(WRONG_KEY_RESPONSE), 401

    uid_param = request.args.get("uid")
    server_name_param = request.args.get("server_name", "BD").upper()

    if not uid_param:
        return jsonify({"error": "UID is required", "credits": "@HAFSAN_KUN"}), 400

    all_tokens = load_all_tokens()
    if not all_tokens:
        return jsonify({
            "error": "No tokens loaded from token_bd_1.json or token_bd_2.json",
            "credits": "@HAFSAN_KUN"
        }), 500

    with state_lock:
        state = load_state()
        usage = state["usage"]

    profile_token = get_next_profile_token(all_tokens, state)
    if not profile_token:
        return jsonify({"error": "No token for profile check."}), 500

    enc_uid = enc_profile_check_payload(uid_param)
    before_info = make_profile_check_request(enc_uid, server_name_param, profile_token)
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
            "credits": "@HAFSAN_KUN"
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

    after_info = make_profile_check_request(enc_uid, server_name_param, profile_token)
    after_likes = int(after_info.AccountInfo.Likes) if (after_info and hasattr(after_info, 'AccountInfo')) else before_likes
    nickname = "N/A"
    actual_uid = int(uid_param)
    player_level = 0
    if after_info and hasattr(after_info, 'AccountInfo'):
        actual_uid = int(after_info.AccountInfo.UID)
        if after_info.AccountInfo.PlayerNickname:
            nickname = str(after_info.AccountInfo.PlayerNickname)
        if hasattr(after_info.AccountInfo, 'Level') and after_info.AccountInfo.Level:
            player_level = int(after_info.AccountInfo.Level)

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
        "credits": "@HAFSAN_KUN"
    })


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)