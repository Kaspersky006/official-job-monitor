from __future__ import annotations

import hashlib
import json
import os
import re
import smtplib
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "companies.yml"
STATE_PATH = ROOT / "data" / "seen_jobs.json"

STUDENT_PATTERNS = re.compile(
    r"\b(working\s*student|werkstudent(?:in)?|student(?:ische)?\s*(?:aushilfe|mitarbeiter)|"
    r"intern(?:ship)?|praktik(?:ant|antin|um)|mandatory\s+internship|thesis|abschlussarbeit)\b",
    re.I,
)
RELEVANCE_PATTERNS = re.compile(
    r"\b(devops|ci/?cd|jenkins|github actions|gitlab|python|software|embedded|firmware|"
    r"machine learning|artificial intelligence|data|computer vision|perception|autonomous|"
    r"robotics|validation|verification|test|quality|qa|linux|docker|cloud|infrastructure|"
    r"electrical|electronics|control|automation|matlab|simulink|ros2?|systems? engineering|it)\b",
    re.I,
)
LOCATION_PATTERNS = re.compile(
    r"\b(Germany|Deutschland|Munich|München|Ingolstadt|Berlin|Stuttgart|Hamburg|"
    r"Frankfurt|Erlangen|Nuremberg|Nürnberg|Karlsruhe|Ulm|Dresden|Remote|Hybrid|Europe|EU)\b",
    re.I,
)
ATS_DOMAINS = {
    "boards.greenhouse.io", "job-boards.greenhouse.io", "jobs.lever.co", "jobs.ashbyhq.com",
    "personio.de", "jobs.personio.com", "smartrecruiters.com", "careers.smartrecruiters.com",
    "teamtailor.com", "recruitee.com", "workable.com", "myworkdayjobs.com", "workdayjobs.com",
    "successfactors.eu", "successfactors.com", "join.com"
}
IGNORE_ANCHOR = re.compile(r"^(apply|apply now|learn more|read more|details|view job|see jobs?)$", re.I)


@dataclass(frozen=True)
class Job:
    company: str
    title: str
    location: str
    url: str
    reason: str

    @property
    def fingerprint(self) -> str:
        canonical = f"{self.company}|{self.title}|{self.location}|{self.url}".lower().strip()
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]


def load_companies() -> list[dict]:
    with CONFIG_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f)["companies"]


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def normalized_host(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


def allowed_url(url: str, official_domain: str) -> bool:
    host = normalized_host(url)
    official = official_domain.lower().removeprefix("www.")
    return host == official or host.endswith("." + official) or any(host == d or host.endswith("." + d) for d in ATS_DOMAINS)


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def infer_location(text: str) -> str:
    matches = LOCATION_PATTERNS.findall(text)
    if matches:
        unique = list(dict.fromkeys(clean(x) for x in matches))
        return ", ".join(unique[:3])
    return "Location not stated"


def reason_for(title: str, context: str) -> str:
    terms = list(dict.fromkeys(m.group(0) for m in RELEVANCE_PATTERNS.finditer(f"{title} {context}")))
    if terms:
        return "Matches your background through " + ", ".join(terms[:4]) + "."
    return "Matches your requested Working Student/Internship profile in a technical company."


def extract_jsonld_jobs(soup: BeautifulSoup, company: str, base_url: str) -> list[Job]:
    jobs: list[Job] = []
    for tag in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        items = payload if isinstance(payload, list) else [payload]
        expanded: list[dict] = []
        for item in items:
            if isinstance(item, dict) and isinstance(item.get("@graph"), list):
                expanded.extend(x for x in item["@graph"] if isinstance(x, dict))
            elif isinstance(item, dict):
                expanded.append(item)
        for item in expanded:
            if item.get("@type") != "JobPosting":
                continue
            title = clean(item.get("title", ""))
            description = clean(BeautifulSoup(item.get("description", ""), "html.parser").get_text(" "))
            if not STUDENT_PATTERNS.search(f"{title} {description}"):
                continue
            if not RELEVANCE_PATTERNS.search(f"{title} {description}"):
                continue
            location = "Location not stated"
            job_loc = item.get("jobLocation")
            if isinstance(job_loc, dict):
                addr = job_loc.get("address", {})
                if isinstance(addr, dict):
                    location = clean(", ".join(str(addr.get(k, "")) for k in ("addressLocality", "addressRegion", "addressCountry") if addr.get(k))) or location
            elif isinstance(job_loc, list):
                location = infer_location(json.dumps(job_loc))
            url = item.get("url") or base_url
            url = urljoin(base_url, url)
            jobs.append(Job(company, title, location, url, reason_for(title, description)))
    return jobs


def extract_anchor_jobs(soup: BeautifulSoup, company: str, base_url: str, official_domain: str) -> list[Job]:
    jobs: list[Job] = []
    page_text = clean(soup.get_text(" "))
    for a in soup.find_all("a", href=True):
        title = clean(a.get_text(" "))
        href = urljoin(base_url, a["href"])
        if not title or len(title) < 5 or len(title) > 180 or IGNORE_ANCHOR.match(title):
            continue
        if not allowed_url(href, official_domain):
            continue
        parent_text = clean(a.parent.get_text(" ") if a.parent else title)
        combined = f"{title} {parent_text}"
        if not STUDENT_PATTERNS.search(combined):
            continue
        if not RELEVANCE_PATTERNS.search(combined):
            continue
        location = infer_location(combined)
        jobs.append(Job(company, title, location, href, reason_for(title, combined)))
    return jobs


def discover_links(soup: BeautifulSoup, base_url: str, official_domain: str) -> list[str]:
    links: list[str] = []
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"])
        text = clean(a.get_text(" "))
        if not allowed_url(href, official_domain):
            continue
        marker = f"{text} {href}"
        if re.search(r"job|career|position|vacanc|opening|stellen|work-with|join-us|intern|student", marker, re.I):
            links.append(href.split("#")[0])
    return list(dict.fromkeys(links))[:30]


def fetch(session: requests.Session, url: str) -> requests.Response | None:
    try:
        response = session.get(url, timeout=25, allow_redirects=True)
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        if "text/html" not in content_type and "application/xhtml" not in content_type and not response.text.lstrip().startswith("<"):
            return None
        return response
    except requests.RequestException as exc:
        print(f"WARN fetch failed: {url}: {exc}", file=sys.stderr)
        return None


def scan_company(company: dict) -> tuple[list[Job], str]:
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (compatible; OfficialJobMonitor/1.0; +https://github.com/)",
        "Accept-Language": "en-US,en;q=0.9,de;q=0.8",
    })
    start = company["careers_url"]
    queue = [start]
    visited: set[str] = set()
    found: dict[str, Job] = {}
    max_pages = int(os.getenv("MAX_PAGES_PER_COMPANY", "18"))

    while queue and len(visited) < max_pages:
        url = queue.pop(0)
        if url in visited or not allowed_url(url, company["domain"]):
            continue
        visited.add(url)
        response = fetch(session, url)
        if not response:
            continue
        final_url = response.url
        soup = BeautifulSoup(response.text, "html.parser")
        for job in extract_jsonld_jobs(soup, company["name"], final_url):
            found[job.fingerprint] = job
        for job in extract_anchor_jobs(soup, company["name"], final_url, company["domain"]):
            found[job.fingerprint] = job
        for link in discover_links(soup, final_url, company["domain"]):
            if link not in visited and link not in queue:
                queue.append(link)
        time.sleep(float(os.getenv("REQUEST_DELAY_SECONDS", "0.15")))

    status = f"checked {len(visited)} page(s), found {len(found)} matching role(s)"
    return list(found.values()), status


