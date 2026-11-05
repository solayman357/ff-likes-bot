import os
import json
import base64
import sys
import threading
import time
import requests
import aiohttp
import asyncio
from datetime import datetime, date
from datetime import time as datetime_time
from http.server import BaseHTTPRequestHandler, HTTPServer
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters, CallbackQueryHandler, TypeHandler
from telegram.request import HTTPXRequest
from telegram.error import BadRequest, NetworkError, RetryAfter, TelegramError

# Protobuf imports (local copies for Render deployment)
import uid_generator_pb2  # noqa: E402

# Add parent directory to sys.path for github_persistence module
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from github_persistence import init_github_persistence, get_github_persistence

# ============ CONFIGURATION ============
API_URL = os.getenv("API_URL", "http://127.0.0.1:5000")
ENCODED_KEY = os.getenv("ENCODED_KEY", "U29sYXltYW4=")
API_KEY = base64.b64decode(ENCODED_KEY).decode()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

# AES constants (must match api/app.py encrypt_message)
MAIN_KEY = b'Yg&tc%DEuh6%Zc^8'
MAIN_IV = b'6oyZDr22E3ychjM%'

# ============ DATA FILES ============
DATA_FILES = {
    'allowed': 'allowed_groups.json',
    'stats': 'daily_stats.json',
    'users': 'user_limits.json',
    'config': 'bot_config.json'
}

# Token upload: bot forwards tokens to API's /upload-tokens endpoint

# ============ GLOBALS ============
bot_status = "on"
bot_mode = "public"
allowed_groups = {}
daily_stats = {}
user_limits = {}
daily_limit = 2
like_tokens = 240          # tokens per /like request (mode)
force_join = False
force_join_channels = []
force_join_links = {}

forward_channel_id = ""
forward_channel_link = ""

# GitHub persistence
github_persistence = None

# ============ HELPER FUNCTIONS ============
def _read_json(path, default):
    try:
        with open(path, 'r') as f:
            return json.load(f)
    except Exception:
        return default


def load_data():
    """Load persisted data. GitHub is the source of truth when enabled."""
    global allowed_groups, daily_stats, user_limits, bot_status, bot_mode, daily_limit, like_tokens, force_join, force_join_channels, force_join_links, forward_channel_id, forward_channel_link

    if github_persistence:
        try:
            restored = github_persistence.pull_latest()
            print(f"☁️ GitHub restore: {restored} file(s) pulled", flush=True)
        except Exception as e:
            print(f"⚠️ GitHub restore failed, using local files: {e}", flush=True)

    allowed_groups = _read_json(DATA_FILES['allowed'], {})
    daily_stats = _read_json(DATA_FILES['stats'], {})
    user_limits = _read_json(DATA_FILES['users'], {})
    cfg = _read_json(DATA_FILES['config'], None)

    # Apply saved values WITHOUT resetting anything to defaults.
    if isinstance(cfg, dict):
        bot_status = cfg.get('status', bot_status)
        bot_mode = cfg.get('mode', bot_mode)
        try:
            daily_limit = int(cfg.get('limit', daily_limit))
        except Exception:
            pass
        try:
            like_tokens = int(cfg.get('like_tokens', like_tokens))
        except Exception:
            pass
        force_join = bool(cfg.get('force_join', force_join))
        force_join_channels = list(cfg.get('force_join_channels', force_join_channels) or [])
        force_join_links = dict(cfg.get('force_join_links', force_join_links) or {})
        forward_channel_id = cfg.get('forward_channel_id', forward_channel_id)
        forward_channel_link = cfg.get('forward_channel_link', forward_channel_link)

    # First run with persistence enabled: upload existing settings instead of
    # replacing them with defaults.
    if github_persistence:
        try:
            seeded = github_persistence.seed_missing()
            if seeded:
                print(f"☁️ GitHub seed: {seeded} file(s) uploaded", flush=True)
        except Exception as e:
            print(f"⚠️ GitHub seed failed: {e}", flush=True)

    print(f"⚙️ Loaded settings: status={bot_status} mode={bot_mode} "
          f"limit={daily_limit} like_tokens={like_tokens} "
          f"force_join={force_join} channels={len(force_join_channels)}", flush=True)

def save_all():
    try:
        with open(DATA_FILES['allowed'], 'w') as f: json.dump(allowed_groups, f, indent=2)
        with open(DATA_FILES['stats'], 'w') as f: json.dump(daily_stats, f, indent=2)
        with open(DATA_FILES['users'], 'w') as f: json.dump(user_limits, f, indent=2)
        with open(DATA_FILES['config'], 'w') as f:
            json.dump({
                'status': bot_status,
                'mode': bot_mode,
                'limit': daily_limit,
                'like_tokens': like_tokens,
                'force_join': force_join,
                'force_join_channels': force_join_channels,
                'force_join_links': force_join_links,
                'forward_channel_id': forward_channel_id,
                'forward_channel_link': forward_channel_link,
            }, f, indent=2)
    except Exception as e:
        print(f"⚠️ save_all local write failed: {e}", flush=True)

    # Queue for GitHub sync (background, retried, never blocks the bot).
    if github_persistence:
        for key in ('allowed', 'stats', 'users', 'config'):
            try:
                github_persistence.queue_push(key)
            except Exception as e:
                print(f"⚠️ GitHub queue failed for {key}: {e}", flush=True)

FORCE_JOIN_TEXT = (
    "🔒 *JOIN ALL REQUIRED CHANNELS*\n\n"
    "To use FF LIKES PRO, please join every channel/group below.\n\n"
    "1️⃣ Tap each button and join\n"
    "2️⃣ Come back and tap ✅ I'VE JOINED - VERIFY"
)


