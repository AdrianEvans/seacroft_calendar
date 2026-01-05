#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import asyncio
import ftplib
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any

from spond import spond

# ---------------------------------------------------------------------
# Config from environment
# ---------------------------------------------------------------------
SPOND_USERNAME = os.getenv("SPOND_USERNAME")
SPOND_PASSWORD = os.getenv("SPOND_PASSWORD")
GROUP_ID       = os.getenv("GROUP_ID")

FTP_HOST   = os.getenv("FTP_HOST")
FTP_USER   = os.getenv("FTP_USER")
FTP_PASS   = os.getenv("FTP_PASS")
FTP_PORT   = int(os.getenv("FTP_PORT", "21"))
REMOTE_DIR = os.getenv("REMOTE_DIR", "calendars")  # dir on server

ICS_FILENAME = "spond_events.ics"

# Window for events exported to ICS
PAST_DAYS   = int(os.getenv("PAST_DAYS", "30"))     # include recent past
FUTURE_DAYS = int(os.getenv("FUTURE_DAYS", "365"))  # include distant future

# You said max ~600 in window; give some headroom
MAX_EVENTS = int(os.getenv("MAX_EVENTS", "1200"))


# ---------------------------------------------------------------------
# iCalendar helpers
# ---------------------------------------------------------------------
def format_dt_utc(dt):
    """Format a datetime as UTC for ICS, e.g. 20251127T090000Z"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y%m%dT%H%M%SZ")


def ical_escape(text):
    """Escape text for iCalendar (basic escaping)."""
    if not text:
        return ""
    text = text.replace("\\", "\\\\")
    text = text.replace(",", "\\,")
    text = text.replace(";", "\\;")
    text = text.replace("\r\n", "\\n").replace("\n", "\\n")
    return text


def fold_ical_line(line):
    """Fold a single ICS line at 75 characters with continuation."""
    max_len = 75
    if len(line) <= max_len:
        return line
    parts = []
    while len(line) > max_len:
        parts.append(line[:max_len])
        line = " " + line[max_len:]
    parts.append(line)
    return "\r\n".join(parts)


def parse_iso(ts):
    """Parse ISO timestamps that may end with 'Z'. Returns aware datetime or None."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


# ---------------------------------------------------------------------
# Spond → events
# ---------------------------------------------------------------------
async def fetch_events():
    """
    Fetch Spond events for a bounded time window, requesting enough events
    (max ~600 expected) so we don't hit the library's default cap.
    Ensures the underlying HTTP client session is closed cleanly.
    """
    s = spond.Spond(username=SPOND_USERNAME, password=SPOND_PASSWORD)

    try:
        now = datetime.now(timezone.utc)
        start_window = now - timedelta(days=PAST_DAYS)
        end_window   = now + timedelta(days=FUTURE_DAYS)

        # Ask Spond for the window + enough items to cover it.
        # (If the library ignores min/max filters, we still do a local filter below.)
        try:
            events = await s.get_events(
                group_id=GROUP_ID,
                include_scheduled=True,
                max_events=MAX_EVENTS,
                min_start=start_window,
                max_start=end_window,
            )
        except TypeError:
            # Older versions of the library may not support min_start/max_start.
            # Still request lots of events then filter locally.
            events = await s.get_events(
                group_id=GROUP_ID,
                include_scheduled=True,
                max_events=MAX_EVENTS,
            )

        filtered = []
        for e in events:
            start = parse_iso(e.get("startTimestamp"))
            if not start:
                continue
            if start_window <= start <= end_window:
                e["_parsed_start"] = start
                filtered.append(e)

        # De-dup (just in case)
        seen = set()
        uniq = []
        for e in filtered:
            eid = e.get("id") or e.get("eventId")
            key = eid or (e.get("heading"), e["_parsed_start"].isoformat())
            if key in seen:
                continue
            seen.add(key)
            uniq.append(e)

        uniq.sort(key=lambda x: x["_parsed_start"])
        return uniq

    finally:
        # Close aiohttp client session to avoid "Unclosed client session" warnings
        if hasattr(s, "clientsession"):
            await s.clientsession.close()


