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


def build_briefing(user: dict, target_date: str = None) -> str:
    """
    Build a full daily briefing string for a user.
    target_date: optional 'YYYY-MM-DD' — defaults to today.
    """
    import pytz

    user_id = user["id"]
    name    = (user.get("name") or "there").split()[0]
    tz_name = user.get("timezone") or "Asia/Kolkata"
    tz      = pytz.timezone(tz_name)

    now_local = datetime.now(tz)

    # Resolve target date
    if target_date:
        try:
            target_dt = datetime.strptime(target_date, "%Y-%m-%d")
            target_dt = tz.localize(target_dt.replace(hour=0, minute=0))
        except Exception:
            target_dt = now_local
    else:
        target_dt = now_local

    target_date_obj = target_dt.date()
    is_today        = (target_date_obj == now_local.date())

    date_str  = target_dt.strftime("%A, %-d %B %Y")
    greeting  = "Good morning" if is_today else f"Briefing for {target_dt.strftime('%-d %B')}"

    lines = [f"🌅 *{greeting}, {name}!*", f"_{date_str}_", ""]

    # ── 1. Calendar events for target date ───────────────────────────────────
    cal_client = ac.build_client(user)
    if cal_client:
        try:
            days_ahead = max(1, (target_date_obj - now_local.date()).days + 1)
            all_events = cal_client.get_upcoming_events(days=days_ahead + 1)
            events = [
                e for e in all_events
                if e.get("start") and e["start"].date() == target_date_obj
                and not e.get("is_reminder")   # shown in Reminders section instead
            ]
            if events:
                lines.append("📅 *Calendar*")
                for evt in events[:8]:
                    start = evt.get("start")
                    title = evt.get("title", "(Untitled)")
                    time_str = start.strftime("%-I:%M %p") if start else "All day"
                    lines.append(f"  • {time_str} — {title}")
            else:
                lines.append("📅 *Calendar* — Nothing scheduled, enjoy the open space!")
        except Exception as exc:
            log.warning("Briefing: calendar fetch failed: %s", exc)
            lines.append("📅 *Calendar* — Couldn't fetch events right now.")
    else:
        lines.append("📅 *Calendar* — Not connected. Use /settings to link your Apple ID.")
    lines.append("")

    # ── 2. Tasks ──────────────────────────────────────────────────────────────
    today_tasks = db.get_tasks(user_id, status="today")
    week_tasks  = db.get_tasks(user_id, status="this_week")
    queue_tasks = db.get_tasks(user_id, status="queue")

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

    # ── 3. Reminders for target date ──────────────────────────────────────────
    all_reminders = db.get_pending_reminders(user_id)
    target_prefix = target_date_obj.strftime("%Y-%m-%d")
    day_reminders = [r for r in all_reminders if r["remind_at"].startswith(target_prefix)]

    if day_reminders:
        lines.append("⏰ *Reminders*")
        for r in day_reminders[:8]:
            try:
                dt_utc   = datetime.strptime(r["remind_at"], "%Y-%m-%d %H:%M").replace(tzinfo=pytz.utc)
                dt_local = dt_utc.astimezone(tz)
                time_label = dt_local.strftime("%-I:%M %p")
                lines.append(f"  • {time_label} — {r['content']}")
            except Exception:
                lines.append(f"  • {r['content']}")
        lines.append("")

    # ── 4. Serendipity (today only) ───────────────────────────────────────────
    if is_today and user.get("serendipity_on", 1):
        memory = db.get_random_memory(user_id)
        if memory:
            saved_raw = memory.get("created_at", "")[:10]
            try:
                saved = datetime.strptime(saved_raw, "%Y-%m-%d").strftime("%-d %b %Y")
            except Exception:
                saved = saved_raw
            content = memory["content"]
            if len(content) > 200:
                content = content[:197] + "..."
            lines.append("✨ *A Memory From Your Past*")
            lines.append(f"  _{content}_")
            if saved:
                lines.append(f"  — saved on {saved}")
            lines.append("")

    # ── 5. Quick stats ────────────────────────────────────────────────────────
    mem_count   = db.get_memory_count(user_id)
    task_counts = db.get_task_counts(user_id)
    total_tasks = sum(v for k, v in task_counts.items() if k != "done")
    done_tasks  = task_counts.get("done", 0)

    lines.append("📊 *Your Stats*")
    lines.append(f"  🧠 {mem_count} memories  ·  ✅ {total_tasks} open tasks  ·  🏁 {done_tasks} done")
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
    user_id  = user["id"]
    tz_name  = user.get("timezone") or "Asia/Kolkata"
    target   = (intent.get("target") or "all").lower()
    query    = (intent.get("query")  or "").strip()
    filt     = (intent.get("filter") or "").lower()

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
                coll  = m.get("collection", "General")
                lines.append(f"  • _{content}_")
                lines.append(f"    #{m['id']} · {coll} · {saved}")
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
                dl  = f" · due {t['deadline']}" if t.get("deadline") else ""
                lines.append(f"  {status_icon} *{t['title']}*{pri}")
                lines.append(f"    #{t['id']} · {t['status'].replace('_',' ').title()}{dl}")
            lines.append("")
        elif target in ("tasks", "task"):
            lines.append("✅ No tasks found.")
            lines.append("")

    # Reminders
    if target in ("reminders", "all", "reminder"):
        reminders = db.get_pending_reminders(user_id)
        if reminders:
            import pytz
            tz_obj = pytz.timezone(tz_name)
            lines.append(f"⏰ *Reminders* ({len(reminders)} pending)")
            for r in reminders[:5]:
                try:
                    dt_utc   = datetime.strptime(r["remind_at"], "%Y-%m-%d %H:%M").replace(tzinfo=pytz.utc)
                    dt_local = dt_utc.astimezone(tz_obj)
                    time_str = dt_local.strftime("%-d %b, %-I:%M %p")
                except Exception:
                    time_str = r["remind_at"]
                lines.append(f"  • ⏰ *{time_str}* — {r['content']}")
            lines.append("")

    # Calendar
    if target in ("calendar", "all", "events"):
        lines.append("📅 *Calendar* — Fetching from Apple Calendar...")
        cal_client = ac.build_client(user)
        if cal_client:
            try:
                all_events = cal_client.get_upcoming_events(days=7)
                events = [e for e in all_events if not e.get("is_reminder")]
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
