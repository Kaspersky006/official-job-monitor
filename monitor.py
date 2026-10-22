#!/usr/bin/env python3
"""Hourly job-page monitor.

Fetches the career pages listed in companies.yml, keeps links that look like
working-student or internship postings, and sends a Telegram message for every
link it has not reported before. Reported links are stored in state/seen.json
and are never reported again.
"""
import argparse
import datetime as dt
import html
import json
import os
import re
import sys
from urllib.parse import parse_qsl, urldefrag, urlencode, urljoin, urlsplit, urlunsplit

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
MAX_SEEN_PER_COMPANY = 1000
MAX_ITEMS_PER_COMPANY_IN_ALERT = 10
TRACKING_PARAMS = {"gclid", "fbclid", "ref", "source", "sessionid", "jsessionid"}

GERMAN_PLACES = re.compile(
    r"germany|deutschland|bayern|bavaria|baden|w[uü]rttemberg|hessen|nordrhein|north rhine|sachsen|niedersachsen"
    r"|th[uü]ringen|brandenburg|rheinland|saarland|schleswig|mecklenburg"
    r"|m[uü]nchen|muenchen|munich|ingolstadt|stuttgart|berlin|hamburg|k[oö]ln|koeln|cologne|frankfurt"
    r"|d[uü]sseldorf|duesseldorf|dortmund|leipzig|dresden|n[uü]rnberg|nuernberg|nuremberg|erlangen"
    r"|regensburg|augsburg|karlsruhe|mannheim|heidelberg|\bulm\b|\bbonn\b|hannover|bremen|wolfsburg"
    r"|braunschweig|gaimersheim|manching|neubiberg|garching|unterschlei[sß]heim|b[oö]blingen|sindelfingen"
    r"|ludwigsburg|friedrichshafen|aachen|darmstadt|kassel|freiburg|w[uü]rzburg|potsdam|rostock|\bjena\b"
    r"|chemnitz|paderborn|bielefeld|m[uü]nster|neckarsulm|gifhorn|dingolfing|landshut|passau|r[uü]sselsheim"
    r"|bietigheim|bruchsal|ottobrunn|taufkirchen|oberpfaffenhofen|lindau|ravensburg|\bessen\b|gelsenkirchen"
    r"|wiesbaden|mainz|kiel|l[uü]beck|oldenburg|osnabr[uü]ck|saarbr[uü]cken|heilbronn|reutlingen|t[uü]bingen",
    re.I,
)
FOREIGN_PLACES = re.compile(
    r"\busa\b|united states|u\.s\.|india|china|romania|rum[aä]nien|poland|polen|hungary|ungarn|czech|tschech"
    r"|slovakia|slowakei|israel|bulgaria|bulgarien|spain|spanien|france|frankreich|italy|italien|netherlands"
    r"|niederlande|belgium|belgien|austria|[oö]sterreich|switzerland|schweiz|sweden|schweden|norway|norwegen"
    r"|denmark|d[aä]nemark|finland|finnland|portugal|turkey|t[uü]rkei|mexico|brazil|brasilien|argentina|canada"
    r"|kanada|japan|korea|singapore|malaysia|thailand|vietnam|philippines|australia|united kingdom|england"
    r"|scotland|ireland|serbia|croatia|slovenia|greece|egypt|morocco|south africa|\buae\b|dubai|saudi"
    r"|tel aviv|jerusalem|haifa|bangalore|bengaluru|pune|chennai|hyderabad|shanghai|beijing|shenzhen|london"
    r"|paris|madrid|barcelona|milan|vienna|\bwien\b|zurich|z[uü]rich|bucharest|cluj|brno|prague|warsaw"
    r"|krakow|budapest|gy[oő]r|detroit|michigan|texas|california|ohio|boston|seattle|bratislava|sofia"
    r"|timisoara|iasi|brasov|istanbul|lisbon|porto|amsterdam|brussels|stockholm|gothenburg|oslo|copenhagen"
    r"|helsinki|tokyo|seoul|taipei|kuala lumpur|bangkok|hanoi|manila|sydney|melbourne|toronto|montreal",
    re.I,
)


