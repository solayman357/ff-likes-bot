from flask import Flask, request, jsonify
import asyncio
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
import binascii
import aiohttp
import requests
import json
import base64
import time
import like_pb2
import uid_generator_pb2
import visit_count_pb2
from google.protobuf.message import DecodeError
from collections import OrderedDict

import os
import concurrent.futures

app = Flask(__name__)

# Base directory for resolving data files regardless of working directory
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ✅ Valid API keys (loaded from environment variable)
API_KEY_ENV = os.getenv("API_KEY", "ZEXXY")
VALID_API_KEYS = {API_KEY_ENV}

# 🔑 JWT API URLs (comma-separated) for generating tokens from account credentials
JWT_API_URLS_STR = os.getenv("JWT_API_URLS", "")
JWT_API_URLS = [u.strip() for u in JWT_API_URLS_STR.split(",") if u.strip()]

# 🔢 Like limit tracking
daily_limit = 250
used_count = 0

# 📂 Account files
ACCOUNT_FILES = {
    "IND": os.path.join(BASE_DIR, "account_ind.txt"),
    "BD": os.path.join(BASE_DIR, "account_bd.txt"),
}
for _r in {"BR", "US", "SAC", "NA"}:
    ACCOUNT_FILES[_r] = os.path.join(BASE_DIR, "account_ind.txt")


def load_accounts(server_name):
    """Load UID:password accounts from file."""
    path = ACCOUNT_FILES.get(server_name, ACCOUNT_FILES["BD"])
    accounts = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if ":" in line:
                    uid, pwd = line.split(":", 1)
                    uid = uid.strip()
                    pwd = pwd.strip()
                    if uid and pwd and uid.isdigit():
                        accounts.append({"uid": uid, "password": pwd})
    except Exception as e:
        app.logger.error(f"Error loading accounts for {server_name}: {e}")
    return accounts


