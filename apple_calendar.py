"""
apple_calendar.py — Apple Calendar integration via iCloud CalDAV
Fixed for caldav 3.x (save_event + icalendar_component API)
"""

import logging
import uuid
from datetime import datetime, timedelta, date
from typing import Optional

import pytz

log = logging.getLogger(__name__)

ICLOUD_CALDAV_URL = "https://caldav.icloud.com/"


class AppleCalendarClient:

    def __init__(self, apple_id: str, app_password: str, timezone: str = "Asia/Kolkata"):
        self.apple_id    = apple_id
        self.app_password = app_password
        self.tz          = pytz.timezone(timezone)
        self._client     = None
        self._principal  = None
        self._calendars  = None

    # ── Private helpers ───────────────────────────────────────────────────────

    def _get_client(self):
        if self._client is not None:
            return self._client
        import caldav
        self._client = caldav.DAVClient(
            url=ICLOUD_CALDAV_URL,
            username=self.apple_id,
            password=self.app_password,
        )
        return self._client

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
            raise RuntimeError("No calendars found in iCloud account.")
        # Prefer Home / Personal / Calendar — else use first writable one
        for cal in cals:
            name = (str(getattr(cal, "name", "") or "")).lower()
            if name in ("home", "personal", "calendar", "icloud"):
                return cal
        return cals[0]

    def _localise(self, dt: datetime) -> datetime:
        if dt.tzinfo is None:
            return self.tz.localize(dt)
        return dt.astimezone(self.tz)

    def _build_ical(self, title: str, start_local: datetime,
                    end_local: datetime, description: str = "") -> str:
        """Build a valid iCal string for a single event."""
        from icalendar import Calendar, Event as ICalEvent

        cal   = Calendar()
        cal.add("prodid", "-//MemoraeBot//EN")
        cal.add("version", "2.0")
        cal.add("calscale", "GREGORIAN")

        evt = ICalEvent()
        evt.add("uid",      str(uuid.uuid4()) + "@memoraebot")
        evt.add("summary",  title)
        evt.add("dtstart",  start_local)
        evt.add("dtend",    end_local)
        evt.add("dtstamp",  datetime.now(pytz.utc))
        if description:
            evt.add("description", description)
        cal.add_component(evt)

        return cal.to_ical().decode("utf-8")

    # ── Public API ────────────────────────────────────────────────────────────

    def test_connection(self) -> tuple[bool, str]:
        try:
            cals  = self._get_calendars()
            names = [str(getattr(c, "name", "?")) for c in cals[:5]]
            return True, f"Connected ✅ — {len(cals)} calendar(s): {', '.join(names)}"
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
    ) -> tuple[bool, str]:
        """
        Create a calendar event. Returns (success, error_message).
        start/end should be naive datetimes in the user's local timezone.
        """
        try:
            start_local = self._localise(start)
            end_local   = self._localise(end) if end else start_local + timedelta(hours=1)

            ical_str = self._build_ical(title, start_local, end_local, description)
            calendar = self._get_default_calendar()

            # caldav 3.x uses save_event(); older versions used add_event()
            # Try save_event first, fall back to add_event
            try:
                calendar.save_event(ical_str)
            except AttributeError:
                calendar.add_event(ical_str)

            log.info("Event created in Apple Calendar: %s at %s", title, start_local)
            return True, ""

        except Exception as exc:
            log.error("Create event failed: %s", exc)
            return False, str(exc)

    def get_upcoming_events(self, days: int = 7) -> list[dict]:
        """Fetch events from all calendars for the next N days."""
        results   = []
        now       = datetime.now(pytz.utc)
        end_range = now + timedelta(days=days)

        try:
            for cal in self._get_calendars():
                events = self._fetch_from_calendar(cal, now, end_range)
                results.extend(events)
        except Exception as exc:
            log.error("get_upcoming_events failed: %s", exc)

        # Deduplicate by (title, start) — CalDAV can return dupes across calendars
        seen   = set()
        unique = []
        for e in results:
            key = (e["title"], str(e["start"]))
            if key not in seen:
                seen.add(key)
                unique.append(e)

        unique.sort(key=lambda e: (e["start"] or datetime.min.replace(tzinfo=pytz.utc)))
        return unique

    def _fetch_from_calendar(self, cal, now, end_range) -> list[dict]:
        """Try multiple strategies to fetch events from a single calendar."""
        cal_name = str(getattr(cal, "name", "?"))

        # Strategy 1: date_search with expand=False (iCloud compatible)
        try:
            raw_events = cal.date_search(start=now, end=end_range, expand=False)
            results = self._parse_events_list(raw_events, cal, now, end_range)
            if results:
                return results
        except TypeError:
            pass  # older caldav, try without expand
        except Exception as e1:
            log.debug("date_search(expand=False) failed for %s: %s", cal_name, e1)

        # Strategy 2: date_search without expand kwarg
        try:
            raw_events = cal.date_search(start=now, end=end_range)
            results = self._parse_events_list(raw_events, cal, now, end_range)
            if results:
                return results
        except Exception as e2:
            log.debug("date_search() failed for %s: %s", cal_name, e2)

        # Strategy 3: fetch all events and filter client-side
        # (slower but works for calendars that reject REPORT queries)
        try:
            raw_events = cal.events()
            return self._parse_events_list(raw_events, cal, now, end_range)
        except Exception as e3:
            log.warning("All fetch strategies failed for calendar '%s': %s", cal_name, e3)

        return []

    def _parse_events_list(self, raw_events, cal, now, end_range) -> list[dict]:
        """Parse a list of raw caldav events, filtering to the date window."""
        results = []
        for vevent in raw_events:
            try:
                parsed = self._parse_event(vevent, cal)
                if not parsed:
                    continue
                start = parsed.get("start")
                if start:
                    start_utc = start.astimezone(pytz.utc)
                    if now <= start_utc <= end_range:
                        results.append(parsed)
                else:
                    # All-day events with no parseable time — include them
                    results.append(parsed)
            except Exception as inner:
                log.debug("Skipping malformed event: %s", inner)
        return results

    def _parse_event(self, vevent, cal) -> dict | None:
        """Parse a caldav event object into a plain dict. Works with caldav 3.x and older."""
        title = start_dt = end_dt = description = None

        # ── Try caldav 3.x icalendar_component first ──────────────────────────
        try:
            comp = vevent.icalendar_component
            title       = str(comp.get("SUMMARY", "") or "").strip() or "(Untitled)"
            description = str(comp.get("DESCRIPTION", "") or "").strip()
            raw_start   = comp.get("DTSTART")
            raw_end     = comp.get("DTEND")
            start_dt    = raw_start.dt if raw_start else None
            end_dt      = raw_end.dt   if raw_end   else None
        except Exception:
            pass

        # ── Fallback: vobject_instance (older caldav) ─────────────────────────
        if title is None:
            try:
                comp        = vevent.vobject_instance.vevent
                title       = str(getattr(comp, "summary",     None) or "").strip() or "(Untitled)"
                description = str(getattr(comp, "description", None) or "").strip()
                dtstart     = getattr(comp, "dtstart", None)
                dtend       = getattr(comp, "dtend",   None)
                start_dt    = dtstart.value if dtstart else None
                end_dt      = dtend.value   if dtend   else None
            except Exception:
                return None

        if title is None:
            return None

        # Normalise date → datetime
        if isinstance(start_dt, date) and not isinstance(start_dt, datetime):
            start_dt = datetime.combine(start_dt, datetime.min.time()).replace(tzinfo=pytz.utc)
        if isinstance(end_dt, date) and not isinstance(end_dt, datetime):
            end_dt = datetime.combine(end_dt, datetime.min.time()).replace(tzinfo=pytz.utc)

        # Localise for display
        if start_dt and getattr(start_dt, "tzinfo", None):
            start_dt = start_dt.astimezone(self.tz)
        if end_dt and getattr(end_dt, "tzinfo", None):
            end_dt = end_dt.astimezone(self.tz)

        return {
            "title":       title,
            "start":       start_dt,
            "end":         end_dt,
            "calendar":    str(getattr(cal, "name", None) or "Calendar"),
            "description": description or "",
        }

    def get_today_events(self) -> list[dict]:
        today = datetime.now(self.tz).date()
        return [
            e for e in self.get_upcoming_events(days=1)
            if e["start"] and e["start"].date() == today
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


# ── Module-level convenience ──────────────────────────────────────────────────

def build_client(user: dict) -> Optional[AppleCalendarClient]:
    apple_id  = (user.get("apple_id")       or "").strip()
    apple_pwd = (user.get("apple_password") or "").strip()
    if not apple_id or not apple_pwd:
        return None
    return AppleCalendarClient(apple_id, apple_pwd,
                               timezone=user.get("timezone") or "Asia/Kolkata")
