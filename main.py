"""
main.py — MemoraeBot
A personal memory operating system for Telegram.

Features (all 4 phases):
  Phase 1 — Memory bubbles, 8-intent router, onboarding
  Phase 2 — Task board (Queue/This Week/Today/Done), Apple Calendar events
  Phase 3 — Reminders (APScheduler via PTB JobQueue), daily briefings, Serendipity, Trunk
  Phase 4 — Memory collections, smart context, voice→full flow, weekly stats
"""

import os
import re
import json
import logging
import asyncio
import datetime
import base64
from io import BytesIO

from dotenv import load_dotenv
load_dotenv()

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
)
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)
from telegram.constants import ParseMode

import database as db
import intent_router as ir
import apple_calendar as ac
import briefing as briefing_module

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
TOKEN             = os.getenv("TELEGRAM_BOT_TOKEN", "")
GEMINI_API_KEY    = os.getenv("GEMINI_API_KEY", "")
DEFAULT_TIMEZONE  = os.getenv("DEFAULT_TIMEZONE", "Asia/Kolkata")
DEFAULT_BRIEFING  = os.getenv("DEFAULT_BRIEFING_TIME", "07:00")

# ── Drive upload (Trunk) — reuse TaskAdderBot logic ───────────────────────────
def _upload_to_drive(file_path: str, name: str, mime_type: str) -> str | None:
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload

        raw = os.getenv("GOOGLE_JSON", "")
        if not raw:
            return None
        try:
            info = json.loads(raw)
        except Exception:
            info = json.loads(base64.b64decode(raw).decode())

        creds = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/drive.file"]
        )
        service = build("drive", "v3", credentials=creds)
        meta = {"name": name}
        folder = os.getenv("GOOGLE_DRIVE_FOLDER_ID", "").strip()
        if folder:
            meta["parents"] = [folder]
        media = MediaFileUpload(file_path, mimetype=mime_type, resumable=True)
        f = service.files().create(body=meta, media_body=media, fields="id,webViewLink").execute()
        fid = f.get("id")
        try:
            service.permissions().create(fileId=fid, body={"type": "anyone", "role": "reader"}).execute()
        except Exception:
            pass
        return f.get("webViewLink") or (f"https://drive.google.com/file/d/{fid}/view" if fid else None)
    except Exception as exc:
        log.warning("Drive upload failed: %s", exc)
        return None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_or_create_user(telegram_id: int, name: str = "") -> dict:
    return db.upsert_user(telegram_id, name)


def _get_full_user(telegram_id: int) -> dict | None:
    return db.get_user(telegram_id)


def _schedule_daily_briefing(app, user: dict) -> None:
    """Schedule (or reschedule) the daily briefing job for a user."""
    import pytz
    job_id = f"briefing_{user['id']}"
    current_jobs = app.job_queue.get_jobs_by_name(job_id)
    for j in current_jobs:
        j.schedule_removal()

    time_str = user.get("briefing_time") or DEFAULT_BRIEFING
    tz_name  = user.get("timezone") or DEFAULT_TIMEZONE
    try:
        h, m = map(int, time_str.split(":"))
        tz   = pytz.timezone(tz_name)
        briefing_time = datetime.time(h, m, tzinfo=tz)
        app.job_queue.run_daily(
            _send_daily_briefing,
            time=briefing_time,
            name=job_id,
            data={"telegram_id": user["telegram_id"]},
        )
        log.info("Scheduled briefing for user %d at %s %s", user["id"], time_str, tz_name)
    except Exception as exc:
        log.warning("Could not schedule briefing for user %d: %s", user["id"], exc)


async def _send_daily_briefing(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Job callback that sends the daily briefing to a user."""
    telegram_id = context.job.data.get("telegram_id")
    if not telegram_id:
        return
    user = db.get_user(telegram_id)
    if not user or not user.get("onboarded"):
        return
    try:
        text = briefing_module.build_briefing(user)
        await context.bot.send_message(
            chat_id=telegram_id,
            text=text,
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as exc:
        log.error("Daily briefing send failed for %d: %s", telegram_id, exc)


async def _check_due_reminders(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Poll job that fires due reminders (runs every minute)."""
    due = db.get_all_due_reminders()
    for r in due:
        try:
            await context.bot.send_message(
                chat_id=r["telegram_id"],
                text=f"⏰ *Reminder*\n\n{r['content']}",
                parse_mode=ParseMode.MARKDOWN,
            )
            db.mark_reminder_sent(r["id"])
        except Exception as exc:
            log.error("Reminder send failed for reminder %d: %s", r["id"], exc)


# ── Task board keyboard ───────────────────────────────────────────────────────

def _task_action_keyboard(task_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎯 Today",     callback_data=f"task_move_{task_id}_today"),
            InlineKeyboardButton("📆 This Week", callback_data=f"task_move_{task_id}_this_week"),
        ],
        [
            InlineKeyboardButton("✅ Done",  callback_data=f"task_move_{task_id}_done"),
            InlineKeyboardButton("🗑 Delete", callback_data=f"task_delete_{task_id}"),
        ],
    ])