def render_email(new_jobs: list[Job], statuses: list[str], errors: list[str]) -> tuple[str, str]:
    timestamp = datetime.now(ZoneInfo("Europe/Berlin")).strftime("%d %B %Y, %H:%M %Z")
    if new_jobs:
        subject = f"Job monitor: {len(new_jobs)} new matching role(s)"
        lines = [f"Official careers-page search completed at {timestamp}.", "", "NEW MATCHING ROLES", ""]
        for i, job in enumerate(sorted(new_jobs, key=lambda j: (j.company, j.title)), 1):
            lines.extend([
                f"{i}. {job.title}",
                f"Company: {job.company}",
                f"Location: {job.location}",
                f"Official application link: {job.url}",
                f"Why it matches: {job.reason}",
                "",
            ])
    else:
        subject = "Job monitor completed: no new matching roles"
        lines = [
            f"Official careers-page search completed successfully at {timestamp}.",
            "",
            "No new matching Working Student or Internship openings were found in this run.",
            "",
        ]
    lines.extend(["RUN DETAILS", *[f"- {s}" for s in statuses]])
    if errors:
        lines.extend(["", "WARNINGS", *[f"- {e}" for e in errors]])
    lines.extend(["", "Only official company careers pages and their linked official ATS pages were used."])
    return subject, "\n".join(lines)


def send_email(subject: str, body: str) -> None:
    username = os.environ.get("SMTP_USERNAME")
    password = os.environ.get("SMTP_APP_PASSWORD")
    recipient = os.environ.get("ALERT_EMAIL", username or "")
    if not username or not password or not recipient:
        raise RuntimeError("SMTP_USERNAME, SMTP_APP_PASSWORD and ALERT_EMAIL must be configured")
    msg = EmailMessage()
    msg["From"] = username
    msg["To"] = recipient
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(username, password)
        smtp.send_message(msg)


def main() -> int:
    force = os.getenv("FORCE_RUN", "false").lower() == "true"
    now = datetime.now(ZoneInfo("Europe/Berlin"))
    if not force and not (11 <= now.hour <= 23):
        print(f"Outside Berlin run window: {now.isoformat()}")
        return 0

    state = load_state()
    first_run = not bool(state)
    report_existing = os.getenv("REPORT_EXISTING_ON_FIRST_RUN", "true").lower() == "true"
    all_jobs: list[Job] = []
    statuses: list[str] = []
    errors: list[str] = []

    for company in load_companies():
        try:
            jobs, status = scan_company(company)
            all_jobs.extend(jobs)
            statuses.append(f"{company['name']}: {status}")
        except Exception as exc:  # continue other companies
            error = f"{company['name']}: {type(exc).__name__}: {exc}"
            print("ERROR", error, file=sys.stderr)
            errors.append(error)

    unique = {job.fingerprint: job for job in all_jobs}
    if first_run and report_existing:
        new_jobs = list(unique.values())
    else:
        new_jobs = [job for fp, job in unique.items() if fp not in state]

    timestamp = now.isoformat()
    for fp, job in unique.items():
        state[fp] = {**asdict(job), "last_seen": timestamp, "first_seen": state.get(fp, {}).get("first_seen", timestamp)}
    save_state(state)

    subject, body = render_email(new_jobs, statuses, errors)
    print(body)
    send_email(subject, body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
