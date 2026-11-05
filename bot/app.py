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
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters, CallbackQueryHandler, ChatMemberHandler
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

# ============ GROUP MANAGEMENT (SQLite) ============
import sqlite3

GROUP_DB_FILE = "group_management.db"
GROUP_DB_LOCK = threading.Lock()


def init_group_db():
    """Initialize the SQLite group management database."""
    try:
        with GROUP_DB_LOCK:
            conn = sqlite3.connect(GROUP_DB_FILE)
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS groups (
                    chat_id INTEGER PRIMARY KEY,
                    chat_title TEXT,
                    chat_username TEXT,
                    chat_type TEXT,
                    status TEXT DEFAULT 'allowed',
                    added_at TEXT,
                    updated_at TEXT,
                    bot_member_status TEXT DEFAULT 'member',
                    added_by INTEGER
                )
            """)
            conn.commit()
            conn.close()
        print("✅ Group management SQLite DB initialized", flush=True)
    except Exception as e:
        print(f"⚠️ Group DB init failed: {e}", flush=True)


def upsert_group(chat_id, chat_title=None, chat_username=None, chat_type=None,
                 status=None, added_by=None, bot_member_status=None):
    """Add or update a group in the SQLite database."""
    try:
        with GROUP_DB_LOCK:
            conn = sqlite3.connect(GROUP_DB_FILE)
            cur = conn.cursor()
            now = datetime.now().isoformat()

            # Check if group exists
            cur.execute("SELECT chat_id, status, added_at FROM groups WHERE chat_id = ?", (chat_id,))
            row = cur.fetchone()

            if row is None:
                # New group
                cur.execute(
                    "INSERT INTO groups (chat_id, chat_title, chat_username, chat_type, status, added_at, updated_at, bot_member_status, added_by) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (chat_id, chat_title, chat_username, chat_type,
                     status or 'allowed', now, now, bot_member_status or 'member', added_by)
                )
            else:
                # Update existing group - preserve added_at, only update fields provided
                existing_status = row[1]
                existing_added_at = row[2]
                new_status = status if status is not None else existing_status
                cur.execute(
                    "UPDATE groups SET chat_title = COALESCE(?, chat_title), chat_username = COALESCE(?, chat_username), "
                    "chat_type = COALESCE(?, chat_type), status = ?, updated_at = ?, "
                    "bot_member_status = COALESCE(?, bot_member_status) WHERE chat_id = ?",
                    (chat_title, chat_username, chat_type, new_status, now,
                     bot_member_status, chat_id)
                )
                # Ensure added_at is preserved
                cur.execute("UPDATE groups SET added_at = ? WHERE chat_id = ?", (existing_added_at, chat_id))

            conn.commit()
            conn.close()
        # Queue for immediate GitHub sync
        if github_persistence:
            try:
                github_persistence.queue_push('groups')
            except Exception as e:
                print(f"⚠️ GitHub queue failed for groups: {e}", flush=True)
    except Exception as e:
        print(f"⚠️ upsert_group failed: {e}", flush=True)


def set_group_status(chat_id, status):
    """Set the status (allowed/blocked/inactive) of a group."""
    try:
        with GROUP_DB_LOCK:
            conn = sqlite3.connect(GROUP_DB_FILE)
            cur = conn.cursor()
            cur.execute(
                "UPDATE groups SET status = ?, updated_at = ? WHERE chat_id = ?",
                (status, datetime.now().isoformat(), chat_id)
            )
            conn.commit()
            changed = cur.rowcount > 0
            conn.close()
        # Queue for immediate GitHub sync
        if github_persistence:
            try:
                github_persistence.queue_push('groups')
            except Exception as e:
                print(f"⚠️ GitHub queue failed for groups: {e}", flush=True)
        return changed
    except Exception as e:
        print(f"⚠️ set_group_status failed: {e}", flush=True)
        return False


def get_all_groups():
    """Get all groups from the database."""
    try:
        with GROUP_DB_LOCK:
            conn = sqlite3.connect(GROUP_DB_FILE)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("SELECT * FROM groups ORDER BY added_at DESC")
            rows = [dict(r) for r in cur.fetchall()]
            conn.close()
        return rows
    except Exception as e:
        print(f"⚠️ get_all_groups failed: {e}", flush=True)
        return []


def get_group_count_by_status():
    """Get counts of groups by status."""
    try:
        with GROUP_DB_LOCK:
            conn = sqlite3.connect(GROUP_DB_FILE)
            cur = conn.cursor()
            cur.execute("SELECT status, COUNT(*) as cnt FROM groups GROUP BY status")
            result = {}
            for row in cur.fetchall():
                result[row[0]] = row[1]
            conn.close()
        return result
    except Exception as e:
        print(f"⚠️ get_group_count_by_status failed: {e}", flush=True)
        return {}


def get_group_detail(chat_id):
    """Get details of a single group."""
    try:
        with GROUP_DB_LOCK:
            conn = sqlite3.connect(GROUP_DB_FILE)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("SELECT * FROM groups WHERE chat_id = ?", (chat_id,))
            row = cur.fetchone()
            conn.close()
        return dict(row) if row else None
    except Exception as e:
        print(f"⚠️ get_group_detail failed: {e}", flush=True)
        return None


def is_group_allowed_db(chat_id, chat_type):
    """Check if a group is allowed based on SQLite DB status."""
    if chat_type == "private":
        return True
    if bot_mode == "public":
        return True
    try:
        with GROUP_DB_LOCK:
            conn = sqlite3.connect(GROUP_DB_FILE)
            cur = conn.cursor()
            cur.execute("SELECT status FROM groups WHERE chat_id = ?", (chat_id,))
            row = cur.fetchone()
            conn.close()
        if row is None:
            # Group not in DB - default to allowed if public mode
            return bot_mode == "public"
        return row[0] == 'allowed'
    except Exception as e:
        print(f"⚠️ is_group_allowed_db failed: {e}", flush=True)
        return bot_mode == "public"


def sync_groups_from_broadcast():
    """Sync existing broadcast chats into the SQLite database.

    Preserves existing ALLOWED/BLOCKED/INACTIVE status — does NOT overwrite
    permissions. Only inserts new groups (defaulting to 'allowed') or updates
    title/type fields for existing ones.
    """
    try:
        data = _load_broadcast_data()
        chats = data.get("chats", {})
        count = 0
        for cid, info in chats.items():
            try:
                chat_id = int(cid)
            except (TypeError, ValueError):
                continue
            upsert_group(
                chat_id=chat_id,
                chat_title=info.get("title"),
                chat_type=info.get("chat_type"),
                # Do NOT pass status — preserve existing ALLOWED/BLOCKED/INACTIVE
                bot_member_status='member'
            )
            count += 1
        print(f"✅ Synced {count} groups from broadcast list to SQLite", flush=True)
        return count
    except Exception as e:
        print(f"⚠️ sync_groups_from_broadcast failed: {e}", flush=True)
        return 0
# ============ AUTO LIKE SCHEDULER ============
import zoneinfo

try:
    BD_TZ = zoneinfo.ZoneInfo("Asia/Dhaka")
except Exception:
    import pytz
    BD_TZ = pytz.timezone("Asia/Dhaka")

AUTO_LIKE_DB_FILE = GROUP_DB_FILE  # Same SQLite database
AUTO_LIKE_LOCK = threading.Lock()
AUTO_LIKE_SCHEDULER_TASK = None
AUTO_LIKE_SCHEDULER_RUNNING = False

# Broadcast destinations for auto like announcements
AUTO_LIKE_BROADCAST_ENABLED = True
AUTO_LIKE_CHANNEL_ID = None
AUTO_LIKE_GROUP_ID = None
AUTO_LIKE_CHANNEL_MSG_ID = None
AUTO_LIKE_GROUP_MSG_ID = None

# Admin configurable: max target likes per task
MAX_AUTO_LIKE_TARGET = 100000


def init_auto_like_db():
    """Initialize the Auto Like scheduler tables in SQLite."""
    try:
        with GROUP_DB_LOCK:
            conn = sqlite3.connect(AUTO_LIKE_DB_FILE)
            cur = conn.cursor()

            # Auto like tasks table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS auto_like_tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_id INTEGER NOT NULL,
                    owner_username TEXT,
                    owner_name TEXT,
                    uid INTEGER NOT NULL,
                    region TEXT NOT NULL,
                    target_likes INTEGER NOT NULL,
                    delivered_likes INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'scheduled',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_executed TEXT,
                    next_execution TEXT,
                    channel_msg_id INTEGER,
                    group_msg_id INTEGER,
                    UNIQUE(owner_id, uid)
                )
            """)

            # Auto like execution history table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS auto_like_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER NOT NULL,
                    owner_id INTEGER NOT NULL,
                    uid INTEGER NOT NULL,
                    region TEXT NOT NULL,
                    likes_delivered INTEGER NOT NULL,
                    before_likes INTEGER,
                    after_likes INTEGER,
                    executed_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error_message TEXT,
                    FOREIGN KEY (task_id) REFERENCES auto_like_tasks(id)
                )
            """)

            conn.commit()
            conn.close()
        print("✅ Auto Like scheduler DB initialized", flush=True)
    except Exception as e:
        print(f"⚠️ Auto Like DB init failed: {e}", flush=True)


def upsert_auto_like_task(owner_id, owner_username, owner_name, uid, region,
                          target_likes, status=None, delivered_likes=None,
                          last_executed=None, next_execution=None,
                          channel_msg_id=None, group_msg_id=None):
    """Add or update an auto like task."""
    try:
        with GROUP_DB_LOCK:
            conn = sqlite3.connect(AUTO_LIKE_DB_FILE)
            cur = conn.cursor()
            now = datetime.now().isoformat()

            cur.execute(
                "SELECT id, delivered_likes, status, next_execution FROM auto_like_tasks "
                "WHERE owner_id = ? AND uid = ?",
                (owner_id, uid)
            )
            row = cur.fetchone()

            if row is None:
                # New task
                task_id = cur.execute(
                    "INSERT INTO auto_like_tasks "
                    "(owner_id, owner_username, owner_name, uid, region, target_likes, "
                    "delivered_likes, status, created_at, updated_at, last_executed, "
                    "next_execution, channel_msg_id, group_msg_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (owner_id, owner_username, owner_name, uid, region,
                     target_likes, delivered_likes or 0,
                     status or 'scheduled', now, now,
                     last_executed, next_execution,
                     channel_msg_id, group_msg_id)
                ).lastrowid
            else:
                task_id = row[0]
                existing_delivered = row[1]
                existing_status = row[2]
                existing_next = row[3]

                new_delivered = delivered_likes if delivered_likes is not None else existing_delivered
                new_status = status if status is not None else existing_status
                new_next = next_execution if next_execution is not None else existing_next

                cur.execute(
                    "UPDATE auto_like_tasks SET "
                    "owner_username = COALESCE(?, owner_username), "
                    "owner_name = COALESCE(?, owner_name), "
                    "target_likes = COALESCE(?, target_likes), "
                    "delivered_likes = ?, status = ?, updated_at = ?, "
                    "last_executed = COALESCE(?, last_executed), "
                    "next_execution = ?, "
                    "channel_msg_id = COALESCE(?, channel_msg_id), "
                    "group_msg_id = COALESCE(?, group_msg_id) "
                    "WHERE id = ?",
                    (owner_username, owner_name, target_likes,
                     new_delivered, new_status, now,
                     last_executed, new_next,
                     channel_msg_id, group_msg_id, task_id)
                )

            conn.commit()
            conn.close()
        return task_id
    except Exception as e:
        print(f"⚠️ upsert_auto_like_task failed: {e}", flush=True)
        return None


def get_auto_like_tasks(owner_id=None, status=None):
    """Get auto like tasks, optionally filtered by owner and/or status."""
    try:
        with GROUP_DB_LOCK:
            conn = sqlite3.connect(AUTO_LIKE_DB_FILE)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()

            if owner_id is not None and status is not None:
                cur.execute(
                    "SELECT * FROM auto_like_tasks WHERE owner_id = ? AND status = ? "
                    "ORDER BY created_at DESC",
                    (owner_id, status)
                )
            elif owner_id is not None:
                cur.execute(
                    "SELECT * FROM auto_like_tasks WHERE owner_id = ? "
                    "ORDER BY created_at DESC",
                    (owner_id,)
                )
            elif status is not None:
                cur.execute(
                    "SELECT * FROM auto_like_tasks WHERE status = ? "
                    "ORDER BY created_at DESC",
                    (status,)
                )
            else:
                cur.execute("SELECT * FROM auto_like_tasks ORDER BY created_at DESC")

            rows = [dict(r) for r in cur.fetchall()]
            conn.close()
        return rows
    except Exception as e:
        print(f"⚠️ get_auto_like_tasks failed: {e}", flush=True)
        return []


def get_auto_like_task(task_id):
    """Get a single auto like task by ID."""
    try:
        with GROUP_DB_LOCK:
            conn = sqlite3.connect(AUTO_LIKE_DB_FILE)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("SELECT * FROM auto_like_tasks WHERE id = ?", (task_id,))
            row = cur.fetchone()
            conn.close()
        return dict(row) if row else None
    except Exception as e:
        print(f"⚠️ get_auto_like_task failed: {e}", flush=True)
        return None


def get_auto_like_task_by_uid(owner_id, uid):
    """Get auto like task by owner and UID."""
    try:
        with GROUP_DB_LOCK:
            conn = sqlite3.connect(AUTO_LIKE_DB_FILE)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute(
                "SELECT * FROM auto_like_tasks WHERE owner_id = ? AND uid = ?",
                (owner_id, uid)
            )
            row = cur.fetchone()
            conn.close()
        return dict(row) if row else None
    except Exception as e:
        print(f"⚠️ get_auto_like_task_by_uid failed: {e}", flush=True)
        return None


def update_auto_like_task_progress(task_id, delivered_likes, status=None,
                                    last_executed=None, next_execution=None):
    """Update task progress after execution."""
    try:
        with GROUP_DB_LOCK:
            conn = sqlite3.connect(AUTO_LIKE_DB_FILE)
            cur = conn.cursor()
            now = datetime.now().isoformat()

            cur.execute(
                "SELECT delivered_likes FROM auto_like_tasks WHERE id = ?",
                (task_id,)
            )
            row = cur.fetchone()
            if row is None:
                conn.close()
                return False

            existing_delivered = row[0]
            new_delivered = existing_delivered + delivered_likes

            new_status = status if status is not None else 'scheduled'

            cur.execute(
                "UPDATE auto_like_tasks SET "
                "delivered_likes = ?, status = ?, updated_at = ?, "
                "last_executed = ?, next_execution = ? "
                "WHERE id = ?",
                (new_delivered, new_status, now,
                 last_executed, next_execution, task_id)
            )
            conn.commit()
            conn.close()
        return True
    except Exception as e:
        print(f"⚠️ update_auto_like_task_progress failed: {e}", flush=True)
        return False


def add_auto_like_history(task_id, owner_id, uid, region, likes_delivered,
                          before_likes, after_likes, status, error_message=None):
    """Add execution history record."""
    try:
        with GROUP_DB_LOCK:
            conn = sqlite3.connect(AUTO_LIKE_DB_FILE)
            cur = conn.cursor()
            now = datetime.now().isoformat()

            cur.execute(
                "INSERT INTO auto_like_history "
                "(task_id, owner_id, uid, region, likes_delivered, "
                "before_likes, after_likes, executed_at, status, error_message) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (task_id, owner_id, uid, region, likes_delivered,
                 before_likes, after_likes, now, status, error_message)
            )
            conn.commit()
            conn.close()
    except Exception as e:
        print(f"⚠️ add_auto_like_history failed: {e}", flush=True)


def get_auto_like_history(task_id, limit=10):
    """Get execution history for a task."""
    try:
        with GROUP_DB_LOCK:
            conn = sqlite3.connect(AUTO_LIKE_DB_FILE)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute(
                "SELECT * FROM auto_like_history WHERE task_id = ? "
                "ORDER BY executed_at DESC LIMIT ?",
                (task_id, limit)
            )
            rows = [dict(r) for r in cur.fetchall()]
            conn.close()
        return rows
    except Exception as e:
        print(f"⚠️ get_auto_like_history failed: {e}", flush=True)
        return []


def get_due_auto_like_tasks():
    """Get tasks that are due for execution (next_execution <= now)."""
    try:
        with GROUP_DB_LOCK:
            conn = sqlite3.connect(AUTO_LIKE_DB_FILE)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            now = datetime.now(BD_TZ).isoformat()
            cur.execute(
                "SELECT * FROM auto_like_tasks "
                "WHERE status IN ('scheduled', 'paused', 'resumed') "
                "AND next_execution IS NOT NULL "
                "AND next_execution <= ? "
                "ORDER BY next_execution ASC",
                (now,)
            )
            rows = [dict(r) for r in cur.fetchall()]
            conn.close()
        return rows
    except Exception as e:
        print(f"⚠️ get_due_auto_like_tasks failed: {e}", flush=True)
        return []


def set_auto_like_task_status(task_id, status):
    """Set task status (scheduled, running, paused, completed, failed, stopped)."""
    try:
        with GROUP_DB_LOCK:
            conn = sqlite3.connect(AUTO_LIKE_DB_FILE)
            cur = conn.cursor()
            cur.execute(
                "UPDATE auto_like_tasks SET status = ?, updated_at = ? WHERE id = ?",
                (status, datetime.now().isoformat(), task_id)
            )
            conn.commit()
            changed = cur.rowcount > 0
            conn.close()
        return changed
    except Exception as e:
        print(f"⚠️ set_auto_like_task_status failed: {e}", flush=True)
        return False


def get_auto_like_stats():
    """Get overall statistics for all auto like tasks."""
    try:
        with GROUP_DB_LOCK:
            conn = sqlite3.connect(AUTO_LIKE_DB_FILE)
            cur = conn.cursor()

            cur.execute("SELECT COUNT(*) FROM auto_like_tasks")
            total = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM auto_like_tasks WHERE status = 'scheduled'")
            scheduled = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM auto_like_tasks WHERE status = 'running'")
            running = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM auto_like_tasks WHERE status = 'completed'")
            completed = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM auto_like_tasks WHERE status = 'paused'")
            paused = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM auto_like_tasks WHERE status = 'failed'")
            failed = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM auto_like_tasks WHERE status = 'stopped'")
            stopped = cur.fetchone()[0]

            cur.execute("SELECT COALESCE(SUM(target_likes), 0) FROM auto_like_tasks")
            total_target = cur.fetchone()[0]

            cur.execute("SELECT COALESCE(SUM(delivered_likes), 0) FROM auto_like_tasks")
            total_delivered = cur.fetchone()[0]

            conn.close()

        return {
            'total': total,
            'scheduled': scheduled,
            'running': running,
            'completed': completed,
            'paused': paused,
            'failed': failed,
            'stopped': stopped,
            'total_target': total_target,
            'total_delivered': total_delivered,
            'total_remaining': total_target - total_delivered,
        }
    except Exception as e:
        print(f"⚠️ get_auto_like_stats failed: {e}", flush=True)
        return {
            'total': 0, 'scheduled': 0, 'running': 0, 'completed': 0,
            'paused': 0, 'failed': 0, 'stopped': 0,
            'total_target': 0, 'total_delivered': 0, 'total_remaining': 0,
        }


# ============ AUTO LIKE EXECUTION ============

async def execute_auto_like_task(task):
    """Execute a single auto like task via the existing API."""
    task_id = task['id']
    uid = task['uid']
    region = task['region']
    owner_id = task['owner_id']
    target_likes = task['target_likes']
    delivered_likes = task.get('delivered_likes', 0) or 0

    remaining = target_likes - delivered_likes
    if remaining <= 0:
        set_auto_like_task_status(task_id, 'completed')
        return

    # Determine how many tokens to request this time
    # Cap at remaining to avoid exceeding target
    tokens_to_request = min(remaining, 400)  # Max 400 per API request

    try:
        # Call the existing like API
        result = await call_like_api(region, str(uid), tokens_to_request)

        if result is None or result.get("error"):
            error_msg = result.get("error", "Unknown error") if result else "No response"
            add_auto_like_history(
                task_id, owner_id, uid, region, 0,
                0, 0, 'failed', error_msg
            )
            print(f"⚠️ Auto like task {task_id} failed: {error_msg}", flush=True)
            return

        status = result.get('status')
        likes_given = result.get('LikesGivenByAPI', 0)
        before = result.get('LikesbeforeCommand', 0)
        after = result.get('LikesafterCommand', 0)

        if status == 1:
            # Successful likes
            actual_given = likes_given if likes_given > 0 else (after - before if after > before else 0)
            actual_given = max(0, min(actual_given, remaining))  # Cap at remaining

            new_delivered = delivered_likes + actual_given
            new_remaining = target_likes - new_delivered

            # Update task progress
            update_auto_like_task_progress(
                task_id, actual_gives if False else actual_given,
                status='completed' if new_remaining <= 0 else 'scheduled',
                last_executed=datetime.now(BD_TZ).isoformat(),
                next_execution=(
                    (datetime.now(BD_TZ) + __import__('datetime').timedelta(days=1))
                    .replace(hour=4, minute=1, second=0, microsecond=0)
                    .isoformat()
                ) if new_remaining > 0 else None
            )

            add_auto_like_history(
                task_id, owner_id, uid, region, actual_given,
                before, after, 'success'
            )

            print(f"✅ Auto like task {task_id}: +{actual_given} likes "
                  f"({new_delivered}/{target_likes})", flush=True)

            # Announce progress
            if AUTO_LIKE_BROADCAST_ENABLED:
                await announce_auto_like_progress(
                    task_id, owner_id, uid, region,
                    actual_given, new_delivered, target_likes, new_remaining,
                    status='progress' if new_remaining > 0 else 'completed'
                )

            if new_remaining <= 0:
                set_auto_like_task_status(task_id, 'completed')
        else:
            # Status 2 - limit reached or other
            add_auto_like_history(
                task_id, owner_id, uid, region, 0,
                before, after, 'failed',
                f"API status {status}: Likes not given"
            )
            print(f"⚠️ Auto like task {task_id}: API status {status}, no likes given", flush=True)

    except Exception as e:
        print(f"⚠️ execute_auto_like_task {task_id} error: {e}", flush=True)
        add_auto_like_history(
            task_id, owner_id, uid, region, 0,
            0, 0, 'failed', str(e)[:200]
        )


async def auto_like_scheduler_loop():
    """Background scheduler loop - checks every minute for due tasks."""
    global AUTO_LIKE_SCHEDULER_RUNNING
    AUTO_LIKE_SCHEDULER_RUNNING = True

    print("⏰ Auto Like scheduler started", flush=True)

    while AUTO_LIKE_SCHEDULER_RUNNING:
        try:
            now_bd = datetime.now(BD_TZ)

            # Check if it's 4:01 AM Bangladesh time
            if now_bd.hour == 4 and now_bd.minute == 1:
                tasks = get_due_auto_like_tasks()
                for task in tasks:
                    if not AUTO_LIKE_SCHEDULER_RUNNING:
                        break
                    await execute_auto_like_task(task)

            # Sleep 60 seconds before next check
            await asyncio.sleep(60)
        except Exception as e:
            print(f"⚠️ Auto like scheduler error: {e}", flush=True)
            await asyncio.sleep(60)


def start_auto_like_scheduler():
    """Start the auto like scheduler background task."""
    global AUTO_LIKE_SCHEDULER_TASK, AUTO_LIKE_SCHEDULER_RUNNING
    if AUTO_LIKE_SCHEDULER_TASK is None or AUTO_LIKE_SCHEDULER_TASK.done():
        AUTO_LIKE_SCHEDULER_RUNNING = True
        AUTO_LIKE_SCHEDULER_TASK = asyncio.create_task(auto_like_scheduler_loop())
        print("🚀 Auto Like scheduler task created", flush=True)


def stop_auto_like_scheduler():
    """Stop the auto like scheduler."""
    global AUTO_LIKE_SCHEDULER_RUNNING
    AUTO_LIKE_SCHEDULER_RUNNING = False
    if AUTO_LIKE_SCHEDULER_TASK and not AUTO_LIKE_SCHEDULER_TASK.done():
        AUTO_LIKE_SCHEDULER_TASK.cancel()
        print("🛑 Auto Like scheduler stopped", flush=True)


# Global reference to the Telegram Application for announcements
app_bot_instance = None


def set_app_bot(app_instance):
    """Store reference to the Telegram Application for announcements."""
    global app_bot_instance
    app_bot_instance = app_instance


async def announce_auto_like_created(task, bot_instance=None):
    """Announce a new auto like task to channel and group."""
    if not AUTO_LIKE_BROADCAST_ENABLED:
        return

    bot = bot_instance or app_bot_instance
    if bot is None:
        return

    uid = task['uid']
    region = task['region']
    target = task['target_likes']
    delivered = task.get('delivered_likes', 0) or 0
    remaining = target - delivered
    pct = (delivered / target * 100) if target > 0 else 0

    msg = (
        "🔥 *AUTO LIKE ACTIVATED*\n\n"
        f"👤 UID: `{uid}`\n"
        f"🌍 Region: {region}\n"
        f"🎯 Target: {target:,} Likes\n"
        f"❤️ Delivered: {delivered:,}\n"
        f"📉 Remaining: {remaining:,}\n"
        f"📊 Progress: {pct:.1f}%\n"
        f"⏰ Schedule: Daily 04:01 AM\n"
        f"🇧🇩 Timezone: Bangladesh\n"
        f"🟢 Status: SCHEDULED"
    )

    await _send_announcement(bot, msg, task['id'], update_existing=False)


async def announce_auto_like_progress(task_id, owner_id, uid, region,
                                       likes_delivered, total_delivered,
                                       target_likes, remaining, status,
                                       bot_instance=None):
    """Update progress announcement in channel and group."""
    if not AUTO_LIKE_BROADCAST_ENABLED:
        return

    bot = bot_instance or app_bot_instance
    if bot is None:
        return

    pct = (total_delivered / target_likes * 100) if target_likes > 0 else 0
    bar_length = 20
    filled = int(bar_length * total_delivered / target_likes) if target_likes > 0 else 0
    bar = "█" * filled + "░" * (bar_length - filled)

    status_emoji = {
        'progress': '🔄',
        'completed': '🎉',
        'failed': '❌',
        'paused': '⏸️',
    }.get(status, '🔄')

    status_text = {
        'progress': 'PROGRESS',
        'completed': 'COMPLETED',
        'failed': 'FAILED',
        'paused': 'PAUSED',
    }.get(status, 'PROGRESS')

    msg = (
        f"{status_emoji} *AUTO LIKE {status_text}*\n\n"
        f"👤 UID: `{uid}`\n"
        f"🌍 Region: {region}\n"
        f"🎯 Target: {target_likes:,} Likes\n"
        f"❤️ Delivered: {total_delivered:,}\n"
        f"📉 Remaining: {remaining:,}\n"
        f"📊 Progress: {pct:.1f}%\n"
        f"{bar}\n"
        f"🟢 Status: {status_text}"
    )

    await _send_announcement(bot, msg, task_id, update_existing=True)


async def announce_auto_like_completed(task, bot_instance=None):
    """Announce completion of auto like task."""
    if not AUTO_LIKE_BROADCAST_ENABLED:
        return

    bot = bot_instance or app_bot_instance
    if bot is None:
        return

    uid = task['uid']
    region = task['region']
    target = task['target_likes']
    delivered = task.get('delivered_likes', 0) or 0

    msg = (
        "🎉 *AUTO LIKE COMPLETED!*\n\n"
        f"👤 UID: `{uid}`\n"
        f"🌍 Region: {region}\n"
        f"🎯 Target: {target:,} Likes\n"
        f"❤️ Successfully Delivered: {delivered:,}\n"
        f"🟢 Status: AUTOMATICALLY DISABLED"
    )

    await _send_announcement(bot, msg, task['id'], update_existing=False)


async def _send_announcement(bot, text, task_id, update_existing=False):
    """Send or update announcement in channel and group."""
    global AUTO_LIKE_CHANNEL_ID, AUTO_LIKE_GROUP_ID

    task = get_auto_like_task(task_id)
    if task is None:
        return

    channel_msg_id = task.get('channel_msg_id')
    group_msg_id = task.get('group_msg_id')

    targets = []
    if AUTO_LIKE_CHANNEL_ID:
        targets.append(('channel', AUTO_LIKE_CHANNEL_ID, channel_msg_id))
    if AUTO_LIKE_GROUP_ID:
        targets.append(('group', AUTO_LIKE_GROUP_ID, group_msg_id))

    if not targets:
        return

    for target_type, chat_id, msg_id in targets:
        try:
            if update_existing and msg_id:
                try:
                    await bot.edit_message_text(
                        chat_id=chat_id, message_id=msg_id, text=text,
                        parse_mode='Markdown'
                    )
                except Exception:
                    sent = await bot.send_message(
                        chat_id=chat_id, text=text, parse_mode='Markdown'
                    )
                    if sent:
                        if target_type == 'channel':
                            with GROUP_DB_LOCK:
                                conn = sqlite3.connect(AUTO_LIKE_DB_FILE)
                                cur = conn.cursor()
                                cur.execute(
                                    "UPDATE auto_like_tasks SET channel_msg_id = ? WHERE id = ?",
                                    (sent.message_id, task_id)
                                )
                                conn.commit()
                                conn.close()
                        elif target_type == 'group':
                            with GROUP_DB_LOCK:
                                conn = sqlite3.connect(AUTO_LIKE_DB_FILE)
                                cur = conn.cursor()
                                cur.execute(
                                    "UPDATE auto_like_tasks SET group_msg_id = ? WHERE id = ?",
                                    (sent.message_id, task_id)
                                )
                                conn.commit()
                                conn.close()
            else:
                sent = await bot.send_message(
                    chat_id=chat_id, text=text, parse_mode='Markdown'
                )
                if sent:
                    if target_type == 'channel':
                        with GROUP_DB_LOCK:
                            conn = sqlite3.connect(AUTO_LIKE_DB_FILE)
                            cur = conn.cursor()
                            cur.execute(
                                "UPDATE auto_like_tasks SET channel_msg_id = ? WHERE id = ?",
                                (sent.message_id, task_id)
                            )
                            conn.commit()
                            conn.close()
                    elif target_type == 'group':
                        with GROUP_DB_LOCK:
                            conn = sqlite3.connect(AUTO_LIKE_DB_FILE)
                            cur = conn.cursor()
                            cur.execute(
                                "UPDATE auto_like_tasks SET group_msg_id = ? WHERE id = ?",
                                (sent.message_id, task_id)
                            )
                            conn.commit()
                            conn.close()
        except Exception as e:
            print(f"⚠️ Announcement send failed for {target_type}: {e}", flush=True)


forward_channel_ids = []       # List of channel IDs to forward successful likes to
forward_channel_links = {}     # Map: channel_id -> invite link

# Bot state globals (initialized before load_data)
bot_status = "on"
bot_mode = "public"
allowed_groups = {}
daily_stats = {}
user_limits = {}
daily_limit = 2
like_tokens = 240
force_join = False
force_join_channels = []
force_join_links = {}

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
    global allowed_groups, daily_stats, user_limits, bot_status, bot_mode, daily_limit, like_tokens, force_join, force_join_channels, force_join_links, forward_channel_ids, forward_channel_links, forward_channel_id, forward_channel_link

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
        forward_channel_ids = list(cfg.get('forward_channel_ids', forward_channel_ids) or [])
        forward_channel_links = dict(cfg.get('forward_channel_links', forward_channel_links) or {})

        # Backward compatibility: if forward_channel_ids is empty but forward_channel_id exists,
        # migrate the single value to the list
        if not forward_channel_ids:
            old_single = cfg.get('forward_channel_id', '')
            if old_single:
                forward_channel_ids = [old_single]
                print(f"🔄 Migrated forward_channel_id '{old_single}' to forward_channel_ids list", flush=True)

    # Migrate stored force-join channel identifiers (e.g. "channel-1" -> "@FFLIKESPRO2")
    try:
        _migrate_force_join_channels()
    except Exception as e:
        print(f"⚠️ Force-join migration failed: {e}", flush=True)

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

    # Initialize group management SQLite DB and sync existing broadcast groups
    try:
        init_group_db()
        synced = sync_groups_from_broadcast()
        if synced:
            print(f"📦 Group DB: {synced} group(s) synced from broadcast list", flush=True)
    except Exception as e:
        print(f"⚠️ Group DB init/sync failed: {e}", flush=True)


def _save_successful_likes():
    """No-op placeholder — successful likes are saved immediately by log_successful_like()."""
    pass


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
                'forward_channel_ids': forward_channel_ids,
                'forward_channel_links': forward_channel_links,
                # Backward compatibility: also save single value
                'forward_channel_id': forward_channel_ids[0] if forward_channel_ids else '',
                'forward_channel_link': forward_channel_links.get(forward_channel_ids[0], '') if forward_channel_ids else '',
            }, f, indent=2)
        # Save broadcast list
        try:
            with open(BROADCAST_FILE, 'w') as f:
                json.dump(_load_broadcast_data(), f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"⚠️ save_all broadcast write failed: {e}", flush=True)
        # Save successful likes log
        try:
            _save_successful_likes()
        except Exception as e:
            print(f"⚠️ save_all likes write failed: {e}", flush=True)
    except Exception as e:
        print(f"⚠️ save_all local write failed: {e}", flush=True)

    # Queue for GitHub sync (background, retried, never blocks the bot).
    if github_persistence:
        for key in ('allowed', 'stats', 'users', 'config', 'groups', 'broadcast', 'likes'):
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


async def safe_edit_message(target, text: str, reply_markup=None, parse_mode: str = 'Markdown') -> bool:
    """Edit a message safely. Returns True if edited, or if content was unchanged ('not modified').

    Never raises — swallows 'Message is not modified' so tapping Verify again
    with the same result never crashes the handler.
    """
    try:
        await target.edit_message_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
        return True
    except BadRequest as e:
        if "Message is not modified" in str(e):
            return True  # Same content as before — that's fine
        print(f"⚠️ Edit message BadRequest: {e}", flush=True)
        return False
    except Exception as e:
        print(f"⚠️ Edit message error: {e}", flush=True)
        return False


async def is_user_member_single(context: ContextTypes.DEFAULT_TYPE, user_id: int, channel: str) -> bool:
    """Check membership in one channel. Returns True only if truly joined.

    Tries the stored channel identifier first. If that fails (e.g. placeholder
    name like "channel-1"), falls back to extracting the username from the
    stored invite link.
    """
    # Try the stored channel identifier first
    try:
        member = await context.bot.get_chat_member(chat_id=channel, user_id=user_id)
        joined = _member_is_joined(member)
        if not joined:
            print(f"🔍 getChatMember '{channel}' user={user_id}: status={getattr(member, 'status', '?')} → NOT joined", flush=True)
        return joined
    except BadRequest as e:
        msg = str(e).lower()
        # If chat not found, try extracting from the link
        if "chat not found" in msg:
            link = force_join_links.get(str(channel), "") or ""
            extracted = _extract_username_from_link(link)
            if extracted and extracted != channel:
                print(f"🔄 Falling back to link-extracted identifier '{extracted}' for '{channel}'", flush=True)
                try:
                    member = await context.bot.get_chat_member(chat_id=extracted, user_id=user_id)
                    joined = _member_is_joined(member)
                    if not joined:
                        print(f"🔍 getChatMember '{extracted}' user={user_id}: status={getattr(member, 'status', '?')} → NOT joined", flush=True)
                    return joined
                except BadRequest as e2:
                    print(f"❌ getChatMember '{extracted}' failed: {e2}", flush=True)
                    return False
                except Exception as e2:
                    print(f"⚠️ getChatMember '{extracted}' failed: {type(e2).__name__}: {e2}", flush=True)
                    return False
        print(f"❌ getChatMember '{channel}': chat not found — bot may not be in this channel or ID is wrong: {e}", flush=True)
        return False
    except Exception as e:
        print(f"⚠️ getChatMember '{channel}' failed: {type(e).__name__}: {e}", flush=True)
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
            await safe_edit_message(update.callback_query, text, reply_markup=reply_markup)
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
    # Check SQLite DB first (authoritative group status)
    db_allowed = is_group_allowed_db(chat_id, chat_type)
    # Also check legacy allowed_groups for backward compatibility
    legacy_allowed = str(chat_id) in allowed_groups
    return db_allowed or legacy_allowed

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
    register_chat(update)
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
        "`/groups` – List and manage all groups (pagination)\n"
        "`/allowgroup <chat_id>` – Allow a group\n"
        "`/blockgroup <chat_id>` – Block a group\n"
        "`/syncgroups` – Sync groups from broadcast list"
    )
    await reply(update, msg)

async def info_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await force_join_check(update, context): return
    if bot_status == "off":
        await reply(update, "🔴 Bot is OFF")
        return
    register_chat(update)
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
    register_chat(update)

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
        # Persist successful like history
        log_successful_like(uid, region, given, before, after, user_id)
        save_all()
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

    # Forward the EXACT success result message to ALL configured forward channels.
    # Native forwardMessage preserves the original formatting exactly.
    if status == 1 and sent_msg is not None:
        for fwd_channel_id in forward_channel_ids:
            if not fwd_channel_id:
                continue
            try:
                await context.bot.forward_message(
                    chat_id=fwd_channel_id,
                    from_chat_id=sent_msg.chat.id,
                    message_id=sent_msg.message_id,
                )
                print(f"✅ Forwarded successful like to {fwd_channel_id}", flush=True)
            except Exception as e:
                # Log the error but never break the successful like operation.
                print(f"⚠️ Failed to forward successful like to {fwd_channel_id}: {e}", flush=True)

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


async def my_chat_member_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Auto-register groups when the bot is added, and unregister when removed/kicked.

    Handles my_chat_member updates so groups are tracked even when no user
    sends a command. Also removes groups from broadcast when the bot leaves
    or is kicked.
    """
    try:
        chat = update.effective_chat
        if chat is None:
            return
        my_member = update.my_chat_member
        if my_member is None:
            return

        old_status = my_member.old_chat_member.status
        new_status = my_member.new_chat_member.status

        # Get status as string (PTB enum -> str)
        def _status_str(s):
            return getattr(s, "value", s)

        old_s = _status_str(old_status)
        new_s = _status_str(new_status)

        # Bot was added to or became admin/member in a group/supergroup/channel
        if new_s in ("member", "administrator", "creator") and old_s not in ("member", "administrator", "creator"):
            add_to_broadcast(chat.id, chat.type, getattr(chat, "title", None))
            upsert_group(
                chat_id=chat.id,
                chat_title=getattr(chat, "title", None),
                chat_username=getattr(chat, "username", None),
                chat_type=chat.type,
                status='allowed',
                bot_member_status=new_s,
                added_by=update.effective_user.id if update.effective_user else None
            )
            print(f"✅ Auto-registered chat {chat.id} ({chat.type}) via my_chat_member", flush=True)
            return

        # Bot was already a member but chat info changed (title/username update)
        if new_s in ("member", "administrator", "creator") and old_s in ("member", "administrator", "creator"):
            upsert_group(
                chat_id=chat.id,
                chat_title=getattr(chat, "title", None),
                chat_username=getattr(chat, "username", None),
                chat_type=chat.type,
                # Do NOT pass status — preserve existing ALLOWED/BLOCKED/INACTIVE
                bot_member_status=new_s
            )
            print(f"🔄 Updated chat {chat.id} ({chat.type}) info via my_chat_member", flush=True)
            return

        # Bot was kicked/left — mark as inactive in DB (don't delete record)
        if new_s in ("left", "kicked") and old_s in ("member", "administrator", "creator"):
            remove_from_broadcast(chat.id)
            set_group_status(chat.id, 'inactive')
            upsert_group(
                chat_id=chat.id,
                chat_title=getattr(chat, "title", None),
                chat_username=getattr(chat, "username", None),
                chat_type=chat.type,
                status='inactive',
                bot_member_status=new_s
            )
            print(f"🗑️ Marked chat {chat.id} ({chat.type}) as inactive (bot removed)", flush=True)
            return
    except Exception as e:
        print(f"⚠️ my_chat_member_handler failed: {e}", flush=True)