def location_ok(context, strict):
    """Germany-only filter.

    A German place in the card text always passes. A foreign place without a German one is dropped.
    With no location information at all, a strict source drops the job and a loose source keeps it.
    """
    if GERMAN_PLACES.search(context):
        return True
    if FOREIGN_PLACES.search(context):
        return False
    return not strict


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


def clean_url(url):
    """Drop fragments and tracking parameters so the same job always has the same key."""
    url = urldefrag(url)[0]
    parts = urlsplit(url)
    query = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in TRACKING_PARAMS
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def extract_links(markup, base_url):
    """Return [(text, absolute_url, context)] for every link on the page.

    context is the text of the surrounding job card (up to 350 characters), used to read the location.
    """
    soup = BeautifulSoup(markup, "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        text = a.get_text(" ", strip=True)
        context = text
        for parent in list(a.parents)[:5]:
            t = parent.get_text(" ", strip=True)
            hrefs = {x["href"] for x in parent.find_all("a", href=True)}
            if len(t) > 350 or len(hrefs) > 2:  # stop before the card merges with neighbouring jobs
                break
            context = t
        links.append((text, clean_url(urljoin(base_url, href)), context))
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
    out = []
    for o in r.json().get("offers", []):
        place = " ".join(str(o.get(k) or "") for k in ("location", "city", "country"))
        if str(o.get("country_code") or "").upper() == "DE":
            place += " Germany"
        out.append((o.get("title", ""), clean_url(o.get("careers_url", "")), place))
    return out


def fetch_arbeitnow(source):
    """Read Arbeitnow's open job feed and keep jobs from the companies named in source["companies"].

    Returns (links, total_jobs_seen) where links = [(title, url, location)].
    """
    wanted = re.compile(source.get("companies", ".*"), re.I)
    out, total = [], 0
    for page in range(1, int(source.get("pages", 5)) + 1):
        r = requests.get(
            "https://www.arbeitnow.com/api/job-board-api",
            params={"page": page}, headers={"User-Agent": UA}, timeout=25,
        )
        r.raise_for_status()
        jobs = r.json().get("data", [])
        if not jobs:
            break
        total += len(jobs)
        for j in jobs:
            company = j.get("company_name") or ""
            if wanted.search(company):
                out.append((f"{company}: {j.get('title', '')}", clean_url(j.get("url", "")), j.get("location") or ""))
    return out, total


def check_company(company, include, exclude):
    """Fetch one source. Returns {"links", "matches": [(title, url)], "dropped", "error"}."""
    kind = company.get("type", "html")
    url = company.get("url", "")
    total = None
    try:
        if kind == "arbeitnow":
            links, total = fetch_arbeitnow(company)
        elif kind == "recruitee":
            links = fetch_recruitee(url)
        elif kind == "playwright":
            links = extract_links(fetch_playwright(url, company.get("wait_ms", 3000)), url)
        else:
            links = extract_links(fetch_html(url), url)
    except Exception as exc:  # network errors, blocks, timeouts
        return {"links": 0, "matches": [], "dropped": 0, "error": f"{type(exc).__name__}: {str(exc)[:160]}"}

    strict = company.get("location", "loose") == "strict"
    matches, seen_urls, dropped = [], set(), 0
    for text, link, context in links:
        if link in seen_urls or not link:
            continue
        if not is_match(text, link, include, exclude):
            continue
        if not location_ok(f"{context} {link}", strict):
            dropped += 1
            continue
        seen_urls.add(link)
        matches.append((text or link.rsplit("/", 1)[-1], link))
    return {"links": total if total is not None else len(links), "matches": matches, "dropped": dropped, "error": None}


# ---------- notifications ----------

def telegram_send(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("::error::Telegram secrets are missing (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID). Message not sent.")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=25,
        )
    except requests.RequestException as exc:
        print(f"::error::Telegram request failed: {type(exc).__name__}")
        return False
    if not r.ok:
        print(f"::error::Telegram rejected the message: {r.status_code} {r.text[:200]}")
    return r.ok


def format_alert(by_company, icon="🆕", max_items=MAX_ITEMS_PER_COMPANY_IN_ALERT):
    blocks = []
    for name, items in by_company.items():
        lines = [f"{icon} <b>{html.escape(name)}</b>"]
        for title, url in items[:max_items]:
            lines.append(f"• {html.escape(title[:120])}\n{html.escape(url)}")
        extra = len(items) - max_items
        if extra > 0:
            lines.append(f"…and {extra} more")
        blocks.append("\n".join(lines))
    return blocks


def send_blocks(blocks, limit=3800):
    """Send blocks in as few messages as possible. Returns True only if every message was sent."""
    ok, chunk = True, ""
    for b in blocks:
        if chunk and len(chunk) + len(b) + 2 > limit:
            ok = telegram_send(chunk) and ok
            chunk = ""
        chunk += ("\n\n" if chunk else "") + b
    if chunk:
        ok = telegram_send(chunk) and ok
    return ok


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
    ap.add_argument("--report-all", action="store_true",
                    help="send every currently open matching job once and mark them all as reported")
    ap.add_argument("--only", help="check only companies whose name contains this text")
    args = ap.parse_args(argv)

    with open(os.path.join(ROOT, "companies.yml"), encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    include, exclude = compile_rx(cfg["include"]), compile_rx(cfg["exclude"])
    min_links = cfg.get("min_links", 5)

    seen = load_json(STATE_FILE, {})
    health, to_report, baseline, matched_titles = {}, {}, [], {}
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
            "dropped": res["dropped"],
            "error": res["error"],
            "checked": now.isoformat(timespec="seconds"),
        }
        matched_titles[name] = [t for t, _ in res["matches"]]
        print(f"{name:28} {status:9} links={res['links']:4} matches={len(res['matches'])} outside_germany={res['dropped']}")
        if status != "ok":
            continue  # never baseline or alert from a page that looks broken

        known = set(seen.get(name, []))
        if args.report_all:
            if res["matches"]:
                to_report[name] = res["matches"]
        elif name not in seen:
            baseline.append(name)  # first look at this company: record only, no alert
        else:
            fresh = [(t, u) for t, u in res["matches"] if u not in known]
            if fresh:
                to_report[name] = fresh

        # Mark as reported now; reverted below if Telegram fails.
        if not args.dry_run:
            seen[name] = (seen.get(name, []) + [u for _, u in res["matches"] if u not in known])[-MAX_SEEN_PER_COMPANY:]

    telegram_ok = True
    if to_report:
        if args.report_all:
            blocks = format_alert(to_report, icon="📋", max_items=30)
        else:
            blocks = format_alert(to_report)
        if args.dry_run:
            print("\n\n".join(blocks))
        else:
            telegram_ok = send_blocks(blocks)
            if not telegram_ok:
                # Do not mark jobs as reported when the message did not go out: they are retried next hour.
                previous = load_json(STATE_FILE, {})
                for name in to_report:
                    if name in previous:
                        seen[name] = previous[name]
                    else:
                        seen.pop(name, None)

    daily_health = now.hour == 6  # about 08:00 in Germany, once per day
    if not args.dry_run and (baseline or daily_health or args.report_all):
        msg = format_health(health)
        if baseline:
            msg += f"\n✅ First check done for {len(baseline)} sources. New postings will alert from now on."
        telegram_ok = telegram_send(msg) and telegram_ok

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(f"Telegram: {'ok' if telegram_ok else 'FAILED, see the log'}\n\n")
            f.write("| Source | Status | Links | Matches | Outside Germany | Matched titles |\n|---|---|---|---|---|---|\n")
            for n, h in health.items():
                titles = "; ".join(t[:50] for t in matched_titles.get(n, [])[:5]).replace("|", "/")
                f.write(f"| {n} | {h['status']} | {h['links']} | {h['matches']} | {h.get('dropped', 0)} | {titles} |\n")

    if not args.dry_run:
        save_json(STATE_FILE, seen)
        save_json(HEALTH_FILE, health)

    if not telegram_ok:
        return 2
    return 1 if health and all(h["status"] == "error" for h in health.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
