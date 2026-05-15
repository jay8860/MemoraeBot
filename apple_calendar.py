"""
apple_calendar.py — Apple Calendar integration via iCloud CalDAV

Apple does NOT have a public REST API for Calendar — the correct protocol is CalDAV.
iCloud exposes CalDAV at: https://caldav.icloud.com/

Auth: Apple ID + App-Specific Password
  → Generate at: https://appleid.apple.com → Sign-In & Security → App-Specific Passwords

Usage:
  client = AppleCalendarClient(apple_id="you@icloud.com", app_password="xxxx-xxxx-xxxx-xxxx")
  client.create_event(title="Team meeting", start=datetime(2025,6,1,14,0), end=datetime(2025,6,1,15,0))
  events = client.get_upcoming_events(days=7)
"""

import logging
import uuid
from datetime import datetime, timedelta, date
from typing import Optional

import pytz

log = logging.getLogger(__name__)

# iCloud CalDAV discovery URL
ICLOUD_CALDAV_URL = "https://caldav.icloud.com/"


class AppleCalendarClient:
    """Thin wrapper around the caldav library for iCloud calendars."""

    def __init__(self, apple_id: str, app_password: str, timezone: str = "Asia/Kolkata"):
        self.apple_id = apple_id
        self.app_password = app_password
        self.tz = pytz.timezone(timezone)
        self._client = None
        self._principal = None
        self._calendars = None

    # ── Private helpers ───────────────────────────────────────────────────────

    def _get_client(self):
        if self._client is not None:
            return self._client
        try:
            import caldav
            self._client = caldav.DAVClient(
                url=ICLOUD_CALDAV_URL,
                username=self.apple_id,
                password=self.app_password,
            )
            return self._client
        except ImportError:
            raise RuntimeError("caldav package not installed. Run: pip install caldav")

    def _get_principal(self):
        if self._principal is None:
            self._principal = self._get_client().principal()
        return self._principal

    def _get_calendars(self):
        if self._calendars is None:
            self._calendars = self._get_principal().calendars()
        return self._calendars

    def _get_default_calendar(self):
        cals = self._get_calendars()
        if not cals:
            raise RuntimeError("No calendars found in your iCloud account.")
        # Prefer a calendar named 'Home' or 'Personal' — otherwise use first
        for cal in cals:
            name = (getattr(cal, 'name', None) or "").lower()
            if name in ("home", "personal", "calendar"):
                return cal
        return cals[0]

    def _localise(self, dt: datetime) -> datetime:
        if dt.tzinfo is None:
            return self.tz.localize(dt)
        return dt.astimezone(self.tz)

    # ── Public API ────────────────────────────────────────────────────────────

    def test_connection(self) -> tuple[bool, str]:
        """Test if credentials are valid. Returns (ok, message)."""
        try:
            cals = self._get_calendars()
            names = [getattr(c, 'name', '?') for c in cals[:5]]
            return True, f"Connected. Found {len(cals)} calendar(s): {', '.join(str(n) for n in names)}"
        except Exception as exc:
            log.error("Apple Calendar connection failed: %s", exc)
            return False, str(exc)

    def create_event(
        self,
        title: str,
        start: datetime,
        end: datetime = None,
        description: str = "",
        all_day: bool = False,
    ) -> bool:
        """
        Create a new calendar event.
        start/end should be naive datetime objects in the user's local timezone.
        """
        try:
            from icalendar import Calendar, Event

            start_local = self._localise(start)
            end_local   = self._localise(end) if end else start_local + timedelta(hours=1)

            cal = Calendar()
            cal.add("prodid", "-//MemoraeBot//EN")
            cal.add("version", "2.0")

            event = Event()
            event.add("summary", title)
            if all_day:
                event.add("dtstart", start_local.date())
                event.add("dtend",   (end_local.date() + timedelta(days=1)))
            else:
                event.add("dtstart", start_local)
                event.add("dtend",   end_local)
            if description:
                event.add("description", description)
            event.add("uid", str(uuid.uuid4()))
            event.add("dtstamp", datetime.utcnow().replace(tzinfo=pytz.utc))
            cal.add_component(event)

            calendar = self._get_default_calendar()
            calendar.add_event(cal.to_ical().decode("utf-8"))
            log.info("Event created: %s at %s", title, start_local)
            return True

        except Exception as exc:
            log.error("Create event failed: %s", exc)
            return False

    def get_upcoming_events(self, days: int = 7) -> list[dict]:
        """
        Fetch events from all calendars for the next N days.
        Returns a list of dicts: {title, start, end, calendar, description}
        """
        results = []
        now = datetime.now(pytz.utc)
        end_range = now + timedelta(days=days)

        try:
            for cal in self._get_calendars():
                try:
                    raw_events = cal.date_search(start=now, end=end_range, expand=True)
                    for vevent in raw_events:
                        try:
                            comp = vevent.vobject_instance.vevent
                            title = str(getattr(comp, "summary", None) or "").strip() or "(Untitled)"
                            dtstart = getattr(comp, "dtstart", None)
                            dtend   = getattr(comp, "dtend",   None)

                            start_dt = dtstart.value if dtstart else None
                            end_dt   = dtend.value   if dtend   else None

                            # Normalise to datetime
                            if isinstance(start_dt, date) and not isinstance(start_dt, datetime):
                                start_dt = datetime.combine(start_dt, datetime.min.time(), tzinfo=pytz.utc)
                            if isinstance(end_dt, date) and not isinstance(end_dt, datetime):
                                end_dt = datetime.combine(end_dt, datetime.min.time(), tzinfo=pytz.utc)

                            # Localise for display
                            if start_dt and start_dt.tzinfo:
                                start_dt = start_dt.astimezone(self.tz)
                            if end_dt and end_dt.tzinfo:
                                end_dt = end_dt.astimezone(self.tz)

                            description = str(getattr(comp, "description", None) or "").strip()
                            cal_name = str(getattr(cal, "name", None) or "Calendar")

                            results.append({
                                "title":       title,
                                "start":       start_dt,
                                "end":         end_dt,
                                "calendar":    cal_name,
                                "description": description,
                            })
                        except Exception as inner:
                            log.debug("Skipping malformed event: %s", inner)
                except Exception as cal_exc:
                    log.warning("Error searching calendar %s: %s", getattr(cal, "name", "?"), cal_exc)

        except Exception as exc:
            log.error("get_upcoming_events failed: %s", exc)

        # Sort by start time
        results.sort(key=lambda e: (e["start"] or datetime.min.replace(tzinfo=pytz.utc)))
        return results

    def get_today_events(self) -> list[dict]:
        return [
            e for e in self.get_upcoming_events(days=1)
            if e["start"] and e["start"].date() == datetime.now(self.tz).date()
        ]

    def get_calendar_names(self) -> list[str]:
        try:
            return [str(getattr(c, "name", "?")) for c in self._get_calendars()]
        except Exception:
            return []

    def format_event_for_telegram(self, event: dict) -> str:
        start = event.get("start")
        end   = event.get("end")
        title = event.get("title", "(Untitled)")
        cal   = event.get("calendar", "")

        if start:
            time_str = start.strftime("%-d %b, %I:%M %p")
            if end and (end - start) < timedelta(hours=24):
                time_str += f" → {end.strftime('%I:%M %p')}"
        else:
            time_str = "All day"

        line = f"📅 *{title}*  —  {time_str}"
        if cal:
            line += f"\n   _{cal}_"
        if event.get("description"):
            line += f"\n   {event['description'][:80]}"
        return line


# ── Module-level convenience functions ────────────────────────────────────────

def build_client(user: dict) -> Optional[AppleCalendarClient]:
    """Build a client from a user DB row. Returns None if not configured."""
    apple_id  = (user.get("apple_id")       or "").strip()
    apple_pwd = (user.get("apple_password") or "").strip()
    if not apple_id or not apple_pwd:
        return None
    tz = user.get("timezone") or "Asia/Kolkata"
    return AppleCalendarClient(apple_id, apple_pwd, timezone=tz)