def remove_from_broadcast(chat_id):
    """Remove a chat from the broadcast list (e.g. when bot is kicked from group)."""
    try:
        data = _load_broadcast_data()
        cid = str(chat_id)
        removed = False
        if cid in data.get("chats", {}):
            del data["chats"][cid]
            removed = True
        if removed:
            _save_broadcast_data(data)
            return True
        return False
    except Exception as e:
        print(f"⚠️ remove_from_broadcast failed: {e}", flush=True)
        return False


async def register_group_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manually register a group for broadcasts and SQLite DB. Admin only.

    Usage: /registergroup              (registers current group)
           /registergroup <CHAT_ID>    (registers a group by its chat ID)
    """
    if not is_admin(update.effective_user.id):
        await reply(update, "❌ Admin only")
        return

    # Determine target chat_id
    if context.args and context.args[0].lstrip('-').isdigit():
        # Manual registration by chat ID
        target_chat_id = int(context.args[0])
        try:
            chat = await context.bot.get_chat(target_chat_id)
            chat_title = getattr(chat, "title", None)
            chat_username = getattr(chat, "username", None)
            chat_type = chat.type
        except Exception as e:
            await reply(update, f"❌ Could not fetch chat `{target_chat_id}`:\n`{type(e).__name__}: {e}`")
            return
    else:
        # Register current chat
        chat = update.effective_chat
        if chat is None or chat.type == "private":
            await reply(update, "❌ Use this command in a group/supergroup, or provide a CHAT_ID.")
            return
        target_chat_id = chat.id
        chat_title = getattr(chat, "title", None)
        chat_username = getattr(chat, "username", None)
        chat_type = chat.type

    # Register in broadcast list
    add_to_broadcast(target_chat_id, chat_type, chat_title)

    # Register in SQLite DB (preserves existing ALLOWED/BLOCKED status)
    upsert_group(
        chat_id=target_chat_id,
        chat_title=chat_title,
        chat_username=chat_username,
        chat_type=chat_type,
        # Do NOT pass status — preserve existing ALLOWED/BLOCKED/INACTIVE
        bot_member_status='member'
    )

    await reply(
        update,
        f"✅ *Group Registered*\n\n"
        f"📢 Chat ID: `{target_chat_id}`\n"
        f"📝 Title: {chat_title or 'Unknown'}\n"
        f"📛 Username: {chat_username or 'No username'}\n"
        f"📁 Type: {chat_type}\n"
        f"🔢 Total groups in DB: `{len(get_all_groups())}`"
    )


async def apistatus_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show API token status. Admin only. Reads from the API's /token_info endpoint."""
    if not is_admin(update.effective_user.id):
        await reply(update, "❌ Admin only")
        return

    try:
        base = API_URL.rstrip("/") + "/"
        url = f"{base}token_info?key={API_KEY}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    await reply(update, f"❌ API Error: HTTP {resp.status}")
                    return
                data = await resp.json(content_type=None)

        total = data.get("total", {})
        files = data.get("files", {})
        config = data.get("config", {})

        total_tokens = total.get("tokens", 0)
        used_today = total.get("used_today", 0)
        capacity_today = total.get("capacity_today", 0)
        remaining_today = total.get("remaining_today", 0)
        likes_per_token = config.get("LIKES_PER_TOKEN_PER_DAY", 0)

        # Count tokens per file
        file_token_counts = {}
        for fname, fstat in files.items():
            file_token_counts[fname] = fstat.get("token_count", 0)

        # Active = tokens remaining capacity (fresh/valid in bank)
        active_tokens = total_tokens  # All tokens in bank are valid/fresh
        invalid_tokens = 0

        # Currently configured per-request tokens (from /setapi, /setapi100, /setapimax, /maxff)
        current_like_tokens = like_tokens

        # Determine mode name based on current like_tokens setting
        if current_like_tokens == 400:
            mode_name = "MAX FF Mode"
        elif current_like_tokens == 100:
            mode_name = "100 Token Mode"
        else:
            mode_name = "Custom API Mode"

        msg = (
            "🔑 *API STATUS*\n\n"
            f"📊 Total API Tokens: `{total_tokens}`\n"
            f"🟢 Active Tokens: `{active_tokens}`\n"
            f"🔴 Invalid/Disabled: `{invalid_tokens}`\n\n"
            f"⚙️ *API CONFIGURATION*\n"
            f"📦 Total Available Tokens: `{total_tokens}`\n"
            f"🎯 Currently Set Tokens: `{current_like_tokens}`\n"
            f"❤️ Expected Likes: ~{current_like_tokens}\n"
            f"🔧 Current Mode: {mode_name}\n\n"
            f"📈 *DAILY USAGE*\n"
            f"📊 Used Today: `{used_today}`\n"
            f"📉 Remaining Today: `{remaining_today}`\n"
            f"❤️ Likes Per Token/Day: `{likes_per_token}`\n"
        )

        # Per-file breakdown
        if file_token_counts:
            msg += "\n*Per File:*\n"
            for fname, count in file_token_counts.items():
                msg += f"• `{fname}`: `{count}` tokens\n"

        msg += "\n🔐 @DCFsolayman"
        await reply(update, msg)

    except asyncio.TimeoutError:
        await reply(update, "❌ API request timed out.")
    except Exception as e:
        await reply(update, f"❌ Error fetching API status: `{type(e).__name__}`")


