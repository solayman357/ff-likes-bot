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
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters, CallbackQueryHandler
from telegram.request import HTTPXRequest
from telegram.error import BadRequest, NetworkError, RetryAfter, TelegramError

# Protobuf imports (local copies for Render deployment)
import uid_generator_pb2  # noqa: E402

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
force_join_channel = ""
force_join_link = "https://t.me/+k4aq8eU9tYw1MTU1"

# ============ HELPER FUNCTIONS ============
def load_data():
    global allowed_groups, daily_stats, user_limits, bot_status, bot_mode, daily_limit, like_tokens, force_join, force_join_channel, force_join_link
    try:
        with open(DATA_FILES['allowed'], 'r') as f: allowed_groups = json.load(f)
    except: allowed_groups = {}
    try:
        with open(DATA_FILES['stats'], 'r') as f: daily_stats = json.load(f)
    except: daily_stats = {}
    try:
        with open(DATA_FILES['users'], 'r') as f: user_limits = json.load(f)
    except: user_limits = {}
    try:
        with open(DATA_FILES['config'], 'r') as f:
            cfg = json.load(f)
            bot_status = cfg.get('status', 'on')
            bot_mode = cfg.get('mode', 'public')
            daily_limit = cfg.get('limit', 2)
            like_tokens = int(cfg.get('like_tokens', 240))
            force_join = cfg.get('force_join', False)
            force_join_channel = cfg.get('force_join_channel', '')
            force_join_link = cfg.get('force_join_link', 'https://t.me/+k4aq8eU9tYw1MTU1')
    except:
        bot_status, bot_mode, daily_limit, like_tokens, force_join, force_join_channel, force_join_link = 'on', 'public', 2, 240, False, '', 'https://t.me/+k4aq8eU9tYw1MTU1'

def save_all():
    with open(DATA_FILES['allowed'], 'w') as f: json.dump(allowed_groups, f, indent=2)
    with open(DATA_FILES['stats'], 'w') as f: json.dump(daily_stats, f, indent=2)
    with open(DATA_FILES['users'], 'w') as f: json.dump(user_limits, f, indent=2)
    with open(DATA_FILES['config'], 'w') as f: json.dump({'status': bot_status, 'mode': bot_mode, 'limit': daily_limit, 'like_tokens': like_tokens, 'force_join': force_join, 'force_join_channel': force_join_channel, 'force_join_link': force_join_link}, f, indent=2)

