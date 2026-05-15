"""
briefing.py — Daily morning digest builder for MemoraeBot

Compiles:
  1. Today's Apple Calendar events
  2. Tasks for today + this week
  3. Pending reminders
  4. One random memory (Serendipity)
  5. Quick stats
"""

import logging
from datetime import datetime
from typing import Optional

import database as db
import apple_calendar as ac

log = logging.getLogger(__name__)


def build_briefing(user: dict) -> str:
    """
    Build a full daily briefing string for a user.
    user: dict row from the users table
    """
    user_id     = user["id"]
    name        = (user.get("name") or "there").split()[0]
    tz_name     = user.get("timezone") or "Asia/Kolkata"

    import pytz
    tz = pytz.timezone(tz_name)
    now_local = datetime.now(tz)
    date_str  = now_local.strftime("%A, %-d %B %Y")

    lines = []
    lines.append(f"🌅 *Good morning, {name}!*")
    lines.append(f"_{date_str}_")
    lines.append("")

    # ── 1. Calendar events ────────────────────────────────────────────────────
    cal_client = ac.build_client(user)
    calendar_section = []
    if cal_client:
        try:
            events = cal_client.get_today_events()
            if events:
                calendar_section.append("📅 *Today's Calendar*")
                for evt in events[:8]:
                    start = evt.get("start")
                    title = evt.get("title", "(Untitled)")
                    time_str = start.strftime("%-I:%M %p") if start else "All day"
                    calendar_section.append(f"  • {time_str} — {title}")
            else:
                calendar_section.append("📅 *Calendar* — Nothing scheduled today, enjoy the open space!")
        except Exception as exc:
            log.warning("Briefing: calendar fetch failed: %s", exc)
            calendar_section.append("📅 *Calendar* — Couldn't fetch events right now.")
    else:
        calendar_section.append("📅 *Calendar* — Not connected. Use /settings to link your Apple ID.")

    lines.extend(calendar_section)
    lines.append("")

    # ── 2. Tasks ──────────────────────────────────────────────────────────────
    today_tasks   = db.get_tasks(user_id, status="today")
    week_tasks    = db.get_tasks(user_id, status="this_week")
    queue_tasks   = db.get_tasks(user_id, status="queue")

    lines.append("✅ *Tasks*")
    if today_tasks:
        lines.append(f"  🎯 *Today ({len(today_tasks)})*")
        for t in today_tasks[:5]:
            pri = "🔴 " if t.get("priority") == "high" else ""
            lines.append(f"    {pri}• {t['title']}")
    else:
        lines.append("  🎯 *Today* — Nothing pinned for today yet.")

    if week_tasks:
        lines.append(f"  📆 *This Week ({len(week_tasks)})*")
        for t in week_tasks[:3]:
            lines.append(f"    • {t['title']}")

    if queue_tasks:
        lines.append(f"  📋 *Queue* — {len(queue_tasks)} item(s) waiting.")
    lines.append("")

    # ── 3. Reminders ──────────────────────────────────────────────────────────
    pending_reminders = db.get_pending_reminders(user_id)
    upcoming = []
    if pending_reminders:
        for r in pending_reminders[:5]:
            try:
                remind_dt = datetime.strptime(r["remind_at"], "%Y-%m-%d %H:%M")
                remind_local = remind_dt.replace(tzinfo=__import__("pytz").utc).astimezone(tz)
                time_label = remind_local.strftime("%-d %b %-I:%M %p")
                upcoming.append(f"  • {time_label} — {r['content']}")
            except Exception:
                upcoming.append(f"  • {r['content']}")

    if upcoming:
        lines.append("⏰ *Upcoming Reminders*")
        lines.extend(upcoming)
        lines.append("")

    # ── 4. Serendipity ────────────────────────────────────────────────────────
    if user.get("serendipity_on", 1):
        memory = db.get_random_memory(user_id)
        if memory:
            saved_at = memory.get("created_at", "")[:10]
            content  = memory["content"]
            if len(content) > 200:
                content = content[:197] + "..."
            lines.append("✨ *A Memory From Your Past*")
            lines.append(f"  _{content}_")
            if saved_at:
                lines.append(f"  — saved on {saved_at}")
            lines.append("")

    # ── 5. Quick stats ────────────────────────────────────────────────────────
    mem_count   = db.get_memory_count(user_id)
    task_counts = db.get_task_counts(user_id)
    total_tasks = sum(v for k, v in task_counts.items() if k != "done")
    done_tasks  = task_counts.get("done", 0)

    lines.append("📊 *Your Stats*")
    lines.append(f"  🧠 {mem_count} memories saved  ·  ✅ {total_tasks} open tasks  ·  🏁 {done_tasks} done")
    lines.append("")
    lines.append("_Have a great day! Send me anything to save it._")

    return "\n".join(lines)


