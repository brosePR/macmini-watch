#!/usr/bin/env python3
"""
Polls Apple Refurb, Amazon, and Best Buy for an M4 Mac mini at $599 or less,
in stock, new (or refurbished, in Apple's case). On a new hit, posts to Slack
via webhook. Dedupes via state.json.
"""

import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

PRICE_CAP = 600
STATE_PATH = Path("state.json")
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "").strip()

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.0 Safari/605.1.15"
)
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
            print(f"[fetch] {url} -> {resp.status} ({len(body)} bytes)", file=sys.stderr)
            return body
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
        print(f"[fetch] {url} -> {e}", file=sys.stderr)
        return ""


def looks_blocked(html: str) -> bool:
    # Only treat short responses or obvious block pages as blocked. Full-size
    # product pages legitimately contain the word "captcha" inside JS bundles.
    if len(html) < 50_000:
        low = html.lower()
        if any(s in low for s in ("captcha", "robot check", "to discuss automated access")):
            return True
    if "/errors/validateCaptcha" in html or "Robot Check</title>" in html:
        return True
    return False


def check_apple_refurb() -> list[dict]:
    url = "https://www.apple.com/shop/refurbished/mac/mac-mini"
    html = fetch(url)
    if not html:
        return []
    # Diagnostics: confirm the page actually contains product data, not just
    # the React shell.
    mac_mini_count = len(re.findall(r"Mac mini", html, re.IGNORECASE))
    m4_count = len(re.findall(r"\bM4\b", html))
    prices = sorted({p for p in re.findall(r"\$\s*([0-9][0-9,]{2,4}\.\d{2})", html)})
    print(
        f"[apple] 'Mac mini' x{mac_mini_count}, 'M4' x{m4_count}, "
        f"distinct prices: {prices[:15]}{'...' if len(prices) > 15 else ''}",
        file=sys.stderr,
    )
    hits = []
    # Apple refurb listings: each product block contains the product title
    # and a price near it. We anchor on "Mac mini" + "M4" and find the
    # nearest price within the same block.
    pattern = re.compile(
        r"(Refurbished[^<]{0,200}Mac mini[^<]{0,200}M4[^<]{0,400}?)"
        r"[\s\S]{0,3000}?\$\s*([0-9][0-9,]{2,4})\.\d{2}",
        re.IGNORECASE,
    )
    seen = set()
    for m in pattern.finditer(html):
        title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m.group(1))).strip()
        price = int(m.group(2).replace(",", ""))
        key = (title, price)
        if key in seen:
            continue
        seen.add(key)
        if price <= PRICE_CAP:
            hits.append(
                {
                    "retailer": "Apple Refurb",
                    "variant": title[:140],
                    "price": price,
                    "url": url,
                }
            )
    return hits


def check_amazon() -> list[dict]:
    # Amazon aggressively blocks GitHub Actions IPs. This is best-effort —
    # if it gets a CAPTCHA page we just bail. Adjust strategy if needed.
    url = "https://www.amazon.com/s?k=mac+mini+m4&i=electronics"
    html = fetch(url)
    if not html or looks_blocked(html):
        print("[amazon] blocked or empty response", file=sys.stderr)
        return []
    hits = []
    # Look for product cards containing "Mac mini" + "M4" and a price
    # under the cap. Filter out third-party sellers by requiring "Apple"
    # in the title (Apple is the brand on legitimate listings).
    card_pattern = re.compile(
        r'data-component-type="s-search-result"[\s\S]{0,8000}?</div>\s*</div>\s*</div>',
        re.IGNORECASE,
    )
    for card in card_pattern.findall(html):
        if "Mac mini" not in card or "M4" not in card:
            continue
        if "Apple" not in card:
            continue
        # Skip refurb / renewed / used
        if re.search(r"(renewed|refurbished|used|open[- ]box)", card, re.I):
            continue
        price_m = re.search(r'<span class="a-offscreen">\$([0-9][0-9,]{2,4})\.\d{2}</span>', card)
        if not price_m:
            continue
        price = int(price_m.group(1).replace(",", ""))
        if price > PRICE_CAP:
            continue
        title_m = re.search(r'<span class="[^"]*a-text-normal[^"]*">([^<]{10,200})</span>', card)
        title = (title_m.group(1) if title_m else "Mac mini M4").strip()
        link_m = re.search(r'href="(/[^"]+/dp/[^"]+)"', card)
        link = "https://www.amazon.com" + link_m.group(1) if link_m else url
        hits.append(
            {
                "retailer": "Amazon",
                "variant": title[:140],
                "price": price,
                "url": link,
            }
        )
    return hits


