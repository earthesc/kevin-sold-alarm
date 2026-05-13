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
  ALARM_BLAST_COUNT   (optional)  - default '30'
  ALARM_BLAST_DELAY   (optional)  - default '0.3'

Telegram commands (send to your bot chat):
  /test           — fire a simulated blast using the most recent matching tweet
  /lastsold       — show the single most recent matching tweet (no blast)
  /lastsold N     — show the last N matching tweets (max 20, no blast)
  /help           — show this list
"""

import asyncio
import json
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
ALARM_BLAST_COUNT  = int(env("ALARM_BLAST_COUNT", "30"))
ALARM_BLAST_DELAY  = float(env("ALARM_BLAST_DELAY", "0.3"))

KEYWORD_RE = re.compile(rf"\b{re.escape(KEYWORD)}\b", re.IGNORECASE)

# Shared lock around the single Playwright page so polling + commands don't trample.
page_lock = asyncio.Lock()

# Cache of the most recent polling fetch, so /test and small /lastsold can piggyback
# on the polling loop instead of triggering extra fetches (which is what trips X's throttle).
last_poll_tweets: list = []
last_poll_ts: float = 0.0
CACHE_MAX_AGE_SEC = 90  # commands will reuse cache if it's fresher than this

# Exponential backoff on throttle. When X serves "Something went wrong" we'll
# pause this many seconds before the next request, doubling each consecutive hit.
throttle_backoff_sec: float = 0.0


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


def telegram_get_updates_sync(offset: int, timeout: int = 25):
    """Blocking long-poll. Call via run_in_executor from async code."""
    params = urllib.parse.urlencode({"offset": offset, "timeout": timeout})
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates?{params}"
    with urllib.request.urlopen(url, timeout=timeout + 10) as r:
        return json.loads(r.read())


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


async def fetch_tweets(page, username: str, target_count: int = 0):
    """
    Reload the profile page and extract visible tweets as [{id, text}, ...].
    If target_count > 0, scroll and ACCUMULATE tweets across scrolls (X virtualises
    the DOM so old tweets disappear as you scroll). Dedupes by tweet id.
    On empty result, logs a diagnostic dump (URL, title, body snippet) so we can
    see what X actually served instead of a timeline.
    """
    global throttle_backoff_sec

    seen: dict[str, dict] = {}

    def merge(batch):
        for t in batch:
            if t.get("id"):
                seen[t["id"]] = t

    # If a previous fetch saw a throttle, honour an exponential cooldown before
    # touching x.com again. This prevents the bot from compounding the throttle.
    if throttle_backoff_sec > 0:
        log(f"throttle cooldown — sleeping {throttle_backoff_sec:.0f}s before fetch")
        await asyncio.sleep(throttle_backoff_sec)

    await page.goto(
        f"https://x.com/{username}",
        wait_until="domcontentloaded",
        timeout=30000,
    )
    try:
        await page.wait_for_selector('article[data-testid="tweet"]', timeout=20000)
    except PWTimeout:
        log("selector 'article[data-testid=tweet]' did not appear within 20s")
    await asyncio.sleep(1.5)
    merge(await page.evaluate(JS_EXTRACT_TWEETS))

    # X soft-throttle: page loads with profile header but timeline shows
    # "Something went wrong." Bump the cooldown so the NEXT fetch waits, but
    # don't try to recover this fetch — clicking Retry just re-hits the throttled
    # endpoint and makes things worse.
    if not seen:
        try:
            body_text = await page.evaluate(
                "() => (document.body && document.body.innerText || '')"
            )
            if "Something went wrong" in body_text:
                # Exponential backoff: 30s → 60s → 120s → 300s (capped)
                throttle_backoff_sec = min(
                    300.0,
                    max(30.0, throttle_backoff_sec * 2) if throttle_backoff_sec else 30.0,
                )
                log(
                    f"X throttle detected; backing off {throttle_backoff_sec:.0f}s "
                    f"before next fetch"
                )
        except Exception as e:
            log(f"throttle detection failed: {e!r}")
    else:
        # Successful fetch — reset the cooldown.
        if throttle_backoff_sec > 0:
            log(f"throttle cleared; resetting backoff")
        throttle_backoff_sec = 0.0

    if not seen:
        # Diagnostic dump — find out what X actually served when we got 0 tweets.
        try:
            current_url = page.url
            title = await page.title()
            body_snip = await page.evaluate(
                "() => (document.body && document.body.innerText || '').slice(0, 500)"
            )
            log(
                f"EMPTY FETCH DIAGNOSTIC | "
                f"requested=https://x.com/{username} | "
                f"actual_url={current_url} | "
                f"title={title!r} | "
                f"body[:500]={body_snip!r}"
            )
        except Exception as e:
            log(f"diagnostic dump failed: {e!r}")
    if target_count <= len(seen):
        return list(seen.values())

    last_count = len(seen)
    stable = 0
    for _ in range(40):  # up to 40 scrolls
        await page.evaluate("window.scrollBy(0, 2500)")
        await asyncio.sleep(1.0)
        merge(await page.evaluate(JS_EXTRACT_TWEETS))
        if len(seen) >= target_count:
            break
        if len(seen) == last_count:
            stable += 1
            if stable >= 3:
                break
        else:
            stable = 0
            last_count = len(seen)
    return list(seen.values())


async def fetch_tweets_safe(page, username: str, target_count: int = 0):
    async with page_lock:
        return await fetch_tweets(page, username, target_count)


def find_matches(tweets):
    """Filter tweets by KEYWORD_RE, return list sorted newest-first by id."""
    matches = [t for t in tweets if KEYWORD_RE.search(t.get("text") or "")]
    matches.sort(key=lambda t: int(t["id"]), reverse=True)
    return matches


def blast_for_tweet(t: dict, label_prefix: str = "") -> None:
    """Send the configured blast pattern for a single matched tweet."""
    tweet_url = f"https://x.com/{TARGET_USERNAME}/status/{t['id']}"
    full_msg = (
        f"{label_prefix}🚨🚨🚨 '{KEYWORD}' from @{TARGET_USERNAME} 🚨🚨🚨\n\n"
        f"{t.get('text','')}\n\n{tweet_url}"
    )
    short_msg = f"🚨🚨 {KEYWORD.upper()} — {tweet_url}"
    telegram_send(full_msg)
    for i in range(ALARM_BLAST_COUNT - 1):
        telegram_send(f"{short_msg}  [{i+2}/{ALARM_BLAST_COUNT}]")
        time.sleep(ALARM_BLAST_DELAY)


# ---------------------- command handlers ----------------------

async def cmd_test(page) -> None:
    # Prefer the polling-loop cache, BUT only if it has enough tweets to be
    # meaningful — otherwise fall back to a fresh deep fetch.
    age = time.time() - last_poll_ts
    if last_poll_tweets and age < CACHE_MAX_AGE_SEC and len(last_poll_tweets) >= 15:
        telegram_send(f"🧪 /test — using last poll ({int(age)}s old, {len(last_poll_tweets)} tweets)")
        tweets = last_poll_tweets
    else:
        telegram_send("🧪 /test — fetching fresh (cache too small or stale)...")
        tweets = await fetch_tweets_safe(page, TARGET_USERNAME, target_count=30)
    matches = find_matches(tweets)
    if not matches:
        telegram_send(
            f"🧪 /test — no recent '{KEYWORD}' tweet found in last {len(tweets)} "
            f"tweets from @{TARGET_USERNAME}."
        )
        return
    log(f"/test → blasting tweet {matches[0]['id']}")
    blast_for_tweet(matches[0], label_prefix="🧪 TEST — ")


async def cmd_lastsold(page, n: int) -> None:
    n = max(1, min(n, 100))
    # Try cache first: if it already contains >= n matches, no fresh fetch needed.
    age = time.time() - last_poll_ts
    if last_poll_tweets and age < CACHE_MAX_AGE_SEC:
        cached_matches = find_matches(last_poll_tweets)
        if len(cached_matches) >= n:
            telegram_send(
                f"📋 /lastsold {n} — using last poll ({int(age)}s old). "
                f"Showing {n} most recent '{KEYWORD}' tweet(s):"
            )
            for m in cached_matches[:n]:
                url = f"https://x.com/{TARGET_USERNAME}/status/{m['id']}"
                telegram_send(f"{m.get('text','')}\n\n{url}")
                time.sleep(0.25)
            return
    # Cache insufficient — do a fresh deep scrape.
    telegram_send(f"📋 /lastsold {n} — scraping @{TARGET_USERNAME}'s timeline...")
    tweets = await fetch_tweets_safe(page, TARGET_USERNAME, target_count=max(20, n * 6))
    matches = find_matches(tweets)[:n]
    if not matches:
        telegram_send(
            f"No tweets matching '{KEYWORD}' in the last {len(tweets)} tweets "
            f"from @{TARGET_USERNAME}."
        )
        return
    telegram_send(
        f"📋 Showing {len(matches)} most recent '{KEYWORD}' tweet(s) "
        f"from @{TARGET_USERNAME} (scanned {len(tweets)} tweets):"
    )
    for m in matches:
        url = f"https://x.com/{TARGET_USERNAME}/status/{m['id']}"
        telegram_send(f"{m.get('text','')}\n\n{url}")
        time.sleep(0.25)


async def cmd_help() -> None:
    telegram_send(
        f"Commands:\n"
        f"/test  — fire a simulated alarm using @{TARGET_USERNAME}'s most recent "
        f"'{KEYWORD}' tweet\n"
        f"/lastsold  — show the single most recent '{KEYWORD}' tweet\n"
        f"/lastsold N  — show the last N matching tweets (max 100)\n"
        f"/help  — this list"
    )


# ---------------------- background loops ----------------------

async def polling_loop(page) -> None:
    global last_poll_tweets, last_poll_ts
    tweets = await fetch_tweets_safe(page, TARGET_USERNAME)
    if tweets:
        last_poll_tweets = tweets
        last_poll_ts = time.time()
    last_seen_id = max((int(t["id"]) for t in tweets), default=0)
    log(f"poll seed: last_seen_id={last_seen_id}, scanning every {POLL_INTERVAL_SEC}s")

    consecutive_errors = 0
    consecutive_empty = 0
    alerted_empty = False
    while True:
        try:
            tweets = await fetch_tweets_safe(page, TARGET_USERNAME)
            consecutive_errors = 0
            if tweets:
                last_poll_tweets = tweets
                last_poll_ts = time.time()

            # Silent-failure detection: 0 tweets returned probably means cookies
            # expired / X served a login wall / account locked.
            if not tweets:
                consecutive_empty += 1
                if consecutive_empty == 5 and not alerted_empty:
                    telegram_send(
                        "⚠️ Bot can't read any tweets from X. Most likely your "
                        "X_AUTH_TOKEN or X_CT0 cookies expired or @peptidemaxer "
                        "got a verification challenge. Log into x.com in your "
                        "browser, do any challenges, copy fresh auth_token + ct0 "
                        "cookies into Railway."
                    )
                    alerted_empty = True
            else:
                if alerted_empty:
                    telegram_send("✅ Bot is reading tweets again.")
                consecutive_empty = 0
                alerted_empty = False

            new_tweets = [t for t in tweets if int(t["id"]) > last_seen_id]
            new_tweets.sort(key=lambda t: int(t["id"]))

            for t in new_tweets:
                last_seen_id = max(last_seen_id, int(t["id"]))
                preview = (t["text"] or "").replace("\n", " ")[:120]
                log(f"new tweet {t['id']}: {preview}")
                if KEYWORD_RE.search(t["text"] or ""):
                    log(f"MATCH → blasting {ALARM_BLAST_COUNT} msgs for {t['id']}")
                    blast_for_tweet(t)
        except Exception as e:
            consecutive_errors += 1
            log(f"poll error #{consecutive_errors}: {e!r}")
            if consecutive_errors == 5:
                telegram_send(f"⚠️ Bot is hitting errors talking to X: {e!r}. Will keep retrying.")
            if consecutive_errors >= 60:
                telegram_send("❌ Bot giving up after 60 consecutive errors.")
                sys.exit(1)

        await asyncio.sleep(POLL_INTERVAL_SEC)


async def command_listener(page) -> None:
    offset = 0
    loop = asyncio.get_event_loop()
    log("command listener started (long-polling Telegram)")
    while True:
        try:
            resp = await loop.run_in_executor(None, telegram_get_updates_sync, offset, 25)
            if not resp.get("ok"):
                log(f"getUpdates not ok: {resp}")
                await asyncio.sleep(3)
                continue
            for update in resp.get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message") or update.get("channel_post") or {}
                text = (msg.get("text") or "").strip()
                chat_id = str((msg.get("chat") or {}).get("id", ""))
                if not text:
                    continue
                if chat_id != TELEGRAM_CHAT_ID:
                    log(f"ignoring command from unauthorized chat {chat_id}")
                    continue
                cmd_lower = text.lower()
                log(f"cmd: {text}")
                if cmd_lower.startswith("/test"):
                    asyncio.create_task(cmd_test(page))
                elif cmd_lower.startswith("/lastsold") or cmd_lower.startswith("/recentsold"):
                    parts = text.split()
                    n = 1
                    if len(parts) > 1 and parts[1].isdigit():
                        n = int(parts[1])
                    asyncio.create_task(cmd_lastsold(page, n))
                elif cmd_lower.startswith("/help") or cmd_lower.startswith("/start"):
                    asyncio.create_task(cmd_help())
        except Exception as e:
            log(f"command listener error: {e!r}")
            await asyncio.sleep(5)


# ---------------------- main ----------------------

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
            {"name": "auth_token", "value": X_AUTH_TOKEN,
             "domain": ".x.com", "path": "/",
             "httpOnly": True, "secure": True, "sameSite": "None"},
            {"name": "ct0", "value": X_CT0,
             "domain": ".x.com", "path": "/",
             "httpOnly": False, "secure": True, "sameSite": "Lax"},
        ])
        page = await context.new_page()

        telegram_send(
            f"Bot online. Watching @{TARGET_USERNAME} every {POLL_INTERVAL_SEC}s "
            f"for '{KEYWORD}'. Commands: /test, /lastsold [N], /help"
        )

        # Run both loops concurrently. If either crashes hard, the process exits.
        await asyncio.gather(
            polling_loop(page),
            command_listener(page),
        )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("stopped by SIGINT")