# ============ GROUP MANAGEMENT COMMANDS ============

async def groups_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all groups with pagination and ALLOW/BLOCK/DETAILS buttons. Admin only."""
    if not is_admin(update.effective_user.id):
        await reply(update, "❌ Admin only")
        return

    # Parse page number from args
    page = 0
    if context.args and context.args[0].isdigit():
        page = int(context.args[0])
    if page < 0:
        page = 0

    groups = get_all_groups()
    if not groups:
        counts = get_group_count_by_status()
        await reply(
            update,
            "👥 *Group Management*\n\n"
            f"📊 Total Groups: `{counts.get('allowed', 0) + counts.get('blocked', 0) + counts.get('inactive', 0)}`\n"
            f"🟢 Allowed: `{counts.get('allowed', 0)}`\n"
            f"🔴 Blocked: `{counts.get('blocked', 0)}`\n"
            f"⚪ Inactive: `{counts.get('inactive', 0)}`\n\n"
            "No groups registered yet. Use `/syncgroups` to import existing groups, "
            "or the bot will auto-register groups when added via my_chat_member."
        )
        return

    counts = get_group_count_by_status()
    total = len(groups)
    per_page = 10
    total_pages = (total + per_page - 1) // per_page
    if page >= total_pages:
        page = total_pages - 1

    start = page * per_page
    end = start + per_page
    page_groups = groups[start:end]

    # Build header
    msg = (
        "👥 *Group Management*\n\n"
        f"📊 Total: `{total}` | 🟢 Allowed: `{counts.get('allowed', 0)}` | "
        f"🔴 Blocked: `{counts.get('blocked', 0)}` | ⚪ Inactive: `{counts.get('inactive', 0)}`\n"
        f"📄 Page `{page + 1}` of `{total_pages}`\n\n"
    )

    # Build group entries
    for g in page_groups:
        cid = g['chat_id']
        title = g.get('chat_title') or 'Unknown'
        username = g.get('chat_username') or 'No username'
        ctype = g.get('chat_type') or 'group'
        status = g.get('status', 'allowed')
        added = (g.get('added_at') or 'Unknown')[:10]

        status_emoji = "🟢" if status == 'allowed' else ("🔴" if status == 'blocked' else "⚪")
        status_text = status.upper()

        msg += (
            f"{status_emoji} *{title}*\n"
            f"   ID: `{cid}` | Type: {ctype} | Status: {status_text}\n"
            f"   Username: {username} | Added: {added}\n\n"
        )

    # Build inline keyboard
    keyboard = []
    for g in page_groups:
        cid = g['chat_id']
        status = g.get('status', 'allowed')
        allow_btn_text = "✅ ALLOW" if status != 'allowed' else "✅ ALLOWED"
        block_btn_text = "🔴 BLOCK" if status != 'blocked' else "🔴 BLOCKED"
        keyboard.append([
            InlineKeyboardButton(allow_btn_text, callback_data=f"group_allow_{cid}"),
            InlineKeyboardButton(block_btn_text, callback_data=f"group_block_{cid}"),
            InlineKeyboardButton("ℹ️ DETAILS", callback_data=f"group_details_{cid}"),
        ])

    # Pagination buttons
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"groups_page_{page - 1}"))
    if end < total:
        nav_buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"groups_page_{page + 1}"))
    if nav_buttons:
        keyboard.append(nav_buttons)

    reply_markup = InlineKeyboardMarkup(keyboard)
    try:
        await update.message.reply_text(msg, reply_markup=reply_markup, parse_mode='Markdown')
    except Exception as e:
        print(f"⚠️ groups_cmd reply failed: {e}", flush=True)


async def allowgroup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Allow a group by chat ID. Admin only."""
    if not is_admin(update.effective_user.id):
        await reply(update, "❌ Admin only")
        return
    if not context.args or not context.args[0].lstrip('-').isdigit():
        await reply(update, "❌ Usage: `/allowgroup <chat_id>`\nExample: `/allowgroup -1001234567890`")
        return
    chat_id = int(context.args[0])
    set_group_status(chat_id, 'allowed')
    # Also add to legacy allowed_groups for backward compat
    allowed_groups[str(chat_id)] = {'name': '', 'by': update.effective_user.id, 'date': today_str()}
    save_all()
    detail = get_group_detail(chat_id)
    title = detail.get('chat_title', 'Unknown') if detail else 'Unknown'
    await reply(
        update,
        f"✅ *Group Allowed*\n\n"
        f"📢 Chat ID: `{chat_id}`\n"
        f"📝 Title: {title}\n"
        f"Users can now use like commands in this group."
    )


