from flask import Flask, request, jsonify
import asyncio
import aiohttp
import requests
import json
import base64
import time
import os
from collections import OrderedDict
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
import binascii

# Protobuf modules
import freefire_pb2
import core_pb2
import account_show_pb2
import send_like_pb2
from google.protobuf import json_format, message as protobuf_message

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ✅ Valid API keys
API_KEY_ENV = os.getenv("API_KEY", "Solayman")
VALID_API_KEYS = {API_KEY_ENV}

# 🔢 Like limit tracking
daily_limit = 250
used_count = 0

# 🔑 Garena API constants (from freefire-like-and-guest-api)
MAIN_KEY = base64.b64decode('WWcmdGMlREV1aDYlWmNeOA==')   # b'Yg&tc%DEuh6%Zc^8'
MAIN_IV = base64.b64decode('Nm95WkRyMjJFM3ljaGpNJQ==')       # b'6oyZDr22E3ychjM%'
RELEASEVERSION = "OB50"
USERAGENT = "Dalvik/2.1.0 (Linux; U; Android 13; CPH2095 Build/RKQ1.211119.001)"

CLIENT_SECRET = "2ee44819e9b4598845141067b281621874d0d5d7af9d8f7e00c1e54715b7d1e3"
CLIENT_ID = "100067"

SUPPORTED_REGIONS = ["IND", "BR", "SG", "RU", "ID", "TW", "US", "VN", "TH", "ME", "PK", "CIS", "BD"]

# 📂 Account files
ACCOUNT_FILES = {
    "IND": os.path.join(BASE_DIR, "account_ind.txt"),
    "BD": os.path.join(BASE_DIR, "account_bd.txt"),
}
for _r in {"BR", "US", "SAC", "NA"}:
    ACCOUNT_FILES[_r] = os.path.join(BASE_DIR, "account_ind.txt")


# ════════════════════════════════════════════════════════════════════════════
#  CRYPTO HELPERS
# ════════════════════════════════════════════════════════════════════════════

def pad_pkcs7(data: bytes) -> bytes:
    padding_length = AES.block_size - (len(data) % AES.block_size)
    padding = bytes([padding_length] * padding_length)
    return data + padding


def aes_cbc_encrypt(key: bytes, iv: bytes, plaintext: bytes) -> bytes:
    cipher = AES.new(key, AES.MODE_CBC, iv)
    return cipher.encrypt(pad_pkcs7(plaintext))


# ════════════════════════════════════════════════════════════════════════════
#  ACCOUNT LOADER
# ════════════════════════════════════════════════════════════════════════════

def load_accounts(server_name: str):
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


# ════════════════════════════════════════════════════════════════════════════
#  JWT GENERATION (Garena real OAuth + MajorLogin flow)
# ════════════════════════════════════════════════════════════════════════════

