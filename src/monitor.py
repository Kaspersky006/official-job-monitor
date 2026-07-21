from __future__ import annotations

import hashlib
import json
import os
import re
import smtplib
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse
from zoneinfo import ZoneInfo

import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "companies.yml"
STATE_PATH = ROOT / "data" / "seen_jobs.json"

STUDENT_PATTERNS = re.compile(
    r"\b(working\s*student|working\s*student\s*/\s*intern|werkstudent(?:in)?|"
    r"student(?:ische)?\s*(?:aushilfe|mitarbeiter)|student\s+(?:assistant|researcher|engineer|worker)|"
    r"intern(?:ship)?|praktik(?:ant|antin|um)|mandatory\s+internship|"
    r"bachelor(?:'s)?\s+thesis|master(?:'s)?\s+thesis|thesis|abschlussarbeit)\b",
    re.I,
)

RELEVANCE_PATTERNS = re.compile(
    r"\b(devops|ci/?cd|jenkins|github actions|gitlab|python|software|embedded|firmware|"
    r"machine learning|artificial intelligence|\bai\b|\bml\b|data|computer vision|perception|"
    r"autonomous|robotics|validation|verification|test(?:ing)?|quality|\bqa\b|linux|docker|"
    r"cloud|infrastructure|electrical|electronics|control|automation|matlab|simulink|ros2?|"
    r"systems? engineering|information technology|\bit\b|backend|platform|tooling|simulation|"
    r"sensor|avionics|mechatronics|semiconductor)\b",
    re.I,
)

LOCATION_PATTERNS = re.compile(
    r"\b(Germany|Deutschland|Munich|München|Ingolstadt|Berlin|Stuttgart|Hamburg|"
    r"Frankfurt|Erlangen|Nuremberg|Nürnberg|Karlsruhe|Ulm|Dresden|Duisburg|Aachen|"
    r"Gilching|Ottobrunn|Manching|Remote|Hybrid|Europe|EU)\b",
    re.I,
)

ATS_DOMAINS = {
    "boards.greenhouse.io",
    "job-boards.greenhouse.io",
    "jobs.lever.co",
    "jobs.ashbyhq.com",
    "personio.de",
    "jobs.personio.com",
    "smartrecruiters.com",
    "careers.smartrecruiters.com",
    "teamtailor.com",
    "recruitee.com",
    "workable.com",
    "myworkdayjobs.com",
    "workdayjobs.com",
    "successfactors.eu",
    "successfactors.com",
    "join.com",
    "jobs.infineon.com",
    "jobs.eightfold.ai",
    "careers.eightfold.ai",
}

BAD_TITLES = {
    "",
    "skip to content",
    "skip to main content",
    "home",
    "menu",
    "privacy",
    "privacy policy",
    "imprint",
    "impressum",
    "cookie settings",
    "cookies",
    "contact",
    "login",
    "log in",
    "register",
    "sign in",
    "search",
    "jobs",
    "careers",
    "apply",
    "apply now",
    "learn more",
    "read more",
    "details",
    "view job",
    "see jobs",
    "back",
    "next",
    "previous",
}

EXCLUDED_TITLE_PATTERNS = re.compile(
    r"\b(business development|sales|marketing|communications?|public relations|finance|"
    r"accounting|legal|human resources|\bhr\b|people(?:\s+operations)?|talent acquisition|"
    r"recruit(?:er|ing)|customer success|office management|office manager|commercial|"
    r"social media|content creation|event management)\b",
    re.I,
)

IGNORE_ANCHOR = re.compile(
    r"^(apply|apply now|learn more|read more|details|view job|see jobs?)$",
    re.I,
)

DISCOVERY_MARKERS = re.compile(
    r"job|career|position|vacanc|opening|stellen|work-with|join-us|intern|student|werkstudent|praktikum",
    re.I,
)

NON_HTTP_SCHEMES = ("mailto:", "tel:", "javascript:", "data:")


@dataclass(frozen=True)
class Job:
    company: str
    title: str
    location: str
    url: str
    reason: str

    @property
    def fingerprint(self) -> str:
        canonical = f"{self.company}|{self.title}|{self.location}|{canonicalize_url(self.url)}".lower().strip()
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]


def load_companies() -> list[dict]:
    with CONFIG_PATH.open(encoding="utf-8") as file:
        payload = yaml.safe_load(file)

    if not isinstance(payload, dict) or not isinstance(payload.get("companies"), list):
        raise ValueError("companies.yml must contain a top-level 'companies' list")

    companies: list[dict] = []
    for index, company in enumerate(payload["companies"], start=1):
        if not isinstance(company, dict):
            raise ValueError(f"Company entry #{index} must be a mapping")
        missing = {"name", "domain", "careers_url"} - company.keys()
        if missing:
            raise ValueError(f"Company entry #{index} is missing: {', '.join(sorted(missing))}")
        companies.append(company)
    return companies


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        payload = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def canonicalize_url(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, parsed.query, ""))