def build_serendipity_message(user: dict) -> str:
    """Build a standalone serendipity (random memory) message."""
    memory = db.get_random_memory(user["id"])
    if not memory:
        return "✨ *Serendipity*\n\nYou haven't saved any memories yet. Send me a thought to get started!"

    content  = memory["content"]
    saved_at = memory.get("created_at", "")[:10]
    coll     = memory.get("collection", "General")

    lines = ["✨ *Serendipity — A Random Memory*", ""]
    lines.append(f"_{content}_")
    lines.append("")
    if saved_at:
        lines.append(f"Saved on {saved_at}  ·  Collection: {coll}")
    lines.append("")
    lines.append("_Send_ `serendipity` _again for another random gem._")
    return "\n".join(lines)


def build_query_response(user: dict, intent: dict) -> str:
    """
    Build a response for a QUERY intent.
    intent: {target, query, filter}
    """
    user_id = user["id"]
    target  = (intent.get("target") or "all").lower()
    query   = (intent.get("query")  or "").strip()
    filt    = (intent.get("filter") or "").lower()

    lines = []

    # Memories
    if target in ("memories", "all", "memory"):
        if query:
            memories = db.search_memories(user_id, query, limit=8)
        elif filt:
            memories = db.get_memories(user_id, collection=filt.title(), limit=8)
        else:
            memories = db.get_memories(user_id, limit=8)

        if memories:
            lines.append(f"🧠 *Memories* ({len(memories)} found)")
            for m in memories:
                content = m["content"]
                if len(content) > 120:
                    content = content[:117] + "..."
                saved = m.get("created_at", "")[:10]
                lines.append(f"  • [{m['id']}] _{content}_ — {saved}")
            lines.append("")
        elif target in ("memories", "memory"):
            lines.append("🧠 No memories found" + (f" matching '{query}'" if query else "") + ".")
            lines.append("")

    # Tasks
    if target in ("tasks", "all", "task"):
        status_filter = None
        if filt in ("today", "this_week", "queue", "done"):
            status_filter = filt
        tasks = db.get_tasks(user_id, status=status_filter)
        if tasks:
            lines.append(f"✅ *Tasks* ({len(tasks)} found)")
            for t in tasks[:10]:
                status_icon = {"today": "🎯", "this_week": "📆", "queue": "📋", "done": "✅"}.get(t["status"], "•")
                pri = " 🔴" if t.get("priority") == "high" else ""
                dl  = f"  _{t['deadline']}_" if t.get("deadline") else ""
                lines.append(f"  {status_icon} [{t['id']}] *{t['title']}*{pri}{dl}")
            lines.append("")
        elif target in ("tasks", "task"):
            lines.append("✅ No tasks found.")
            lines.append("")

    # Reminders
    if target in ("reminders", "all", "reminder"):
        reminders = db.get_pending_reminders(user_id)
        if reminders:
            lines.append(f"⏰ *Reminders* ({len(reminders)} pending)")
            for r in reminders[:5]:
                remind_at = r["remind_at"]
                lines.append(f"  • [{r['id']}] {remind_at} — {r['content']}")
            lines.append("")

    # Calendar
    if target in ("calendar", "all", "events"):
        lines.append("📅 *Calendar* — Fetching from Apple Calendar...")
        cal_client = ac.build_client(user)
        if cal_client:
            try:
                events = cal_client.get_upcoming_events(days=7)
                if events:
                    lines[-1] = f"📅 *Calendar* — Next {len(events)} event(s)"
                    for evt in events[:8]:
                        lines.append("  " + cal_client.format_event_for_telegram(evt))
                    lines.append("")
                else:
                    lines[-1] = "📅 *Calendar* — No upcoming events in the next 7 days."
                    lines.append("")
            except Exception as exc:
                lines[-1] = f"📅 *Calendar* — Error fetching events: {exc}"
                lines.append("")
        else:
            lines[-1] = "📅 *Calendar* — Not connected. Use /settings to link your Apple ID."
            lines.append("")

    if not lines:
        lines.append("Nothing found for your query. Try being more specific, or send a message to save something!")

    return "\n".join(lines)