async def get_access_token(uid: str, password: str):
    """Get OAuth access token from Garena's guest token grant endpoint."""
    url = "https://ffmconnect.live.gop.garenanow.com/oauth/guest/token/grant"
    payload = f"uid={uid}&password={password}&response_type=token&client_type=2&client_secret={CLIENT_SECRET}&client_id={CLIENT_ID}"
    headers = {
        "User-Agent": USERAGENT,
        "Connection": "Keep-Alive",
        "Accept-Encoding": "gzip",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(url, data=payload, headers=headers, timeout=20) as resp:
            data = await resp.json()
            return data.get("access_token", "0"), data.get("open_id", "0")


async def create_jwt(uid: str, password: str):
    """Generate a genuine Garena Free Fire JWT session token."""
    access_token, open_id = await get_access_token(uid, password)
    if access_token == "0":
        raise ValueError("Failed to obtain access token")

    json_data = json.dumps({
        "open_id": open_id,
        "open_id_type": "4",
        "login_token": access_token,
        "orign_platform_type": "4",
    })

    # Build LoginReq protobuf
    login_req = freefire_pb2.LoginReq()
    json_format.ParseDict(json.loads(json_data), login_req)
    encoded_result = login_req.SerializeToString()

    # AES-CBC encrypt
    payload = aes_cbc_encrypt(MAIN_KEY, MAIN_IV, encoded_result)

    # MajorLogin
    url = "https://loginbp.ggblueshark.com/MajorLogin"
    headers = {
        "User-Agent": USERAGENT,
        "Connection": "Keep-Alive",
        "Accept-Encoding": "gzip",
        "Content-Type": "application/octet-stream",
        "Expect": "100-continue",
        "X-Unity-Version": "2018.4.11f1",
        "X-GA": "v1 1",
        "ReleaseVersion": RELEASEVERSION,
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(url, data=payload, headers=headers, timeout=20) as resp:
            response_content = await resp.read()
            # Decode protobuf response
            login_res = freefire_pb2.LoginRes()
            login_res.ParseFromString(response_content)
            msg_json = json_format.MessageToJson(login_res)
            msg_dict = json.loads(msg_json)

            token = msg_dict.get("token", "0")
            region = msg_dict.get("lockRegion", "0")
            server_url = msg_dict.get("serverUrl", "0")

            if token == "0":
                raise ValueError("Failed to obtain JWT")

            return token, region, server_url


# ════════════════════════════════════════════════════════════════════════════
#  LIKE PAYLOAD (encrypted LikeProfile)
# ════════════════════════════════════════════════════════════════════════════

def create_like_payload(target_uid: int, region: str) -> bytes:
    """Build and encrypt the protobuf payload for /LikeProfile request."""
    msg = send_like_pb2.like()
    msg.uid = int(target_uid)
    msg.region = region
    protobuf_bytes = msg.SerializeToString()
    encrypted = aes_cbc_encrypt(MAIN_KEY, MAIN_IV, protobuf_bytes)
    return encrypted


# ════════════════════════════════════════════════════════════════════════════
#  ACCOUNT INFO (GetPlayerPersonalShow)
# ════════════════════════════════════════════════════════════════════════════

def get_base_url(region: str) -> str:
    if region == "IND":
        return "https://client.ind.freefiremobile.com"
    elif region in {"BR", "US", "SAC", "NA"}:
        return "https://client.us.freefiremobile.com"
    else:
        return "https://clientbp.ggblueshark.com"


async def get_account_info(target_uid: str, region: str, jwt_token: str):
    """Get player info from Garena's GetPlayerPersonalShow endpoint."""
    region_main = region.upper()
    if region_main not in SUPPORTED_REGIONS:
        return {"error": f"Unsupported region: {region}"}

    # Build GetPlayerPersonalShow protobuf
    json_data = json.dumps({"a": target_uid, "b": "0"})
    req = core_pb2.GetPlayerPersonalShow()
    json_format.ParseDict(json.loads(json_data), req)
    encoded = req.SerializeToString()
    payload = aes_cbc_encrypt(MAIN_KEY, MAIN_IV, encoded)

    # Get server URL from JWT (we need to call create_jwt again or pass it)
    # For simplicity, use the base URL
    base_url = get_base_url(region_main)
    url = f"{base_url}/GetPlayerPersonalShow"

    headers = {
        "User-Agent": USERAGENT,
        "Connection": "Keep-Alive",
        "Accept-Encoding": "gzip",
        "Content-Type": "application/octet-stream",
        "Expect": "100-continue",
        "Authorization": f"Bearer {jwt_token}",
        "X-Unity-Version": "2018.4.11f1",
        "X-GA": "v1 1",
        "ReleaseVersion": RELEASEVERSION,
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(url, data=payload, headers=headers, timeout=20) as resp:
            content = await resp.read()
            try:
                info = account_show_pb2.AccountPersonalShowInfo()
                info.ParseFromString(content)
                msg_json = json_format.MessageToJson(info)
                return json.loads(msg_json)
            except Exception:
                return {"error": f"HTTP {resp.status}", "raw": content.decode("utf-8", errors="ignore")[:200]}


# ════════════════════════════════════════════════════════════════════════════
#  LIKE SENDING
# ════════════════════════════════════════════════════════════════════════════

async def send_like_with_guest(guest: dict, target_uid: str, region: str, semaphore):
    """Send a single like using one guest account."""
    guest_uid = str(guest["uid"])
    guest_pass = guest["password"]

    async with semaphore:
        try:
            # Generate JWT for this guest
            jwt_token, lock_region, server_url = await create_jwt(guest_uid, guest_pass)

            # Build like payload
            payload = create_like_payload(int(target_uid), region)

            # Determine LikeProfile URL
            base_url = get_base_url(region)
            url = f"{base_url}/LikeProfile"

            headers = {
                "User-Agent": USERAGENT,
                "Connection": "Keep-Alive",
                "Accept-Encoding": "gzip",
                "Content-Type": "application/octet-stream",
                "Expect": "100-continue",
                "Authorization": f"Bearer {jwt_token}",
                "X-Unity-Version": "2018.4.11f1",
                "X-GA": "v1 1",
                "ReleaseVersion": RELEASEVERSION,
            }

            async with aiohttp.ClientSession() as session:
                async with session.post(url, data=payload, headers=headers, timeout=30) as resp:
                    if resp.status == 200:
                        return True, guest_uid
                    else:
                        return False, guest_uid

        except Exception as e:
            app.logger.error(f"Like error for guest {guest_uid}: {e}")
            return False, guest_uid


async def send_likes_batch(target_uid: str, region: str, accounts: list, max_likes: int = 300):
    """Send likes using multiple guest accounts concurrently."""
    semaphore = asyncio.Semaphore(50)
    tasks = []
    for guest in accounts[:max_likes]:
        tasks.append(send_like_with_guest(guest, target_uid, region, semaphore))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    success = sum(1 for r in results if isinstance(r, tuple) and r[0] is True)
    return success, len(results)


# ════════════════════════════════════════════════════════════════════════════
#  API ENDPOINTS
# ════════════════════════════════════════════════════════════════════════════

@app.route("/health", methods=["GET"])
def health_check():
    return jsonify({"status": "ok", "service": "ff-likes-api"})


@app.route("/remain", methods=["GET"])
def remain_info():
    global used_count
    remaining = max(daily_limit - used_count, 0)
    return jsonify({
        "daily_limit": daily_limit,
        "remaining": remaining,
        "used": used_count,
        "reset_info": "4:00 AM IST",
    })


@app.route("/like", methods=["GET"])
def handle_requests():
    global used_count

    # API key check
    api_key = request.args.get("key")
    if api_key not in VALID_API_KEYS:
        return jsonify({"error": "Invalid or missing API key", "status": 3}), 401

    uid = request.args.get("uid")
    region = request.args.get("region", "").upper()
    if not uid or not region:
        return jsonify({"error": "UID and region are required"}), 400

    if region not in SUPPORTED_REGIONS:
        return jsonify({"error": f"Unsupported region. Use: {', '.join(SUPPORTED_REGIONS)}"}), 400

    try:
        def process_request():
            global used_count

            # Load accounts for this region
            accounts = load_accounts(region)
            if not accounts:
                return jsonify({"error": f"No accounts available for {region}", "status": 4}), 503

            # Get account info (before likes)
            # We need a JWT first to call GetPlayerPersonalShow
            # Use the first account to get a JWT for info lookup
            info_jwt = None
            info_region = region
            for acc in accounts[:5]:
                try:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    token, lock_region, server_url = loop.run_until_complete(
                        create_jwt(acc["uid"], acc["password"])
                    )
                    loop.close()
                    if token:
                        info_jwt = token
                        info_region = lock_region or region
                        break
                except Exception:
                    continue

            if not info_jwt:
                return jsonify({"error": "Failed to generate JWT for account info", "status": 4}), 503

            # Get before likes
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            before_info = loop.run_until_complete(
                get_account_info(uid, info_region, info_jwt)
            )
            loop.close()

            if before_info.get("error"):
                return jsonify({"error": before_info["error"], "status": 4}), 503

            basic_info = before_info.get("basicInfo", {})
            before_like = int(basic_info.get("liked", 0))
            player_nickname = basic_info.get("nickname", "Unknown")
            player_level = basic_info.get("level", 0)

            # Send likes
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            success_count, total_attempted = loop.run_until_complete(
                send_likes_batch(uid, region, accounts, max_likes=300)
            )
            loop.close()

            # Get after likes
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            after_info = loop.run_until_complete(
                get_account_info(uid, info_region, info_jwt)
            )
            loop.close()

            after_like = 0
            if not after_info.get("error"):
                after_basic = after_info.get("basicInfo", {})
                after_like = int(after_basic.get("liked", 0))

            like_given = after_like - before_like
            if like_given < 0:
                like_given = 0

            status = 1 if like_given > 0 else 2

            # Count usage
            if status == 1:
                used_count += 1

            remaining = max(daily_limit - used_count, 0)

            return jsonify(OrderedDict([
                ("LikesGivenByAPI", like_given),
                ("LikesafterCommand", after_like),
                ("LikesbeforeCommand", before_like),
                ("PlayerNickname", player_nickname),
                ("Level", player_level),
                ("Region", region),
                ("UID", int(uid)),
                ("status", status),
                ("daily_limit", daily_limit),
                ("used", used_count),
                ("remaining", remaining),
            ])), 200

        return process_request()

    except Exception as e:
        app.logger.error(f"Error: {e}")
        return jsonify({"error": str(e), "status": 0}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