def build_force_join_keyboard() -> InlineKeyboardMarkup:
    """One URL button per configured channel, then the verify button."""
    keyboard = []
    for channel in force_join_channels:
        link = (force_join_links.get(str(channel)) or '').strip()
        if link:
            keyboard.append([InlineKeyboardButton(f"📢 Join {channel}", url=link)])
        else:
            keyboard.append([InlineKeyboardButton(f"⚠️ {channel} (no link set)", callback_data="forcejoin_noop")])
    keyboard.append([InlineKeyboardButton("✅ I'VE JOINED - VERIFY", callback_data="verify_join")])
    return InlineKeyboardMarkup(keyboard)


def _member_is_joined(member) -> bool:
    """Accept member/admin/creator, and restricted users who are still members."""
    status = getattr(member, "status", "")
    status = getattr(status, "value", status)  # enum -> str
    if status in ("member", "administrator", "creator"):
        return True
    if status == "restricted" and getattr(member, "is_member", False):
        return True
    return False


async def is_user_member_single(context: ContextTypes.DEFAULT_TYPE, user_id: int, channel: str) -> bool:
    """Check membership in one channel. Errors are treated as 'not joined'."""
    try:
        member = await context.bot.get_chat_member(chat_id=channel, user_id=user_id)
        return _member_is_joined(member)
    except Exception as e:
        print(f"⚠️ getChatMember failed for '{channel}': {e}", flush=True)
        return False


