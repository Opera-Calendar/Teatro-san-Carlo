#!/usr/bin/env python3
"""Teatro di San Carlo opera calendars, refreshed for every published season."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import hashlib
import json
import logging
import re

import requests
from bs4 import BeautifulSoup, NavigableString, Tag

ROOT = Path(__file__).resolve().parent
TZ = ZoneInfo("Europe/Rome")
BASE = "https://www.classicalive.it/it/rassegne/teatro-di-san-carlo/"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; OperaCalendar/1.0)"}
MONTHS = {
    "gennaio": 1, "febbraio": 2, "marzo": 3, "aprile": 4,
    "maggio": 5, "giugno": 6, "luglio": 7, "agosto": 8,
    "settembre": 9, "ottobre": 10, "novembre": 11, "dicembre": 12,
}
DATE_RE = re.compile(
    r"\b(?:Lun|Mar|Mer|Gio|Ven|Sab|Dom)\s+(\d{1,2})\s+"
    r"(" + "|".join(MONTHS) + r")\s+(20\d{2})\s+(\d{2}:\d{2})\b",
    re.IGNORECASE,
)


def discover_pages(session):
    """Try rolling season URLs; no fixed ending season is embedded."""
    year = datetime.now(TZ).year
    urls = []
    for first in range(year - 2, year + 7):
        urls.append(f"{BASE}stagione-dopera-{first}-{first + 1}/")
    # Additional known URL spelling can be added here if source changes.
    for url in urls:
        try:
            response = session.get(url, headers=HEADERS, timeout=25)
            if response.status_code == 404:
                continue
            response.raise_for_status()
            if "Stagione d'opera" not in response.text and "Stagione d’opera" not in response.text:
                continue
            yield url, BeautifulSoup(response.text, "html.parser")
        except requests.RequestException as exc:
            logging.warning("Could not read %s: %s", url, exc)


def parse_page(url, soup):
    """Extract dated performance links grouped below each opera heading."""
    found = {}
    for heading in soup.find_all("h2"):
        title = heading.get_text(" ", strip=True)
        if not title or title.lower() in {"in programma", "teatro di san carlo"}:
            continue
        anchors = []
        snippets = []
        for element in heading.next_elements:
            if isinstance(element, Tag) and element.name == "h2":
                break
            if isinstance(element, Tag) and element.name == "a":
                label = element.get_text(" ", strip=True)
                if DATE_RE.search(label):
                    anchors.append((label, element.get("href") or url))
            elif isinstance(element, NavigableString):
                t = str(element).strip()
                if t and len(snippets) < 60:
                    snippets.append(t)
        if not anchors:
            continue
        summary = " ".join(snippets)
        conductor = ""
        match = re.search(r"Direttore\s+(.+?)(?:\s*[·•]|\s+Regia|\s+\d+\s+recite)", summary, re.I)
        if match:
            conductor = match.group(1).strip()
        for label, link in anchors:
            match = DATE_RE.search(label)
            if not match:
                continue
            day, month, year, time = match.groups()
            start = datetime.strptime(
                f"{year}-{MONTHS[month.lower()]:02d}-{int(day):02d} {time}",
                "%Y-%m-%d %H:%M",
            ).replace(tzinfo=TZ)
            uid = hashlib.sha256(f"{title}|{start.isoformat()}".encode()).hexdigest()[:24] + "@san-carlo-opera"
            description = f"Source: {url}"
            if conductor:
                description = f"Conductor: {conductor}\\n" + description
            found[uid] = {
                "uid": uid, "title": title, "start": start,
                "description": description, "url": url,
            }
    return found


def ics_escape(value):
    return (str(value).replace("\\", "\\\\").replace("\n", "\\n")
            .replace(",", "\\,").replace(";", "\\;"))


def utc_stamp(dt):
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def event_block(event):
    start = event["start"]
    # Approximate duration; no claim of an exact finish time.
    end = start + timedelta(hours=3)
    return [
        "BEGIN:VEVENT", f"UID:{event['uid']}",
        f"DTSTAMP:{utc_stamp(datetime.now(timezone.utc))}",
        f"DTSTART:{utc_stamp(start)}", f"DTEND:{utc_stamp(end)}",
        f"SUMMARY:{ics_escape(event['title'])}",
        "LOCATION:Teatro di San Carlo\\, Napoli",
        f"DESCRIPTION:{ics_escape(event['description'] + chr(10) + 'End time approximate (3h).')}",
        f"URL:{event['url']}", "END:VEVENT",
    ]


def old_blocks(path):
    """Keep previously published performances across season rollover."""
    if not path.exists():
        return {}
    content = path.read_text(encoding="utf-8")
    entries = {}
    for block in re.findall(r"BEGIN:VEVENT\r?\n.*?END:VEVENT", content, re.S):
        match = re.search(r"^UID:(.+)$", block, re.M)
        if match:
            entries[match.group(1).strip()] = block.splitlines()
    return entries


def write_ics(path, events, calendar_name, keep_history=True):
    blocks = old_blocks(path) if keep_history else {}
    for event in events.values():
        blocks[event["uid"]] = event_block(event)
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Opera Calendar//San Carlo//EN",
             "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
             f"X-WR-CALNAME:{ics_escape(calendar_name)}",
             "X-WR-TIMEZONE:Europe/Rome"]
    for uid in sorted(blocks):
        lines.extend(blocks[uid])
    lines.append("END:VCALENDAR")
    path.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
    logging.info("Wrote %s with %d events", path.name, len(blocks))


def ticket_events():
    """Read ONLY verified sales dates; never guess opening dates."""
    path = ROOT / "ticket_sales.json"
    if not path.exists():
        return {}
    sales = json.loads(path.read_text(encoding="utf-8"))
    result = {}
    for row in sales:
        start = datetime.fromisoformat(row["start"]).replace(tzinfo=TZ)
        label = row["title"]
        uid = hashlib.sha256((label + start.isoformat()).encode()).hexdigest()[:24] + "@san-carlo-sales"
        result[uid] = {
            "uid": uid, "title": "Ticket sale: " + label,
            "start": start, "description": "Verified ticket-sale opening. Source: " + row["source"],
            "url": row["source"],
        }
    return result


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    session = requests.Session()
    performances = {}
    discovered = 0
    for url, soup in discover_pages(session):
        items = parse_page(url, soup)
        if items:
            discovered += 1
            performances.update(items)
            logging.info("Found %d events from %s", len(items), url)
    if not performances and not (ROOT / "san-carlo-opera.ics").exists():
        raise RuntimeError("No opera dates found. Refusing to publish an empty opera calendar.")
    if not performances:
        logging.warning("No new programme data. Keeping previously downloaded calendars.")
    write_ics(ROOT / "san-carlo-opera.ics", performances, "San Carlo - Opera")
    saturday = {uid: item for uid, item in performances.items() if item["start"].weekday() == 5}
    write_ics(ROOT / "san-carlo-saturdays.ics", saturday, "San Carlo - Saturday Opera")
    sales = ticket_events()
    write_ics(ROOT / "san-carlo-ticket-sales.ics", sales, "San Carlo - Ticket Sales")
    logging.info("Seasons found: %d | performances: %d | sales: %d", discovered, len(performances), len(sales))


if __name__ == "__main__":
    main()
