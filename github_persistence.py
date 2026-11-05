"""
GitHub Persistence Module for FF Likes Bot
==========================================

Stores all runtime bot data permanently in the GitHub repository using the
GitHub REST API on a dedicated data branch (default: ``bot-data``).

Why a separate branch?
    The Render service auto-deploys from ``main``. If runtime data were
    committed to ``main``, every /start, /like or setting change would trigger
    a redeploy (and a restart storm). The ``bot-data`` branch keeps runtime
    data completely separate from source-code commits.

Guarantees
    * Never stores tokens, API keys or passwords (only the whitelisted data
      files below are ever uploaded).
    * Pulls the latest data on startup before the bot loads its settings.
    * Pushes after every change, in a background worker, with retries.
    * A failed GitHub call never crashes or blocks the bot.
    * Concurrent writes are serialised with a lock.
"""

import base64
import json
import logging
import os
import subprocess
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Dict, Optional, Set

logger = logging.getLogger(__name__)

# Only these files are ever read from / written to GitHub.
# key -> filename inside the bot directory
# Files that must be treated as binary (no JSON validation)
BINARY_FILES = {"group_management.db"}

# Only these files are ever read from / written to GitHub.
# key -> filename inside the bot directory
PERSISTED_FILES: Dict[str, str] = {
    "allowed": "allowed_groups.json",
    "stats": "daily_stats.json",
    "users": "user_limits.json",
    "config": "bot_config.json",
    "broadcast": "broadcast_list.json",
    "likes": "successful_likes.json",
    "groups": "group_management.db",
}

DEFAULT_REPO = "solayman357/ff-likes-bot"
DEFAULT_BRANCH = "bot-data"
API_ROOT = "https://api.github.com"


