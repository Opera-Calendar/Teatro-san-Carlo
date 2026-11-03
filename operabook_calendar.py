#!/usr/bin/env python3
"""Teatro di San Carlo opera calendars, refreshed for every published season."""

from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from dateutil.relativedelta import relativedelta
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


def ticket_alarm_blocks(sale_date, title):
    """Four real VALARM notifications, not separate VEVENT calendar entries.

    For date-only ticket sales, use 09:00 Europe/Rome on each reminder day.
    This does not claim the ticket sale itself opens at 09:00.
    """
    offsets = [
        (relativedelta(months=3), "3 months"),
        (relativedelta(months=1), "1 month"),
        (timedelta(weeks=1), "1 week"),
        (timedelta(days=1), "1 day"),
    ]
    lines = []
    for delta, label in offsets:
        reminder_date = sale_date - delta
        trigger = datetime.combine(reminder_date, time(9, 0), TZ)
        lines.extend([
            "BEGIN:VALARM", "ACTION:DISPLAY",
            f"DESCRIPTION:{ics_escape(title + ' - ' + label + ' before')}",
            f"TRIGGER;VALUE=DATE-TIME:{utc_stamp(trigger)}",
            "END:VALARM",
        ])
    return lines


def event_block(event):
    if event.get("kind") == "ticket_sale":
        sale_date = event["date"]
        tomorrow = sale_date + timedelta(days=1)
        lines = [
            "BEGIN:VEVENT", f"UID:{event['uid']}",
            f"DTSTAMP:{utc_stamp(datetime.now(timezone.utc))}",
            f"DTSTART;VALUE=DATE:{sale_date:%Y%m%d}",
            f"DTEND;VALUE=DATE:{tomorrow:%Y%m%d}",
            f"SUMMARY:{ics_escape(event['title'])}",
            "LOCATION:Teatro di San Carlo\\, Napoli",
            f"DESCRIPTION:{ics_escape(event['description'])}",
            f"URL:{event['url']}",
        ]
        lines.extend(ticket_alarm_blocks(sale_date, event["title"]))
        return lines + ["END:VEVENT"]

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


SALE_CATEGORIES = {
    "public": "Public ticket sales",
    "released_seats": "Release of unrenewed subscription seats",
}


def ticket_events():
    """One visible event per confirmed sale category per season.

    The local ticket_sales.json is the source of truth; we never guess dates.
    Entries must contain season, type, date (YYYY-MM-DD), and source.
    """
    path = ROOT / "ticket_sales.json"
    if not path.exists():
        logging.warning("ticket_sales.json absent: ticket calendar will be empty")
        return {}
    sales = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(sales, list):
        raise ValueError("ticket_sales.json must contain a JSON array")
    result = {}
    per_season = set()
    for row in sales:
        season = row["season"]
        sale_type = row["type"]
        if sale_type not in SALE_CATEGORIES:
            raise ValueError(f"Unsupported ticket sale type: {sale_type!r}")
        key = (season, sale_type)
        if key in per_season:
            raise ValueError(f"Duplicate ticket-sale category for season {season}: {sale_type}")
        per_season.add(key)
        sale_date = date.fromisoformat(row["date"])
        source = row["source"]
        uid = f"{sale_type}-{season.replace('/', '-')}@san-carlo-sales"
        label = f"San Carlo {season} - {SALE_CATEGORIES[sale_type]}"
        result[uid] = {
            "uid": uid, "kind": "ticket_sale", "date": sale_date,
            "title": label,
            "description": (
                f"{SALE_CATEGORIES[sale_type]} for season {season}.\n"
                "Opening hour not published/verified: event is all-day.\n"
                "Advance alerts are notifications, not separate events.\n"
                f"Source: {source}"
            ),
            "url": source,
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
    write_ics(ROOT / "san-carlo-ticket-sales.ics", sales, "San Carlo - Ticket Sales", keep_history=False)
    logging.info("Seasons found: %d | performances: %d | sales: %d", discovered, len(performances), len(sales))


if __name__ == "__main__":
    main()
