import os
import json
import base64
import aiohttp
import asyncio
from datetime import datetime, date, time
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.request import HTTPXRequest

# ============ CONFIGURATION ============
API_URL = os.getenv("API_URL", "http://127.0.0.1:5000")
ENCODED_KEY = os.getenv("ENCODED_KEY", "WkVYWFk=")
API_KEY = base64.b64decode(ENCODED_KEY).decode()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")       # ← Set via environment variable
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]  # ← Set via environment variable

# ============ DATA FILES ============
DATA_FILES = {
    'allowed': 'allowed_groups.json',
    'stats': 'daily_stats.json',
    'users': 'user_limits.json',
    'config': 'bot_config.json'
}

# ============ GLOBALS ============
bot_status = "on"
bot_mode = "public"
allowed_groups = {}
daily_stats = {}
user_limits = {}
daily_limit = 2

# ============ HELPER FUNCTIONS ============
def load_data():
    global allowed_groups, daily_stats, user_limits, bot_status, bot_mode, daily_limit
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
    except:
        bot_status, bot_mode, daily_limit = 'on', 'public', 2

def save_all():
    with open(DATA_FILES['allowed'], 'w') as f: json.dump(allowed_groups, f, indent=2)
    with open(DATA_FILES['stats'], 'w') as f: json.dump(daily_stats, f, indent=2)
    with open(DATA_FILES['users'], 'w') as f: json.dump(user_limits, f, indent=2)
    with open(DATA_FILES['config'], 'w') as f: json.dump({'status': bot_status, 'mode': bot_mode, 'limit': daily_limit}, f, indent=2)

def is_admin(uid): return uid in ADMIN_IDS
def today_str(): return str(date.today())

def can_user_like(uid):
    if is_admin(uid):
        return True
    t = today_str()
    if uid not in user_limits or user_limits[uid]['date'] != t:
        user_limits[uid] = {'date': t, 'count': 0}
        return True
    return user_limits[uid]['count'] < daily_limit

def update_user_like(uid):
    if is_admin(uid):
        return
    t = today_str()
    if uid not in user_limits or user_limits[uid]['date'] != t:
        user_limits[uid] = {'date': t, 'count': 0}
    user_limits[uid]['count'] += 1
    
    if t not in daily_stats:
        daily_stats[t] = {'total': 0, 'users': {}}
    daily_stats[t]['total'] += 1
    uid_str = str(uid)
    if uid_str not in daily_stats[t]['users']:
        daily_stats[t]['users'][uid_str] = 0
    daily_stats[t]['users'][uid_str] += 1
    save_all()

async def call_like_api(region, uid):
    try:
        base = API_URL.rstrip("/") + "/"
        url = f"{base}like?uid={uid}&region={region}&key={API_KEY}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    return await resp.json()
                else:
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
        await update.message.reply_text("🚫 *Bot works only in groups!*\n(Admins can use in private)", parse_mode='Markdown')
        return True
    return False

async def reply(update, text):
    await update.message.reply_text(text, parse_mode='Markdown')

# ============ USER COMMANDS ============
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await block_non_admin_private(update): return
    if bot_status == "off":
        await reply(update, "🔴 *Bot is currently OFF*")
        return
    msg = (
        "✨ *𝑭𝑹𝑬𝑬 𝑭𝑰𝑹𝑬 𝑳𝑰𝑲𝑬 𝑩𝑶𝑻* ✨\n"
        "⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n"
        "💬 `/like REGION UID` – Send a like\n"
        "💬 `/help` – Show all commands\n"
        "💬 `/info` – Your remaining likes\n\n"
        "📌 *Example:* `/like IND 14160011100`\n"
        f"🔥 Your daily limit: `{daily_limit}` likes\n"
        "🌍 *Any region code works* (e.g., IND, USA, GER, etc.)\n"
        "⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯"
    )
    await reply(update, msg)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await block_non_admin_private(update): return
    if bot_status == "off":
        await reply(update, "🔴 Bot is OFF")
        return
    msg = (
        "📖 *𝑪𝑶𝑴𝑴𝑨𝑵𝑫 𝑳𝑰𝑺𝑻*\n\n"
        "🔹 `/like REGION UID` – Send 1 like (any region)\n"
        "🔹 `/info` – Check your remaining likes\n"
        "🔹 `/start` – Welcome message\n\n"
        "*Example:* `/like IND 1234567890`\n"
        "*Supports any region* – just type the code.\n\n"
        "👑 *Admin commands:*\n"
        "`/allow` – Allow current group\n"
        "`/off` / `/on` – Turn bot off/on\n"
        "`/stats` – Today's usage\n"
        "`/setprivate` / `/setpublic` – Change group mode\n"
        "`/setlimit <num>` – Set daily limit per user"
    )
    await reply(update, msg)

