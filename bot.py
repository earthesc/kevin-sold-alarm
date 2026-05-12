"""
bot.py — Cloud Telegram alarm for "sold" tweets from a target X user.
Uses headless Chrome (Playwright) so X sees a real browser session.

Env vars:
  TELEGRAM_BOT_TOKEN  (required)  - your bot token from BotFather
  TELEGRAM_CHAT_ID    (required)  - your Telegram user id
  X_AUTH_TOKEN        (required)  - value of x.com 'auth_token' cookie
  X_CT0               (required)  - value of x.com 'ct0' cookie
  TARGET_USERNAME     (optional)  - default 'kevinxu'
  KEYWORD             (optional)  - default 'sold' (whole word, case-insensitive)
  POLL_INTERVAL_SEC   (optional)  - default '10'
"""

import asyncio
import os
import re
import sys
import time
import urllib.parse
import urllib.request

from playwright.async_api import async_playwright, TimeoutError as PWTimeout


def env(name: str, default: str | None = None, required: bool = False) -> str:
    v = os.environ.get(name, default)
    if required and not v:
        print(f"FATAL: missing env var {name}", flush=True)
        sys.exit(1)
    return v  # type: ignore[return-value]


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
    """POST to Telegram sendMessage; logs but never raises."""
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


JS_EXTRACT_TWEETS = """
() => {
  const articles = document.querySelectorAll('article[data-testid="tweet"]');
  const results = [];
  articles.forEach(a => {
    const links = a.querySelectorAll('a[href*="/status/"]');
    let id = null;
    for (const l of links) {
      const m = l.getAttribute('href').match(/status\\/(\\d+)/);
      if (m) { id = m[1]; break; }
    }
    if (!id) return;
    const textEl = a.querySelector('[data-testid="tweetText"]');
    const text = textEl ? textEl.innerText : "";
    results.push({id, text});
  });
  return results;
}
"""


async def fetch_tweets(page, username: str):
    """Reload the profile page and extract visible tweets as [{id, text}, ...]."""
    await page.goto(
        f"https://x.com/{username}",
        wait_until="domcontentloaded",
        timeout=30000,
    )
    # Wait until at least one tweet article is in the DOM (or timeout)
    try:
        await page.wait_for_selector('article[data-testid="tweet"]', timeout=20000)
    except PWTimeout:
        log("warning: no tweet articles appeared within 20s")
    # Small extra wait for additional tweets to render
    await asyncio.sleep(1.5)
    return await page.evaluate(JS_EXTRACT_TWEETS)


async def main() -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
        )
        await context.add_cookies([
            {
                "name": "auth_token", "value": X_AUTH_TOKEN,
                "domain": ".x.com", "path": "/",
                "httpOnly": True, "secure": True, "sameSite": "None",
            },
            {
                "name": "ct0", "value": X_CT0,
                "domain": ".x.com", "path": "/",
                "httpOnly": False, "secure": True, "sameSite": "Lax",
            },
        ])
        page = await context.new_page()

        log(f"loading @{TARGET_USERNAME} for seed...")
        tweets = await fetch_tweets(page, TARGET_USERNAME)
        log(f"found {len(tweets)} tweets on initial load")
        if not tweets:
            telegram_send(
                "Bot started but couldn't read any tweets — cookies may be invalid "
                "or X rendered a login page. Check Railway logs."
            )
        last_seen_id = max((int(t["id"]) for t in tweets), default=0)
        log(f"seeded last_seen_id={last_seen_id}, watching every {POLL_INTERVAL_SEC}s")

        telegram_send(
            f"Bot online. Watching @{TARGET_USERNAME} every {POLL_INTERVAL_SEC}s "
            f"for '{KEYWORD}' (whole word, case-insensitive)."
        )

        consecutive_errors = 0

        while True:
            try:
                tweets = await fetch_tweets(page, TARGET_USERNAME)
                consecutive_errors = 0

                new_tweets = [t for t in tweets if int(t["id"]) > last_seen_id]
                new_tweets.sort(key=lambda t: int(t["id"]))

                for t in new_tweets:
                    last_seen_id = max(last_seen_id, int(t["id"]))
                    preview = (t["text"] or "").replace("\n", " ")[:120]
                    log(f"new tweet {t['id']}: {preview}")

                    if KEYWORD_RE.search(t["text"] or ""):
                        tweet_url = f"https://x.com/{TARGET_USERNAME}/status/{t['id']}"
                        msg = (
                            f"🚨 '{KEYWORD}' detected from @{TARGET_USERNAME}\n\n"
                            f"{t['text']}\n\n{tweet_url}"
                        )
                        log(f"MATCH → telegram: {tweet_url}")
                        telegram_send(msg)
            except Exception as e:
                consecutive_errors += 1
                log(f"poll error #{consecutive_errors}: {e!r}")
                if consecutive_errors == 5:
                    telegram_send(
                        f"⚠️ Bot is hitting errors talking to X: {e!r}. Will keep retrying."
                    )
                if consecutive_errors >= 60:
                    telegram_send("❌ Bot giving up after 60 consecutive errors.")
                    sys.exit(1)

            await asyncio.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("stopped by SIGINT")