async def get_missing_channels(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    """Return the list of required channels the user has NOT joined."""
    missing = []
    for channel in force_join_channels:
        if not await is_user_member_single(context, user_id, channel):
            missing.append(channel)
    return missing


async def is_user_member(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    """True only when the user is a member of EVERY required channel."""
    if not force_join or not force_join_channels:
        return True
    return len(await get_missing_channels(context, user_id)) == 0


async def force_join_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Block access until the user has joined ALL required channels."""
    if not force_join or not force_join_channels:
        return True
    user = update.effective_user
    if user is None:
        return True
    if is_admin(user.id):
        return True

    missing = await get_missing_channels(context, user.id)
    if not missing:
        return True

    reply_markup = build_force_join_keyboard()
    text = FORCE_JOIN_TEXT
    try:
        if update.callback_query is not None:
            await update.callback_query.edit_message_text(text, reply_markup=reply_markup, parse_mode='Markdown')
        elif update.message is not None:
            await update.message.reply_text(text, reply_markup=reply_markup, parse_mode='Markdown')
    except Exception as e:
        print(f"Force join reply error: {e}", flush=True)
    return False


def is_admin(uid): return uid in ADMIN_IDS
def today_str(): return str(date.today())

def can_user_like(uid):
    if is_admin(uid):
        return True
    t = today_str()
    suid = str(uid)
    if suid not in user_limits or user_limits[suid]['date'] != t:
        user_limits[suid] = {'date': t, 'count': 0}
        return True
    return user_limits[suid]['count'] < daily_limit

def update_user_like(uid):
    if is_admin(uid):
        return
    t = today_str()
    suid = str(uid)
    if suid not in user_limits or user_limits[suid]['date'] != t:
        user_limits[suid] = {'date': t, 'count': 0}
    user_limits[suid]['count'] += 1
    
    if t not in daily_stats:
        daily_stats[t] = {'total': 0, 'users': {}}
    daily_stats[t]['total'] += 1
    if suid not in daily_stats[t]['users']:
        daily_stats[t]['users'][suid] = 0
    daily_stats[t]['users'][suid] += 1
    save_all()

async def call_like_api(server_name, uid, limit=None):
    try:
        base = API_URL.rstrip("/") + "/"
        url = f"{base}like?uid={uid}&server_name={server_name}&key={API_KEY}"
        if limit:
            url += f"&limit={int(limit)}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=180)) as resp:
                if resp.status == 200:
                    return await resp.json()
                else:
                    # Surface the API's real error message instead of a bare HTTP code.
                    try:
                        body = await resp.json(content_type=None)
                    except Exception:
                        body = None
                    if isinstance(body, dict) and body.get("error"):
                        body.setdefault("status", body.get("status", resp.status))
                        return body
                    return {"error": f"HTTP {resp.status}"}
    except asyncio.TimeoutError:
        return {"error": "Timeout"}
    except Exception as e:
        return {"error": str(e)}

def is_group_allowed(chat_id, chat_type):
    if chat_type == "private":
        return True
    if bot_mode == "public":
        return True
    return str(chat_id) in allowed_groups

async def reset_midnight(context: ContextTypes.DEFAULT_TYPE):
    global user_limits
    user_limits = {}
    save_all()
    print(f"[{datetime.now()}] ✅ Daily limits reset")

async def block_non_admin_private(update: Update) -> bool:
    chat_type = update.effective_chat.type
    user_id = update.effective_user.id
    if chat_type == "private" and not is_admin(user_id):
        await update.message.reply_text("🚫 *Bot works only in groups!*\n(Admins can use in private)")
        return True
    return False

async def reply(update, text):
    try:
        await update.message.reply_text(text)
    except Exception as e:
        print(f"Reply failed: {e}", flush=True)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log errors and notify user with actual error details."""
    import traceback
    err = context.error
    print(f"Exception while handling update: {err}", flush=True)
    traceback.print_exc()
    err_type = type(err).__name__
    err_msg = str(err)[:200]
    try:
        if hasattr(update, 'effective_message') and update.effective_message:
            await update.effective_message.reply_text(
                f"❌ Bot error: {err_type}\n{err_msg}\n\nPlease contact the bot admin."
            )
    except Exception:
        pass

# ============ USER COMMANDS ============
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command."""
    if not await force_join_check(update, context): return
    if bot_status == "off":
        await reply(update, "🔴 Bot is currently OFF")
        return
    # Register this chat for broadcasts (users + groups)
    register_chat(update)
    msg = (
        "✨ *𝑭𝑹𝑬𝑬 𝑭𝑰𝑹𝑬 𝑳𝑰𝑲𝑬 𝑩𝑶𝑻* ✨\n"
        "⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n"
        "💬 `/like REGION UID` – Send a like\n"
        "💬 `/help` – Show all commands\n"
        "💬 `/info` – Your remaining likes\n\n"
        "📌 *Example:* `/like BD UID`\n"
        f"🔥 Your daily limit: `{daily_limit}` likes\n"
        "🌍 *Any region code works* (e.g., IND, USA, GER, etc.)\n"
        "⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯"
    )
    await reply(update, msg)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await force_join_check(update, context): return
    if bot_status == "off":
        await reply(update, "🔴 Bot is OFF")
        return
    msg = (
        "📖 *𝑪𝑶𝑴𝑴𝑨𝑵𝑫 𝑳𝑰𝑺𝑻*\n\n"
        "🔹 `/like REGION UID` – Send likes\n"
        "🔹 `/info` – Check your remaining likes\n"
        "🔹 `/start` – Welcome message\n\n"
        "*Example:* `/like BD UID`\n"
        "*Supports any region* – just type the code.\n\n"
        "👑 *Admin commands:*\n"
        "`/allow` – Allow current group\n"
        "`/off` / `/on` – Turn bot off/on\n"
        "`/stats` – Today's usage\n"
        "`/setprivate` / `/setpublic` – Change group mode\n"
        "`/setlimit <num>` – Set daily limit per user\n"
        "`/setapi100` – Set to 100 tokens (~100–110 likes)\n"
        "`/setapi <num>` – Set custom tokens (10–400)\n"
        "`/setapimax` or `/maxff` – Set to 400 tokens (max)\n"
        "`/setforcejoin <channel> <link>` – Add required channel\n"
        "`/addforcejoin <channel> <link>` – Add another required channel\n"
        "`/removeforcejoin <channel>` – Remove one required channel\n"
        "`/removeforcejoin all` – Remove all required channels\n"
        "`/forcejoinlist` – List required channels\n"
        "`/forcejoinstatus` – Show force-join configuration\n"
        "`/forcejoinon` / `/forcejoinoff` – Enable/disable force join\n"
        "`/broadcast <message>` – Send message to all users and groups"
    )
    await reply(update, msg)

async def info_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await force_join_check(update, context): return
    if bot_status == "off":
        await reply(update, "🔴 Bot is OFF")
        return
    uid = update.effective_user.id
    if is_admin(uid):
        await reply(update, "👑 *Admin Account*\n🔥 *Unlimited likes* – No daily limit.")
        return
    t = today_str()
    suid = str(uid)
    used = user_limits.get(suid, {}).get('count', 0) if suid in user_limits and user_limits[suid]['date'] == t else 0
    remaining = daily_limit - used
    msg = (
        "🤖 *𝑩𝑶𝑻 𝑰𝑵𝑭𝑶*\n\n"
        f"⚙️ Mode: {bot_mode.upper()}\n"
        f"🟢 Status: {bot_status.upper()}\n"
        f"📅 Daily limit: {daily_limit} likes\n"
        f"✅ Used today: {used}\n"
        f"🟢 Remaining: {remaining}\n"
        f"👥 Allowed groups: {len(allowed_groups)}"
    )
    await reply(update, msg)

async def like_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /like REGION UID command."""
    if await block_non_admin_private(update): return
    if not await force_join_check(update, context):
        return
    if bot_status == "off":
        await reply(update, "🔴 Bot is currently OFF")
        return

    try:
        await _like_cmd_inner(update, context)
    except Exception as e:
        import traceback
        print(f"❌ Uncaught exception in like_cmd: {e}", flush=True)
        traceback.print_exc()
        await reply(update, f"❌ Like command error: {type(e).__name__}\n{str(e)[:200]}")


async def _like_cmd_inner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Inner like command logic (separated for error handling)."""
    chat_id = update.effective_chat.id
    chat_type = update.effective_chat.type
    if chat_type != "private" and not is_group_allowed(chat_id, chat_type):
        await reply(update, "🚫 This bot works only in allowed groups!")
        return

    if len(context.args) != 2:
        await reply(update, "❌ Usage: /like REGION UID\nExample: /like BD UID")
        return

    region = context.args[0].upper()
    uid = context.args[1]
    if not uid.isdigit():
        await reply(update, "❌ UID must contain only numbers!")
        return

    user_id = update.effective_user.id
    if not can_user_like(user_id):
        used = user_limits.get(str(user_id), {}).get('count', 0)
        await reply(update, f"⚠️ Daily limit reached!\nYou have used `{used}/{daily_limit}` likes today.\n💡 Try again tomorrow.")
        return

    # Send processing message (protected)
    proc_msg = None
    try:
        proc_msg = await update.message.reply_text(
            f"🔄 Processing...\nUID: {uid}\nRegion: {region}\n🔢 Tokens: {like_tokens}"
        )
    except (BadRequest, NetworkError, RetryAfter, TelegramError) as e:
        import traceback
        print(f"❌ Failed to send processing message in like_cmd: {e}", flush=True)
        traceback.print_exc()
        await reply(update, f"❌ Failed to start like request. Telegram error: {e}")
        return
    except Exception as e:
        import traceback
        print(f"❌ Unexpected error sending processing message: {e}", flush=True)
        traceback.print_exc()
        await reply(update, f"❌ Failed to start like request: {e}")
        return

    # Call the API
    data = await call_like_api(region, uid, like_tokens)

    # Helper to safely edit or fall back to new message
    async def _respond(text, fallback_text=None):
        """Try to edit proc_msg; if that fails, send a new message."""
        sent = None
        if proc_msg is not None:
            try:
                await proc_msg.edit_text(text)
                return proc_msg
            except (BadRequest, NetworkError, RetryAfter, TelegramError) as e:
                import traceback
                print(f"❌ Failed to edit processing message in like_cmd: {e}", flush=True)
                traceback.print_exc()
            except Exception as e:
                import traceback
                print(f"❌ Unexpected error editing processing message: {e}", flush=True)
                traceback.print_exc()
        # Fallback: send new message
        try:
            sent = await update.message.reply_text(fallback_text or text)
            return sent
        except (BadRequest, NetworkError, RetryAfter, TelegramError) as e:
            import traceback
            print(f"❌ Failed to send fallback message in like_cmd: {e}", flush=True)
            traceback.print_exc()
        except Exception as e:
            import traceback
            print(f"❌ Unexpected error sending fallback message: {e}", flush=True)
            traceback.print_exc()
        return None

    # API returned an error
    if data is None or "error" in data:
        error_msg = data.get("error", "Unknown error") if data else "No response"
        if data and data.get("status") == 4:
            await _respond(f"🔑 Token Bank Error\n{error_msg}")
        else:
            await _respond(f"❌ API Error!\n{error_msg}\nPlease try again later.")
        return

    # Handle error responses from ZIP API
    if data.get("error"):
        error_msg = data.get("error", "Unknown error")
        if data.get("status") == 0:
            await _respond(f"❌ API Error!\n{error_msg}\nPlease try again later.")
        else:
            await _respond(f"❌ API Error!\n{error_msg}")
        return

    status = data.get('status')
    if status is None:
        await _respond("❌ Invalid API response\nThe server returned an unexpected format.")
        return

    player = data.get('PlayerNickname', 'Unknown')
    uid_resp = data.get('UID', uid)
    region_resp = region
    level = data.get('Level', 'N/A')
    before = data.get('LikesbeforeCommand', 0)
    after = data.get('LikesafterCommand', 0)
    given = data.get('LikesGivenByAPI', 0)
    api_rem = data.get('remains', 'N/A')

    if status == 1:
        update_user_like(user_id)
        user_used = user_limits.get(str(user_id), {}).get('count', 0) if not is_admin(user_id) else "Unlimited"
        result = (
            f"✅ LIKE SENT ✅\n\n"
            f"👤 Player: {player}\n"
            f"🏷️ UID: {uid_resp}\n"
            f"🌍 Region: {region_resp}\n"
            f"⭐ Level: {level}\n\n"
            f"❤️ Likes given: +{given}\n"
            f"📊 Total: {before} → {after}\n\n"
            f"📅 Your usage: {user_used}/{daily_limit if not is_admin(user_id) else '∞'}\n"
            f"🎉 Success!"
        )
    elif status == 2:
        result = (
            f"⚠️ LIMIT REACHED ⚠️\n\n"
            f"👤 Player: {player}\n"
            f"🏷️ UID: {uid_resp}\n"
            f"🌍 Region: {region_resp}\n"
            f"⭐ Level: {level}\n\n"
            f"❌ Likes sent: 0\n"
            f"📊 Total likes: {before} (unchanged)\n\n"
            f"📅 API remaining: {api_rem}\n\n"
            f"😔 Could not send like to {player}"
        )
    else:
        result = f"❓ Unknown API response\nStatus code: {status}\nPlease contact the bot admin."

    # Reply to the user and capture the sent message (for native forwarding)
    sent_msg = await _respond(result)

    # Forward the EXACT success result message to the configured channel.
    # Native forwardMessage preserves the original formatting exactly.
    if status == 1 and forward_channel_id and sent_msg is not None:
        try:
            await context.bot.forward_message(
                chat_id=forward_channel_id,
                from_chat_id=sent_msg.chat.id,
                message_id=sent_msg.message_id,
            )
        except Exception as e:
            # Log the error but never break the successful like operation.
            print(f"⚠️ Failed to forward successful like to {forward_channel_id}: {e}", flush=True)

# ============ JWT TOKEN UPLOAD HANDLER ============
# Broadcast list file
BROADCAST_FILE = "broadcast_list.json"

# Successful likes log file
LIKE_LOG_FILE = "successful_likes.json"


def _load_broadcast_data():
    data = _read_json(BROADCAST_FILE, None)
    if not isinstance(data, dict):
        data = {}
    data.setdefault("chats", {})
    data.setdefault("users", {})
    return data


def _save_broadcast_data(data):
    try:
        with open(BROADCAST_FILE, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"⚠️ Broadcast list write failed: {e}", flush=True)
        return
    if github_persistence:
        try:
            github_persistence.queue_push('broadcast')
        except Exception as e:
            print(f"⚠️ GitHub queue failed for broadcast: {e}", flush=True)


def add_to_broadcast(chat_id, chat_type, chat_title=None):
    """Register a group/supergroup/channel chat for broadcasts."""
    data = _load_broadcast_data()
    cid = str(chat_id)
    entry = data["chats"].get(cid)
    if entry is None:
        data["chats"][cid] = {
            "chat_id": chat_id,
            "chat_type": chat_type,
            "title": chat_title or "",
            "added_at": str(date.today()),
        }
        _save_broadcast_data(data)
    elif chat_title and entry.get("title") != chat_title:
        entry["title"] = chat_title
        entry["chat_type"] = chat_type
        _save_broadcast_data(data)


def add_user_to_broadcast(user_id, username=None, first_name=None):
    """Register a private user chat for broadcasts."""
    data = _load_broadcast_data()
    uid = str(user_id)
    entry = data["users"].get(uid)
    if entry is None:
        data["users"][uid] = {
            "user_id": user_id,
            "username": username or "",
            "first_name": first_name or "",
            "added_at": str(date.today()),
        }
        _save_broadcast_data(data)
    else:
        changed = False
        if username and entry.get("username") != username:
            entry["username"] = username
            changed = True
        if first_name and entry.get("first_name") != first_name:
            entry["first_name"] = first_name
            changed = True
        if changed:
            _save_broadcast_data(data)


def register_chat(update: Update):
    """Save every user/group that interacts with the bot (for broadcasts)."""
    try:
        chat = update.effective_chat
        if chat is None:
            return
        if chat.type == "private":
            add_user_to_broadcast(chat.id, getattr(chat, "username", None), getattr(chat, "first_name", None))
        else:
            add_to_broadcast(chat.id, chat.type, getattr(chat, "title", None))
    except Exception as e:
        print(f"⚠️ register_chat failed: {e}", flush=True)


async def register_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """TypeHandler: runs for every update, before command handlers."""
    register_chat(update)


def log_successful_like(uid, region, likes_given, before, after, user_id):
    """Log a successful like to the log file."""
    try:
        with open(LIKE_LOG_FILE, "r") as f:
            data = json.load(f)
    except Exception:
        data = {"likes": []}

    data["likes"].append({
        "target_uid": uid,
        "region": region,
        "likes_given": likes_given,
        "before": before,
        "after": after,
        "requested_by": user_id,
        "timestamp": str(date.today())
    })

    with open(LIKE_LOG_FILE, "w") as f:
        json.dump(data, f, indent=2)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await reply(update, "❌ Admin only command")
        return

    document = update.message.document
    if not document:
        await reply(update, "❌ No document found.")
        return

    file_name = document.file_name or ""
    if not file_name.endswith(".json"):
        await reply(update, "❌ Please upload a valid JSON file.")
        return

    try:
        file = await context.bot.get_document(document.file_id)
        file_bytes = await file.download_as_bytearray()

        try:
            new_tokens = json.loads(file_bytes.decode('utf-8'))
        except Exception as e:
            await reply(update, f"❌ Invalid JSON file: `{str(e)[:100]}`")
            return

        # Forward tokens to API's /upload-tokens endpoint
        upload_url = f"{API_URL.rstrip('/')}/upload-tokens"
        payload = {"tokens": new_tokens}

        async with aiohttp.ClientSession() as session:
            async with session.post(upload_url, json=payload, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                result = await resp.json()

        if resp.status == 200:
            await reply(update,
                f"✅ Token Update Complete\n\n"
                f"✅ Added: {result.get('added', 0)}\n"
                f"❌ Expired: {result.get('expired', 0)}\n"
                f"📁 Total in bank: {result.get('total_in_bank', 0)}"
            )
        else:
            error_msg = result.get("error", "Unknown error") if isinstance(result, dict) else str(result)
            await reply(update, f"❌ API Error: `{error_msg}`")

    except Exception as e:
        await reply(update, f"❌ Error processing file: `{str(e)[:100]}`")


async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Broadcast an exact message to every registered user and group. Admin only."""
    if not is_admin(update.effective_user.id):
        await reply(update, "❌ Admin only")
        return

    if not context.args:
        await reply(update, "❌ Usage: `/broadcast <message>`\nExample: `/broadcast Hello everyone!`")
        return

    # Preserve the exact text, including newlines and formatting.
    raw_text = (update.message.text or "") if update.message else ""
    parts = raw_text.split(None, 1)
    message = parts[1] if len(parts) > 1 else " ".join(context.args)
    if not message.strip():
        await reply(update, "❌ Broadcast message is empty.")
        return

    data = _load_broadcast_data()
    targets = []
    seen = set()
    for source in (data.get("chats", {}), data.get("users", {})):
        for raw_id in source.keys():
            try:
                chat_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            if chat_id in seen:
                continue
            seen.add(chat_id)
            targets.append(chat_id)

    if not targets:
        await reply(
            update,
            "❌ No registered users or groups yet.\n\n"
            "Recipients are added automatically whenever someone sends /start "
            "or interacts with the bot. Try again after users have used the bot."
        )
        return

    sent = 0
    failed = 0
    failed_ids = []

    for chat_id in targets:
        try:
            await context.bot.send_message(chat_id=chat_id, text=message)
            sent += 1
        except RetryAfter as e:
            try:
                await asyncio.sleep(min(int(getattr(e, "retry_after", 1)), 5))
                await context.bot.send_message(chat_id=chat_id, text=message)
                sent += 1
            except Exception:
                failed += 1
                failed_ids.append(chat_id)
        except Exception as e:
            failed += 1
            failed_ids.append(chat_id)
            print(f"⚠️ Broadcast failed for {chat_id}: {e}", flush=True)

    report = (
        "📢 *Broadcast Report*\n\n"
        f"👥 Total recipients: `{len(targets)}`\n"
        f"✅ Successful deliveries: `{sent}`\n"
        f"❌ Failed deliveries: `{failed}`"
    )
    if failed_ids:
        report += "\n\n*Failed chat IDs:*\n" + "\n".join(f"`{i}`" for i in failed_ids[:20])
    await reply(update, report)

# ============ ADMIN COMMANDS ============
async def allow_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await reply(update, "❌ *Admin only command*")
        return
    chat = update.effective_chat
    if chat.type == "private":
        await reply(update, "❌ Use this command in a group")
        return
    gid = str(chat.id)
    allowed_groups[gid] = {'name': chat.title, 'by': update.effective_user.id, 'date': today_str()}
    save_all()
    await reply(update, f"✅ *Group allowed*\n{chat.title}\nBot will now work here (if private mode)")

async def off_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await reply(update, "❌ Admin only")
        return
    global bot_status
    bot_status = "off"
    save_all()
    await reply(update, "🔴 *Bot is now OFF* (no commands work)")

async def on_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await reply(update, "❌ Admin only")
        return
    global bot_status
    bot_status = "on"
    save_all()
    await reply(update, "🟢 *Bot is now ON*")

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await force_join_check(update, context):
        return
    if not is_admin(update.effective_user.id):
        await reply(update, "❌ Admin only")
        return
    t = today_str()
    if t not in daily_stats:
        await reply(update, "📊 *No stats for today*")
        return
    total = daily_stats[t]['total']
    users_count = len(daily_stats[t]['users'])
    msg = (
        f"📊 *𝑻𝑶𝑫𝑨𝒀'𝑺 𝑺𝑻𝑨𝑻𝑺*\n\n"
        f"📅 Date: `{t}`\n"
        f"❤️ Total likes sent: `{total}`\n"
        f"👥 Users: `{users_count}`\n"
        f"⚙️ Limit per user: `{daily_limit}`\n"
        f"🎯 Mode: `{bot_mode.upper()}`"
    )
    await reply(update, msg)

async def set_private(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await reply(update, "❌ Admin only")
        return
    global bot_mode
    bot_mode = "private"
    save_all()
    await reply(update, "🔒 *Bot is now PRIVATE* – works only in allowed groups")

async def set_public(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await reply(update, "❌ Admin only")
        return
    global bot_mode
    bot_mode = "public"
    save_all()
    await reply(update, "🌍 *Bot is now PUBLIC* – works in all groups")

async def set_limit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await reply(update, "❌ Admin only")
        return
    if len(context.args) != 1 or not context.args[0].isdigit():
        await reply(update, "❌ Usage: `/setlimit <number>`\nExample: `/setlimit 5`")
        return
    global daily_limit
    daily_limit = int(context.args[0])
    save_all()
    await reply(update, f"✅ *Daily limit set to `{daily_limit}` likes per user*")


# ============ LIKE MODE COMMANDS ============
async def set_api_100(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """100-token mode (~100-110 likes per request)."""
    if not is_admin(update.effective_user.id):
        await reply(update, "❌ Admin only")
        return
    global like_tokens
    like_tokens = 100
    save_all()
    await reply(update,
                "✅ *API 100 mode ON*\n\n"
                "🔢 Tokens per request: `100`\n"
                "❤️ Expected likes: `~100-110`\n\n"
                "Now send `/like BD UID`")


# ============ FORCE JOIN COMMANDS ============
def _normalize_channel(value: str) -> str:
    return (value or "").strip()


def _find_channel_index(channel: str) -> int:
    """Find a configured channel by exact or case-insensitive username match."""
    for i, existing in enumerate(force_join_channels):
        if existing == channel:
            return i
    target = channel.lower().lstrip("@")
    for i, existing in enumerate(force_join_channels):
        if existing.lower().lstrip("@") == target:
            return i
    return -1


def _format_force_join_list() -> str:
    if not force_join_channels:
        return "  None configured"
    lines = []
    for i, ch in enumerate(force_join_channels, start=1):
        link = force_join_links.get(str(ch), "") or "No link set"
        lines.append(f"  {i}. `{ch}`\n     🔗 {link}")
    return "\n".join(lines)


async def _add_force_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await reply(update, "❌ Admin only")
        return

    global force_join
    if len(context.args) < 1:
        await reply(
            update,
            "❌ Usage:\n`/setforcejoin <channel_username_or_id> <invite_link>`\n\n"
            "Example:\n`/setforcejoin @yourchannel https://t.me/+xxxxx`\n\n"
            "Add more with `/addforcejoin <channel> <link>`."
        )
        return

    channel = _normalize_channel(context.args[0])
    link = _normalize_channel(context.args[1]) if len(context.args) > 1 else ""

    # Be forgiving if the admin passes link first.
    if channel.lower().startswith(("http://", "https://", "tg://")) and link and not link.lower().startswith(("http://", "https://", "tg://")):
        channel, link = link, channel

    if not channel:
        await reply(update, "❌ Invalid channel.")
        return

    index = _find_channel_index(channel)
    if index >= 0:
        old = force_join_channels[index]
        force_join_channels[index] = channel
        if old != channel:
            force_join_links.pop(str(old), None)
        force_join_links[str(channel)] = link
        action = "updated"
    else:
        force_join_channels.append(channel)
        force_join_links[str(channel)] = link
        action = "added"

    force_join = True
    save_all()

    note = "" if link else "\n\n⚠️ No invite link set. Add one so users get a Join button."
    await reply(
        update,
        f"✅ *Force Join Channel {action.title()}*\n\n"
        f"📢 Channel: `{channel}`\n"
        f"🔗 Link: {link if link else 'Not provided'}\n"
        f"🔢 Total required channels: `{len(force_join_channels)}`\n"
        f"🟢 Force join: Enabled{note}"
    )


async def setforcejoin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add the first/another required channel."""
    await _add_force_join(update, context)


async def addforcejoin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add another required channel (alias of /setforcejoin)."""
    await _add_force_join(update, context)


async def removeforcejoin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove one required channel, or all of them."""
    if not is_admin(update.effective_user.id):
        await reply(update, "❌ Admin only")
        return

    global force_join
    if len(context.args) < 1:
        await reply(
            update,
            "❌ Usage:\n`/removeforcejoin <channel_username_or_id>`\n"
            "`/removeforcejoin all`\n\n"
            "Example:\n`/removeforcejoin @yourchannel`"
        )
        return

    target = _normalize_channel(context.args[0])

    if target.lower() == "all":
        count = len(force_join_channels)
        force_join_channels.clear()
        force_join_links.clear()
        force_join = False
        save_all()
        await reply(update, f"✅ *All Force Join Channels Removed*\n\nRemoved: `{count}`\n🟢 Force join: Disabled")
        return

    index = _find_channel_index(target)
    if index < 0:
        await reply(update, f"❌ Channel `{target}` is not in the force-join list.\n\nUse `/forcejoinlist` to see configured channels.")
        return

    removed = force_join_channels.pop(index)
    force_join_links.pop(str(removed), None)
    if not force_join_channels:
        force_join = False
    save_all()
    await reply(
        update,
        f"✅ *Force Join Channel Removed*\n\n"
        f"📢 Channel: `{removed}`\n"
        f"🔢 Remaining required channels: `{len(force_join_channels)}`\n"
        f"🟢 Force join: {'Enabled' if force_join else 'Disabled'}"
    )


async def forcejoinlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await reply(update, "❌ Admin only")
        return
    await reply(
        update,
        f"📋 *Force Join Channels*\n\n"
        f"🟢 Force join: {'Enabled' if force_join else 'Disabled'}\n"
        f"🔢 Total: `{len(force_join_channels)}`\n\n"
        f"{_format_force_join_list()}"
    )


async def forcejoinstatus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the real saved configuration and verify each channel is reachable."""
    if not is_admin(update.effective_user.id):
        await reply(update, "❌ Admin only")
        return

    if not force_join_channels:
        await reply(
            update,
            "📊 *Force Join Status*\n\n"
            f"Status: {'✅ Enabled' if force_join else '❌ Disabled'}\n"
            "Required channels: `0`\n\n"
            "No channels configured. Add one with `/setforcejoin <channel> <link>`."
        )
        return

    lines = []
    for ch in force_join_channels:
        link = force_join_links.get(str(ch), "") or "No link set"
        try:
            chat = await context.bot.get_chat(ch)
            title = getattr(chat, "title", None) or getattr(chat, "username", None) or "unknown"
            lines.append(f"• `{ch}` — ✅ reachable\n   {title} (id: `{chat.id}`)\n   🔗 {link}")
        except Exception as e:
            lines.append(f"• `{ch}` — ⚠️ not reachable by bot\n   {type(e).__name__}: {str(e)[:80]}\n   🔗 {link}")

    await reply(
        update,
        "📊 *Force Join Status*\n\n"
        f"Status: {'✅ Enabled' if force_join else '❌ Disabled (channels saved but force join is OFF)'}\n"
        f"Required channels: `{len(force_join_channels)}`\n\n"
        + "\n".join(lines)
    )


async def forcejoinon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await reply(update, "❌ Admin only")
        return
    global force_join
    if not force_join_channels:
        await reply(update, "❌ No channels configured. Add one with `/setforcejoin <channel> <link>` first.")
        return
    force_join = True
    save_all()
    await reply(update, f"🟢 *Force Join Enabled*\n\nRequired channels: `{len(force_join_channels)}`")


async def forcejoinoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await reply(update, "❌ Admin only")
        return
    global force_join
    force_join = False
    save_all()
    await reply(update, "🔴 *Force Join Disabled*\n\nSaved channels are kept. Re-enable with `/forcejoinon`.")


async def verify_join_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if is_admin(user_id):
        await query.edit_message_text("✅ Admin verified. You can use the bot now.")
        return

    if not force_join or not force_join_channels:
        await query.edit_message_text("✅ Force join is disabled. You can use the bot now.")
        return

    # Always check real membership in EVERY channel. Never trust the click.
    missing = await get_missing_channels(context, user_id)
    if not missing:
        await query.edit_message_text(
            "✅ *Verification Successful!*\n\n"
            "You can now use the bot.\n"
            "Send /start or /like to continue.",
            parse_mode='Markdown'
        )
        return

    missing_lines = "\n".join(f"• `{ch}`" for ch in missing)
    await query.edit_message_text(
        "❌ *Verification Failed*\n\n"
        "You still need to join:\n"
        f"{missing_lines}\n\n"
        "Join every channel above, then tap ✅ I'VE JOINED - VERIFY again.",
        reply_markup=build_force_join_keyboard(),
        parse_mode='Markdown'
    )


async def forcejoin_noop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("No invite link is configured for this channel. Ask an admin.", show_alert=True)


# ============ LIKE MODE COMMANDS ============
async def set_api_100(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """100-token mode (~100-110 likes per request)."""
    if not is_admin(update.effective_user.id):
        await reply(update, "❌ Admin only")
        return
    global like_tokens
    like_tokens = 100
    save_all()
    await reply(update,
                "✅ *API 100 mode ON*\n\n"
                "🔢 Tokens per request: `100`\n"
                "❤️ Expected likes: `~100-110`\n\n"
                "Now send `/like BD UID`")


async def set_api_max(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """MAX mode (400 tokens per request)."""
    if not is_admin(update.effective_user.id):
        await reply(update, "❌ Admin only")
        return
    global like_tokens
    like_tokens = 400
    save_all()
    await reply(update,
                "✅ *MAX FF mode ON*\n\n"
                "🔢 Tokens per request: `400`\n"
                "❤️ Likes: as many as the 400 tokens can give\n\n"
                "Now send `/like BD UID`")


async def set_api(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set custom token count per request."""
    if not is_admin(update.effective_user.id):
        await reply(update, "❌ Admin only")
        return
    if len(context.args) != 1 or not context.args[0].isdigit():
        await reply(update, "❌ Usage: `/setapi <number>`\nExample: `/setapi 111`\nMax: 400")
        return
    global like_tokens
    tokens = int(context.args[0])
    if tokens < 10:
        tokens = 10
    elif tokens > 400:
        tokens = 400
    like_tokens = tokens
    save_all()
    await reply(update,
                f"✅ *API Custom Mode ON*\n\n"
                f"🔢 Tokens per request: `{tokens}`\n"
                f"❤️ Expected likes: ~{tokens}\n\n"
                f"Now send `/like BD UID`")


# ============ RENDER HEALTH SERVER (free web_service must bind $PORT) ============
class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/health"):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        # Silence default stderr logging to keep Render logs clean
        return


def start_health_server():
    """Run a lightweight HTTP health server on $PORT in a daemon thread.
    Required by Render free web_service (no Background Worker on free plan).
    Telegram polling keeps running on the main thread via app.run_polling().
    """
    port = int(os.environ.get("PORT", "10000"))
    for try_port in (port, 10000, 8080, 5000):
        try:
            server = HTTPServer(("0.0.0.0", try_port), _HealthHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            print(f"🌐 Health server listening on 0.0.0.0:{try_port}", flush=True)
            return
        except Exception as e:
            print(f"⚠️ Health server port {try_port} failed: {e}", flush=True)
            continue
    print("❌ Health server could not bind to any port", flush=True)


# ============ MAIN ============
def main():
    global github_persistence
    # Initialize GitHub persistence
    github_persistence = init_github_persistence()
    
    # Start health server first so Render can reach us even if polling is down
    try:
        start_health_server()
    except Exception as e:
        print(f"❌ Health server failed: {e}", flush=True)

    try:
        load_data()
    except Exception as e:
        print(f"❌ load_data failed: {e}", flush=True)

    # Build the Telegram application
    try:
        request = HTTPXRequest(connect_timeout=30.0, read_timeout=30.0)
        app = Application.builder().token(BOT_TOKEN).request(request).build()

        job_queue = app.job_queue
        if job_queue:
            job_queue.run_daily(reset_midnight, time=datetime_time(hour=0, minute=0, second=0), days=tuple(range(7)))
            print("⏰ Midnight reset scheduled", flush=True)

        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("help", help_cmd))
        app.add_handler(CommandHandler("info", info_cmd))
        app.add_handler(CommandHandler("like", like_cmd))
        app.add_handler(CommandHandler("allow", allow_group))
        app.add_handler(CommandHandler("off", off_cmd))
        app.add_handler(CommandHandler("on", on_cmd))
        app.add_handler(CommandHandler("stats", stats_cmd))
        app.add_handler(CommandHandler("setprivate", set_private))
        app.add_handler(CommandHandler("setpublic", set_public))
        app.add_handler(CommandHandler("setlimit", set_limit))
        app.add_handler(CommandHandler("setapi100", set_api_100))
        app.add_handler(CommandHandler("setapi", set_api))
        app.add_handler(CommandHandler("setapimax", set_api_max))
        app.add_handler(CommandHandler("maxff", set_api_max))
        app.add_handler(CommandHandler("setforcejoin", setforcejoin))
        app.add_handler(CommandHandler("addforcejoin", addforcejoin))
        app.add_handler(CommandHandler("removeforcejoin", removeforcejoin))
        app.add_handler(CommandHandler("forcejoinlist", forcejoinlist))
        app.add_handler(CommandHandler("forcejoinstatus", forcejoinstatus))
        app.add_handler(CommandHandler("forcejoinon", forcejoinon))
        app.add_handler(CommandHandler("forcejoinoff", forcejoinoff))
        app.add_handler(CommandHandler("broadcast", broadcast_cmd))
        app.add_handler(CallbackQueryHandler(verify_join_callback, pattern="^verify_join$"))
        app.add_handler(CallbackQueryHandler(forcejoin_noop_callback, pattern="^forcejoin_noop$"))
        # Register every user/group that interacts with the bot (broadcast registry).
        app.add_handler(TypeHandler(Update, register_update), group=-1)
        # Stick silences: stickers produce NO reply — not even "Admin only" / "No document".
        # filters.Document.ALL matches only documents (never stickers).
        app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
        app.add_error_handler(error_handler)

        print("🤖 Bot is running...", flush=True)
        print("   → Normal users: only in groups, daily limit applies", flush=True)
        print("   → Admin users: anywhere (DM + groups), unlimited likes", flush=True)

        # Test bot identity before polling
        try:
            bot_info = app.bot.get_me()
            print(f"✅ Bot identity confirmed: @{bot_info.username} (id={bot_info.id})", flush=True)
        except Exception as e:
            print(f"❌ Failed to get bot info: {e}", flush=True)

        # Delete any existing webhook to avoid conflicts
        try:
            app.bot.delete_webhook(drop_pending_updates=True)
            print("🗑️ Deleted existing webhook, dropped pending updates", flush=True)
        except Exception as e:
            print(f"⚠️ Failed to delete webhook: {e}", flush=True)

        # Polling loop with auto-restart on failure
        print("🚀 Starting polling...", flush=True)
        while True:
            try:
                print("📡 Starting polling...", flush=True)
                app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
            except Exception as e:
                import traceback
                print(f"❌ Polling error: {e}", flush=True)
                traceback.print_exc()
                print("🔄 Restarting polling in 5 seconds...", flush=True)
                time.sleep(5)
    except Exception as e:
        print(f"❌ Bot setup failed: {e}", flush=True)
        import traceback
        traceback.print_exc()
        # Keep process alive so health server can still respond
        while True:
            time.sleep(60)

if __name__ == "__main__":
    main()