async def blockgroup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Block a group by chat ID. Admin only."""
    if not is_admin(update.effective_user.id):
        await reply(update, "❌ Admin only")
        return
    if not context.args or not context.args[0].lstrip('-').isdigit():
        await reply(update, "❌ Usage: `/blockgroup <chat_id>`\nExample: `/blockgroup -1001234567890`")
        return
    chat_id = int(context.args[0])
    set_group_status(chat_id, 'blocked')
    # Remove from legacy allowed_groups for backward compat
    allowed_groups.pop(str(chat_id), None)
    save_all()
    detail = get_group_detail(chat_id)
    title = detail.get('chat_title', 'Unknown') if detail else 'Unknown'
    await reply(
        update,
        f"🔴 *Group Blocked*\n\n"
        f"📢 Chat ID: `{chat_id}`\n"
        f"📝 Title: {title}\n"
        f"Normal users cannot use like commands in this group. Admin commands still work."
    )


async def syncgroups_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sync groups from broadcast list into the SQLite database. Admin only.

    Also attempts to fetch fresh chat info via get_chat() for each registered
    broadcast chat so titles and usernames stay up to date.
    """
    if not is_admin(update.effective_user.id):
        await reply(update, "❌ Admin only")
        return

    # Step 1: Sync from broadcast list (preserves existing ALLOWED/BLOCKED status)
    synced = sync_groups_from_broadcast()

    # Step 2: Try to refresh chat info via get_chat() for each broadcast chat
    data = _load_broadcast_data()
    chats = data.get("chats", {})
    refreshed = 0
    for cid, info in chats.items():
        try:
            chat_id = int(cid)
        except (TypeError, ValueError):
            continue
        try:
            chat = await context.bot.get_chat(chat_id)
            upsert_group(
                chat_id=chat_id,
                chat_title=getattr(chat, "title", None) or info.get("title"),
                chat_username=getattr(chat, "username", None),
                chat_type=chat.type,
                # Do NOT pass status — preserve existing ALLOWED/BLOCKED/INACTIVE
                bot_member_status='member'
            )
            refreshed += 1
        except Exception as e:
            print(f"⚠️ Could not refresh chat {chat_id}: {e}", flush=True)

    total = len(get_all_groups())
    await reply(
        update,
        f"🔄 *Group Sync Complete*\n\n"
        f"Synced from broadcast: `{synced}`\n"
        f"Refreshed via get_chat: `{refreshed}`\n"
        f"Total groups in DB: `{total}`\n\n"
        f"⚠️ Telegram does not provide a list of all groups the bot joined.\n"
        f"Use `/registergroup <CHAT_ID>` to manually add missing groups."
    )





