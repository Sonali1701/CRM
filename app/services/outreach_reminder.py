from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

from sqlalchemy.orm import Session

_MONTHS: dict[str, int] = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6,
    "jul": 7, "july": 7, "aug": 8, "august": 8, "sep": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}

_WEEKDAYS: dict[str, int] = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}

_MONTH_PAT = (
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?"
    r"|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
)
_WEEKDAY_PAT = r"monday|tuesday|wednesday|thursday|friday|saturday|sunday"


def _extract_date_from_notes(notes: str) -> date | None:
    today = date.today()
    text = notes.lower()

    if "tomorrow" in text:
        return today + timedelta(days=1)

    # "next Monday" / "this Friday"
    for prefix in ("next", "this"):
        m = re.search(rf"{prefix}\s+({_WEEKDAY_PAT})", text)
        if m:
            target = _WEEKDAYS[m.group(1)]
            delta = (target - today.weekday()) % 7
            if delta == 0:
                delta = 7
            return today + timedelta(days=delta)

    # "May 25" / "June 3rd"
    m = re.search(
        rf"\b({_MONTH_PAT})\s+(\d{{1,2}})(?:st|nd|rd|th)?\b", text
    )
    if m:
        month = _MONTHS.get(m.group(1).rstrip("h").rstrip("d").rstrip("r").rstrip("s"))
        if not month:
            # handle abbreviated + full via _MONTHS directly
            for k in _MONTHS:
                if m.group(1).startswith(k[:3]):
                    month = _MONTHS[k]
                    break
        day = int(m.group(2))
        if month and 1 <= day <= 31:
            year = today.year
            try:
                d = date(year, month, day)
                if d < today:
                    d = date(year + 1, month, day)
                return d
            except ValueError:
                pass

    # "25 May" / "3rd June"
    m = re.search(
        rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MONTH_PAT})\b", text
    )
    if m:
        day = int(m.group(1))
        raw_month = m.group(2)
        month = None
        for k in _MONTHS:
            if raw_month.startswith(k[:3]):
                month = _MONTHS[k]
                break
        if month and 1 <= day <= 31:
            year = today.year
            try:
                d = date(year, month, day)
                if d < today:
                    d = date(year + 1, month, day)
                return d
            except ValueError:
                pass

    # MM/DD or MM-DD (with optional year)
    m = re.search(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\b", text)
    if m:
        try:
            mo, dy = int(m.group(1)), int(m.group(2))
            yr = int(m.group(3)) if m.group(3) else today.year
            if yr < 100:
                yr += 2000
            if 1 <= mo <= 12 and 1 <= dy <= 31:
                d = date(yr, mo, dy)
                if d < today and not m.group(3):
                    d = date(d.year + 1, d.month, d.day)
                return d
        except ValueError:
            pass

    return None


def schedule_outreach_reminder(db: Session, lead, user_id: int):
    """Create a TASK reminder when outreach notes mention a follow-up date.
    Safe to call on every save — deduplicates by lead + date."""
    from app.models import Activity
    from app.models.activity import ActivityType

    target: date | None = None

    if lead.outreach_reconnect_date:
        target = lead.outreach_reconnect_date
    elif lead.notes:
        target = _extract_date_from_notes(lead.notes)

    if not target:
        return None

    due_at = datetime(target.year, target.month, target.day, 9, 0, tzinfo=timezone.utc)
    if due_at < datetime.now(timezone.utc):
        return None

    date_start = datetime(target.year, target.month, target.day, 0, 0, tzinfo=timezone.utc)
    date_end = date_start + timedelta(days=1)

    existing = (
        db.query(Activity)
        .filter(
            Activity.lead_id == lead.id,
            Activity.type == ActivityType.TASK,
            Activity.completed == False,
            Activity.due_at >= date_start,
            Activity.due_at < date_end,
        )
        .first()
    )
    if existing:
        return existing

    snippet = (lead.notes or "")[:200]
    activity = Activity(
        type=ActivityType.TASK,
        subject=f"Follow up with {lead.name}",
        body=f"Outreach reminder from notes: {snippet}",
        lead_id=lead.id,
        client_id=lead.client_id,
        created_by_id=user_id,
        due_at=due_at,
        completed=False,
    )
    db.add(activity)
    return activity
