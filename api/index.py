# ============================================================
#   🔥 RIFAT LIKE API 🔥
#   Free Fire Auto Like API — Vercel Ready
#   Author  : RIFAT
#   Version : 2.0
# ============================================================

from flask import Flask, request, jsonify
import asyncio
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from google.protobuf.json_format import MessageToJson
import binascii
import aiohttp
import requests
import json
import like_pb2
import like_count_pb2
import uid_generator_pb2
import time
from collections import defaultdict
from datetime import datetime, timedelta
import random
import os
import urllib.parse
import jwt
import threading
import pickle

app = Flask(__name__)

# ============================================================
#   ⚙️ CONFIG (RIFAT)
# ============================================================
NORMAL_API_KEY   = "RIFAT_API_KEY_HERE"          # 🔴 আপনার গোপন key
INFO_API_ENDPOINT = "YOUR_INFO_API_URL"           # 🔴 আপনার info API

JWT_API_URLS = [
    "YOUR_JWT_API_URL_1",                         # 🔴 API #1
    "YOUR_JWT_API_URL_2",                         # 🔴 API #2
]

# ============================================================
#   🌍 GLOBAL STATE
# ============================================================
TOKEN_CACHE      = {}
FAILED_TOKENS    = {}
TOKEN_THREAD_LOCK = threading.Lock()
TOKEN_EXPIRY_TIME_HOURS = 6

KEY_LIMIT        = 999
tracker          = defaultdict(lambda: [0, 0])
liked_cache      = defaultdict(set)
TRACKER_LOCK     = threading.Lock()

BANGLADESH_OFFSET = 6 * 3600

# ============================================================
#   💾 PERSISTENT STORAGE
# ============================================================
def save_data():
    try:
        os.makedirs('/tmp', exist_ok=True)
        with TRACKER_LOCK:
            with open('/tmp/tracker_data.pkl', 'wb') as f:
                pickle.dump(dict(tracker), f)
            with open('/tmp/liked_cache.pkl', 'wb') as f:
                pickle.dump(dict(liked_cache), f)
            with open('/tmp/token_cache.pkl', 'wb') as f:
                pickle.dump(dict(TOKEN_CACHE), f)
            with open('/tmp/failed_tokens.pkl', 'wb') as f:
                pickle.dump(dict(FAILED_TOKENS), f)
    except Exception as e:
        print(f"❌ Save error: {e}")


def load_data():
    global tracker, liked_cache, TOKEN_CACHE, FAILED_TOKENS
    try:
        os.makedirs('/tmp', exist_ok=True)

        if os.path.exists('/tmp/tracker_data.pkl'):
            with open('/tmp/tracker_data.pkl', 'rb') as f:
                tracker.update(pickle.load(f))

        if os.path.exists('/tmp/liked_cache.pkl'):
            with open('/tmp/liked_cache.pkl', 'rb') as f:
                liked_cache.update(pickle.load(f))

        if os.path.exists('/tmp/token_cache.pkl'):
            with open('/tmp/token_cache.pkl', 'rb') as f:
                TOKEN_CACHE.update(pickle.load(f))

        if os.path.exists('/tmp/failed_tokens.pkl'):
            with open('/tmp/failed_tokens.pkl', 'rb') as f:
                FAILED_TOKENS.update(pickle.load(f))

        print(f"✅ RIFAT Loaded: {len(TOKEN_CACHE)} tokens | {len(FAILED_TOKENS)} failed")
    except Exception as e:
        print(f"❌ Load error: {e}")


def get_bangladesh_midnight_timestamp():
    now_utc = datetime.utcnow()
    now_bd  = now_utc + timedelta(hours=BANGLADESH_OFFSET)
    midnight_bd = datetime(now_bd.year, now_bd.month, now_bd.day)
    midnight_utc = midnight_bd - timedelta(hours=BANGLADESH_OFFSET)
    return midnight_utc.timestamp()