def check_bestbuy() -> list[dict]:
    url = "https://www.bestbuy.com/site/searchpage.jsp?st=mac+mini+m4"
    html = fetch(url)
    if not html or looks_blocked(html):
        print("[bestbuy] blocked or empty response", file=sys.stderr)
        return []
    mac_mini_count = len(re.findall(r"Mac mini", html, re.IGNORECASE))
    m4_count = len(re.findall(r"\bM4\b", html))
    prices = sorted({p for p in re.findall(r"\$\s*([0-9][0-9,]{2,4}\.\d{2})", html)})
    print(
        f"[bestbuy] 'Mac mini' x{mac_mini_count}, 'M4' x{m4_count}, "
        f"distinct prices: {prices[:15]}{'...' if len(prices) > 15 else ''}",
        file=sys.stderr,
    )
    # Proximity-based: find every "Mac mini" mention with "M4" nearby (within
    # ~600 chars) and the closest price within ~2000 chars. Skip refurb/open-box
    # blocks. Dedupe by (title-ish, price).
    hits = []
    seen = set()
    for m in re.finditer(r"Mac mini", html, re.IGNORECASE):
        start = max(0, m.start() - 300)
        end = min(len(html), m.end() + 600)
        block = html[start:end]
        if not re.search(r"\bM4\b", block):
            continue
        # New only — skip open-box / refurb blocks
        if re.search(r"(open[- ]box|geek squad|refurb)", block, re.I):
            continue
        # Find prices in a wider neighborhood (Best Buy often renders price
        # in a sibling component a bit further away)
        wide = html[max(0, m.start() - 500): min(len(html), m.end() + 2000)]
        price_m = re.search(r"\$\s*([0-9][0-9,]{2,4})\.\d{2}", wide)
        if not price_m:
            continue
        price = int(price_m.group(1).replace(",", ""))
        if price > PRICE_CAP:
            continue
        # Best-effort title: a chunk of nearby text mentioning Mac mini + M4
        title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", block)).strip()
        title = re.search(r"([^.]{0,140}Mac mini[^.]{0,140}M4[^.]{0,80})", title, re.I)
        title = title.group(1).strip() if title else "Mac mini M4"
        key = (title[:80], price)
        if key in seen:
            continue
        seen.add(key)
        hits.append(
            {
                "retailer": "Best Buy",
                "variant": title[:140],
                "price": price,
                "url": url,
            }
        )
    return hits


def signature(hit: dict) -> str:
    return f"{hit['retailer']}|{hit['variant']}|{hit['price']}"


def post_slack(hit: dict) -> None:
    if not SLACK_WEBHOOK_URL:
        print(f"[slack] (dry-run) would post: {hit}", file=sys.stderr)
        return
    text = (
        f":rotating_light: Mac mini ${PRICE_CAP} hit — {hit['retailer']}\n"
        f"{hit['variant']} at ${hit['price']}\n"
        f"{hit['url']}"
    )
    payload = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(
        SLACK_WEBHOOK_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
        print(f"[slack] post failed: {e}", file=sys.stderr)


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text())
    except json.JSONDecodeError:
        return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def main() -> int:
    if os.environ.get("TEST_PING") == "1":
        print("[test] sending Slack test ping")
        post_slack(
            {
                "retailer": "TEST",
                "variant": "macmini-watch end-to-end test",
                "price": 0,
                "url": "https://github.com/brosePR/macmini-watch",
            }
        )
        return 0

    all_hits: list[dict] = []
    for fn in (check_apple_refurb, check_amazon, check_bestbuy):
        try:
            all_hits.extend(fn())
        except Exception as e:
            print(f"[{fn.__name__}] error: {e}", file=sys.stderr)

    print(f"hits this run: {len(all_hits)}")
    for h in all_hits:
        print(f"  - {signature(h)} -> {h['url']}")

    previous = load_state()
    current = {signature(h): h for h in all_hits}
    new_keys = set(current) - set(previous)

    for key in new_keys:
        print(f"[alert] new hit: {key}")
        post_slack(current[key])

    save_state(current)
    return 0


if __name__ == "__main__":
    sys.exit(main())