def generate_jwt_from_account(uid, password, jwt_url):
    """Call JWT API to get a session token for an account."""
    try:
        import urllib.parse
        encoded_pwd = urllib.parse.quote(password, safe="")
        url = f"{jwt_url}?uid={uid}&password={encoded_pwd}"
        resp = requests.get(url, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            token = None
            if isinstance(data, dict):
                if "token" in data:
                    token = data["token"]
                elif "access_token" in data:
                    token = data["access_token"]
                elif "data" in data and isinstance(data["data"], dict):
                    token = data["data"].get("token")
            if token:
                return token
    except Exception as e:
        app.logger.error(f"JWT gen error for {uid}: {e}")
    return None


def generate_tokens_for_accounts(server_name, max_tokens=300):
    """Generate fresh JWT tokens from accounts via JWT API."""
    accounts = load_accounts(server_name)
    if not accounts or not JWT_API_URLS:
        return []

    tokens = []
    jwt_url = JWT_API_URLS[0]

    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        futures = {}
        for acc in accounts[:max_tokens]:
            future = executor.submit(generate_jwt_from_account, acc["uid"], acc["password"], jwt_url)
            futures[future] = acc

        for future in concurrent.futures.as_completed(futures, timeout=60):
            token = future.result()
            if token:
                tokens.append(token)
            if len(tokens) >= max_tokens:
                break

    return tokens


def load_tokens(region):
    try:
        if region == "IND":
            path = os.path.join(BASE_DIR, "token_ind.json")
        elif region in {"BR", "US", "SAC", "NA"}:
            path = os.path.join(BASE_DIR, "token_bd.json")
        else:
            path = os.path.join(BASE_DIR, "token_bd.json")
        with open(path, "r") as f:
            tokens = json.load(f)
        return tokens
    except Exception as e:
        app.logger.error(f"Error loading tokens for region {region}: {e}")
        return None


def token_expiry(token):
    """Return the JWT 'exp' claim (unix seconds), or 0 if it cannot be read."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return int(claims.get("exp", 0))
    except Exception:
        return 0


def valid_tokens(tokens):
    """Filter out expired/duplicate tokens so we never call the API with dead JWTs."""
    now = time.time()
    result = []
    seen = set()
    for entry in tokens or []:
        token = entry.get("token") if isinstance(entry, dict) else None
        if not token or token in seen:
            continue
        if token_expiry(token) > now:
            seen.add(token)
            result.append(entry)
    return result


def encrypt_message(plaintext):
    try:
        key = b'Yg&tc%DEuh6%Zc^8'
        iv = b'6oyZDr22E3ychjM%'
        cipher = AES.new(key, AES.MODE_CBC, iv)
        padded_message = pad(plaintext, AES.block_size)
        encrypted_message = cipher.encrypt(padded_message)
        return binascii.hexlify(encrypted_message).decode('utf-8')
    except Exception as e:
        app.logger.error(f"Error encrypting message: {e}")
        return None


def create_protobuf_message(user_id, region):
    try:
        message = like_pb2.like()
        message.uid = int(user_id)
        message.region = region
        return message.SerializeToString()
    except Exception as e:
        app.logger.error(f"Error creating protobuf message: {e}")
        return None


async def send_request(encrypted_uid, token, url):
    try:
        edata = bytes.fromhex(encrypted_uid)
        headers = {
            "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 9; ASUS_Z01QD Build/PI)",
            "Connection": "Keep-Alive",
            "Accept-Encoding": "gzip",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Expect": "100-continue",
            "X-Unity-Version": "2018.4.11f1",
            "X-GA": "v1 1",
            "ReleaseVersion": "OB55"
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=edata, headers=headers) as response:
                return await response.text()
    except Exception as e:
        app.logger.error(f"Exception in send_request: {e}")
        return None


async def send_multiple_requests(uid, region, url, token_list=None):
    try:
        protobuf_message = create_protobuf_message(uid, region)
        if protobuf_message is None:
            return None
        encrypted_uid = encrypt_message(protobuf_message)
        if encrypted_uid is None:
            return None
        # Use pre-generated tokens if provided, otherwise load from bank
        if token_list is not None:
            tokens = [{"token": t} for t in token_list]
        else:
            tokens = load_tokens(region)
            if tokens is None:
                return None
            tokens = valid_tokens(tokens)
        if not tokens:
            app.logger.error("send_multiple_requests: no unexpired tokens available")
            return None
        tasks = []
        for i in range(min(300, len(tokens))):
            token = tokens[i]["token"]
            tasks.append(send_request(encrypted_uid, token, url))
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return results
    except Exception as e:
        app.logger.error(f"Exception in send_multiple_requests: {e}")
        return None


def create_protobuf(uid):
    try:
        message = uid_generator_pb2.uid_generator()
        message.saturn_ = int(uid)
        message.garena = 1
        return message.SerializeToString()
    except Exception as e:
        app.logger.error(f"Error creating uid protobuf: {e}")
        return None


def enc(uid):
    protobuf_data = create_protobuf(uid)
    if protobuf_data is None:
        return None
    return encrypt_message(protobuf_data)


def make_request(encrypt, region, token):
    try:
        if region == "IND":
            url = "https://client.ind.freefiremobile.com/GetPlayerPersonalShow"
        elif region in {"BR", "US", "SAC", "NA"}:
            url = "https://client.us.freefiremobile.com/GetPlayerPersonalShow"
        else:
            url = "https://client.ind.freefiremobile.com/GetPlayerPersonalShow"
        edata = bytes.fromhex(encrypt)
        headers = {
            "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 9; ASUS_Z01QD Build/PI)",
            "Connection": "Keep-Alive",
            "Accept-Encoding": "gzip",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Expect": "100-continue",
            "X-Unity-Version": "2018.4.11f1",
            "X-GA": "v1 1",
            "ReleaseVersion": "OB55"
        }
        response = requests.post(url, data=edata, headers=headers, verify=False)
        binary = response.content
        decoded = visit_count_pb2.Info()
        decoded.ParseFromString(binary)
        return decoded
    except DecodeError as e:
        app.logger.error(f"DecodeError: {e}")
        return None
    except Exception as e:
        app.logger.error(f"Error in make_request: {e}")
        return None


@app.route('/like', methods=['GET'])
def handle_requests():
    global used_count  # ✅ fix added

    # ✅ API key check
    api_key = request.args.get("key")
    if api_key not in VALID_API_KEYS:
        result = OrderedDict([
            ("error", "Invalid or missing API key"),
            ("status", 3)
        ])
        return app.response_class(
            response=json.dumps(result, separators=(',', ':')),
            status=401,
            mimetype='application/json'
        )

    uid = request.args.get("uid")
    region = request.args.get("region", "").upper()
    if not uid or not region:
        return {"error": "UID and region are required"}, 400

    try:
        def process_request():
            global used_count  # ✅ fix added again (for nested function)

            # 🔑 Generate fresh JWT tokens from accounts via JWT API
            jwt_tokens = generate_tokens_for_accounts(region, max_tokens=300)
            if not jwt_tokens:
                return app.response_class(
                    response=json.dumps(OrderedDict([
                        ("error", "Failed to generate JWT tokens from accounts. Check JWT_API_URLS and account files."),
                        ("status", 4)
                    ]), separators=(',', ':')),
                    status=503,
                    mimetype='application/json'
                )

            encrypted_uid = enc(uid)
            if encrypted_uid is None:
                raise Exception("Encryption of UID failed.")

            # Convert string tokens to entry dicts for compatibility
            token_entries = [{"token": t} for t in jwt_tokens]

            # Rotate across tokens until one is accepted by the game server.
            working_token = None
            before = None
            for entry in token_entries[:20]:
                candidate = entry["token"]
                before = make_request(encrypted_uid, region, candidate)
                if before is not None:
                    working_token = candidate
                    break
            if before is None or working_token is None:
                return app.response_class(
                    response=json.dumps(OrderedDict([
                        ("error", "Token bank is unusable: every available token was rejected by the game server."),
                        ("status", 4)
                    ]), separators=(',', ':')),
                    status=503,
                    mimetype='application/json'
                )
            before_like = before.AccountInfo.Likes

            if region == "IND":
                url = "https://client.ind.freefiremobile.com/LikeProfile"
            elif region in {"BR", "US", "SAC", "NA"}:
                url = "https://client.us.freefiremobile.com/LikeProfile"
            else:
                url = "https://client.ind.freefiremobile.com/LikeProfile"

            asyncio.run(send_multiple_requests(uid, region, url, token_list=jwt_tokens))

            after = make_request(encrypted_uid, region, working_token)
            if after is None:
                for candidate in jwt_tokens[:20]:
                    if candidate == working_token:
                        continue
                    after = make_request(encrypted_uid, region, candidate)
                    if after is not None:
                        break
            if after is None:
                raise Exception("Failed to get final info.")
            after_like = after.AccountInfo.Likes
            like_given = after_like - before_like
            status = 1 if like_given > 0 else 2

            # ✅ Count only when successful (status == 1)
            if status == 1:
                used_count += 1

            remaining = max(daily_limit - used_count, 0)

            result = OrderedDict([
                ("LikesGivenByAPI", like_given),
                ("LikesafterCommand", after_like),
                ("LikesbeforeCommand", before_like),
                ("PlayerNickname", after.AccountInfo.PlayerNickname),
                ("Level", after.AccountInfo.Levels),
                ("Region", after.AccountInfo.PlayerRegion),
                ("UID", after.AccountInfo.UID),
                ("status", status),
                ("daily_limit", daily_limit),
                ("used", used_count),
                ("remaining", remaining)
            ])

            return app.response_class(
                response=json.dumps(result, separators=(',', ':')),
                status=200,
                mimetype='application/json'
            )

        return process_request()

    except Exception as e:
        app.logger.error(f"Error: {e}")
        return {"error": str(e)}, 500


# 🆕 Health check endpoint
@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({"status": "ok", "service": "ff-likes-api"})


# 🆕 /remain endpoint
@app.route('/remain', methods=['GET'])
def remain_info():
    global used_count  # ✅ fix added

    remaining = max(daily_limit - used_count, 0)
    data = {
        "daily_limit": daily_limit,
        "remaining": remaining,
        "used": used_count,
        "reset_info": "4:00 AM IST"
    }
    return jsonify(data)


# 🆕 Upload tokens endpoint (for bot to send tokens to API on Render)
@app.route('/upload-tokens', methods=['POST'])
def upload_tokens():
    try:
        data = request.get_json()
        if not data or "tokens" not in data:
            return {"error": "Missing 'tokens' field in JSON body"}, 400

        new_tokens = data["tokens"]
        if not isinstance(new_tokens, list):
            return {"error": "'tokens' must be a list"}, 400

        # Validate each token by checking JWT expiry
        now = time.time()
        valid_tokens_list = []
        expired_count = 0

        for entry in new_tokens:
            token = entry.get("token") if isinstance(entry, dict) else entry
            if not token:
                continue
            try:
                payload = token.split(".")[1]
                payload += "=" * (-len(payload) % 4)
                claims = json.loads(base64.urlsafe_b64decode(payload))
                if int(claims.get("exp", 0)) > now:
                    valid_tokens_list.append({"token": token})
                else:
                    expired_count += 1
            except Exception:
                expired_count += 1

        # Load existing tokens and merge
        existing = load_tokens("BD") or []
        existing_uids = set()
        for e in existing:
            t = e.get("token", "")
            try:
                p = t.split(".")[1]
                p += "=" * (-len(p) % 4)
                c = json.loads(base64.urlsafe_b64decode(p))
                uid = c.get("account_id")
                if uid:
                    existing_uids.add(str(uid))
            except Exception:
                pass

        # Add only new UIDs
        for entry in valid_tokens_list:
            t = entry.get("token", "")
            try:
                p = t.split(".")[1]
                p += "=" * (-len(p) % 4)
                c = json.loads(base64.urlsafe_b64decode(p))
                uid = c.get("account_id")
                if uid and str(uid) not in existing_uids:
                    existing.append(entry)
                    existing_uids.add(str(uid))
            except Exception:
                pass

        # Save merged tokens
        bank_path = os.path.join(BASE_DIR, "token_bd.json")
        with open(bank_path, "w") as f:
            json.dump(existing, f)

        return {
            "status": "ok",
            "added": len(valid_tokens_list),
            "expired": expired_count,
            "total_in_bank": len(existing)
        }

    except Exception as e:
        app.logger.error(f"Error uploading tokens: {e}")
        return {"error": str(e)}, 500


if __name__ == '__main__':
    import os
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
