#!/usr/bin/env python3
"""Merge fresh (unexpired) JWT exports into the token bank.

Safe by design:
  * only tokens whose `exp` claim is in the future are imported
  * existing entries are never removed
  * a token replaces an older one for the same account_id (dedupe by UID)
  * the bank is backed up before every write and validated afterwards

Run manually:  python3 merge_tokens.py
Scheduled:     pm2 start merge_tokens.py --name ff-token-watch --cron "*/10 * * * *" --no-autorestart
"""

import base64
import glob
import json
import os
import shutil
import sys
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BANK = os.path.join(BASE_DIR, "api", "token_bd.json")

SOURCE_GLOBS = [
    "/sdcard/Download/Telegram/JWT_RESULTS_*.json",
    "/sdcard/Download/Telegram/jwt_token*.json",
    "/sdcard/Download/JWT_RESULTS_*.json",
    "/sdcard/Download/jwt_token*.json",
    "/sdcard/GLORY/**/jwt_token.json",
    "/root/JWT_RESULTS_*.json",
]


def claims(token):
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def read_entries(path):
    try:
        with open(path, "r") as fh:
            data = json.load(fh)
    except Exception as exc:
        print(f"skip {path}: {exc}")
        return []
    entries = []
    for item in data if isinstance(data, list) else []:
        token = item.get("token") if isinstance(item, dict) else item
        if isinstance(token, str) and token.count(".") == 2:
            entries.append({"token": token})
    return entries


def main():
    if not os.path.exists(BANK):
        print(f"bank not found: {BANK}")
        return 1

    with open(BANK, "r") as fh:
        bank = json.load(fh)
    if not isinstance(bank, list):
        print("bank is not a list, refusing to touch it")
        return 1

    now = time.time()
    by_uid = {}
    for entry in bank:
        token = entry.get("token", "") if isinstance(entry, dict) else ""
        uid = claims(token).get("account_id")
        key = str(uid) if uid else token
        by_uid[key] = {"token": token}

    added = 0
    replaced = 0
    seen_files = 0
    for pattern in SOURCE_GLOBS:
        for path in sorted(glob.glob(pattern, recursive=True)):
            seen_files += 1
            for entry in read_entries(path):
                info = claims(entry["token"])
                if int(info.get("exp", 0)) <= now:
                    continue
                uid = info.get("account_id")
                key = str(uid) if uid else entry["token"]
                old = by_uid.get(key)
                if old is None:
                    added += 1
                elif old["token"] != entry["token"]:
                    replaced += 1
                else:
                    continue
                by_uid[key] = entry

    if not added and not replaced:
        print(f"no new unexpired tokens (scanned {seen_files} files, bank {len(bank)} entries)")
        return 0

    stamp = time.strftime("%Y%m%d%H%M%S")
    backup = f"{BANK}.bak.{stamp}"
    shutil.copy2(BANK, backup)

    merged = list(by_uid.values())
    tmp = f"{BANK}.tmp"
    with open(tmp, "w") as fh:
        json.dump(merged, fh)
    with open(tmp, "r") as fh:
        json.load(fh)  # validate before promoting
    os.replace(tmp, BANK)

    print(
        f"merged: +{added} new, {replaced} replaced, bank {len(bank)} -> {len(merged)}; "
        f"backup {os.path.basename(backup)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
