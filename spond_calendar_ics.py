#!/usr/bin/env python3
import os
import asyncio
import ftplib
from datetime import datetime, timedelta, timezone
from spond import spond

# ---------------------------------------------------------------------
# Config from environment (same pattern as your other scripts)
# ---------------------------------------------------------------------
SPOND_USERNAME = os.getenv("SPOND_USERNAME")
SPOND_PASSWORD = os.getenv("SPOND_PASSWORD")
GROUP_ID       = os.getenv("GROUP_ID")

FTP_HOST   = os.getenv("FTP_HOST")
FTP_USER   = os.getenv("FTP_USER")
FTP_PASS   = os.getenv("FTP_PASS")
FTP_PORT   = int(os.getenv("FTP_PORT", "21"))
REMOTE_DIR = os.getenv("REMOTE_DIR", "calendars")  # <- calendar dir on server

ICS_FILENAME = "spond_events.ics"


# ---------------------------------------------------------------------
# iCalendar helpers
# ---------------------------------------------------------------------
def format_dt_utc(dt: datetime) -> str:
    """
    Format a datetime as UTC for ICS, e.g. 20251127T090000Z
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y%m%dT%H%M%SZ")


def ical_escape(text: str) -> str:
    """
    Escape text for iCalendar (basic escaping).
    """
    if not text:
        return ""
    text = text.replace("\\", "\\\\")
    text = text.replace(",", "\\,")
    text = text.replace(";", "\\;")
    text = text.replace("\r\n", "\\n").replace("\n", "\\n")
    return text


def fold_ical_line(line: str) -> str:
    """
    Fold a single ICS line at 75 characters with continuation.
    """
    max_len = 75
    if len(line) <= max_len:
        return line
    parts = []
    while len(line) > max_len:
        parts.append(line[:max_len])
        line = " " + line[max_len:]
    parts.append(line)
    return "\r\n".join(parts)


# ---------------------------------------------------------------------
# Spond → events
# ---------------------------------------------------------------------
async def fetch_events() -> list:
    """
    Fetch Spond events and return a filtered list in the desired date range.
    Ensures the underlying HTTP client session is closed cleanly.
    """
    s = spond.Spond(username=SPOND_USERNAME, password=SPOND_PASSWORD)

    try:
        # You can add max_events=... if needed later
        events = await s.get_events(group_id=GROUP_ID, include_scheduled=True)

        now = datetime.now(timezone.utc)
        start_window = now - timedelta(days=30)       # include recent past
        end_window   = now + timedelta(days=365)      # and distant future

        filtered = []
        for e in events:
            ts = e.get("startTimestamp")
            if not ts:
                continue
            try:
                start = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except Exception:
                continue
            if start_window <= start <= end_window:
                e["_parsed_start"] = start
                filtered.append(e)

        filtered.sort(key=lambda x: x["_parsed_start"])
        return filtered

    finally:
        # Close aiohttp client session to avoid "Unclosed client session" warnings
        if hasattr(s, "clientsession"):
            await s.clientsession.close()


# ---------------------------------------------------------------------
# Build ICS
# ---------------------------------------------------------------------
def build_ics(events: list) -> str:
    """
    Build a complete .ics string from Spond events.
    """
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

        # Title visible in Spond = heading (to match spond_pull.py)
        title = (e.get("heading") or "Club Event").strip()

        # Any description text from Spond itself
        description_from_spond = (e.get("description") or "").strip()

        # Location: build from Spond location object if present
        loc = e.get("location") or {}
        if isinstance(loc, dict):
            location = ", ".join(
                filter(None, [loc.get("feature"), loc.get("address")])
            )
        else:
            location = ""

        start = e.get("_parsed_start")
        if not start:
            continue

        # End time – if Spond doesn't give an endTimestamp, assume 2 hours
        end_ts = e.get("endTimestamp")
        if end_ts:
            try:
                end = datetime.fromisoformat(end_ts.replace("Z", "+00:00"))
            except Exception:
                end = start + timedelta(hours=2)
        else:
            end = start + timedelta(hours=2)

        # Use Spond modification timestamp if available, else now
        last_mod_ts = e.get("lastUpdatedTimestamp") or e.get("updatedAt")
        if last_mod_ts:
            try:
                last_mod = datetime.fromisoformat(last_mod_ts.replace("Z", "+00:00"))
            except Exception:
                last_mod = now
        else:
            last_mod = now

        uid = f"spond-{event_id}@seacroftwheelers.co.uk"

        # Event-specific Spond link
        spond_link = f"https://spond.com/client/sponds/{event_id}"

        # Standard description footer with links
        base_description = (
            "For more detail on this ride visit:\n"
            "https://www.seacroftwheelers.co.uk/rides/\n\n"
            "To join a ride please use Spond to sign-up:\n"
            "https://club.spond.com/landing/signup/seacroftwheelers/form/2F862229C4DF48B585EF5220E2F914DA\n\n"
            f"If you are registered, please open this event on Spond:\n{spond_link}"
        )

        description_parts = []
        if description_from_spond:
            description_parts.append(description_from_spond)
        description_parts.append(base_description)
        description = "\n\n".join(description_parts)

        vevent = []
        vevent.append("BEGIN:VEVENT")
        vevent.append(f"UID:{uid}")
        vevent.append(f"DTSTAMP:{format_dt_utc(now)}")
        vevent.append(f"DTSTART:{format_dt_utc(start)}")
        vevent.append(f"DTEND:{format_dt_utc(end)}")
        vevent.append(fold_ical_line("SUMMARY:" + ical_escape(title)))
        if location:
            vevent.append(fold_ical_line("LOCATION:" + ical_escape(location)))
        vevent.append(fold_ical_line("DESCRIPTION:" + ical_escape(description)))
        vevent.append(f"LAST-MODIFIED:{format_dt_utc(last_mod)}")
        vevent.append("END:VEVENT")

        lines.extend(vevent)

    lines.append("END:VCALENDAR")

    # Join with CRLF as per spec
    return "\r\n".join(lines) + "\r\n"


# ---------------------------------------------------------------------
# FTP upload
# ---------------------------------------------------------------------
def upload_via_ftp(local_path: str):
    """
    Upload the generated ICS file to your web hosting via FTP.
    """
    print(
        f"[ICS] Uploading {local_path} to FTP "
        f"{FTP_HOST}:{FTP_PORT}/{REMOTE_DIR}/{ICS_FILENAME}"
    )
    with ftplib.FTP() as ftp:
        ftp.connect(FTP_HOST, FTP_PORT, timeout=30)
        ftp.login(FTP_USER, FTP_PASS)
        if REMOTE_DIR:
            ftp.cwd(REMOTE_DIR)
        with open(local_path, "rb") as f:
            ftp.storbinary(f"STOR {ICS_FILENAME}", f)
    print("[ICS] Upload complete.")


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------
async def main():
    if not all([SPOND_USERNAME, SPOND_PASSWORD, GROUP_ID]):
        raise RuntimeError("Missing SPOND_* env vars")

    if not all([FTP_HOST, FTP_USER, FTP_PASS]):
        raise RuntimeError("Missing FTP_* env vars")

    print("[ICS] Fetching Spond events …")
    events = await fetch_events()
    print(f"[ICS] Got {len(events)} events in window.")

    ics_text = build_ics(events)
    local_path = ICS_FILENAME

    with open(local_path, "w", encoding="utf-8", newline="") as f:
        f.write(ics_text)

    upload_via_ftp(local_path)
    print("[ICS] Done.")


if __name__ == "__main__":
    asyncio.run(main())