def normalized_host(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


def allowed_url(url: str, official_domain: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False

    host = parsed.netloc.lower().removeprefix("www.")
    official = official_domain.lower().removeprefix("www.")
    return (
        host == official
        or host.endswith("." + official)
        or any(host == domain or host.endswith("." + domain) for domain in ATS_DOMAINS)
    )


def is_ignored_link(raw_href: str) -> bool:
    href = clean(raw_href).lower()
    return (
        not href
        or href.startswith("#")
        or href.startswith(NON_HTTP_SCHEMES)
    )


def is_excluded_title(title: str) -> bool:
    return bool(EXCLUDED_TITLE_PATTERNS.search(title))


def infer_location(text: str) -> str:
    matches = LOCATION_PATTERNS.findall(text)
    if matches:
        unique = list(dict.fromkeys(clean(match) for match in matches))
        return ", ".join(unique[:3])
    return "Location not stated"


def reason_for(title: str, context: str) -> str:
    matches: list[str] = []
    seen: set[str] = set()
    for match in RELEVANCE_PATTERNS.finditer(f"{title} {context}"):
        value = clean(match.group(0))
        key = value.casefold()
        if key not in seen:
            seen.add(key)
            matches.append(value)

    if matches:
        return "Relevant skills/areas: " + ", ".join(matches[:5]) + "."
    return "Relevant student role at a technical company."


def iter_jsonld_objects(payload: object) -> list[dict]:
    objects: list[dict] = []
    if isinstance(payload, list):
        for item in payload:
            objects.extend(iter_jsonld_objects(item))
    elif isinstance(payload, dict):
        objects.append(payload)
        graph = payload.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                objects.extend(iter_jsonld_objects(item))
    return objects


def extract_jsonld_jobs(soup: BeautifulSoup, company: str, base_url: str) -> list[Job]:
    jobs: list[Job] = []
    for tag in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue

        for item in iter_jsonld_objects(payload):
            item_type = item.get("@type")
            is_job = item_type == "JobPosting" or (
                isinstance(item_type, list) and "JobPosting" in item_type
            )
            if not is_job:
                continue

            title = clean(str(item.get("title", "")))
            description = clean(
                BeautifulSoup(str(item.get("description", "")), "html.parser").get_text(" ")
            )
            combined = f"{title} {description}"

            if not title or title.casefold() in BAD_TITLES or is_excluded_title(title):
                continue
            if not STUDENT_PATTERNS.search(combined):
                continue
            if not RELEVANCE_PATTERNS.search(combined):
                continue

            location = "Location not stated"
            job_location = item.get("jobLocation")
            if isinstance(job_location, dict):
                address = job_location.get("address", {})
                if isinstance(address, dict):
                    location = clean(
                        ", ".join(
                            str(address.get(key, ""))
                            for key in ("addressLocality", "addressRegion", "addressCountry")
                            if address.get(key)
                        )
                    ) or location
            elif isinstance(job_location, list):
                location = infer_location(json.dumps(job_location, ensure_ascii=False))

            raw_url = str(item.get("url") or base_url)
            url = canonicalize_url(urljoin(base_url, raw_url))
            jobs.append(Job(company, title, location, url, reason_for(title, description)))

    return jobs


def extract_anchor_jobs(
    soup: BeautifulSoup,
    company: str,
    base_url: str,
    official_domain: str,
) -> list[Job]:
    jobs: list[Job] = []

    for anchor in soup.find_all("a", href=True):
        title = clean(anchor.get_text(" "))
        normalized_title = title.casefold()
        raw_href = clean(str(anchor.get("href", "")))

        if normalized_title in BAD_TITLES:
            continue
        if not title or len(title) < 5 or len(title) > 180:
            continue
        if IGNORE_ANCHOR.fullmatch(title):
            continue
        if is_excluded_title(title):
            continue
        if is_ignored_link(raw_href):
            continue

        href = canonicalize_url(urljoin(base_url, raw_href))
        if not allowed_url(href, official_domain):
            continue

        # Use a small nearby container rather than the entire page. This avoids
        # navigation links inheriting unrelated student/technical keywords.
        container = anchor.find_parent(["article", "li", "section", "div"])
        context = clean(container.get_text(" ") if container else title)
        if len(context) > 2500:
            context = context[:2500]
        combined = f"{title} {context}"

        if not STUDENT_PATTERNS.search(combined):
            continue
        if not RELEVANCE_PATTERNS.search(combined):
            continue

        location = infer_location(combined)
        jobs.append(Job(company, title, location, href, reason_for(title, combined)))

    return jobs


def discover_links(soup: BeautifulSoup, base_url: str, official_domain: str) -> list[str]:
    links: list[str] = []

    for anchor in soup.find_all("a", href=True):
        raw_href = clean(str(anchor.get("href", "")))
        if is_ignored_link(raw_href):
            continue

        href = canonicalize_url(urljoin(base_url, raw_href))
        if not allowed_url(href, official_domain):
            continue

        text = clean(anchor.get_text(" "))
        marker = f"{text} {href}"
        if DISCOVERY_MARKERS.search(marker):
            links.append(href)

    return list(dict.fromkeys(links))[:30]


def fetch(session: requests.Session, url: str) -> requests.Response | None:
    retries = max(1, int(os.getenv("FETCH_RETRIES", "2")))
    timeout = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "30"))

    for attempt in range(1, retries + 1):
        try:
            response = session.get(url, timeout=timeout, allow_redirects=True)
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").lower()
            starts_like_html = response.text.lstrip().startswith("<")
            if (
                "text/html" not in content_type
                and "application/xhtml" not in content_type
                and not starts_like_html
            ):
                return None
            return response
        except requests.RequestException as exc:
            if attempt == retries:
                print(f"WARN fetch failed: {url}: {exc}", file=sys.stderr)
                return None
            time.sleep(min(1.0 * attempt, 3.0))

    return None