# ============================================================
#   📂 ACCOUNT LOADER
# ============================================================
def load_accounts(server_name):
    try:
        if server_name in {"IND", "BR", "US", "SAC", "NA"}:
            filename = "account_ind.txt"
        else:
            filename = "account_bd.txt"

        if not os.path.exists(filename):
            print(f"⚠️ {filename} not found!")
            return []

        accounts = []
        seen = set()

        with open(filename, "r", encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue

                if ':' in line:
                    parts = line.split(':', 1)
                    uid = parts[0].strip()
                    pwd = parts[1].strip()

                    if uid and pwd and uid.isdigit() and uid not in seen:
                        seen.add(uid)
                        accounts.append({"uid": uid, "password": pwd})

        print(f"📂 Loaded {len(accounts)} accounts from {filename}")
        return accounts

    except Exception as e:
        print(f"❌ Account load error: {e}")
        return []


# ============================================================
#   🔑 JWT TOKEN GENERATOR
# ============================================================
async def generate_jwt_token_with_api(uid, password, api_url, retry_count=0):
    encoded_pwd = urllib.parse.quote(password, safe='')
    max_attempts = 3

    for attempt in range(retry_count, max_attempts):
        try:
            url = f"{api_url}?uid={uid}&password={encoded_pwd}"

            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=30) as response:
                    if response.status == 200:
                        data = await response.json()
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
                            try:
                                payload = jwt.decode(token, options={"verify_signature": False})
                                exp = payload.get("exp", int(time.time()) + (TOKEN_EXPIRY_TIME_HOURS * 3600))
                            except Exception:
                                exp = int(time.time()) + (TOKEN_EXPIRY_TIME_HOURS * 3600)

                            TOKEN_CACHE[uid] = {
                                "token": token,
                                "expires_at": datetime.utcfromtimestamp(exp),
                                "api_used": api_url
                            }
                            FAILED_TOKENS.pop(uid, None)
                            print(f"✅ Token OK: {uid}")
                            return token
                        else:
                            print(f"⚠️ No token: {uid}")
                    else:
                        print(f"❌ API {response.status}: {uid}")

        except asyncio.TimeoutError:
            print(f"⏰ Timeout: {uid}")
        except Exception as e:
            print(f"❌ Error {uid}: {e}")

        if attempt < max_attempts - 1:
            await asyncio.sleep(2)

    FAILED_TOKENS[uid] = {'timestamp': time.time(), 'api_used': api_url}
    print(f"❌ Failed token: {uid}")
    return None


# ============================================================
#   🚀 PARALLEL TOKEN GENERATION
# ============================================================
async def generate_tokens_batch_parallel(accounts, api_urls):
    if not accounts:
        return {}

    total = len(accounts)
    split = total // len(api_urls)

    api1_accs = accounts[:split]
    api2_accs = accounts[split:]

    print(f"📊 API1: {len(api1_accs)} | API2: {len(api2_accs)}")

    async def gen(idx, accs):
        if not accs:
            return {}
        tokens = {}
        sem = asyncio.Semaphore(100)

        async def worker(acc):
            async with sem:
                t = await generate_jwt_token_with_api(acc['uid'], acc['password'], api_urls[idx])
                if t:
                    tokens[acc['uid']] = t

        await asyncio.gather(*[worker(a) for a in accs], return_exceptions=True)
        return tokens

    start = time.time()
    results = await asyncio.gather(
        gen(0, api1_accs),
        gen(1, api2_accs) if len(api_urls) > 1 else asyncio.sleep(0),
        return_exceptions=True
    )

    all_tokens = {}
    for r in results:
        if isinstance(r, dict):
            all_tokens.update(r)

    print(f"✅ Total: {len(all_tokens)} tokens | {round(time.time()-start,2)}s")
    return all_tokens


# ============================================================
#   ❤️ LIKE SENDING
# ============================================================
async def send_like(encrypted_uid, token, url):
    try:
        edata = bytes.fromhex(encrypted_uid)
        headers = {
            'User-Agent': "Dalvik/2.1.0 (Linux; U; Android 9; ASUS_Z01QD Build/PI)",
            'Authorization': f"Bearer {token}",
            'Content-Type': "application/x-www-form-urlencoded",
            'X-GA': "v1 1",
            'ReleaseVersion': "OB55"
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=edata, headers=headers, timeout=5) as response:
                return response.status
    except Exception:
        return 500


async def send_likes_batch(target_uid, encrypted_uid, accounts_with_tokens, url):
    if not accounts_with_tokens:
        return 0, 0

    sem = asyncio.Semaphore(100)
    success = 0
    failed = 0

    async def worker(acc, token):
        nonlocal success, failed
        async with sem:
            status = await send_like(encrypted_uid, token, url)
            if status == 200:
                with TRACKER_LOCK:
                    liked_cache[target_uid].add(acc['uid'])
                success += 1
            else:
                failed += 1

    await asyncio.gather(*[worker(a, t) for a, t in accounts_with_tokens], return_exceptions=True)
    return success, failed


