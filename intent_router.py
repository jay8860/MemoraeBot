"""
intent_router.py — Gemini-powered 8-intent classifier for MemoraeBot

Intents:
  ADD_MEMORY    — save a thought, note, link, observation
  ADD_TASK      — add a to-do item to the task board
  CREATE_EVENT  — schedule a calendar event
  SET_REMINDER  — set a timed reminder
  QUERY         — search/show memories, tasks, calendar, reminders
  GET_BRIEFING  — get today's morning/daily summary
  SERENDIPITY   — randomly resurface an old memory
  SAVE_FILE     — triggered separately when media is attached (fallback)
"""

import os
import re
import json
import logging
from datetime import datetime, date, timedelta

from google import genai
from google.genai import types

log = logging.getLogger(__name__)

# ── Gemini setup ──────────────────────────────────────────────────────────────
_MODEL_NAME = (os.getenv("GEMINI_MODEL") or "gemini-2.5-flash").strip()

def _get_client() -> genai.Client:
    return genai.Client(api_key=os.getenv("GEMINI_API_KEY", ""))


# ── Core classifier ───────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are the intent classifier for MemoraeBot — a personal memory and life-organisation assistant.
Today's date is {today}. Classify the user's message into EXACTLY ONE of these intents:

  ADD_MEMORY         — user wants to save/remember something (thought, idea, note, link, quote)
  ADD_TASK           — user wants to add a to-do item, action item, or task
  CREATE_EVENT       — user wants to schedule a calendar event / meeting / appointment
  SET_REMINDER       — user wants to be reminded at a specific time
  DELETE_REMINDER    — user wants to delete, cancel, or clear one or more reminders
  QUERY              — user wants to search, list, or view existing data
  GET_BRIEFING       — user wants their daily summary / briefing / what's on today
  SERENDIPITY        — user wants a random old memory surfaced
  MANAGE_COLLECTION  — user wants to create, rename, or list collections/categories
  SAVE_FILE          — fallback for media messages without clear intent

Rules:
- "remind me", "alert me", "don't forget" + time → SET_REMINDER
- "schedule", "meeting", "event", "appointment" + time/date → CREATE_EVENT
- "briefing", "my day", "what's on today", "morning summary" → GET_BRIEFING
- "remember", "note", "save", "I learned", "interesting", "add to memory" → ADD_MEMORY
- "add task", "to-do", "action item", "need to do" → ADD_TASK
- "surprise me", "random memory", "serendipity" → SERENDIPITY
- "create collection", "new collection", "make a collection called", "list my collections", "what collections" → MANAGE_COLLECTION
- "delete reminder", "cancel reminder", "remove reminder", "clear reminders", "delete all reminders" → DELETE_REMINDER
- "show", "list", "search", "find", "what did I save", "my tasks", "my reminders" → QUERY

━━━ SMART COLLECTION TAXONOMY for ADD_MEMORY ━━━
Auto-assign the BEST matching collection from this list based on content:
  Travel    — trips, hotels, flights, destinations, restaurants abroad, vacation plans, places to visit
  Work      — official duties, government schemes, targets, projects, meetings, reports, admin work, field visits
  Ideas     — innovations, suggestions, improvements, creative thoughts, things to try, experiments
  Learning  — articles, books, statistics, research findings, insights, things you read/learned
  People    — contacts, someone you met, relationship notes, person's details, references
  Health    — fitness, medical, diet, exercise, wellness, symptoms, doctors
  Finance   — money, budget, expenses, salary, savings, costs, investments
  Personal  — personal thoughts, diary, reflections, emotions, non-work life
  General   — anything that doesn't clearly fit above

Also auto-generate 1-3 relevant tags.

Extract fields based on intent and return ONLY valid JSON — no prose, no markdown:

For ADD_MEMORY:
{{"intent":"ADD_MEMORY","content":"<exact text to save>","collection":"<Travel|Work|Ideas|Learning|People|Health|Finance|Personal|General>","tags":["<tag1>","<tag2>"]}}

For ADD_TASK:
{{"intent":"ADD_TASK","title":"<task title>","description":"<optional detail>","status":"<queue|this_week|today>","priority":"<normal|high>","deadline":"<YYYY-MM-DD or null>"}}

For CREATE_EVENT:
{{"intent":"CREATE_EVENT","title":"<event title>","start_datetime":"<YYYY-MM-DD HH:MM>","end_datetime":"<YYYY-MM-DD HH:MM or null>","description":"<optional>"}}

For SET_REMINDER:
{{"intent":"SET_REMINDER","content":"<what to remind>","remind_at":"<YYYY-MM-DD HH:MM>","is_relative":false}}

For DELETE_REMINDER:
{{"intent":"DELETE_REMINDER","filter":"<all|today|tomorrow|date>","target_date":"<YYYY-MM-DD or null>"}}