def scan_company(company: dict) -> tuple[list[Job], str]:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,de;q=0.8",
        }
    )

    start = canonicalize_url(company["careers_url"])
    queue = [start]
    visited: set[str] = set()
    found: dict[str, Job] = {}
    max_pages = int(os.getenv("MAX_PAGES_PER_COMPANY", "18"))
    request_delay = float(os.getenv("REQUEST_DELAY_SECONDS", "0.2"))

    while queue and len(visited) < max_pages:
        url = queue.pop(0)
        if url in visited or not allowed_url(url, company["domain"]):
            continue

        visited.add(url)
        response = fetch(session, url)
        if response is None:
            continue

        final_url = canonicalize_url(response.url)
        soup = BeautifulSoup(response.text, "html.parser")

        for job in extract_jsonld_jobs(soup, company["name"], final_url):
            found[job.fingerprint] = job

        for job in extract_anchor_jobs(
            soup,
            company["name"],
            final_url,
            company["domain"],
        ):
            found[job.fingerprint] = job

        for link in discover_links(soup, final_url, company["domain"]):
            if link not in visited and link not in queue:
                queue.append(link)

        time.sleep(request_delay)

    status = f"checked {len(visited)} page(s), found {len(found)} matching role(s)"
    return list(found.values()), status


def render_email(
    new_jobs: list[Job],
    statuses: list[str],
    errors: list[str],
) -> tuple[str, str]:
    timestamp = datetime.now(ZoneInfo("Europe/Berlin")).strftime("%d %B %Y, %H:%M %Z")

    if new_jobs:
        subject = f"Job monitor: {len(new_jobs)} new matching role(s)"
        lines = [
            f"Official careers-page search completed at {timestamp}.",
            "",
            "NEW MATCHING ROLES",
            "",
        ]
        for index, job in enumerate(sorted(new_jobs, key=lambda item: (item.company, item.title)), 1):
            lines.extend(
                [
                    f"{index}. {job.title}",
                    f"Company: {job.company}",
                    f"Location: {job.location}",
                    f"Official application link: {job.url}",
                    f"Why it matches: {job.reason}",
                    "",
                ]
            )
    else:
        subject = "Job monitor completed: no new matching roles"
        lines = [
            f"Official careers-page search completed successfully at {timestamp}.",
            "",
            "No new matching Working Student or Internship openings were found in this run.",
            "",
        ]

    lines.extend(["RUN DETAILS", *[f"- {status}" for status in statuses]])
    if errors:
        lines.extend(["", "WARNINGS", *[f"- {error}" for error in errors]])
    lines.extend(
        [
            "",
            "Only official company careers pages and their linked official ATS pages were used.",
        ]
    )
    return subject, "\n".join(lines)


def send_email(subject: str, body: str) -> None:
    username = os.environ.get("SMTP_USERNAME")
    password = os.environ.get("SMTP_APP_PASSWORD")
    recipient = os.environ.get("ALERT_EMAIL", username or "")

    if not username or not password or not recipient:
        raise RuntimeError("SMTP_USERNAME, SMTP_APP_PASSWORD and ALERT_EMAIL must be configured")

    message = EmailMessage()
    message["From"] = username
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(username, password)
        smtp.send_message(message)


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
        except Exception as exc:  # Continue checking the remaining companies.
            error = f"{company['name']}: {type(exc).__name__}: {exc}"
            print("ERROR", error, file=sys.stderr)
            errors.append(error)

    unique = {job.fingerprint: job for job in all_jobs}
    if first_run and report_existing:
        new_jobs = list(unique.values())
    else:
        new_jobs = [job for fingerprint, job in unique.items() if fingerprint not in state]

    timestamp = now.isoformat()
    for fingerprint, job in unique.items():
        previous = state.get(fingerprint, {})
        state[fingerprint] = {
            **asdict(job),
            "last_seen": timestamp,
            "first_seen": previous.get("first_seen", timestamp),
        }
    save_state(state)

    subject, body = render_email(new_jobs, statuses, errors)
    print(body)
    send_email(subject, body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