# ============ DATABASE INTEGRITY & BACKUP ============

BACKUP_DIR = "backups"
BACKUP_META_FILE = "backup_status.json"


def init_backup_dir():
    """Ensure the backup directory exists."""
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
    except Exception as e:
        print(f"⚠️ Backup dir init failed: {e}", flush=True)


def load_backup_status():
    """Load backup metadata."""
    try:
        with open(BACKUP_META_FILE, 'r') as f:
            return json.load(f)
    except Exception:
        return {"last_backup": None, "backup_count": 0, "last_error": None}


def save_backup_status(status):
    """Save backup metadata."""
    try:
        with open(BACKUP_META_FILE, 'w') as f:
            json.dump(status, f, indent=2)
    except Exception as e:
        print(f"⚠️ Backup status write failed: {e}", flush=True)


def verify_database_integrity():
    """Verify SQLite database integrity. Returns (ok, message)."""
    try:
        with GROUP_DB_LOCK:
            conn = sqlite3.connect(GROUP_DB_FILE)
            cur = conn.cursor()
            cur.execute("PRAGMA integrity_check")
            result = cur.fetchone()
            conn.close()
        if result and result[0] == "ok":
            return True, "Database integrity check passed"
        return False, f"Integrity check failed: {result}"
    except Exception as e:
        return False, f"Integrity check error: {e}"