async def send_all_likes(target_uid, server_name, url, accounts, encrypted_uid):
    if not accounts:
        return {'success': 0, 'failed': 0, 'total': 0, 'already_liked': 0}

    with TRACKER_LOCK:
        already = liked_cache.get(target_uid, set())
        fresh = [a for a in accounts if a['uid'] not in already]

    print(f"📊 Total: {len(accounts)} | Fresh: {len(fresh)} | Already: {len(already)}")

    if not fresh:
        return {'success': 0, 'failed': 0, 'total': len(accounts),
                'already_liked': len(already), 'fresh_used': 0}

    random.shuffle(fresh)

    need_tokens = []
    have_tokens = []

    for acc in fresh:
        cached = TOKEN_CACHE.get(acc['uid'])
        if cached and (cached["expires_at"] - datetime.utcnow()).total_seconds() > 0:
            have_tokens.append((acc, cached["token"]))
        else:
            need_tokens.append(acc)

    tokens_dict = {}
    tok_time = 0

    if need_tokens:
        start = time.time()
        tokens_dict = await generate_tokens_batch_parallel(need_tokens, JWT_API_URLS)
        tok_time = time.time() - start
        save_data()

    all_pairs = list(have_tokens)
    for acc in fresh:
        t = tokens_dict.get(acc['uid'])
        if t:
            all_pairs.append((acc, t))

    if not all_pairs:
        return {'success': 0, 'failed': len(fresh), 'total': len(accounts)}

    start = time.time()
    success, failed = await send_likes_batch(target_uid, encrypted_uid, all_pairs, url)
    like_time = time.time() - start

    save_data()

    return {
        'success': success,
        'failed': failed,
        'total': len(accounts),
        'already_liked': len(already),
        'fresh_used': len(all_pairs),
        'token_gen_time': round(tok_time, 2),
        'like_send_time': round(like_time, 2),
        'total_time': round(tok_time + like_time, 2)
    }


# ============================================================
#   🔐 HELPERS
# ============================================================
def get_valid_token_sync(uid):
    cached = TOKEN_CACHE.get(uid)
    if cached and (cached["expires_at"] - datetime.utcnow()).total_seconds() > 0:
        return cached["token"]
    return None


def encrypt_message(plaintext):
    key = b'Yg&tc%DEuh6%Zc^8'
    iv  = b'6oyZDr22E3ychjM%'
    cipher = AES.new(key, AES.MODE_CBC, iv)
    padded = pad(plaintext, AES.block_size)
    return binascii.hexlify(cipher.encrypt(padded)).decode('utf-8')


def enc(uid):
    msg = uid_generator_pb2.uid_generator()
    msg.krishna_ = int(uid)
    msg.teamXdarks = 1
    return encrypt_message(msg.SerializeToString())


def decode_protobuf(binary):
    try:
        items = like_count_pb2.Info()
        items.ParseFromString(binary)
        return items
    except Exception:
        return None


def get_player_info(encrypted_uid, server_name, token):
    if server_name == "IND":
        url = "https://client.ind.freefiremobile.com/GetPlayerPersonalShow"
    elif server_name in {"BR", "US", "SAC", "NA"}:
        url = "https://client.us.freefiremobile.com/GetPlayerPersonalShow"
    else:
        url = "https://clientbp.ggpolarbear.com/GetPlayerPersonalShow"

    edata = bytes.fromhex(encrypted_uid)
    headers = {
        'User-Agent': "Dalvik/2.1.0 (Linux; U; Android 9; ASUS_Z01QD Build/PI)",
        'Authorization': f"Bearer {token}",
        'Content-Type': "application/x-www-form-urlencoded",
        'X-GA': "v1 1",
        'ReleaseVersion': "OB55"
    }

    try:
        r = requests.post(url, data=edata, headers=headers, verify=False, timeout=10)
        return decode_protobuf(r.content)
    except Exception:
        return None


def get_player_level_from_info(uid):
    try:
        url = f"{INFO_API_ENDPOINT}?uid={uid}"
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            data = r.json()
            if 'basicInfo' in data and 'level' in data['basicInfo']:
                return data['basicInfo']['level']
        return None
    except Exception:
        return None


