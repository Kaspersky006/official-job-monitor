#!/usr/bin/env python3
"""Hourly job-page monitor.

Fetches the career pages listed in companies.yml, keeps links that look like
working-student or internship postings, and sends a Telegram message for every
link it has not seen before.
"""
import argparse
import datetime as dt
import html
import json
import os
import re
import sys
from urllib.parse import urldefrag, urljoin

import requests
import yaml
from bs4 import BeautifulSoup

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(ROOT, "state", "seen.json")
HEALTH_FILE = os.path.join(ROOT, "state", "health.json")
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
MAX_SEEN_PER_COMPANY = 500
MAX_ITEMS_PER_COMPANY_IN_ALERT = 10


# ---------- small helpers ----------

def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, ensure_ascii=False, sort_keys=True)


def compile_rx(patterns):
    return re.compile("|".join(f"(?:{p})" for p in patterns), re.I)


def is_match(text, href, include, exclude):
    """True when a link looks like a working-student or internship posting."""
    if exclude.search(text):
        return False
    return bool(include.search(text) or include.search(href))


def extract_links(markup, base_url):
    """Return [(text, absolute_url)] for every link on the page."""
    soup = BeautifulSoup(markup, "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        url = urldefrag(urljoin(base_url, href))[0]
        text = a.get_text(" ", strip=True)
        links.append((text, url))
    return links


# ---------- fetchers ----------

def fetch_html(url):
    r = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "de,en;q=0.8"}, timeout=25)
    r.raise_for_status()
    return r.text


def fetch_playwright(url, wait_ms=3000):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(user_agent=UA, locale="de-DE")
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            page.wait_for_timeout(wait_ms)
            return page.content()
        finally:
            browser.close()


def fetch_recruitee(base_url):
    r = requests.get(base_url.rstrip("/") + "/api/offers", headers={"User-Agent": UA}, timeout=25)
    r.raise_for_status()
    return [(o.get("title", ""), o.get("careers_url", "")) for o in r.json().get("offers", [])]


def check_company(company, include, exclude):
    """Fetch one source. Returns {"links": int, "matches": [(title, url)], "error": str|None}."""
    kind = company.get("type", "html")
    url = company["url"]
    try:
        if kind == "recruitee":
            links = fetch_recruitee(url)
        elif kind == "playwright":
            links = extract_links(fetch_playwright(url, company.get("wait_ms", 3000)), url)
        else:
            links = extract_links(fetch_html(url), url)
    except Exception as exc:  # network errors, blocks, timeouts
        return {"links": 0, "matches": [], "error": f"{type(exc).__name__}: {str(exc)[:160]}"}

    matches, seen_urls = [], set()
    for text, link in links:
        if link in seen_urls or not link:
            continue
        if is_match(text, link, include, exclude):
            seen_urls.add(link)
            matches.append((text or link.rsplit("/", 1)[-1], link))
    return {"links": len(links), "matches": matches, "error": None}


# ---------- notifications ----------

def telegram_send(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("Telegram secrets missing; message not sent:\n" + text)
        return False
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
        timeout=25,
    )
    if not r.ok:
        print(f"Telegram error {r.status_code}: {r.text[:200]}")
    return r.ok


def format_alert(new_by_company):
    blocks = []
    for name, items in new_by_company.items():
        lines = [f"🆕 <b>{html.escape(name)}</b>"]
        for title, url in items[:MAX_ITEMS_PER_COMPANY_IN_ALERT]:
            lines.append(f"• {html.escape(title[:120])}\n{html.escape(url)}")
        extra = len(items) - MAX_ITEMS_PER_COMPANY_IN_ALERT
        if extra > 0:
            lines.append(f"…and {extra} more")
        blocks.append("\n".join(lines))
    return blocks


def send_blocks(blocks, limit=3800):
    chunk = ""
    for b in blocks:
        if chunk and len(chunk) + len(b) + 2 > limit:
            telegram_send(chunk)
            chunk = ""
        chunk += ("\n\n" if chunk else "") + b
    if chunk:
        telegram_send(chunk)


def format_health(health):
    ok = [n for n, h in health.items() if h["status"] == "ok"]
    bad = [(n, h) for n, h in health.items() if h["status"] != "ok"]
    lines = [f"📊 Monitor health: {len(ok)} of {len(health)} sources OK"]
    for n, h in bad:
        reason = h.get("error") or f"only {h['links']} links found (page may be JavaScript-only or blocked)"
        lines.append(f"⚠️ {html.escape(n)}: {html.escape(reason)}")
    return "\n".join(lines)


# ---------- main ----------

def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print results, send nothing, save no state")
    ap.add_argument("--notify-existing", action="store_true", help="alert for jobs already on the page too")
    ap.add_argument("--only", help="check only companies whose name contains this text")
    args = ap.parse_args(argv)

    with open(os.path.join(ROOT, "companies.yml"), encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    include, exclude = compile_rx(cfg["include"]), compile_rx(cfg["exclude"])
    min_links = cfg.get("min_links", 5)

    seen = load_json(STATE_FILE, {})
    health, new_by_company, baseline = {}, {}, []
    now = dt.datetime.now(dt.timezone.utc)

    for c in cfg["companies"]:
        if args.only and args.only.lower() not in c["name"].lower():
            continue
        res = check_company(c, include, exclude)
        name = c["name"]
        status = "ok"
        if res["error"]:
            status = "error"
        elif res["links"] < min_links:
            status = "few_links"
        health[name] = {
            "status": status,
            "links": res["links"],
            "matches": len(res["matches"]),
            "error": res["error"],
            "checked": now.isoformat(timespec="seconds"),
        }
        print(f"{name:28} {status:9} links={res['links']:4} matches={len(res['matches'])}")
        if status != "ok":
            continue  # never baseline or alert from a page that looks broken

        urls = [u for _, u in res["matches"]]
        if name not in seen and not args.notify_existing:
            seen[name] = urls
            baseline.append(name)
            continue
        known = set(seen.get(name, []))
        fresh = [(t, u) for t, u in res["matches"] if u not in known]
        if fresh:
            new_by_company[name] = fresh
            seen[name] = (seen.get(name, []) + [u for _, u in fresh])[-MAX_SEEN_PER_COMPANY:]

    if new_by_company:
        blocks = format_alert(new_by_company)
        if args.dry_run:
            print("\n\n".join(blocks))
        else:
            send_blocks(blocks)

    daily_health = now.hour == 6  # about 08:00 in Germany, once per day
    if not args.dry_run and (baseline or daily_health):
        msg = format_health(health)
        if baseline:
            msg += f"\n✅ First check done for {len(baseline)} sources. New postings will alert from now on."
        telegram_send(msg)

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("| Source | Status | Links | Matches |\n|---|---|---|---|\n")
            for n, h in health.items():
                f.write(f"| {n} | {h['status']} | {h['links']} | {h['matches']} |\n")

    if not args.dry_run:
        save_json(STATE_FILE, seen)
        save_json(HEALTH_FILE, health)

    return 1 if health and all(h["status"] == "error" for h in health.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