# ---------------------------------------------------------------------
# Build ICS
# ---------------------------------------------------------------------
def build_ics(events):
    """Build a complete .ics string from Spond events."""
    now = datetime.now(timezone.utc)

    lines = [
        "BEGIN:VCALENDAR",
        "PRODID:-//Seacroft Wheelers//Spond Events//EN",
        "VERSION:2.0",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Seacroft Wheelers Club Events (Spond)",
        "X-WR-TIMEZONE:Europe/London",
    ]

    for e in events:
        event_id = e.get("id") or e.get("eventId")
        if not event_id:
            continue

        title = (e.get("heading") or "Club Event").strip()
        description_from_spond = (e.get("description") or "").strip()

        # Location
        loc = e.get("location") or {}
        if isinstance(loc, dict):
            location = ", ".join(filter(None, [loc.get("feature"), loc.get("address")]))
        else:
            location = ""

        start = e.get("_parsed_start") or parse_iso(e.get("startTimestamp"))
        if not start:
            continue

        # End time: if missing/invalid, assume 2 hours
        end = parse_iso(e.get("endTimestamp")) or (start + timedelta(hours=2))

        # Last modified: use Spond mod timestamp if present
        last_mod = parse_iso(e.get("lastUpdatedTimestamp") or e.get("updatedAt")) or now

        uid = "spond-{}@seacroftwheelers.co.uk".format(event_id)

        # Event-specific Spond link
        spond_link = "https://spond.com/client/sponds/{}".format(event_id)

        base_description = (
            "For more detail on this ride visit:\n"
            "https://www.seacroftwheelers.co.uk/rides/\n\n"
            "To join a ride please use Spond to sign-up:\n"
            "https://club.spond.com/landing/signup/seacroftwheelers/form/2F862229C4DF48B585EF5220E2F914DA\n\n"
            "If you are registered, please open this event on Spond:\n{}".format(spond_link)
        )

        description_parts = []
        if description_from_spond:
            description_parts.append(description_from_spond)
        description_parts.append(base_description)
        description = "\n\n".join(description_parts)

        vevent = [
            "BEGIN:VEVENT",
            "UID:{}".format(uid),
            "DTSTAMP:{}".format(format_dt_utc(now)),
            "DTSTART:{}".format(format_dt_utc(start)),
            "DTEND:{}".format(format_dt_utc(end)),
            fold_ical_line("SUMMARY:" + ical_escape(title)),
        ]

        if location:
            vevent.append(fold_ical_line("LOCATION:" + ical_escape(location)))

        vevent.append(fold_ical_line("DESCRIPTION:" + ical_escape(description)))
        vevent.append("LAST-MODIFIED:{}".format(format_dt_utc(last_mod)))
        vevent.append("END:VEVENT")

        lines.extend(vevent)

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


# ---------------------------------------------------------------------
# FTP upload
# ---------------------------------------------------------------------
def upload_via_ftp(local_path):
    """Upload the generated ICS file to your web hosting via FTP."""
    print("[ICS] Uploading {} -> {}:{}/{}/{}".format(
        local_path,
        FTP_HOST,
        FTP_PORT,
        REMOTE_DIR.rstrip("/"),
        ICS_FILENAME
    ))

    with ftplib.FTP() as ftp:
        ftp.connect(FTP_HOST, FTP_PORT, timeout=30)
        ftp.login(FTP_USER, FTP_PASS)
        if REMOTE_DIR:
            ftp.cwd(REMOTE_DIR)
        with open(local_path, "rb") as f:
            ftp.storbinary("STOR {}".format(ICS_FILENAME), f)

    print("[ICS] Upload complete.")


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------
async def main():
    if not all([SPOND_USERNAME, SPOND_PASSWORD, GROUP_ID]):
        raise RuntimeError("Missing SPOND_USERNAME / SPOND_PASSWORD / GROUP_ID env vars")

    if not all([FTP_HOST, FTP_USER, FTP_PASS]):
        raise RuntimeError("Missing FTP_HOST / FTP_USER / FTP_PASS env vars")

    now = datetime.now(timezone.utc)
    start_window = now - timedelta(days=PAST_DAYS)
    end_window   = now + timedelta(days=FUTURE_DAYS)

    print("[ICS] Fetching Spond events …")
    print("[ICS] Window: {}  →  {}".format(start_window.isoformat(), end_window.isoformat()))
    print("[ICS] Requesting up to {} events from Spond…".format(MAX_EVENTS))

    events = await fetch_events()

    print("[ICS] Got {} events in window.".format(len(events)))
    if events:
        print("[ICS] Earliest: {}".format(events[0]["_parsed_start"].isoformat()))
        print("[ICS] Latest:   {}".format(events[-1]["_parsed_start"].isoformat()))

    ics_text = build_ics(events)

    with open(ICS_FILENAME, "w", encoding="utf-8", newline="") as f:
        f.write(ics_text)

    upload_via_ftp(ICS_FILENAME)
    print("[ICS] Done.")


if __name__ == "__main__":
    asyncio.run(main())