def _format_task(task: dict) -> str:
    status_icons = {"today": "🎯", "this_week": "📆", "queue": "📋", "done": "✅"}
    icon = status_icons.get(task.get("status", "queue"), "•")
    pri  = " 🔴" if task.get("priority") == "high" else ""
    dl   = f"\n  📅 Due: {task['deadline']}" if task.get("deadline") else ""
    ctx  = f"\n  💬 {task['context'][:80]}" if task.get("context") else ""
    return f"{icon} *[{task['id']}] {task['title']}*{pri}{dl}{ctx}"


# ═══════════════════════════════════════════════════════════════════════════════
# COMMAND HANDLERS
# ═══════════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg_user = update.effective_user
    user = _get_or_create_user(tg_user.id, tg_user.first_name or "")

    if user.get("onboarded"):
        await update.message.reply_text(
            f"👋 Welcome back, *{tg_user.first_name}!*\n\n"
            "Just send me anything — I'll save it, remind you, or schedule it.\n\n"
            "Try: `What's my day?` or just send any thought.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    await update.message.reply_text(
        f"👋 *Hi {tg_user.first_name}! Welcome to MemoraeBot.*\n\n"
        "I'm your personal memory layer — I'll remember everything you tell me, "
        "manage your tasks, sync with your Apple Calendar, and brief you every morning.\n\n"
        "*Let's get set up in 2 steps:*\n\n"
        "1️⃣ Connect Apple Calendar (optional but recommended):\n"
        "   Type: `/setapple your@icloud.com your-app-password`\n"
        "   _(Get app-specific password at appleid.apple.com)_\n\n"
        "2️⃣ Set your briefing time:\n"
        "   Type: `/setbriefing 07:00` _(default: 07:00 IST)_\n\n"
        "Or just skip setup and start sending me thoughts! Use /help anytime.",
        parse_mode=ParseMode.MARKDOWN,
    )
    db.update_user(tg_user.id, onboarded=1)
    _schedule_daily_briefing(context.application, db.get_user(tg_user.id))


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "*MemoraeBot — Commands & Usage*\n\n"
        "━━━ *Just send me anything:*\n"
        "  `Remember the milk` → saves a memory\n"
        "  `Add task: Review the report` → adds a task\n"
        "  `Schedule team meeting tomorrow 3pm` → creates calendar event\n"
        "  `Remind me to call Priya at 6pm` → sets a reminder\n"
        "  `What's my day?` → morning briefing\n"
        "  `Surprise me` → random old memory\n"
        "  Send a voice note, photo, or file → saved to memory/trunk\n\n"
        "━━━ *Commands:*\n"
        "  /briefing — get today's briefing now\n"
        "  /tasks — view your task board\n"
        "  /memories — browse recent memories\n"
        "  /calendar — upcoming calendar events\n"
        "  /reminders — pending reminders\n"
        "  /stats — your memory + task stats\n"
        "  /settings — view your settings\n"
        "  /setapple [email] [password] — link Apple Calendar\n"
        "  /setbriefing [HH:MM] — set daily briefing time\n"
        "  /settimezone [timezone] — e.g. Asia/Kolkata\n"
        "  /serendipity — resurface a random memory\n"
        "  /help — this help message\n\n"
        "━━━ *Editing tasks:*\n"
        "  Reply to any task message with `done`, `delete`, "
        "`move to today`, `move to this week`, or `change title to: [new title]`",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_briefing(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = _get_full_user(update.effective_user.id)
    if not user:
        await update.message.reply_text("Please /start first.")
        return
    await update.message.reply_text("⏳ Building your briefing...")
    text = briefing_module.build_briefing(user)
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def cmd_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = _get_full_user(update.effective_user.id)
    if not user:
        await update.message.reply_text("Please /start first.")
        return

    args = context.args
    status_filter = args[0].lower() if args else None

    tasks = db.get_tasks(user["id"], status=status_filter)
    if not tasks:
        await update.message.reply_text(
            "✅ No tasks found." +
            ("\n\nSend me something like `Add task: review the report` to add one!" if not status_filter else "")
        )
        return

    # Group by status
    groups = {"today": [], "this_week": [], "queue": [], "done": []}
    for t in tasks:
        groups.get(t["status"], groups["queue"]).append(t)

    lines = ["*📋 Your Task Board*\n"]
    for status, icon, label in [
        ("today",     "🎯", "Today"),
        ("this_week", "📆", "This Week"),
        ("queue",     "📋", "Queue"),
        ("done",      "✅", "Done"),
    ]:
        bucket = groups[status]
        if bucket:
            lines.append(f"*{icon} {label}*")
            for t in bucket[:8]:
                pri = " 🔴" if t.get("priority") == "high" else ""
                dl  = f" _{t['deadline']}_" if t.get("deadline") else ""
                lines.append(f"  [{t['id']}] {t['title']}{pri}{dl}")
            lines.append("")

    counts = db.get_task_counts(user["id"])
    lines.append(f"_Tap a task ID to manage it, or reply to any task message._")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_memories(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = _get_full_user(update.effective_user.id)
    if not user:
        await update.message.reply_text("Please /start first.")
        return

    args = context.args
    collection = " ".join(args) if args else None

    memories = db.get_memories(user["id"], collection=collection, limit=10)
    collections = db.get_collections(user["id"])

    if not memories:
        text = "🧠 No memories saved yet.\n\nJust send me any thought and I'll save it!"
        if collection:
            text = f"🧠 No memories in collection *{collection}* yet."
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
        return

    lines = [f"🧠 *Recent Memories*" + (f" — {collection}" if collection else ""), ""]
    for m in memories:
        content = m["content"]
        if len(content) > 120:
            content = content[:117] + "..."
        saved = m.get("created_at", "")[:10]
        coll  = m.get("collection", "General")
        lines.append(f"• [{m['id']}] _{content}_")
        lines.append(f"  {saved}  ·  {coll}")
        lines.append("")

    if collections and not collection:
        lines.append(f"📂 *Collections:* {', '.join(collections)}")
        lines.append("Use `/memories [collection]` to filter.")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_calendar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = _get_full_user(update.effective_user.id)
    if not user:
        await update.message.reply_text("Please /start first.")
        return

    cal_client = ac.build_client(user)
    if not cal_client:
        await update.message.reply_text(
            "📅 Apple Calendar not connected.\n\n"
            "Use: `/setapple your@icloud.com your-app-password`\n"
            "_(Get an app-specific password at appleid.apple.com → Sign-In & Security)_",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    await update.message.reply_text("📅 Fetching your calendar...")
    try:
        events = cal_client.get_upcoming_events(days=7)
        if not events:
            await update.message.reply_text("📅 No upcoming events in the next 7 days.")
            return
        lines = [f"📅 *Next 7 Days — {len(events)} event(s)*\n"]
        for evt in events[:10]:
            lines.append(cal_client.format_event_for_telegram(evt))
            lines.append("")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)
    except Exception as exc:
        await update.message.reply_text(f"❌ Calendar error: {exc}")


async def cmd_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = _get_full_user(update.effective_user.id)
    if not user:
        await update.message.reply_text("Please /start first.")
        return

    reminders = db.get_pending_reminders(user["id"])
    if not reminders:
        await update.message.reply_text(
            "⏰ No pending reminders.\n\nSend: `Remind me to call Priya at 6pm`"
        )
        return

    lines = [f"⏰ *Pending Reminders ({len(reminders)})*\n"]
    for r in reminders[:10]:
        lines.append(f"• [{r['id']}] *{r['remind_at']}* UTC")
        lines.append(f"  {r['content']}")
        lines.append("")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = _get_full_user(update.effective_user.id)
    if not user:
        await update.message.reply_text("Please /start first.")
        return

    uid         = user["id"]
    mem_count   = db.get_memory_count(uid)
    task_counts = db.get_task_counts(uid)
    reminders   = db.get_pending_reminders(uid)
    collections = db.get_collections(uid)

    lines = [
        "📊 *Your Memorae Stats*\n",
        f"🧠 *Memories:* {mem_count} total",
        f"📂 *Collections:* {len(collections)} — {', '.join(collections[:5]) or 'None'}",
        "",
        "✅ *Task Board:*",
        f"  🎯 Today: {task_counts.get('today', 0)}",
        f"  📆 This Week: {task_counts.get('this_week', 0)}",
        f"  📋 Queue: {task_counts.get('queue', 0)}",
        f"  ✅ Done: {task_counts.get('done', 0)}",
        "",
        f"⏰ *Pending Reminders:* {len(reminders)}",
        "",
        f"🕐 *Briefing:* {user.get('briefing_time', DEFAULT_BRIEFING)} {user.get('timezone', DEFAULT_TIMEZONE)}",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = _get_full_user(update.effective_user.id)
    if not user:
        await update.message.reply_text("Please /start first.")
        return

    apple_connected = bool(user.get("apple_id") and user.get("apple_password"))
    drive_configured = bool(os.getenv("GOOGLE_JSON"))

    lines = [
        "⚙️ *Your Settings*\n",
        f"👤 *Name:* {user.get('name') or 'Not set'}",
        f"🕐 *Timezone:* {user.get('timezone') or DEFAULT_TIMEZONE}",
        f"🌅 *Daily Briefing:* {user.get('briefing_time') or DEFAULT_BRIEFING}",
        f"📅 *Apple Calendar:* {'✅ Connected' if apple_connected else '❌ Not connected'}",
        f"☁️ *Google Drive (Trunk):* {'✅ Configured' if drive_configured else '❌ Not configured'}",
        f"✨ *Serendipity:* {'✅ On' if user.get('serendipity_on', 1) else '❌ Off'}",
        "",
        "━━━ *Change settings:*",
        "`/setapple [email] [app-password]`",
        "`/setbriefing [HH:MM]`",
        "`/settimezone [timezone]`",
        "`/serendipity_toggle` — on/off",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_set_apple(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "Usage: `/setapple your@icloud.com xxxx-xxxx-xxxx-xxxx`\n\n"
            "Generate an app-specific password at:\n"
            "appleid.apple.com → Sign-In & Security → App-Specific Passwords",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    apple_id  = args[0].strip()
    apple_pwd = args[1].strip()

    await update.message.reply_text("🔌 Testing Apple Calendar connection...")

    client = ac.AppleCalendarClient(apple_id, apple_pwd)
    ok, msg = client.test_connection()

    if ok:
        db.update_user(update.effective_user.id, apple_id=apple_id, apple_password=apple_pwd)
        await update.message.reply_text(
            f"✅ *Apple Calendar connected!*\n\n{msg}\n\n"
            "Your events will appear in /briefing and /calendar.",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await update.message.reply_text(
            f"❌ *Connection failed.*\n\n`{msg}`\n\n"
            "Make sure you're using an *App-Specific Password* (not your Apple ID password).\n"
            "Generate one at appleid.apple.com → Sign-In & Security → App-Specific Passwords",
            parse_mode=ParseMode.MARKDOWN,
        )


async def cmd_set_briefing(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args:
        await update.message.reply_text("Usage: `/setbriefing HH:MM` e.g. `/setbriefing 07:30`", parse_mode=ParseMode.MARKDOWN)
        return

    time_str = args[0].strip()
    try:
        h, m = map(int, time_str.split(":"))
        assert 0 <= h <= 23 and 0 <= m <= 59
    except Exception:
        await update.message.reply_text("❌ Invalid time. Use HH:MM format, e.g. `07:30`", parse_mode=ParseMode.MARKDOWN)
        return

    db.update_user(update.effective_user.id, briefing_time=time_str)
    user = db.get_user(update.effective_user.id)
    _schedule_daily_briefing(context.application, user)
    await update.message.reply_text(
        f"✅ Daily briefing set to *{time_str}* {user.get('timezone', DEFAULT_TIMEZONE)}.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_set_timezone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args:
        await update.message.reply_text(
            "Usage: `/settimezone Asia/Kolkata`\nCommon: `Asia/Kolkata`, `UTC`, `America/New_York`, `Europe/London`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    tz_name = args[0].strip()
    try:
        import pytz
        pytz.timezone(tz_name)
    except Exception:
        await update.message.reply_text(f"❌ Unknown timezone: `{tz_name}`", parse_mode=ParseMode.MARKDOWN)
        return

    db.update_user(update.effective_user.id, timezone=tz_name)
    user = db.get_user(update.effective_user.id)
    _schedule_daily_briefing(context.application, user)
    await update.message.reply_text(f"✅ Timezone set to *{tz_name}*.", parse_mode=ParseMode.MARKDOWN)


async def cmd_serendipity(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = _get_full_user(update.effective_user.id)
    if not user:
        await update.message.reply_text("Please /start first.")
        return
    text = briefing_module.build_serendipity_message(user)
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def cmd_serendipity_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = _get_full_user(update.effective_user.id)
    if not user:
        return
    new_val = 0 if user.get("serendipity_on", 1) else 1
    db.update_user(update.effective_user.id, serendipity_on=new_val)
    state = "ON ✅" if new_val else "OFF ❌"
    await update.message.reply_text(f"✨ Serendipity in morning briefing is now *{state}*.", parse_mode=ParseMode.MARKDOWN)


# ═══════════════════════════════════════════════════════════════════════════════
# MESSAGE HANDLERS — Core intent dispatch
# ═══════════════════════════════════════════════════════════════════════════════

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Main text message handler — classifies intent and dispatches."""
    user_text = (update.message.text or "").strip()
    if not user_text:
        return

    tg_user = update.effective_user
    user    = _get_or_create_user(tg_user.id, tg_user.first_name or "")

    # Handle reply-to-message (task editing)
    if update.message.reply_to_message:
        replied_text = (
            update.message.reply_to_message.text
            or update.message.reply_to_message.caption
            or ""
        )
        # Check if replied message references a task
        task_id = _extract_task_id_from_message(replied_text)
        if task_id:
            await _handle_task_reply(update, context, user, task_id, user_text)
            return
        # Check if replied message references a memory
        mem_id = _extract_memory_id_from_message(replied_text)
        if mem_id:
            await _handle_memory_reply(update, context, user, mem_id, user_text)
            return

    await update.message.reply_text("🔍 Processing...")

    try:
        intent_data = ir.classify(user_text, user.get("timezone") or DEFAULT_TIMEZONE)
        await _dispatch_intent(update, context, user, intent_data, raw_text=user_text)
    except Exception as exc:
        log.error("Text handler error: %s", exc)
        await update.message.reply_text(f"❌ Something went wrong: {exc}")


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Voice note handler — transcribe + classify via Gemini."""
    tg_user = update.effective_user
    user    = _get_or_create_user(tg_user.id, tg_user.first_name or "")

    await update.message.reply_text("🎧 Processing your voice note...")

    file_path = f"/tmp/memorae_voice_{tg_user.id}_{int(datetime.datetime.now().timestamp())}.ogg"
    try:
        voice_file = await update.message.voice.get_file()
        await voice_file.download_to_drive(file_path)

        caption = update.message.caption or ""
        intent_data = ir.classify_voice(file_path, caption=caption)
        await _dispatch_intent(update, context, user, intent_data, raw_text=caption, source="voice")
    except Exception as exc:
        log.error("Voice handler error: %s", exc)
        await update.message.reply_text(f"❌ Voice processing error: {exc}")
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Photo/image handler — save to memory + optional Drive upload."""
    tg_user = update.effective_user
    user    = _get_or_create_user(tg_user.id, tg_user.first_name or "")
    caption = update.message.caption or ""

    await update.message.reply_text("🖼 Processing image...")

    file_path = f"/tmp/memorae_photo_{tg_user.id}_{int(datetime.datetime.now().timestamp())}.jpg"
    try:
        photo = update.message.photo[-1]
        f     = await photo.get_file()
        await f.download_to_drive(file_path)

        # Try to classify via Gemini vision
        intent_data = ir.classify_image(file_path, caption=caption)
        intent      = intent_data.get("intent", "SAVE_FILE")

        drive_url = None
        if os.getenv("GOOGLE_JSON"):
            fname     = f"MemoraeBot_{tg_user.id}_{int(datetime.datetime.now().timestamp())}.jpg"
            drive_url = _upload_to_drive(file_path, fname, "image/jpeg")

        if intent == "ADD_MEMORY" or intent == "SAVE_FILE":
            content = caption or intent_data.get("content") or "Image saved"
            if drive_url:
                content += f"\n🔗 [View image]({drive_url})"
            mem = db.add_memory(user["id"], content, collection="Images", source="image")
            reply = f"🖼 *Image saved to memory*\n\n_{content[:100]}_\nMemory ID: {mem['id']}"
            if drive_url:
                reply += f"\n☁️ [Saved to Drive]({drive_url})"
            await update.message.reply_text(reply, parse_mode=ParseMode.MARKDOWN)
        else:
            await _dispatch_intent(update, context, user, intent_data, raw_text=caption, source="image")

    except Exception as exc:
        log.error("Photo handler error: %s", exc)
        await update.message.reply_text(f"❌ Image processing error: {exc}")
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Document/file handler — save to Trunk (Google Drive) + memory."""
    tg_user = update.effective_user
    user    = _get_or_create_user(tg_user.id, tg_user.first_name or "")
    caption = update.message.caption or ""
    doc     = update.message.document

    await update.message.reply_text(f"📄 Processing document: *{doc.file_name}*...", parse_mode=ParseMode.MARKDOWN)

    file_ext  = os.path.splitext(doc.file_name or "")[-1] or ".bin"
    mime_type = doc.mime_type or "application/octet-stream"
    file_path = f"/tmp/memorae_doc_{tg_user.id}_{int(datetime.datetime.now().timestamp())}{file_ext}"

    try:
        f = await doc.get_file()
        await f.download_to_drive(file_path)

        drive_url = None
        if os.getenv("GOOGLE_JSON"):
            drive_url = _upload_to_drive(file_path, doc.file_name or "file", mime_type)

        content = caption or doc.file_name or "Document"
        if drive_url:
            content += f"\n🔗 [Open file]({drive_url})"

        mem = db.add_memory(user["id"], content, collection="Trunk", source="file")

        reply = f"📦 *Saved to Trunk*\n\n📄 {doc.file_name}\n_{caption}_"
        if drive_url:
            reply += f"\n☁️ [Open in Drive]({drive_url})"
        reply += f"\nMemory ID: {mem['id']}"
        await update.message.reply_text(reply, parse_mode=ParseMode.MARKDOWN)

    except Exception as exc:
        log.error("Document handler error: %s", exc)
        await update.message.reply_text(f"❌ File processing error: {exc}")
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)


# ═══════════════════════════════════════════════════════════════════════════════
# INTENT DISPATCHER
# ═══════════════════════════════════════════════════════════════════════════════

async def _dispatch_intent(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user: dict,
    intent_data: dict,
    raw_text: str = "",
    source: str = "text",
) -> None:
    intent = intent_data.get("intent", "ADD_MEMORY")

    if intent == "ADD_MEMORY":
        await _handle_add_memory(update, user, intent_data, source)

    elif intent == "ADD_TASK":
        await _handle_add_task(update, context, user, intent_data)

    elif intent == "CREATE_EVENT":
        await _handle_create_event(update, user, intent_data)

    elif intent == "SET_REMINDER":
        await _handle_set_reminder(update, context, user, intent_data, raw_text)

    elif intent == "QUERY":
        await _handle_query(update, user, intent_data)

    elif intent == "GET_BRIEFING":
        text = briefing_module.build_briefing(user)
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

    elif intent == "SERENDIPITY":
        text = briefing_module.build_serendipity_message(user)
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

    elif intent == "SAVE_FILE":
        content = intent_data.get("caption") or raw_text or "File saved"
        mem = db.add_memory(user["id"], content, collection="Trunk", source=source)
        await update.message.reply_text(
            f"📦 Saved to Trunk!\n\n_{content[:100]}_\nMemory ID: {mem['id']}",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        # Fallback: save as memory
        content = raw_text or intent_data.get("content", "")
        if content:
            mem = db.add_memory(user["id"], content, source=source)
            await update.message.reply_text(
                f"🧠 Saved to memory!\n\n_{content[:120]}_\nID: {mem['id']}",
                parse_mode=ParseMode.MARKDOWN,
            )


# ── Intent handlers ───────────────────────────────────────────────────────────

async def _handle_add_memory(update: Update, user: dict, intent_data: dict, source: str) -> None:
    content    = (intent_data.get("content") or "").strip()
    collection = (intent_data.get("collection") or "General").strip()
    tags       = intent_data.get("tags") or []

    if not content:
        await update.message.reply_text("❓ I couldn't figure out what to save. Try again with more detail.")
        return

    mem = db.add_memory(user["id"], content, tags=tags, collection=collection, source=source)
    mem_count = db.get_memory_count(user["id"])

    tag_str = f"\n🏷 Tags: {', '.join(tags)}" if tags else ""
    await update.message.reply_text(
        f"🧠 *Memory saved!*\n\n"
        f"_{content[:200]}_\n\n"
        f"📂 Collection: {collection}{tag_str}\n"
        f"ID: {mem['id']}  ·  You now have {mem_count} memories.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def _handle_add_task(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user: dict,
    intent_data: dict,
) -> None:
    title    = (intent_data.get("title")       or "").strip()
    desc     = (intent_data.get("description") or "").strip()
    status   = (intent_data.get("status")      or "queue").lower()
    priority = (intent_data.get("priority")    or "normal").lower()
    deadline = intent_data.get("deadline")

    if status not in ("queue", "this_week", "today", "done"):
        status = "queue"
    if priority not in ("normal", "high"):
        priority = "normal"

    if not title:
        await update.message.reply_text("❓ I couldn't catch the task title. Try: `Add task: call Priya`")
        return

    task = db.add_task(
        user_id=user["id"],
        title=title,
        description=desc,
        status=status,
        priority=priority,
        deadline=deadline,
    )

    status_icons = {"today": "🎯", "this_week": "📆", "queue": "📋"}
    icon  = status_icons.get(status, "📋")
    pri   = " 🔴 *HIGH PRIORITY*" if priority == "high" else ""
    dl    = f"\n📅 Deadline: {deadline}" if deadline else ""

    reply = (
        f"✅ *Task added!*{pri}\n\n"
        f"{icon} *{title}*\n"
        f"Status: {status.replace('_', ' ').title()}{dl}\n"
        f"Task ID: {task['id']}"
    )
    await update.message.reply_text(
        reply,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_task_action_keyboard(task["id"]),
    )


async def _handle_create_event(update: Update, user: dict, intent_data: dict) -> None:
    title      = (intent_data.get("title")          or "").strip()
    raw_start  = intent_data.get("start_datetime")
    raw_end    = intent_data.get("end_datetime")
    description = intent_data.get("description") or ""

    if not title:
        await update.message.reply_text("❓ I couldn't figure out the event title. Try: `Schedule team meeting tomorrow 3pm`")
        return

    cal_client = ac.build_client(user)
    if not cal_client:
        # Save as memory with a calendar tag
        content = f"📅 Event (not synced): {title}"
        if raw_start:
            content += f" at {raw_start}"
        mem = db.add_memory(user["id"], content, collection="Calendar", tags=["event"])
        await update.message.reply_text(
            f"📅 *Event noted* (Apple Calendar not connected)\n\n"
            f"*{title}*\n"
            f"{'Start: ' + raw_start if raw_start else ''}\n\n"
            f"Saved as memory #{mem['id']}. Connect Apple Calendar with /setapple to sync events.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    tz = user.get("timezone") or DEFAULT_TIMEZONE
    start_dt, end_dt = ir.smart_parse_event_times(raw_start, raw_end, tz)

    if not start_dt:
        await update.message.reply_text(
            f"❓ I understood the event *{title}* but couldn't parse the time. "
            "Please be more specific, e.g. `tomorrow 3pm` or `June 5th 2pm`.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    ok, err = cal_client.create_event(title, start_dt, end_dt, description=description)
    if ok:
        import pytz
        tz_obj = pytz.timezone(tz)
        start_local = start_dt if start_dt.tzinfo else tz_obj.localize(start_dt)
        time_str = start_local.strftime("%-d %b %Y, %-I:%M %p")
        await update.message.reply_text(
            f"📅 *Event created in Apple Calendar!*\n\n"
            f"*{title}*\n"
            f"🕐 {time_str} ({tz})\n"
            + (f"📝 {description}" if description else ""),
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await update.message.reply_text(
            f"❌ Failed to create event in Apple Calendar.\n`{err[:120]}`\n\nCheck credentials with /settings.",
            parse_mode=ParseMode.MARKDOWN,
        )


async def _handle_set_reminder(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user: dict,
    intent_data: dict,
    raw_text: str,
) -> None:
    content  = (intent_data.get("content") or raw_text).strip()
    raw_time = intent_data.get("remind_at")
    tz_name  = user.get("timezone") or DEFAULT_TIMEZONE

    import pytz
    tz_obj = pytz.timezone(tz_name)

    # ── Parse time → always store as UTC ─────────────────────────────────────
    remind_utc = None

    if raw_time:
        # Gemini returns time in the USER'S local timezone (e.g. "2026-05-16 10:00" = 10am IST)
        # We must localise it then convert to UTC before storing.
        try:
            dt_naive  = datetime.datetime.strptime(raw_time, "%Y-%m-%d %H:%M")
            dt_local  = tz_obj.localize(dt_naive)          # treat as local time
            dt_utc    = dt_local.astimezone(pytz.utc)
            remind_utc = dt_utc.strftime("%Y-%m-%d %H:%M")
        except Exception:
            remind_utc = None

    if not remind_utc:
        # Fallback: dateparser with explicit timezone conversion
        remind_utc = ir.parse_remind_time(raw_text, tz_name)

    if not remind_utc:
        await update.message.reply_text(
            "⏰ I understood you want a reminder but couldn't parse the time.\n"
            "Try: `Remind me to call Priya at 6pm` or `Remind me tomorrow at 9am`"
        )
        return

    rem = db.add_reminder(user["id"], content, remind_utc)

    # ── Display time back in user's local timezone ────────────────────────────
    try:
        dt_utc_dt  = datetime.datetime.strptime(remind_utc, "%Y-%m-%d %H:%M").replace(tzinfo=pytz.utc)
        dt_local   = dt_utc_dt.astimezone(tz_obj)
        time_display = dt_local.strftime("%-d %b %Y at %-I:%M %p")
    except Exception:
        time_display = remind_utc

    # ── Also create event in Apple Calendar ──────────────────────────────────
    cal_note = ""
    cal_client = ac.build_client(user)
    if cal_client:
        try:
            dt_utc_dt   = datetime.datetime.strptime(remind_utc, "%Y-%m-%d %H:%M").replace(tzinfo=pytz.utc)
            start_local = dt_utc_dt.astimezone(tz_obj).replace(tzinfo=None)  # naive local
            end_local   = start_local + datetime.timedelta(minutes=30)
            ok, err = cal_client.create_event(
                title=f"⏰ {content[:60]}",
                start=start_local,
                end=end_local,
                description=f"MemoraeBot reminder: {content}",
            )
            if ok:
                cal_note = "\n📅 Added to Apple Calendar ✅"
            else:
                cal_note = f"\n⚠️ Calendar sync failed: {err[:80]}"
                log.warning("Calendar reminder event failed: %s", err)
        except Exception as exc:
            cal_note = f"\n⚠️ Calendar sync error: {str(exc)[:80]}"
            log.warning("Calendar event for reminder failed: %s", exc)

    await update.message.reply_text(
        f"⏰ *Reminder set!*\n\n"
        f"_{content}_\n\n"
        f"🕐 {time_display} ({tz_name}){cal_note}\n"
        f"Reminder ID: {rem['id']}",
        parse_mode=ParseMode.MARKDOWN,
    )


async def _handle_query(update: Update, user: dict, intent_data: dict) -> None:
    text = briefing_module.build_query_response(user, intent_data)
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


# ── Reply-to-message handlers (Phase 4: editing) ─────────────────────────────

def _extract_task_id_from_message(text: str) -> int | None:
    m = re.search(r"Task ID[:\s]+(\d+)", text, re.IGNORECASE)
    if m:
        return int(m.group(1))
    m = re.search(r"\[(\d+)\]", text)
    if m:
        return int(m.group(1))
    return None


def _extract_memory_id_from_message(text: str) -> int | None:
    m = re.search(r"Memory ID[:\s]+(\d+)", text, re.IGNORECASE)
    if m:
        return int(m.group(1))
    m = re.search(r"ID[:\s]+(\d+)", text, re.IGNORECASE)
    if m:
        return int(m.group(1))
    return None


async def _handle_task_reply(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user: dict,
    task_id: int,
    user_text: str,
) -> None:
    lower = user_text.lower().strip()
    uid   = user["id"]
    task  = db.get_task_by_id(task_id, uid)
    if not task:
        await update.message.reply_text(f"❓ Task {task_id} not found.")
        return

    if any(kw in lower for kw in ["delete", "remove", "cancel"]):
        db.delete_task(task_id, uid)
        await update.message.reply_text(f"🗑 Task [{task_id}] *{task['title']}* deleted.", parse_mode=ParseMode.MARKDOWN)
        return

    if any(kw in lower for kw in ["done", "complete", "finished", "mark done"]):
        db.update_task(task_id, uid, status="done")
        await update.message.reply_text(f"✅ Task [{task_id}] *{task['title']}* marked as done!", parse_mode=ParseMode.MARKDOWN)
        return

    if "move to today" in lower or "today" in lower:
        db.update_task(task_id, uid, status="today")
        await update.message.reply_text(f"🎯 Task [{task_id}] moved to *Today*.", parse_mode=ParseMode.MARKDOWN)
        return

    if "move to this week" in lower or "this week" in lower:
        db.update_task(task_id, uid, status="this_week")
        await update.message.reply_text(f"📆 Task [{task_id}] moved to *This Week*.", parse_mode=ParseMode.MARKDOWN)
        return

    if "move to queue" in lower or "queue" in lower:
        db.update_task(task_id, uid, status="queue")
        await update.message.reply_text(f"📋 Task [{task_id}] moved back to *Queue*.", parse_mode=ParseMode.MARKDOWN)
        return

    # Title rename
    rename_match = re.search(r"(?:change|rename|title)[:\s]+to[:\s]+(.+)$", lower, re.IGNORECASE)
    if rename_match or re.search(r"^change to[:\s]+", lower):
        new_title = rename_match.group(1).strip() if rename_match else re.sub(r"^change to[:\s]+", "", user_text, flags=re.IGNORECASE).strip()
        db.update_task(task_id, uid, title=new_title)
        await update.message.reply_text(f"✏️ Task [{task_id}] renamed to *{new_title}*.", parse_mode=ParseMode.MARKDOWN)
        return

    # Deadline change
    deadline_match = re.search(r"(?:deadline|due)[:\s]+(.+)$", lower)
    if deadline_match:
        new_dl = ir.parse_remind_time(deadline_match.group(1), user.get("timezone") or DEFAULT_TIMEZONE)
        if new_dl:
            new_dl = new_dl[:10]  # Date only
            db.update_task(task_id, uid, deadline=new_dl)
            await update.message.reply_text(f"📅 Deadline for task [{task_id}] updated to {new_dl}.", parse_mode=ParseMode.MARKDOWN)
            return

    # Priority change
    if "high priority" in lower or "urgent" in lower:
        db.update_task(task_id, uid, priority="high")
        await update.message.reply_text(f"🔴 Task [{task_id}] marked as *High Priority*.", parse_mode=ParseMode.MARKDOWN)
        return

    await update.message.reply_text(
        f"❓ What would you like to do with task [{task_id}] *{task['title']}*?\n\n"
        "Reply with: `done`, `delete`, `move to today`, `move to this week`, `move to queue`, `rename to: [new title]`, or `deadline: [date]`",
        parse_mode=ParseMode.MARKDOWN,
    )


async def _handle_memory_reply(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user: dict,
    mem_id: int,
    user_text: str,
) -> None:
    lower = user_text.lower().strip()
    uid   = user["id"]

    if any(kw in lower for kw in ["delete", "remove", "forget"]):
        ok = db.delete_memory(mem_id, uid)
        if ok:
            await update.message.reply_text(f"🗑 Memory #{mem_id} deleted.")
        else:
            await update.message.reply_text(f"❓ Memory #{mem_id} not found.")
        return

    await update.message.reply_text("❓ Reply with `delete` or `forget` to remove a memory.")


# ── Inline keyboard callback handler ─────────────────────────────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    data    = query.data or ""
    tg_user = update.effective_user
    user    = _get_full_user(tg_user.id)
    if not user:
        return

    # task_move_{task_id}_{status}
    move_match = re.match(r"task_move_(\d+)_(\w+)", data)
    if move_match:
        task_id = int(move_match.group(1))
        new_status = move_match.group(2)
        task = db.get_task_by_id(task_id, user["id"])
        if task:
            db.update_task(task_id, user["id"], status=new_status)
            icons = {"today": "🎯", "this_week": "📆", "queue": "📋", "done": "✅"}
            label = new_status.replace("_", " ").title()
            await query.edit_message_text(
                f"{icons.get(new_status, '•')} *[{task_id}] {task['title']}* → *{label}*",
                parse_mode=ParseMode.MARKDOWN,
            )
        return

    # task_delete_{task_id}
    del_match = re.match(r"task_delete_(\d+)", data)
    if del_match:
        task_id = int(del_match.group(1))
        task = db.get_task_by_id(task_id, user["id"])
        if task:
            db.delete_task(task_id, user["id"])
            await query.edit_message_text(f"🗑 Task [{task_id}] *{task['title']}* deleted.", parse_mode=ParseMode.MARKDOWN)
        return


# ═══════════════════════════════════════════════════════════════════════════════
# BOT STARTUP
# ═══════════════════════════════════════════════════════════════════════════════

async def post_init(app) -> None:
    """Called after the bot is initialised."""
    db.init_db()

    # Schedule briefings for all onboarded users
    all_users = db.get_all_users()
    for u in all_users:
        _schedule_daily_briefing(app, u)
    log.info("Scheduled briefings for %d users.", len(all_users))

    # Set bot commands menu
    await app.bot.set_my_commands([
        BotCommand("start",              "Start / onboarding"),
        BotCommand("briefing",           "Get today's briefing now"),
        BotCommand("tasks",              "View task board"),
        BotCommand("memories",           "Browse memories"),
        BotCommand("calendar",           "Upcoming Apple Calendar events"),
        BotCommand("reminders",          "Pending reminders"),
        BotCommand("serendipity",        "Random memory"),
        BotCommand("stats",              "Your memory + task stats"),
        BotCommand("settings",           "View settings"),
        BotCommand("setapple",           "Link Apple Calendar"),
        BotCommand("setbriefing",        "Set briefing time"),
        BotCommand("settimezone",        "Set timezone"),
        BotCommand("help",               "Help & all commands"),
    ])
    log.info("Bot commands set.")


def main() -> None:
    if not TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN not set in .env")

    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .post_init(post_init)
        .build()
    )

    # Commands
    app.add_handler(CommandHandler("start",              cmd_start))
    app.add_handler(CommandHandler("help",               cmd_help))
    app.add_handler(CommandHandler("briefing",           cmd_briefing))
    app.add_handler(CommandHandler("tasks",              cmd_tasks))
    app.add_handler(CommandHandler("memories",           cmd_memories))
    app.add_handler(CommandHandler("calendar",           cmd_calendar))
    app.add_handler(CommandHandler("reminders",          cmd_reminders))
    app.add_handler(CommandHandler("stats",              cmd_stats))
    app.add_handler(CommandHandler("settings",           cmd_settings))
    app.add_handler(CommandHandler("setapple",           cmd_set_apple))
    app.add_handler(CommandHandler("setbriefing",        cmd_set_briefing))
    app.add_handler(CommandHandler("settimezone",        cmd_set_timezone))
    app.add_handler(CommandHandler("serendipity",        cmd_serendipity))
    app.add_handler(CommandHandler("serendipity_toggle", cmd_serendipity_toggle))

    # Messages
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(
        filters.Document.ALL & ~filters.Document.AUDIO, handle_document
    ))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND, handle_text
    ))

    # Inline keyboard callbacks
    app.add_handler(CallbackQueryHandler(handle_callback))

    # Polling job — check due reminders every 60 seconds
    app.job_queue.run_repeating(
        _check_due_reminders,
        interval=60,
        first=10,
        name="reminder_poller",
    )

    log.info("✅ MemoraeBot started.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
