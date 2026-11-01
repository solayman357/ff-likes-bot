#!/usr/bin/env python3
"""Check Free Fire account levels using the API's make_request function."""

import json
import base64
import time
import warnings
warnings.filterwarnings('ignore')

import app as api


def claims(token):
    """Decode JWT payload to check expiry."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def load_unexpired_tokens():
    bank = json.load(open('token_bd.json'))
    now = time.time()
    return [e['token'] for e in bank if claims(e.get('token', '')).get('exp', 0) > now]


def check_level(uid, region, token):
    enc = api.enc(uid)
    if enc is None:
        return None
    info = api.make_request(enc, region, token)
    if info is None:
        return None
    return {
        "uid": uid,
        "level": info.AccountInfo.Levels,
        "nickname": info.AccountInfo.PlayerNickname,
        "likes": info.AccountInfo.Likes,
        "region": info.AccountInfo.PlayerRegion,
    }


def main():
    # Edit this list with the UIDs you want to check
    uids = [7970337802, 5933029554, 10337109588]

    # Region: BD / IND / US / BR / SAC / NA
    region = "BD"

    tokens = load_unexpired_tokens()
    if not tokens:
        print("No unexpired tokens available.")
        return

    print(f"Loaded {len(tokens)} unexpired tokens. Checking {len(uids)} UIDs...\n")

    for uid in uids:
        result = check_level(uid, region, tokens[0])
        if result:
            print(f"UID:      {result['uid']}")
            print(f"Level:    {result['level']}")
            print(f"Nickname: {result['nickname']}")
            print(f"Likes:    {result['likes']}")
            print(f"Region:   {result['region']}")
        else:
            print(f"UID {uid}: failed to get info")
        print("-" * 40)


if __name__ == "__main__":
    main()