def create_backup():
    """Create a timestamped backup of all persistent data files."""
    init_backup_dir()
    status = load_backup_status()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_files = []

    files_to_backup = [
        DATA_FILES['allowed'],
        DATA_FILES['stats'],
        DATA_FILES['users'],
        DATA_FILES['config'],
        BROADCAST_FILE,
        LIKE_LOG_FILE,
        GROUP_DB_FILE,
    ]

    for src in files_to_backup:
        if not os.path.exists(src):
            continue
        try:
            dst = os.path.join(BACKUP_DIR, f"{timestamp}_{os.path.basename(src)}")
            import shutil
            shutil.copy2(src, dst)
            backup_files.append(os.path.basename(src))
        except Exception as e:
            print(f"⚠️ Backup copy failed for {src}: {e}", flush=True)

    status["last_backup"] = timestamp
    status["backup_count"] = status.get("backup_count", 0) + 1
    status["last_error"] = None
    status["files"] = backup_files
    save_backup_status(status)

    # Also push backup status to GitHub
    if github_persistence:
        try:
            github_persistence.queue_push('config')
        except Exception:
            pass

    return timestamp, backup_files


async def backup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Create a database backup. Admin only."""
    if not is_admin(update.effective_user.id):
        await reply(update, "❌ Admin only")
        return

    # Verify integrity first
    ok, msg = verify_database_integrity()
    if not ok:
        await reply(update, f"❌ Database integrity check failed:\n{msg}")
        return

    try:
        timestamp, files = create_backup()
        await reply(
            update,
            f"✅ *Backup Created*\n\n"
            f"🕒 Timestamp: `{timestamp}`\n"
            f"📁 Files: `{', '.join(files)}`\n"
            f"📍 Location: `{BACKUP_DIR}/`"
        )
    except Exception as e:
        await reply(update, f"❌ Backup failed: `{type(e).__name__}: {e}`")


async def backupstatus_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show backup status. Admin only."""
    if not is_admin(update.effective_user.id):
        await reply(update, "❌ Admin only")
        return

    status = load_backup_status()
    ok, integrity_msg = verify_database_integrity()

    await reply(
        update,
        f"💾 *Backup Status*\n\n"
        f"🔍 Integrity: {'✅ OK' if ok else '❌ FAILED'}\n"
        f"   {integrity_msg}\n"
        f"🕒 Last Backup: `{status.get('last_backup') or 'Never'}`\n"
        f"📦 Backup Count: `{status.get('backup_count', 0)}`\n"
        f"❌ Last Error: `{status.get('last_error') or 'None'}`\n"
        f"📁 Backup Dir: `{BACKUP_DIR}/`"
    )