async def info_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await block_non_admin_private(update): return
    if bot_status == "off":
        await reply(update, "🔴 Bot is OFF")
        return
    uid = update.effective_user.id
    if is_admin(uid):
        await reply(update, "👑 *Admin Account*\n🔥 *Unlimited likes* – No daily limit.")
        return
    t = today_str()
    used = user_limits.get(uid, {}).get('count', 0) if uid in user_limits and user_limits[uid]['date'] == t else 0
    remaining = daily_limit - used
    msg = (
        "🤖 *𝑩𝑶𝑻 𝑰𝑵𝑭𝑶*\n\n"
        f"⚙️ Mode: `{bot_mode.upper()}`\n"
        f"🟢 Status: `{bot_status.upper()}`\n"
        f"📅 Daily limit: `{daily_limit}` likes\n"
        f"✅ Used today: `{used}`\n"
        f"🟢 Remaining: `{remaining}`\n"
        f"👥 Allowed groups: `{len(allowed_groups)}`"
    )
    await reply(update, msg)

async def like_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await block_non_admin_private(update): return
    if bot_status == "off":
        await reply(update, "🔴 *Bot is currently OFF*")
        return
    
    chat_id = update.effective_chat.id
    chat_type = update.effective_chat.type
    if chat_type != "private" and not is_group_allowed(chat_id, chat_type):
        await reply(update, "🚫 *This bot works only in allowed groups!*")
        return
    
    if len(context.args) != 2:
        await reply(update, "❌ *Usage:* `/like REGION UID`\nExample: `/like IND 14160011100`")
        return
    
    region = context.args[0].upper()
    uid = context.args[1]
    if not uid.isdigit():
        await reply(update, "❌ *UID must contain only numbers!*")
        return
    
    user_id = update.effective_user.id
    if not can_user_like(user_id):
        used = user_limits.get(user_id, {}).get('count', 0)
        await reply(update, f"⚠️ *Daily limit reached!*\nYou have used `{used}/{daily_limit}` likes today.\n💡 Try again tomorrow.")
        return
    
    proc_msg = await update.message.reply_text(f"🔄 *Processing...*\nUID: `{uid}`\nRegion: `{region}`", parse_mode='Markdown')
    data = await call_like_api(region, uid)
    
    if data is None or "error" in data:
        error_msg = data.get("error", "Unknown error") if data else "No response"
        await proc_msg.edit_text(f"❌ *API Error!*\n{error_msg}\nPlease try again later.", parse_mode='Markdown')
        return
    
    status = data.get('status')
    if status is None:
        await proc_msg.edit_text("❌ *Invalid API response*\nThe server returned an unexpected format.", parse_mode='Markdown')
        return
    
    player = data.get('PlayerNickname', 'Unknown')
    uid_resp = data.get('UID', uid)
    region_resp = data.get('Region', region)
    level = data.get('Level', 'N/A')
    before = data.get('LikesbeforeCommand', 0)
    after = data.get('LikesafterCommand', 0)
    given = data.get('LikesGivenByAPI', 0)
    api_limit = data.get('daily_limit', 20)
    api_used = data.get('used', 0)
    api_rem = data.get('remaining', 20)
    
    if status == 1:
        update_user_like(user_id)
        user_used = user_limits.get(user_id, {}).get('count', 0) if not is_admin(user_id) else "Unlimited"
        result = (
            f"✅ *𝑳𝑰𝑲𝑬 𝑺𝑬𝑵𝑻* ✅\n\n"
            f"👤 *Player:* {player}\n"
            f"🏷️ *UID:* `{uid_resp}`\n"
            f"🌍 *Region:* {region_resp}\n"
            f"⭐ *Level:* {level}\n\n"
            f"❤️ *Likes given:* +{given}\n"
            f"📊 *Total:* {before} → {after}\n\n"
            f"📅 *Your usage:* {user_used}/{daily_limit if not is_admin(user_id) else '∞'}\n"
            f"🎉 *Success!*"
        )
    elif status == 2:
        result = (
            f"⚠️ *𝑳𝑰𝑴𝑰𝑻 𝑹𝑬𝑨𝑪𝑯𝑬𝑫* ⚠️\n\n"
            f"👤 *Player:* {player}\n"
            f"🏷️ *UID:* `{uid_resp}`\n"
            f"🌍 *Region:* {region_resp}\n"
            f"⭐ *Level:* {level}\n\n"
            f"❌ *Likes sent:* 0\n"
            f"📊 *Total likes:* {before} (unchanged)\n\n"
            f"📅 *API daily limit:* {api_used}/{api_limit}\n"
            f"🟢 *API remaining:* {api_rem}\n\n"
            f"😔 *Could not send like to {player}*"
        )
    else:
        result = f"❓ *Unknown API response*\nStatus code: {status}\nPlease contact the bot admin."
    
    await proc_msg.edit_text(result, parse_mode='Markdown')

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

# ============ MAIN ============
def main():
    load_data()
    
    request = HTTPXRequest(connect_timeout=30.0, read_timeout=30.0)
    app = Application.builder().token(BOT_TOKEN).request(request).build()
    
    job_queue = app.job_queue
    if job_queue:
        job_queue.run_daily(reset_midnight, time=time(hour=0, minute=0, second=0), days=tuple(range(7)))
        print("⏰ Midnight reset scheduled")
    
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
    
    print("🤖 Bot is running...")
    print("   → Normal users: only in groups, daily limit applies")
    print("   → Admin users: anywhere (DM + groups), unlimited likes")
    app.run_polling()

if __name__ == "__main__":
    main()