class GitHubPersistence:
    """Manages GitHub-based persistence for all bot data."""

    def __init__(self, repo_path: Optional[str] = None, bot_dir: Optional[str] = None):
        self.repo_path = Path(repo_path) if repo_path else Path(__file__).resolve().parent
        self.bot_dir = Path(bot_dir) if bot_dir else (self.repo_path / "bot")

        self._lock = threading.RLock()
        self._pending: Set[str] = set()
        self._retry_count: Dict[str, int] = {}
        self._worker: Optional[threading.Thread] = None
        self._running = False
        self._branch_ready = False
        self._last_push = 0
        self._last_error = ""

        self.token = self._discover_token()
        self.owner, self.repo = self._discover_repo()
        self.branch = os.getenv("GITHUB_DATA_BRANCH", DEFAULT_BRANCH).strip() or DEFAULT_BRANCH
        self.enabled = bool(self.token and self.owner and self.repo)

        if self.enabled:
            logger.info("GitHub persistence enabled for %s/%s (branch %s)",
                        self.owner, self.repo, self.branch)
        else:
            logger.warning("GitHub persistence disabled (no token/repo found)")

    # ------------------------------------------------------------------ setup
    def _discover_token(self) -> str:
        for key in ("GITHUB_TOKEN", "GH_TOKEN", "GITHUB_PAT"):
            value = (os.getenv(key) or "").strip()
            if value:
                return value
        # Fall back to a token embedded in the git remote URL (local dev only).
        try:
            result = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                cwd=str(self.repo_path),
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                parsed = urllib.parse.urlparse(result.stdout.strip())
                if parsed.password:
                    return parsed.password
                if parsed.username and parsed.username.startswith("gh"):
                    return parsed.username
        except Exception:
            pass
        return ""

    def _discover_repo(self):
        env_repo = (os.getenv("GITHUB_REPO") or "").strip()
        if "/" in env_repo:
            owner, _, name = env_repo.partition("/")
            return owner.strip(), name.strip()
        try:
            result = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                cwd=str(self.repo_path),
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                parsed = urllib.parse.urlparse(result.stdout.strip())
                parts = [p for p in parsed.path.split("/") if p]
                if len(parts) >= 2:
                    return parts[0], parts[1].removesuffix(".git")
        except Exception:
            pass
        owner, _, name = DEFAULT_REPO.partition("/")
        return owner, name

    # ------------------------------------------------------------- HTTP helpers
    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "ff-likes-bot",
        }

    def _request(self, method: str, path: str, payload: Optional[dict] = None, timeout: int = 20):
        import requests

        url = f"{API_ROOT}{path}"
        try:
            response = requests.request(
                method, url, headers=self._headers(), json=payload, timeout=timeout
            )
            return response
        except Exception as exc:  # network failure
            self._last_error = f"{type(exc).__name__}: {exc}"
            logger.warning("GitHub request failed (%s %s): %s", method, path, exc)
            return None

    def _ensure_branch(self) -> bool:
        if not self.enabled:
            return False
        with self._lock:
            if self._branch_ready:
                return True
            # Does the data branch already exist?
            response = self._request("GET", f"/repos/{self.owner}/{self.repo}/git/ref/heads/{self.branch}")
            if response is not None and response.status_code == 200:
                self._branch_ready = True
                return True
            # Create it from main.
            response = self._request("GET", f"/repos/{self.owner}/{self.repo}/git/ref/heads/main")
            if response is None or response.status_code != 200:
                self._last_error = "cannot read main branch ref"
                return False
            sha = (response.json() or {}).get("object", {}).get("sha")
            if not sha:
                self._last_error = "main branch ref has no sha"
                return False
            response = self._request(
                "POST",
                f"/repos/{self.owner}/{self.repo}/git/refs",
                {"ref": f"refs/heads/{self.branch}", "sha": sha},
            )
            if response is not None and response.status_code in (201, 422):
                self._branch_ready = True
                return True
            self._last_error = f"branch create failed: {getattr(response, 'status_code', 'no response')}"
            return False

    def _get_remote_file(self, filename: str):
        """Return (decoded_bytes, sha) or (None, None)."""
        if not self._ensure_branch():
            return None, None
        response = self._request(
            "GET",
            f"/repos/{self.owner}/{self.repo}/contents/{urllib.parse.quote(filename)}?ref={self.branch}",
        )
        if response is None or response.status_code == 404:
            return None, None
        if response.status_code != 200:
            self._last_error = f"GET {filename} -> {response.status_code}"
            return None, None
        try:
            body = response.json() or {}
            content = base64.b64decode(body.get("content", ""))
            return content, body.get("sha")
        except Exception as exc:
            self._last_error = f"decode {filename}: {exc}"
            return None, None

    def _put_remote_file(self, filename: str, data: bytes, message: str) -> bool:
        if not self._ensure_branch():
            return False
        for attempt in (1, 2):
            _, sha = self._get_remote_file(filename)
            payload = {
                "message": message,
                "content": base64.b64encode(data).decode("ascii"),
                "branch": self.branch,
            }
            if sha:
                payload["sha"] = sha
            response = self._request(
                "PUT",
                f"/repos/{self.owner}/{self.repo}/contents/{urllib.parse.quote(filename)}",
                payload,
            )
            if response is not None and response.status_code in (200, 201):
                return True
            # 409 = sha conflict (another writer); retry once with fresh sha.
            if response is not None and response.status_code == 409 and attempt == 1:
                time.sleep(1)
                continue
            status = getattr(response, "status_code", "no response")
            self._last_error = f"PUT {filename} -> {status}"
            logger.warning("GitHub push failed for %s (status %s)", filename, status)
            return False
        return False

    # ---------------------------------------------------------------- public API
    def pull_latest(self) -> int:
        """Download persisted files from GitHub into the bot directory.

        Returns the number of files restored. Files that are not present
        remotely are left untouched (never deleted).
        """
        if not self.enabled:
            return 0
        restored = 0
        for filename in PERSISTED_FILES.values():
            data, _ = self._get_remote_file(filename)
            if data is None:
                continue
            # For binary files (SQLite db), skip JSON validation.
            if filename in BINARY_FILES:
                try:
                    target = self.bot_dir / filename
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
                    restored += 1
                except Exception as exc:
                    logger.warning("Could not write binary %s: %s", filename, exc)
                continue
            # Validate JSON files before overwriting a working local file.
            try:
                json.loads(data.decode("utf-8"))
            except Exception as exc:
                logger.warning("Skipping corrupt remote file %s: %s", filename, exc)
                continue
            try:
                target = self.bot_dir / filename
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                restored += 1
            except Exception as exc:
                logger.warning("Could not write %s: %s", filename, exc)
        return restored

    def seed_missing(self) -> int:
        """Upload local files that do not exist on GitHub yet.

        This preserves the settings already configured before persistence was
        introduced, instead of resetting them to defaults.
        """
        if not self.enabled:
            return 0
        uploaded = 0
        for filename in PERSISTED_FILES.values():
            remote, _ = self._get_remote_file(filename)
            if remote is not None:
                continue
            local = self.bot_dir / filename
            if not local.exists():
                continue
            try:
                if self._put_remote_file(filename, local.read_bytes(), f"Seed {filename} from existing bot data"):
                    uploaded += 1
            except Exception as exc:
                logger.warning("Seed failed for %s: %s", filename, exc)
        return uploaded

    def push_changes(self, message: str = "Update bot data", files: Optional[list] = None) -> bool:
        """Push the given files (or all persisted files) immediately."""
        if not self.enabled:
            return False
        names = files or list(PERSISTED_FILES.values())
        ok = True
        for filename in names:
            local = self.bot_dir / filename
            if not local.exists():
                continue
            try:
                data = local.read_bytes()
            except Exception as exc:
                logger.warning("Cannot read %s: %s", filename, exc)
                ok = False
                continue
            if not self._put_remote_file(filename, data, message):
                ok = False
        if ok:
            self._last_push = time.time()
        return ok

    def queue_push(self, file_key: str):
        """Queue a file for background upload (safe to call from async code)."""
        if not self.enabled:
            return
        filename = PERSISTED_FILES.get(file_key)
        if not filename:
            logger.warning("Unknown persistence key: %s", file_key)
            return
        with self._lock:
            self._pending.add(file_key)
        self._ensure_worker()

    def _ensure_worker(self):
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._running = True
            self._worker = threading.Thread(
                target=self._worker_loop, name="github-persistence", daemon=True
            )
            self._worker.start()

    def _worker_loop(self):
        # Small delay so a burst of save_all() calls is batched into one push.
        time.sleep(2)
        while self._running:
            with self._lock:
                keys = list(self._pending)
                self._pending.clear()
            if not keys:
                time.sleep(3)
                continue
            for key in keys:
                filename = PERSISTED_FILES.get(key)
                if not filename:
                    continue
                local = self.bot_dir / filename
                if not local.exists():
                    continue
                try:
                    data = local.read_bytes()
                except Exception as exc:
                    logger.warning("Cannot read %s: %s", filename, exc)
                    self._requeue(key)
                    continue
                if self._put_remote_file(filename, data, f"Update {filename}"):
                    self._retry_count.pop(key, None)
                else:
                    self._requeue(key)
            self._last_push = time.time()

    def _requeue(self, key: str):
        with self._lock:
            attempts = self._retry_count.get(key, 0) + 1
            self._retry_count[key] = attempts
            if attempts <= 12:  # ~1 minute of retries with backoff
                self._pending.add(key)
        # Backoff handled by the loop's sleep below.
        time.sleep(min(2 * attempts, 30))

    def get_status(self) -> dict:
        return {
            "enabled": self.enabled,
            "repo": f"{self.owner}/{self.repo}" if self.enabled else "",
            "branch": self.branch,
            "pending_files": len(self._pending),
            "last_push": self._last_push,
            "last_error": self._last_error,
            "persisted_files": list(PERSISTED_FILES.values()),
            "bot_dir": str(self.bot_dir),
        }


# --------------------------------------------------------------------- globals
_instance: Optional[GitHubPersistence] = None
_instance_lock = threading.Lock()


def get_github_persistence() -> GitHubPersistence:
    global _instance
    with _instance_lock:
        if _instance is None:
            _instance = GitHubPersistence()
        return _instance


def init_github_persistence(repo_path: Optional[str] = None, bot_dir: Optional[str] = None) -> GitHubPersistence:
    global _instance
    with _instance_lock:
        _instance = GitHubPersistence(repo_path=repo_path, bot_dir=bot_dir)
        return _instance