For QUERY:
{{"intent":"QUERY","target":"<memories|tasks|calendar|reminders|all>","query":"<search text or null>","filter":"<optional: today|this_week|done|collection_name>"}}

For MANAGE_COLLECTION:
{{"intent":"MANAGE_COLLECTION","action":"<create|list|rename>","name":"<collection name if creating/renaming>","new_name":"<new name if renaming>"}}

For GET_BRIEFING:
{{"intent":"GET_BRIEFING"}}

For SERENDIPITY:
{{"intent":"SERENDIPITY"}}

For SAVE_FILE:
{{"intent":"SAVE_FILE","caption":"<caption text if any>"}}
"""


def classify(user_text: str, user_timezone: str = "Asia/Kolkata") -> dict:
    """
    Classify user_text into one of the 8 intents.
    Returns a dict with 'intent' key and extracted fields.
    On failure returns {"intent": "ADD_MEMORY", "content": user_text}
    """
    today = date.today().strftime("%Y-%m-%d (%A)")
    prompt = _SYSTEM_PROMPT.format(today=today)

    try:
        client = _get_client()
        result = client.models.generate_content(
            model=_MODEL_NAME,
            contents=f"{prompt}\n\nUser message: \"{user_text}\""
        )
        raw = (result.text or "").strip()

        # Strip markdown code fences if present
        if raw.startswith("```json"):
            raw = raw[7:]
        if raw.startswith("```"):
            raw = raw[3:]
        if raw.endswith("```"):
            raw = raw[:-3]
        raw = raw.strip()

        parsed = json.loads(raw)
        intent = parsed.get("intent", "ADD_MEMORY")
        log.info("Intent classified: %s for text: %.60s", intent, user_text)
        return parsed

    except json.JSONDecodeError as exc:
        log.warning("JSON parse failed for intent classification: %s | raw=%s", exc, raw[:200] if 'raw' in dir() else "")
        return _fallback_classify(user_text)
    except Exception as exc:
        log.error("Intent classification error: %s", exc)
        return _fallback_classify(user_text)


def _fallback_classify(text: str) -> dict:
    """Rule-based fallback when Gemini is unavailable."""
    lower = text.lower().strip()

    if any(kw in lower for kw in ["delete reminder", "cancel reminder", "remove reminder",
                                    "clear reminder", "delete all reminder"]):
        filt = "tomorrow" if "tomorrow" in lower else ("today" if "today" in lower else "all")
        return {"intent": "DELETE_REMINDER", "filter": filt, "target_date": None}

    if any(kw in lower for kw in ["remind me", "reminder", "alert me", "don't forget", "dont forget"]):
        return {"intent": "SET_REMINDER", "content": text, "remind_at": None}

    if any(kw in lower for kw in ["schedule", "meeting", "event", "book", "appointment", "at ", "on monday", "on tuesday", "on wednesday", "on thursday", "on friday"]):
        return {"intent": "CREATE_EVENT", "title": text, "start_datetime": None}

    if any(kw in lower for kw in ["add task", "todo", "to-do", "to do", "action:", "task:"]):
        return {"intent": "ADD_TASK", "title": text, "status": "queue"}

    if any(kw in lower for kw in ["briefing", "my day", "what's on", "whats on", "morning", "summary"]):
        return {"intent": "GET_BRIEFING"}

    if any(kw in lower for kw in ["surprise me", "random", "serendipity", "random memory"]):
        return {"intent": "SERENDIPITY"}

    if any(kw in lower for kw in ["create collection", "new collection", "make a collection", "list collections", "my collections", "what collections"]):
        action = "list" if any(kw in lower for kw in ["list", "show", "what"]) else "create"
        name = re.sub(r".*(called|named|:)\s*", "", lower).strip().title() if action == "create" else ""
        return {"intent": "MANAGE_COLLECTION", "action": action, "name": name}

    if any(kw in lower for kw in ["show", "list", "search", "find", "what did", "my tasks", "my memories", "my reminders"]):
        return {"intent": "QUERY", "target": "all", "query": text}

    # Default: save as memory with smart collection guessing
    collection = _guess_collection(lower)
    return {"intent": "ADD_MEMORY", "content": text, "collection": collection, "tags": []}


def _guess_collection(lower: str) -> str:
    """Simple keyword-based collection guesser for offline fallback."""
    if any(kw in lower for kw in ["travel", "trip", "flight", "hotel", "airbnb", "bali", "destination", "vacation", "restaurant"]):
        return "Travel"
    if any(kw in lower for kw in ["work", "scheme", "project", "meeting", "report", "admin", "government", "panchayat", "district", "collector"]):
        return "Work"
    if any(kw in lower for kw in ["idea", "innovation", "suggestion", "try", "experiment", "what if"]):
        return "Ideas"
    if any(kw in lower for kw in ["learn", "read", "article", "book", "research", "study", "stat", "found"]):
        return "Learning"
    if any(kw in lower for kw in ["health", "gym", "diet", "exercise", "fitness", "doctor", "medical"]):
        return "Health"
    if any(kw in lower for kw in ["money", "budget", "expense", "salary", "cost", "finance", "invest"]):
        return "Finance"
    if any(kw in lower for kw in ["met", "contact", "person", "friend", "colleague", "people"]):
        return "People"
    return "General"


# ── Voice/file classification ─────────────────────────────────────────────────

def classify_voice(file_path: str, caption: str = "") -> dict:
    """
    Classify intent from a voice/audio file using Gemini multimodal.
    Returns same dict structure as classify().
    """
    today = date.today().strftime("%Y-%m-%d (%A)")
    prompt = _SYSTEM_PROMPT.format(today=today)

    try:
        log.info("Uploading voice file to Gemini: %s", file_path)
        client   = _get_client()
        uploaded = client.files.upload(file=file_path)
        result   = client.models.generate_content(
            model=_MODEL_NAME,
            contents=[
                uploaded,
                f"{prompt}\n\n(This is a voice note. Transcribe and classify it.)"
                + (f"\nCaption: {caption}" if caption else "")
            ]
        )
        raw = (result.text or "").strip()
        if raw.startswith("```json"): raw = raw[7:]
        if raw.startswith("```"):    raw = raw[3:]
        if raw.endswith("```"):      raw = raw[:-3]
        parsed = json.loads(raw.strip())
        log.info("Voice intent: %s", parsed.get("intent"))
        return parsed
    except Exception as exc:
        log.error("Voice classification failed: %s", exc)
        return {"intent": "ADD_MEMORY", "content": caption or "Voice note", "collection": "Voice", "tags": []}


def classify_image(file_path: str, caption: str = "") -> dict:
    """Classify intent from an image/document using Gemini vision."""
    today = date.today().strftime("%Y-%m-%d (%A)")
    prompt = _SYSTEM_PROMPT.format(today=today)

    try:
        mime_type = "image/jpeg"
        if file_path.lower().endswith(".pdf"):
            mime_type = "application/pdf"
        elif file_path.lower().endswith(".png"):
            mime_type = "image/png"

        client   = _get_client()
        uploaded = client.files.upload(file=file_path)
        result   = client.models.generate_content(
            model=_MODEL_NAME,
            contents=[
                uploaded,
                f"{prompt}\n\n(This is an image/document. Describe what you see and classify the intent.)"
                + (f"\nCaption: {caption}" if caption else "")
            ]
        )
        raw = (result.text or "").strip()
        if raw.startswith("```json"): raw = raw[7:]
        if raw.startswith("```"):    raw = raw[3:]
        if raw.endswith("```"):      raw = raw[:-3]
        return json.loads(raw.strip())
    except Exception as exc:
        log.error("Image classification failed: %s", exc)
        return {"intent": "SAVE_FILE", "caption": caption or ""}


# ── Natural-language time parser ──────────────────────────────────────────────

def parse_remind_time(text: str, user_tz: str = "Asia/Kolkata") -> str | None:
    """
    Parse natural-language time expressions into 'YYYY-MM-DD HH:MM' UTC string.
    Examples: 'tomorrow at 9am', 'next Monday 3pm', 'in 2 hours'
    Returns None if unparseable.
    """
    try:
        import dateparser
        import pytz
        settings = {
            "PREFER_DATES_FROM": "future",
            "TIMEZONE": user_tz,
            "TO_TIMEZONE": "UTC",
            "RETURN_AS_TIMEZONE_AWARE": False,
        }
        dt = dateparser.parse(text, settings=settings)
        if dt:
            return dt.strftime("%Y-%m-%d %H:%M")
    except Exception as exc:
        log.warning("Time parse failed: %s", exc)
    return None


def smart_parse_event_times(raw_start: str | None, raw_end: str | None,
                             user_tz: str = "Asia/Kolkata") -> tuple[datetime | None, datetime | None]:
    """
    Parse event start and end times, returning datetime objects in user's timezone.
    """
    import pytz

    def _parse(s: str | None) -> datetime | None:
        if not s:
            return None
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M"):
            try:
                return datetime.strptime(s, fmt)
            except Exception:
                pass
        try:
            import dateparser
            return dateparser.parse(s, settings={"PREFER_DATES_FROM": "future", "TIMEZONE": user_tz})
        except Exception:
            pass
        return None

    start = _parse(raw_start)
    end = _parse(raw_end)
    if start and not end:
        end = start + timedelta(hours=1)
    return start, end