async def is_user_member(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    if not force_join or not force_join_channel:
        return True
    try:
        member = await context.bot.get_chat_member(chat_id=force_join_channel, user_id=user_id)
        if member.status in ['member', 'administrator', 'creator']:
            return True
        return False
    except Exception as e:
        print(f"Force join check error: {e}")
        return False


async def force_join_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not force_join or not force_join_link:
        return True
    if is_admin(update.effective_user.id):
        return True
    user_id = update.effective_user.id
    try:
        if await is_user_member(context, user_id):
            return True
    except Exception as e:
        print(f"Force join check error: {e}")
        # If we can't check (private channel/bot not admin), be lenient and ask to verify
        pass
    keyboard = [
        [InlineKeyboardButton("📢 Join Group", url=force_join_link)],
        [InlineKeyboardButton("✅ I Joined - Verify", callback_data="verify_join")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    try:
        await reply(update,
                   "⚠️ *Force Join Required*\n\n"
                   "You must join our group to use this bot!\n\n"
                   "1️⃣ Click 'Join Group' and join\n"
                   "2️⃣ Come back and click 'I Joined - Verify'\n\n"
                   "After joining click verify to continue.",
                   reply_markup)
    except Exception as e:
        print(f"Force join reply error: {e}")
    return False


async def verify_join_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if await is_user_member(context, user_id):
        await query.edit_message_text(
            "✅ *Verification Successful!*\n\n"
            "You can now use the bot.\n"
            "Send /start to continue."
        )
    else:
        keyboard = [
            [InlineKeyboardButton("📢 Join Channel", url=force_join_link)],
            [InlineKeyboardButton("🔄 Try Again", callback_data="verify_join")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            "❌ *You haven't joined the channel yet!*\n\n"
            "Please join the channel first, then click Verify again.",
            reply_markup=reply_markup
        )

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
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
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
    """Log errors and notify user."""
    print(f"Exception while handling update: {context.error}", flush=True)
    import traceback
    traceback.print_exc()
    try:
        if hasattr(update, 'effective_message') and update.effective_message:
            await update.effective_message.reply_text(
                "❌ Something went wrong. Please try again later."
            )
    except Exception:
        pass

# ============ USER COMMANDS ============
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await force_join_check(update, context): return
    if bot_status == "off":
        await reply(update, "🔴 Bot is currently OFF")
        return
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
        "`/setapimax` or `/maxff` – Set to 240 tokens (max)"
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
    if not await force_join_check(update, context):
        return
    if bot_status == "off":
        await reply(update, "🔴 Bot is currently OFF")
        return

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
        if proc_msg is not None:
            try:
                await proc_msg.edit_text(text)
                return
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
            await update.message.reply_text(fallback_text or text)
        except (BadRequest, NetworkError, RetryAfter, TelegramError) as e:
            import traceback
            print(f"❌ Failed to send fallback message in like_cmd: {e}", flush=True)
            traceback.print_exc()
        except Exception as e:
            import traceback
            print(f"❌ Unexpected error sending fallback message: {e}", flush=True)
            traceback.print_exc()

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

    await _respond(result)

# ============ JWT TOKEN UPLOAD HANDLER ============
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await reply(update, "❌ *Admin only command*")
        return

    document = update.message.document
    if not document:
        await reply(update, "❌ *No document found.*")
        return

    file_name = document.file_name or ""
    if not file_name.endswith(".json"):
        await reply(update, "❌ *Please upload a valid JSON file.*")
        return

    try:
        file = await context.bot.get_document(document.file_id)
        file_bytes = await file.download_as_bytearray()
        
        try:
            new_tokens = json.loads(file_bytes.decode('utf-8'))
        except Exception as e:
            await reply(update, f"❌ *Invalid JSON file:* `{str(e)[:100]}`")
            return

        # Forward tokens to API's /upload-tokens endpoint
        upload_url = f"{API_URL.rstrip('/')}/upload-tokens"
        payload = {"tokens": new_tokens}

        async with aiohttp.ClientSession() as session:
            async with session.post(upload_url, json=payload, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                result = await resp.json()

        if resp.status == 200:
            await reply(update,
                f"✅ *Token Update Complete*\n\n"
                f"✅ Added: {result.get('added', 0)}\n"
                f"❌ Expired: {result.get('expired', 0)}\n"
                f"📁 Total in bank: {result.get('total_in_bank', 0)}"
            )
        else:
            error_msg = result.get("error", "Unknown error") if isinstance(result, dict) else str(result)
            await reply(update, f"❌ *API Error:* `{error_msg}`")

    except Exception as e:
        await reply(update, f"❌ *Error processing file:* `{str(e)[:100]}`")

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
async def setforcejoin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await reply(update, "❌ Admin only")
        return
    global force_join, force_join_channel, force_join_link
    if len(context.args) < 1:
        await reply(update, "❌ Usage:\n`/setforcejoin <channel_username_or_id> <invite_link>`\n\nExample:\n`/setforcejoin @yourchannel https://t.me/+xxxxx`\n\nTo enable: add both. For private invite links, use the link + also set channel id if possible.")
        return
    channel = context.args[0]
    link = context.args[1] if len(context.args) > 1 else force_join_link
    force_join_channel = channel
    force_join_link = link
    force_join = True
    save_all()
    await reply(update,
               f"✅ *Force Join Enabled*\n\n"
               f"📢 Channel: `{channel}`\n"
               f"🔗 Link: {link}\n\n"
               f"Users must join to use bot.")


async def removeforcejoin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await reply(update, "❌ Admin only")
        return
    global force_join, force_join_channel, force_join_link
    force_join = False
    force_join_channel = ""
    force_join_link = ""
    save_all()
    await reply(update, "✅ *Force Join Disabled*\n\nAll users can use the bot now.")


async def forcejoinstatus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await reply(update, "❌ Admin only")
        return
    status = "✅ Enabled" if force_join else "❌ Disabled"
    await reply(update,
               f"📊 *Force Join Status*\n\n"
               f"Status: {status}\n"
               f"Channel: `{force_join_channel or 'Not set'}`\n"
               f"Link: {force_join_link or 'Not set'}")


async def verify_join_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if not force_join or not force_join_link:
        await query.edit_message_text("✅ Force join is disabled. You can use the bot now.")
        return
    if is_admin(user_id):
        await query.edit_message_text("✅ Admin verified. You can use the bot now.")
        return
    try:
        if await is_user_member(context, user_id):
            await query.edit_message_text(
                "✅ *Verification Successful!*\n\n"
                "You can now use the bot.\n"
                "Send /start or /like to continue."
            )
            return
    except Exception:
        # Can't check membership (bot not admin / private) - allow if they clicked verify after joining
        await query.edit_message_text(
            "✅ *Verified!*\n\n"
            "You can now use the bot.\n"
            "Send /start or /like to continue."
        )
        return
    keyboard = [
        [InlineKeyboardButton("📢 Join Group", url=force_join_link)],
        [InlineKeyboardButton("✅ I Joined - Verify", callback_data="verify_join")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        "❌ *Verification Failed*\n\n"
        "Join the group first, then click 'I Joined - Verify'.",
        reply_markup=reply_markup
    )


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
    """MAX mode (240 tokens per request)."""
    if not is_admin(update.effective_user.id):
        await reply(update, "❌ Admin only")
        return
    global like_tokens
    like_tokens = 240
    save_all()
    await reply(update,
                "✅ *MAX FF mode ON*\n\n"
                "🔢 Tokens per request: `240`\n"
                "❤️ Likes: as many as the 240 tokens can give\n\n"
                "Now send `/like BD UID`")


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
        app.add_handler(CommandHandler("setapimax", set_api_max))
        app.add_handler(CommandHandler("maxff", set_api_max))
        app.add_handler(CommandHandler("setforcejoin", setforcejoin))
        app.add_handler(CommandHandler("removeforcejoin", removeforcejoin))
        app.add_handler(CommandHandler("forcejoinstatus", forcejoinstatus))
        app.add_handler(CallbackQueryHandler(verify_join_callback, pattern="^verify_join$"))
        app.add_handler(MessageHandler(filters.ATTACHMENT, handle_document))
        app.add_error_handler(error_handler)

        print("🤖 Bot is running...", flush=True)
        print("   → Normal users: only in groups, daily limit applies", flush=True)
        print("   → Admin users: anywhere (DM + groups), unlimited likes", flush=True)

        # Polling loop with auto-restart on failure
        while True:
            try:
                app.run_polling()
            except Exception as e:
                print(f"❌ Polling error: {e}", flush=True)
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