# ============================================================
#   📌 MAIN REQUEST PROCESSOR
# ============================================================
def process_like_request(uid, server_name, client_ip, require_key=True, api_key=None):
    total_start = time.time()

    if require_key and api_key != NORMAL_API_KEY:
        return jsonify({"error": "Invalid API Key"}), 403

    if not uid or not server_name:
        return jsonify({"error": "UID and server_name required"}), 400

    valid = ["IND", "BR", "US", "SAC", "NA", "BD", "RU", "ME", "ID"]
    if server_name not in valid:
        return jsonify({"error": f"Invalid server. Use: {valid}"}), 400

    player_level = get_player_level_from_info(uid)

    accounts = load_accounts(server_name)
    if not accounts:
        return jsonify({"error": f"No accounts for {server_name}"}), 500

    bd_midnight = get_bangladesh_midnight_timestamp()

    with TRACKER_LOCK:
        count, last = tracker[client_ip]
        if last < bd_midnight:
            tracker[client_ip] = [0, bd_midnight]
            count = 0
        if count >= KEY_LIMIT:
            return jsonify({
                "error": "Daily limit reached",
                "remains": f"(0/{KEY_LIMIT})",
                "reset_time": "4:00 AM BD Time"
            }), 429

    check_token = None
    for acc in accounts[:5]:
        check_token = get_valid_token_sync(acc['uid'])
        if check_token:
            break

    if not check_token:
        for acc in accounts[:5]:
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                check_token = loop.run_until_complete(
                    generate_jwt_token_with_api(acc['uid'], acc['password'], JWT_API_URLS[0])
                )
                loop.close()
                if check_token:
                    break
            except Exception:
                continue

    if not check_token:
        return jsonify({"error": "Token generation failed"}), 500

    encrypted_uid = enc(uid)

    before = get_player_info(encrypted_uid, server_name, check_token)
    if before is None:
        return jsonify({"error": "Invalid UID or server"}), 200

    try:
        bd = json.loads(MessageToJson(before))
        before_like = int(bd['AccountInfo'].get('Likes', 0))
    except Exception:
        return jsonify({"error": "Parse failed"}), 200

    if server_name == "IND":
        like_url = "https://client.ind.freefiremobile.com/LikeProfile"
    elif server_name in {"BR", "US", "SAC", "NA"}:
        like_url = "https://client.us.freefiremobile.com/LikeProfile"
    else:
        like_url = "https://clientbp.ggpolarbear.com/LikeProfile"

    asyncio.run(send_all_likes(uid, server_name, like_url, accounts, encrypted_uid))

    after = get_player_info(encrypted_uid, server_name, check_token)
    if after is None:
        return jsonify({"error": "Verify failed"}), 200

    try:
        ad = json.loads(MessageToJson(after))
        after_like = int(ad['AccountInfo']['Likes'])
        player_id  = int(ad['AccountInfo']['UID'])
        player_name = str(ad['AccountInfo']['PlayerNickname'])

        given = after_like - before_like

        if given > 0:
            with TRACKER_LOCK:
                tracker[client_ip][0] += 1
                count = tracker[client_ip][0]
            status = 1
        else:
            status = 2

        remains = KEY_LIMIT - count
        save_data()

        resp = {
            "success": True,
            "LikesGivenByAPI": given,
            "LikesafterCommand": after_like,
            "LikesbeforeCommand": before_like,
            "PlayerNickname": player_name,
            "UID": player_id,
            "status": status,
            "remains": f"({remains}/{KEY_LIMIT})",
            "powered_by": "RIFAT"
        }

        if player_level is not None:
            resp["Level"] = player_level

        return jsonify(resp)

    except Exception as e:
        return jsonify({"error": str(e), "status": 0}), 500


# ============================================================
#   🌐 ROUTES
# ============================================================
@app.route('/like', methods=['GET'])
def handle_like():
    return process_like_request(
        request.args.get("uid"),
        request.args.get("server_name", "").upper(),
        request.remote_addr,
        True,
        request.args.get("key")
    )


@app.route('/', methods=['GET'])
def home():
    return jsonify({
        "status": "online",
        "api": "RIFAT LIKE API",
        "owner": "RIFAT",
        "version": "2.0",
        "endpoint": "/like?uid=<UID>&server_name=<SERVER>&key=<API_KEY>",
        "servers": ["IND", "BR", "US", "SAC", "NA", "BD", "RU", "ME", "ID"]
    })


# Load data on startup
load_data()

# Vercel handler
handler = app

# Render / local server
if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)