async def group_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline button callbacks for group management."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if not is_admin(user_id):
        try:
            await query.edit_message_text("❌ Admin only")
        except Exception:
            pass
        return

    data = query.data

    # Parse callback: group_allow_{chat_id}, group_block_{chat_id}, group_details_{chat_id}, groups_page_{page}
    parts = data.split("_")
    if len(parts) < 3:
        return

    action = parts[0] + "_" + parts[1]  # group_allow, group_block, group_details, groups_page

    if action == "group_allow":
        chat_id = int(parts[2])
        set_group_status(chat_id, 'allowed')
        allowed_groups[str(chat_id)] = {'name': '', 'by': user_id, 'date': today_str()}
        save_all()
        detail = get_group_detail(chat_id)
        title = detail.get('chat_title', 'Unknown') if detail else 'Unknown'
        try:
            await query.edit_message_text(
                f"✅ *Group Allowed*\n\n"
                f"📢 Chat ID: `{chat_id}`\n"
                f"📝 Title: {title}\n"
                f"Users can now use like commands in this group.",
                parse_mode='Markdown'
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise

    elif action == "group_block":
        chat_id = int(parts[2])
        set_group_status(chat_id, 'blocked')
        allowed_groups.pop(str(chat_id), None)
        save_all()
        detail = get_group_detail(chat_id)
        title = detail.get('chat_title', 'Unknown') if detail else 'Unknown'
        try:
            await query.edit_message_text(
                f"🔴 *Group Blocked*\n\n"
                f"📢 Chat ID: `{chat_id}`\n"
                f"📝 Title: {title}\n"
                f"Normal users cannot use like commands. Admin commands still work.",
                parse_mode='Markdown'
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise

    elif action == "group_details":
        chat_id = int(parts[2])
        detail = get_group_detail(chat_id)
        if not detail:
            try:
                await query.edit_message_text("❌ Group not found in database.")
            except Exception:
                pass
            return
        status = detail.get('status', 'allowed')
        status_emoji = "🟢" if status == 'allowed' else ("🔴" if status == 'blocked' else "⚪")
        added = (detail.get('added_at') or 'Unknown')[:10]
        updated = (detail.get('updated_at') or 'Unknown')[:16]
        try:
            await query.edit_message_text(
                f"ℹ️ *Group Details*\n\n"
                f"📢 Title: {detail.get('chat_title', 'Unknown')}\n"
                f"🆔 Chat ID: `{detail.get('chat_id')}`\n"
                f"📛 Username: @{detail.get('chat_username') or '-'}\n"
                f"📁 Type: {detail.get('chat_type', 'group')}\n"
                f"{status_emoji} Status: {status.upper()}\n"
                f"🤖 Bot Member: {detail.get('bot_member_status', 'unknown')}\n"
                f"📅 Added: {added}\n"
                f"🔄 Updated: {updated}",
                parse_mode='Markdown'
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise

    elif action == "groups_page":
        page = int(parts[2])
        # Re-show the groups list at the given page
        groups = get_all_groups()
        if not groups:
            try:
                await query.edit_message_text("No groups registered.")
            except Exception:
                pass
            return

        counts = get_group_count_by_status()
        total = len(groups)
        per_page = 10
        total_pages = (total + per_page - 1) // per_page
        if page >= total_pages:
            page = total_pages - 1
        if page < 0:
            page = 0

        start = page * per_page
        end = start + per_page
        page_groups = groups[start:end]

        msg = (
            "👥 *Group Management*\n\n"
            f"📊 Total: `{total}` | 🟢 Allowed: `{counts.get('allowed', 0)}` | "
            f"🔴 Blocked: `{counts.get('blocked', 0)}` | ⚪ Inactive: `{counts.get('inactive', 0)}`\n"
            f"📄 Page `{page + 1}` of `{total_pages}`\n\n"
        )

        for g in page_groups:
            cid = g['chat_id']
            title = g.get('chat_title') or 'Unknown'
            username = g.get('chat_username') or '-'
            ctype = g.get('chat_type') or 'group'
            status = g.get('status', 'allowed')
            added = (g.get('added_at') or 'Unknown')[:10]

            status_emoji = "🟢" if status == 'allowed' else ("🔴" if status == 'blocked' else "⚪")
            status_text = status.upper()

            msg += (
                f"{status_emoji} *{title}*\n"
                f"   ID: `{cid}` | Type: {ctype} | Status: {status_text}\n"
                f"   Username: @{username} | Added: {added}\n\n"
            )

        keyboard = []
        for g in page_groups:
            cid = g['chat_id']
            status = g.get('status', 'allowed')
            allow_btn_text = "✅ ALLOW" if status != 'allowed' else "✅ ALLOWED"
            block_btn_text = "🔴 BLOCK" if status != 'blocked' else "🔴 BLOCKED"
            keyboard.append([
                InlineKeyboardButton(allow_btn_text, callback_data=f"group_allow_{cid}"),
                InlineKeyboardButton(block_btn_text, callback_data=f"group_block_{cid}"),
                InlineKeyboardButton("ℹ️ DETAILS", callback_data=f"group_details_{cid}"),
            ])

        nav_buttons = []
        if page > 0:
            nav_buttons.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"groups_page_{page - 1}"))
        if end < total:
            nav_buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"groups_page_{page + 1}"))
        if nav_buttons:
            keyboard.append(nav_buttons)

        try:
            await query.edit_message_text(
                msg,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode='Markdown'
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise


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
    user_targets = []
    group_targets = []
    seen = set()
    for raw_id in data.get("users", {}).keys():
        try:
            chat_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if chat_id in seen:
            continue
        seen.add(chat_id)
        user_targets.append(chat_id)
    for raw_id in data.get("chats", {}).keys():
        try:
            chat_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if chat_id in seen:
            continue
        seen.add(chat_id)
        group_targets.append(chat_id)

    if not user_targets and not group_targets:
        await reply(
            update,
            "❌ No registered users or groups yet.\n\n"
            "Recipients are added automatically when the bot is added to a group "
            "(my_chat_member) or when someone sends /start /info /like. "
            "Admins can also use /registergroup <CHAT_ID> to manually add groups."
        )
        return

    total_targets = len(user_targets) + len(group_targets)
    sent = 0
    failed = 0
    failed_ids = []

    all_targets = user_targets + group_targets
    for chat_id in all_targets:
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
        f"👤 Total Private Users: `{len(user_targets)}`\n"
        f"👥 Total Groups: `{len(group_targets)}`\n"
        f"📡 Total Recipients: `{total_targets}`\n"
        f"✅ Successful Deliveries: `{sent}`\n"
        f"❌ Failed Deliveries: `{failed}`"
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
import re


def _extract_username_from_link(link: str) -> str:
    """Extract channel username from a Telegram link.

    Supports:
      https://t.me/USERNAME      -> @USERNAME
      https://t.me/+INVITEHASH   -> +INVITEHASH (private invite, kept as-is)
      https://t.me/joinchat/...  -> kept as-is
      @USERNAME                  -> @USERNAME
      USERNAME                   -> @USERNAME
    """
    if not link:
        return ""
    link = link.strip()

    # Already a username
    if link.startswith("@"):
        return link
    if link.isalnum() or "_" in link:
        return "@" + link

    # Parse t.me links
    m = re.match(r"^https?://t\.me/(.+)$", link, re.IGNORECASE)
    if m:
        path = m.group(1)
        if path.startswith("+"):
            # Private invite link — keep as-is for the Join button,
            # but cannot be used with get_chat_member directly.
            return path
        if path.startswith("joinchat/"):
            return path
        return "@" + path

    return link


def _migrate_force_join_channels():
    """Migrate existing force_join_channels to valid Telegram identifiers.

    Old data may contain placeholder names like "channel-1", "channel-2"
    instead of actual usernames. This function extracts the real username
    from force_join_links (e.g. https://t.me/FFLIKESPRO2 -> @FFLIKESPRO2)
    and updates force_join_channels accordingly.
    """
    global force_join_channels, force_join_links
    migrated = False
    new_channels = []
    new_links = {}

    for ch in force_join_channels:
        link = force_join_links.get(str(ch), "") or ""
        # Check if the current channel identifier is likely a placeholder
        # (not a valid username, not a numeric chat ID, not starting with @ or +)
        is_placeholder = (
            not ch.startswith("@")
            and not ch.startswith("+")
            and not ch.startswith("-")
            and not ch.lstrip("-").isdigit()
        )

        if is_placeholder and link:
            # Try to extract username from the link
            extracted = _extract_username_from_link(link)
            if extracted and extracted != ch:
                print(f"🔄 Migrating force-join channel '{ch}' -> '{extracted}' from link", flush=True)
                new_channels.append(extracted)
                new_links[extracted] = link
                migrated = True
                continue

        new_channels.append(ch)
        if link:
            new_links[str(ch)] = link

    if migrated:
        force_join_channels = new_channels
        force_join_links = new_links
        save_all()
        print(f"✅ Force-join channel migration complete: {len(force_join_channels)} channel(s)", flush=True)


def _normalize_channel(value: str) -> str:
    """Normalize a channel value: strip whitespace, extract username from links."""
    value = (value or "").strip()
    if not value:
        return value
    # If it's a t.me link, extract the username
    if value.lower().startswith(("http://", "https://", "tg://")):
        extracted = _extract_username_from_link(value)
        return extracted
    # If it's a bare username without @, add it
    if not value.startswith("@") and not value.startswith("+") and not value.startswith("-"):
        return "@" + value
    return value


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
    """Verify the user has joined ALL required force-join channels.

    Never crashes the bot — every Telegram call is wrapped in try/except,
    and 'Message is not modified' is safely ignored.
    """
    query = update.callback_query
    try:
        await query.answer()
    except Exception as e:
        print(f"⚠️ verify_join_callback: query.answer() failed: {e}", flush=True)
        return

    user_id = query.from_user.id

    # Admins always bypass force join
    if is_admin(user_id):
        await safe_edit_message(query, "✅ Admin verified. You can use the bot now.")
        return

    # If force join is disabled or no channels configured, allow access
    if not force_join or not force_join_channels:
        await safe_edit_message(query, "✅ Force join is disabled. You can use the bot now.")
        return

    # Always check real membership in EVERY channel. Never trust the click.
    try:
        missing = await get_missing_channels(context, user_id)
    except Exception as e:
        print(f"❌ verify_join_callback: get_missing_channels failed: {e}", flush=True)
        missing = list(force_join_channels)  # treat all as missing on error

    if not missing:
        # User joined every required channel
        await safe_edit_message(
            query,
            "✅ *Verification Successful!*\n\n"
            "You can now use the bot.\n"
            "Send /start or /like to continue.",
        )
        return

    # Verification failed — show only the channels NOT yet joined
    missing_lines = "\n".join(f"• `{ch}`" for ch in missing)
    await safe_edit_message(
        query,
        "❌ *Verification Failed*\n\n"
        "You still need to join:\n"
        f"{missing_lines}\n\n"
        "Join every channel above, then tap ✅ I'VE JOINED - VERIFY again.",
        reply_markup=build_force_join_keyboard(),
    )


async def forcejoin_noop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer("No invite link is configured for this channel. Ask an admin.", show_alert=True)
    except Exception as e:
        print(f"⚠️ forcejoin_noop_callback: {e}", flush=True)


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


# ============ AUTO LIKE COMMANDS ============

async def autolike_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Create a new auto like task: /autolike REGION UID TOTAL_LIKES"""
    if not context.args or len(context.args) < 3:
        await reply(
            update,
            "❌ Usage: `/autolike REGION UID TOTAL_LIKES`\n"
            "Example: `/autolike BD 5933029554 30000`"
        )
        return

    user_id = update.effective_user.id
    user = update.effective_user
    region = context.args[0].upper()
    uid_str = context.args[1]
    total_str = context.args[2]

    # Validate UID
    if not uid_str.isdigit():
        await reply(update, "❌ UID must contain only numbers!")
        return

    uid = int(uid_str)

    # Validate total likes
    try:
        total_likes = int(total_str)
    except ValueError:
        await reply(update, "❌ Invalid token count!")
        return

    if total_likes < 1:
        await reply(update, "❌ Total likes must be at least 1!")
        return

    if total_likes > MAX_AUTO_LIKE_TARGET:
        await reply(update, f"❌ Maximum target is {MAX_AUTO_LIKE_TARGET:,} likes!")
        return

    # Check for duplicate active task
    existing = get_auto_like_task_by_uid(user_id, uid)
    if existing and existing.get('status') in ('scheduled', 'running'):
        await reply(
            update,
            f"❌ You already have an active auto like task for UID `{uid}`!\n"
            f"Use `/stopautolike {uid}` to stop it first."
        )
        return

    # Calculate next execution time (next 4:01 AM BD time)
    now_bd = datetime.now(BD_TZ)
    next_exec = now_bd.replace(hour=4, minute=1, second=0, microsecond=0)
    if next_exec <= now_bd:
        next_exec = next_exec + __import__('datetime').timedelta(days=1)

    task_id = upsert_auto_like_task(
        owner_id=user_id,
        owner_username=user.username or "",
        owner_name=user.first_name or "",
        uid=uid,
        region=region,
        target_likes=total_likes,
        status='scheduled',
        next_execution=next_exec.isoformat()
    )

    await reply(
        update,
        "🔥 *AUTO LIKE ACTIVATED*\n\n"
        f"👤 UID: `{uid}`\n"
        f"🌍 Region: {region}\n"
        f"🎯 Target: {total_likes:,} Likes\n"
        f"⏰ Schedule: Daily 04:01 AM\n"
        f"🇧🇩 Timezone: Bangladesh\n"
        f"🟢 Status: SCHEDULED"
    )

    # Announce to channel/group
    task = get_auto_like_task(task_id)
    if task:
        await announce_auto_like_created(task, context.bot)


async def myautolike_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show my auto like tasks: /myautolike"""
    user_id = update.effective_user.id
    tasks = get_auto_like_tasks(owner_id=user_id)

    if not tasks:
        await reply(
            update,
            "📭 No auto like tasks found.\n"
            "Create one with `/autolike REGION UID TOTAL_LIKES`"
        )
        return

    msg = "📋 *MY AUTO LIKE TASKS*\n\n"
    for t in tasks:
        uid = t['uid']
        region = t['region']
        target = t['target_likes']
        delivered = t.get('delivered_likes', 0) or 0
        remaining = target - delivered
        status = t.get('status', 'unknown')
        status_emoji = {
            'scheduled': '🟢', 'running': '🔄', 'completed': '✅',
            'paused': '⏸️', 'failed': '❌', 'stopped': '🛑'
        }.get(status, '⚪')

        msg += (
            f"{status_emoji} UID `{uid}` ({region})\n"
            f"   {delivered:,}/{target:,} ({remaining:,} remaining)\n"
            f"   Status: {status}\n\n"
        )

    await reply(update, msg)


async def autolikeinfo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show task details: /autolikeinfo UID"""
    if not context.args or not context.args[0].isdigit():
        await reply(update, "❌ Usage: `/autolikeinfo UID`")
        return

    user_id = update.effective_user.id
    uid = int(context.args[0])

    task = get_auto_like_task_by_uid(user_id, uid)
    if not task:
        await reply(update, f"❌ No auto like task found for UID `{uid}`")
        return

    target = task['target_likes']
    delivered = task.get('delivered_likes', 0) or 0
    remaining = target - delivered
    pct = (delivered / target * 100) if target > 0 else 0
    status = task.get('status', 'unknown')
    next_exec = task.get('next_execution', 'Not set')
    last_exec = task.get('last_executed', 'Never')

    status_emoji = {
        'scheduled': '🟢', 'running': '🔄', 'completed': '✅',
        'paused': '⏸️', 'failed': '❌', 'stopped': '🛑'
    }.get(status, '⚪')

    await reply(
        update,
        "ℹ️ *AUTO LIKE INFO*\n\n"
        f"👤 UID: `{uid}`\n"
        f"🌍 Region: {task['region']}\n"
        f"🎯 Target: {target:,} Likes\n"
        f"❤️ Delivered: {delivered:,}\n"
        f"📉 Remaining: {remaining:,}\n"
        f"📊 Progress: {pct:.1f}%\n"
        f"{status_emoji} Status: {status}\n"
        f"⏰ Next: {next_exec}\n"
        f"🕐 Last: {last_exec}"
    )


async def stopautolike_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stop an auto like task: /stopautolike UID"""
    if not context.args or not context.args[0].isdigit():
        await reply(update, "❌ Usage: `/stopautolike UID`")
        return

    user_id = update.effective_user.id
    uid = int(context.args[0])

    task = get_auto_like_task_by_uid(user_id, uid)
    if not task:
        await reply(update, f"❌ No auto like task found for UID `{uid}`")
        return

    if task.get('status') in ('completed', 'stopped'):
        await reply(
            update,
            f"⚠️ Task for UID `{uid}` is already {task.get('status')}."
        )
        return

    set_auto_like_task_status(task['id'], 'stopped')
    await reply(
        update,
        f"🛑 Auto like task for UID `{uid}` has been stopped."
    )


async def resumeautolike_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Resume a stopped task: /resumeautolike UID"""
    if not context.args or not context.args[0].isdigit():
        await reply(update, "❌ Usage: `/resumeautolike UID`")
        return

    user_id = update.effective_user.id
    uid = int(context.args[0])

    task = get_auto_like_task_by_uid(user_id, uid)
    if not task:
        await reply(update, f"❌ No auto like task found for UID `{uid}`")
        return

    if task.get('status') == 'completed':
        await reply(update, f"⚠️ Task for UID `{uid}` is already completed.")
        return

    # Calculate next execution
    now_bd = datetime.now(BD_TZ)
    next_exec = now_bd.replace(hour=4, minute=1, second=0, microsecond=0)
    if next_exec <= now_bd:
        next_exec = next_exec + __import__('datetime').timedelta(days=1)

    # Actually update directly
    with GROUP_DB_LOCK:
        conn = sqlite3.connect(AUTO_LIKE_DB_FILE)
        cur = conn.cursor()
        cur.execute(
            "UPDATE auto_like_tasks SET status = 'scheduled', next_execution = ? WHERE owner_id = ? AND uid = ?",
            (next_exec.isoformat(), user_id, uid)
        )
        conn.commit()
        conn.close()

    await reply(
        update,
        f"▶️ Auto like task for UID `{uid}` has been resumed."
    )


async def autolikestatus_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show global auto like status dashboard (admin only): /autolikestatus"""
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await reply(update, "❌ Admin only!")
        return

    stats = get_auto_like_stats()
    tasks = get_auto_like_tasks()

    msg = (
        "📊 *AUTO LIKE STATUS DASHBOARD*\n\n"
        f"📦 Total Tasks: {stats['total']}\n"
        f"🟢 Active: {stats['running'] + stats['scheduled']}\n"
        f"✅ Completed: {stats['completed']}\n"
        f"⏸️ Paused: {stats['paused']}\n"
        f"❌ Failed: {stats['failed']}\n"
        f"🛑 Stopped: {stats['stopped']}\n"
        f"────────────────────\n"
        f"🎯 Total Target: {stats['total_target']:,}\n"
        f"❤️ Total Delivered: {stats['total_delivered']:,}\n"
        f"📉 Total Remaining: {stats['total_remaining']:,}\n"
    )

    if tasks:
        msg += "\n📋 *RECENT TASKS:*\n"
        for t in tasks[:10]:
            uid = t['uid']
            target = t['target_likes']
            delivered = t.get('delivered_likes', 0) or 0
            status = t.get('status', 'unknown')
            status_emoji = {
                'scheduled': '🟢', 'running': '🔄', 'completed': '✅',
                'paused': '⏸️', 'failed': '❌', 'stopped': '🛑'
            }.get(status, '⚪')
            msg += f"{status_emoji} UID `{uid}` - {delivered:,}/{target:,} ({status})\n"

    await reply(update, msg)


async def autolikelist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all auto like tasks (admin only): /autolikelist"""
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await reply(update, "❌ Admin only!")
        return

    tasks = get_auto_like_tasks()

    if not tasks:
        await reply(update, "📭 No auto like tasks found.")
        return

    msg = "📋 *ALL AUTO LIKE TASKS*\n\n"
    for i, t in enumerate(tasks[:50], 1):
        uid = t['uid']
        owner = t.get('owner_name', 'Unknown')
        target = t['target_likes']
        delivered = t.get('delivered_likes', 0) or 0
        remaining = target - delivered
        status = t.get('status', 'unknown')
        status_emoji = {
            'scheduled': '🟢', 'running': '🔄', 'completed': '✅',
            'paused': '⏸️', 'failed': '❌', 'stopped': '🛑'
        }.get(status, '⚪')

        msg += (
            f"{i}. {status_emoji} UID `{uid}` - {owner}\n"
            f"   {delivered:,}/{target:,} ({remaining:,} rem.) - {status}\n"
        )

    if len(tasks) > 50:
        msg += f"\n... and {len(tasks) - 50} more tasks."

    await reply(update, msg)


async def auto_like_scheduler_tick(context: ContextTypes.DEFAULT_TYPE):
    """Job queue callback - runs every minute to check for due auto like tasks."""
    global AUTO_LIKE_SCHEDULER_RUNNING
    if not AUTO_LIKE_SCHEDULER_RUNNING:
        return

    try:
        now_bd = datetime.now(BD_TZ)

        # Only run at 4:01 AM Bangladesh time
        if now_bd.hour != 4 or now_bd.minute != 1:
            return

        tasks = get_due_auto_like_tasks()
        if not tasks:
            return

        print(f"⏰ Auto Like scheduler: {len(tasks)} task(s) due", flush=True)

        for task in tasks:
            if not AUTO_LIKE_SCHEDULER_RUNNING:
                break
            await execute_auto_like_task(task, context.bot)

    except Exception as e:
        print(f"⚠️ Auto like scheduler tick error: {e}", flush=True)


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
async def main():
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

    # Initialize Auto Like scheduler database
    try:
        init_auto_like_db()
        print("✅ Auto Like scheduler DB initialized", flush=True)
    except Exception as e:
        print(f"⚠️ Auto Like DB init failed: {e}", flush=True)

    # Build the Telegram application
    try:
        request = HTTPXRequest(connect_timeout=30.0, read_timeout=30.0)
        app = Application.builder().token(BOT_TOKEN).request(request).build()

        job_queue = app.job_queue
        if job_queue:
            job_queue.run_daily(reset_midnight, time=datetime_time(hour=0, minute=0, second=0), days=tuple(range(7)))
            print("⏰ Midnight reset scheduled", flush=True)

            # Schedule auto like check every minute
            job_queue.run_repeating(auto_like_scheduler_tick, interval=60, first=10)
            print("⏰ Auto Like scheduler tick scheduled (every 60s)", flush=True)

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
        app.add_handler(CommandHandler("registergroup", register_group_cmd))
        app.add_handler(CommandHandler("apistatus", apistatus_cmd))
        # Group Management commands
        app.add_handler(CommandHandler("groups", groups_cmd))
        app.add_handler(CommandHandler("allowgroup", allowgroup_cmd))
        app.add_handler(CommandHandler("blockgroup", blockgroup_cmd))
        app.add_handler(CommandHandler("syncgroups", syncgroups_cmd))
        # Database backup commands
        app.add_handler(CommandHandler("backup", backup_cmd))
        app.add_handler(CommandHandler("backupstatus", backupstatus_cmd))
        # Auto Like Scheduler commands
        app.add_handler(CommandHandler("autolike", autolike_cmd))
        app.add_handler(CommandHandler("myautolike", myautolike_cmd))
        app.add_handler(CommandHandler("autolikeinfo", autolikeinfo_cmd))
        app.add_handler(CommandHandler("stopautolike", stopautolike_cmd))
        app.add_handler(CommandHandler("resumeautolike", resumeautolike_cmd))
        app.add_handler(CommandHandler("autolikestatus", autolikestatus_cmd))
        app.add_handler(CommandHandler("autolikelist", autolikelist_cmd))
        app.add_handler(CallbackQueryHandler(group_callback_handler, pattern="^(group_allow|group_block|group_details|groups_page)_"))
        app.add_handler(CallbackQueryHandler(verify_join_callback, pattern="^verify_join$"))
        app.add_handler(CallbackQueryHandler(forcejoin_noop_callback, pattern="^forcejoin_noop$"))
        # Stick silences: stickers produce NO reply — not even "Admin only" / "No document".
        # filters.Document.ALL matches only documents (never stickers).
        app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
        # Auto-register groups when bot is added; unregister when bot is removed/kicked.
        app.add_handler(ChatMemberHandler(my_chat_member_handler, chat_member_types=ChatMemberHandler.MY_CHAT_MEMBER))
        app.add_error_handler(error_handler)

        # Set bot reference for announcements
        set_app_bot(app)

        print("🤖 Bot is running...", flush=True)
        print("   → Normal users: only in groups, daily limit applies", flush=True)
        print("   → Admin users: anywhere (DM + groups), unlimited likes", flush=True)

        # Start polling — run_polling() manages its own event loop internally,
        # including bot identity check (get_me) and webhook deletion.
        print("🚀 Starting polling...", flush=True)
        await app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
    except Exception as e:
        print(f"❌ Bot setup failed: {e}", flush=True)
        import traceback
        traceback.print_exc()
        # Keep process alive so health server can still respond
        while True:
            time.sleep(60)


if __name__ == "__main__":
    asyncio.run(main())

if __name__ == "__main__":
    main()