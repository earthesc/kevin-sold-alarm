"""
bot.py — Cloud-hosted alarm for "sold" tweets from a target X/Twitter user.
Sends a Telegram message the moment a matching tweet appears.

Required env vars:
  TELEGRAM_BOT_TOKEN   — your bot token (from BotFather)
  TELEGRAM_CHAT_ID     — your Telegram user id
  X_AUTH_TOKEN         — value of the 'auth_token' cookie on x.com (while logged in)
  X_CT0                — value of the 'ct0' cookie on x.com (while logged in)

Optional env vars (with defaults):
  TARGET_USERNAME      — default 'kevinxu'
  KEYWORD              — default 'sold' (whole word, case-insensitive)
  POLL_INTERVAL_SEC    — default '10'
"""

import asyncio
import os
import re
import sys
import time
import urllib.parse
import urllib.request

from twikit import Client


def env(name: str, default: str | None = None, required: bool = False) -> str:
    val = os.environ.get(name, default)
    if required and not val:
        print(f"FATAL: missing env var {name}", flush=True)
        sys.exit(1)
    return val  # type: ignore[return-value]


TELEGRAM_BOT_TOKEN = env("TELEGRAM_BOT_TOKEN", required=True)
TELEGRAM_CHAT_ID   = env("TELEGRAM_CHAT_ID",   required=True)
X_AUTH_TOKEN       = env("X_AUTH_TOKEN",       required=True)
X_CT0              = env("X_CT0",              required=True)
TARGET_USERNAME    = env("TARGET_USERNAME", "kevinxu")
KEYWORD            = env("KEYWORD",         "sold")
POLL_INTERVAL_SEC  = int(env("POLL_INTERVAL_SEC", "10"))

KEYWORD_RE = re.compile(rf"\b{re.escape(KEYWORD)}\b", re.IGNORECASE)


def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def telegram_send(text: str) -> None:
    """Send a Telegram message via plain HTTPS. Logs but does not raise on failure."""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "disable_web_page_preview": "false",
        }).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=15) as r:
            r.read()
    except Exception as e:
        log(f"telegram send error: {e!r}")


async def main() -> None:
    client = Client(language="en-US")
    client.set_cookies({"auth_token": X_AUTH_TOKEN, "ct0": X_CT0})

    log(f"resolving @{TARGET_USERNAME}...")
    user = await client.get_user_by_screen_name(TARGET_USERNAME)
    log(f"target user: id={user.id} name='{user.name}'")

    # Seed last_seen_id so we only alert on NEW tweets posted after startup.
    tweets = await client.get_user_tweets(user.id, "Tweets", count=20)
    last_seen_id = max((int(t.id) for t in tweets), default=0)
    log(f"seeded last_seen_id={last_seen_id}, watching every {POLL_INTERVAL_SEC}s")

    telegram_send(
        f"Bot online. Watching @{TARGET_USERNAME} every {POLL_INTERVAL_SEC}s "
        f"for '{KEYWORD}' (whole word, case-insensitive)."
    )

    consecutive_errors = 0

    while True:
        try:
            tweets = await client.get_user_tweets(user.id, "Tweets", count=20)
            consecutive_errors = 0

            new_tweets = [t for t in tweets if int(t.id) > last_seen_id]
            new_tweets.sort(key=lambda t: int(t.id))  # oldest first

            for t in new_tweets:
                last_seen_id = max(last_seen_id, int(t.id))
                preview = (t.text or "").replace("\n", " ")[:120]
                log(f"new tweet {t.id}: {preview}")

                if KEYWORD_RE.search(t.text or ""):
                    tweet_url = f"https://x.com/{TARGET_USERNAME}/status/{t.id}"
                    msg = (
                        f"🚨 '{KEYWORD}' detected from @{TARGET_USERNAME}\n\n"
                        f"{t.text}\n\n{tweet_url}"
                    )
                    log(f"MATCH → notifying telegram: {tweet_url}")
                    telegram_send(msg)
        except Exception as e:
            consecutive_errors += 1
            log(f"poll error #{consecutive_errors}: {e!r}")
            if consecutive_errors == 5:
                telegram_send(
                    f"⚠️ Bot is hitting errors talking to X. Latest: {e!r}. "
                    f"It will keep retrying."
                )
            if consecutive_errors >= 60:
                telegram_send("❌ Bot giving up after 60 consecutive errors. Check Railway logs.")
                sys.exit(1)

        await asyncio.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("stopped by SIGINT")
