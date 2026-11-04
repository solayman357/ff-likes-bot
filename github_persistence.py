"""
GitHub Persistence Module for FF Likes Bot
Provides automatic synchronization of all bot data with GitHub repository.
"""
import os
import json
import subprocess
import threading
import time
import logging
from pathlib import Path
from typing import Dict, Any, Optional, List
from datetime import datetime
from threading import Lock
from contextlib import contextmanager
from typing import Optional, List

logger = logging.getLogger(__name__)


class GitHubPersistence:
    """Manages GitHub-based persistence for all bot data."""
    
    # Files that should be persisted to GitHub
    PERSISTED_FILES = {
        'allowed_groups.json': 'allowed_groups.json',
        'daily_stats.json': 'daily_stats.json',
        'user_limits.json': 'user_limits.json',
        'bot_config.json': 'bot_config.json',
        'daily_stats.json': 'daily_stats.json',
        'broadcast_list.json': 'broadcast_list.json',
        'successful_likes.json': 'successful_likes.json',
        'like_log.json': 'like_log.json',
    }
    
    # Files that should NOT be committed (secrets, tokens, etc.)
    EXCLUDED_FILES = {
        'bot_config.json',  # Contains force_join settings but NOT tokens
        # Note: bot_config.json DOES NOT contain tokens - only settings
    }
    
    # Never commit these files
    NEVER_COMMIT = {
        'bot_config.json',  # Will be handled specially - only settings, no tokens
        'tg_bot_config.json',
        '*.pkl',
        '*.log',
        '.env*',
        '*.key',
        '*.pem',
        '*.pem',
        '*.key',
        '.env*',
        '*.token',
        '*token*',
        '*secret*',
        '*password*',
        '*.pem',
        '*.key',
        '*.crt',
        '*.p12',
        '*.pfx',
        '*.jks',
        '*.keystore',
    }
    
    def __init__(self, repo_path: str = "/root/OWN-LIKE-BOT", bot_dir: str = "/root/OWN-LIKE-BOT/bot"):
        self.repo_path = Path(repo_path)
        self.bot_dir = Path(bot_dir)
        self._lock = Lock()
        self._push_queue: List[str] = []
        self._push_lock = Lock()
        self._push_thread: Optional[threading.Thread] = None
        self._running = False
        self._last_push = 0
        self._push_interval = 30  # seconds
        self._git_configured = False
        self._init_git()
        
    def _init_git(self):
        """Initialize git configuration for automated commits."""
        try:
            # Check if git is available
            subprocess.run(['git', '--version'], check=True, capture_output=True)
            
            # Configure git user if not set
            subprocess.run(['git', 'config', 'user.email', 'bot@ff-likes-bot.local'], 
                         cwd=self.repo_path, check=False)
            subprocess.run(['git', 'config', 'user.name', 'FF Likes Bot'], 
                         cwd=self.repo_path, check=False)
            
            # Verify we can push
            result = subprocess.run(
                ['git', 'remote', 'get-url', 'origin'],
                cwd=self.repo_path, capture_output=True, text=True
            )
            if result.returncode == 0 and 'github.com' in result.stdout:
                self._git_configured = True
                logger.info("GitHub persistence initialized successfully")
            else:
                logger.warning("GitHub remote not configured, persistence disabled")
        except Exception as e:
            logger.warning(f"Git initialization failed: {e}")
    
    @contextmanager
    def _git_lock(self):
        """Context manager for git operations to prevent conflicts."""
        self._lock.acquire()
        try:
            yield
        finally:
            self._lock.release()
    
    def _run_git(self, args: List[str], check: bool = True) -> subprocess.CompletedProcess:
        """Run a git command in the repo directory."""
        return subprocess.run(
            ['git'] + args,
            cwd=self.repo_path,
            capture_output=True,
            text=True,
            check=False
        )
    
    def pull_latest(self) -> bool:
        """Pull latest changes from GitHub."""
        with self._git_lock():
            try:
                # Stash any local changes
                self._run_git(['stash', '--include-untracked'], check=False)
                
                # Pull latest
                result = self._run_git(['pull', '--rebase', 'origin', 'main'], check=False)
                
                # Restore stashed changes
                self._run_git(['stash', 'pop'], check=False)
                
                if result.returncode == 0:
                    logger.info("Successfully pulled latest changes from GitHub")
                    return True
                else:
                    logger.warning(f"Git pull failed: {result.stderr}")
                    return False
            except Exception as e:
                logger.error(f"Git pull failed: {e}")
                return False
    
    def push_changes(self, message: str = "Auto-sync: Update bot data", files: Optional[List[str]] = None) -> bool:
        """Push changes to GitHub."""
        with self._git_lock():
            try:
                # Determine files to add
                if files is None:
                    # Add all tracked data files
                    files_to_add = [str(self.bot_dir / f) for f in self.PERSISTED_FILES.keys() 
                                   if (self.bot_dir / f).exists()]
                else:
                    files_to_add = [str(self.bot_dir / f) for f in files 
                                   if (self.bot_dir / f).exists()]
                
                if not files_to_add:
                    return True
                
                # Add files
                for f in files_to_add:
                    self._run_git(['add', f], check=False)
                
                # Check if there are changes
                status = self._run_git(['status', '--porcelain'], check=False)
                if not status.stdout.strip():
                    return True  # Nothing to commit
                
                # Commit
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                commit_msg = f"Auto-sync: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - Data update"
                
                result = self._run_git(['commit', '-m', commit_msg], check=False)
                if result.returncode != 0:
                    logger.warning(f"Git commit failed: {result.stderr}")
                    return False
                
                # Push
                result = self._run_git(['push', 'origin', 'main'], check=False)
                if result.returncode == 0:
                    logger.info(f"Successfully pushed changes to GitHub: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                    return True
                else:
                    logger.error(f"Git push failed: {result.stderr}")
                    return False
            except Exception as e:
                logger.error(f"Git push failed: {e}")
                return False
    
    def sync_from_github(self) -> bool:
        """Pull latest data from GitHub and update local files."""
        try:
            logger.info("Syncing data from GitHub...")
            
            # Pull latest
            if not self.pull_latest():
                logger.warning("Failed to pull from GitHub, using local data")
                return False
            
            logger.info("Successfully synced from GitHub")
            return True
        except Exception as e:
            logger.error(f"Sync from GitHub failed: {e}")
            return False
    
    def save_and_sync(self, file_key: str, data: Any, message: Optional[str] = None) -> bool:
        """Save data to file and push to GitHub."""
        if file_key not in self.PERSISTED_FILES:
            logger.warning(f"File {file_key} not in persisted files list")
            return False
        
        file_path = self.bot_dir / self.PERSISTED_FILES[file_key]
        
        try:
            # Save to file
            with open(self.bot_dir / self.PERSISTED_FILES[file_key], 'w') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            
            # Push to GitHub
            msg = f"Update {file_key}: {data}" if message is None else message
            return self.push_changes(files=[self.PERSISTED_FILES[file_key]], message=f"Update {file_key}")
        except Exception as e:
            logger.error(f"Failed to save and sync {file_key}: {e}")
            return False
    
    def load_from_file(self, file_key: str) -> Any:
        """Load data from a persisted file."""
        if file_key not in self.PERSISTED_FILES:
            return None
        
        file_path = self.bot_dir / self.PERSISTED_FILES[file_key]
        if not file_path.exists():
            return None
        
        try:
            with open(file_path, 'r') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to load {file_key}: {e}")
            return None
    
    def start_auto_sync(self, interval: int = 30):
        """Start background auto-sync thread."""
        if self._running:
            return
        
        self._running = True
        self._push_interval = interval
        self._push_thread = threading.Thread(target=self._auto_sync_loop, daemon=True)
        self._push_thread.start()
        logger.info(f"Auto-sync started with {interval}s interval")
    
    def stop_auto_sync(self):
        """Stop the auto-sync thread."""
        self._running = False
        if self._push_thread:
            self._push_thread.join(timeout=5)
    
    def _auto_sync_loop(self):
        """Background thread for auto-sync."""
        while self._running:
            time.sleep(self._push_interval)
            if not self._running:
                break
            
            # Check if there are pending changes
            with self._push_lock:
                if self._push_queue:
                    self.push_changes(message="Auto-sync: Periodic sync")
    
    def queue_push(self, file_key: str):
        """Queue a file for async push."""
        with self._push_lock:
            if file_key in self.PERSISTED_FILES:
                self._push_queue.append(file_key)
    
    def get_status(self) -> Dict[str, Any]:
        """Get persistence status."""
        return {
            'git_configured': self._git_configured,
            'auto_sync_running': self._running,
            'push_interval': self._push_interval,
            'pending_files': len(self._push_queue),
            'last_push': self._last_push,
            'persisted_files': list(self.PERSISTED_FILES.keys()),
            'repo_path': str(self.repo_path),
            'bot_dir': str(self.bot_dir),
        }
    
    def force_sync(self) -> bool:
        """Force a full sync - pull and push."""
        logger.info("Forcing full sync...")
        self.pull_latest()
        # Push all current data
        return self.push_changes(message="Manual sync: Full data sync")


# Global instance
_github_persistence: Optional[GitHubPersistence] = None


def get_github_persistence() -> GitHubPersistence:
    """Get the global GitHub persistence instance."""
    global _github_persistence
    if _github_persistence is None:
        _github_persistence = GitHubPersistence()
    return _github_persistence


def init_github_persistence(repo_path: str = "/root/OWN-LIKE-BOT", bot_dir: str = "/root/OWN-LIKE-BOT/bot") -> GitHubPersistence:
    """Initialize the GitHub persistence system."""
    global _github_persistence
    _github_persistence = GitHubPersistence(repo_path=repo_path, bot_dir=bot_dir)
    return _github_persistence