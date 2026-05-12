"""Cron endpoints — meant to be called by an external scheduler
(Render cron job, cron-job.org, GitHub Actions, etc.) on a daily cadence.

All endpoints are protected by a shared X-Cron-Key header that must match
the MAIL_SYNC_KEY env var.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models import (
    User, Activity, MailAccount, Lead,
    EmailSequence, SequenceStep, SequenceEnrollment,
)
from app.services.smtp_mail import is_smtp_configured, smtp_send
from app.services.mail_sync import send_mail


router = APIRouter()


def _check_key(request: Request) -> None:
    settings = get_settings()
    key = request.headers.get("X-Cron-Key")
    if not settings.mail_sync_key or key != settings.mail_sync_key:
        raise HTTPException(403, "Bad or missing X-Cron-Key")


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _render(text: str, lead: Lead) -> str:
    if not text:
        return ""
    return (
        text
        .replace("{{first_name}}", lead.first_name or "")
        .replace("{{last_name}}", lead.last_name or "")
        .replace("{{name}}", lead.name)
        .replace("{{company}}", lead.company or "")
        .replace("{{title}}", lead.job_title or "")
        .replace("{{email}}", lead.email or "")
    )


# ── Daily task reminder digest ────────────────────────────────────────────────

def _build_digest_html(user: User, overdue: list[Activity], today: list[Activity]) -> tuple[str, str]:
    """Return (subject, html_body) for a rep's daily digest."""
    lines = [
        f"<p>Good morning {user.full_name.split()[0] if user.full_name else ''},</p>",
        f"<p>Here's your task summary for today:</p>",
    ]
    if overdue:
        lines.append(f"<h3 style='color:#dc2626;margin-bottom:6px'>Overdue ({len(overdue)})</h3><ul>")
        for a in overdue[:25]:
            due = a.due_at.strftime("%b %d") if a.due_at else "—"
            lines.append(f"<li><b>{a.subject}</b> — due {due}</li>")
        lines.append("</ul>")
    if today:
        lines.append(f"<h3 style='color:#2563eb;margin-bottom:6px'>Due today ({len(today)})</h3><ul>")
        for a in today[:25]:
            lines.append(f"<li><b>{a.subject}</b></li>")
        lines.append("</ul>")
    if not overdue and not today:
        lines.append("<p style='color:#16a34a'>You're all caught up — nothing overdue or due today.</p>")
    lines.append("<p style='margin-top:16px;color:#64748b;font-size:13px'>— Radixsol CRM</p>")
    subject = f"CRM Daily — {len(overdue)} overdue, {len(today)} due today"
    return subject, "".join(lines)


def _send_via_user_mailbox_or_smtp(db: Session, user: User, subject: str, html: str) -> bool:
    """Send to user.email via their own Graph mailbox if available, else SMTP. Returns True on success."""
    if not user.email:
        return False
    account = (
        db.query(MailAccount)
        .filter_by(user_id=user.id, provider="microsoft_graph")
        .first()
    )
    if account:
        try:
            # Run async send_mail from sync context — we're already inside an event loop here.
            loop = asyncio.get_event_loop()
            loop.run_until_complete(send_mail(db, account, [user.email], None, subject, html, None, None, None))
            return True
        except RuntimeError:
            # When called inside running loop — fall through to SMTP
            pass
        except Exception as e:
            print(f"[cron] Graph send to {user.email} failed: {e}")
    if is_smtp_configured():
        try:
            smtp_send([user.email], None, subject, html)
            return True
        except Exception as e:
            print(f"[cron] SMTP send to {user.email} failed: {e}")
    return False


@router.post("/cron/daily-reminders")
async def daily_reminders(request: Request, db: Session = Depends(get_db)):
    """Email each active user their overdue + due-today task summary."""
    _check_key(request)

    now = datetime.now(timezone.utc)
    end_of_today = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)

    sent = 0
    skipped = 0
    users = db.query(User).filter(User.is_active == True).all()
    for u in users:
        overdue = (
            db.query(Activity)
            .filter(
                Activity.completed == False,
                Activity.due_at < now,
                Activity.due_at.isnot(None),
                Activity.created_by_id == u.id,
            )
            .order_by(Activity.due_at)
            .all()
        )
        today = (
            db.query(Activity)
            .filter(
                Activity.completed == False,
                Activity.due_at >= now,
                Activity.due_at < end_of_today,
                Activity.created_by_id == u.id,
            )
            .order_by(Activity.due_at)
            .all()
        )
        if not overdue and not today:
            skipped += 1
            continue

        subject, html = _build_digest_html(u, overdue, today)
        # Send via SMTP directly (cleanest from async route)
        if is_smtp_configured() and u.email:
            try:
                await asyncio.to_thread(smtp_send, [u.email], None, subject, html)
                sent += 1
                continue
            except Exception as e:
                print(f"[cron] SMTP digest to {u.email} failed: {e}")

        # Else try the user's Graph mailbox (self-send)
        account = db.query(MailAccount).filter_by(user_id=u.id, provider="microsoft_graph").first()
        if account and u.email:
            try:
                await send_mail(db, account, [u.email], None, subject, html, None, None, None)
                sent += 1
                continue
            except Exception as e:
                print(f"[cron] Graph digest to {u.email} failed: {e}")
        skipped += 1

    return {"sent": sent, "skipped": skipped, "users_checked": len(users)}


# ── Email sequence runner ─────────────────────────────────────────────────────

@router.post("/cron/run-sequences")
async def run_sequences(request: Request, db: Session = Depends(get_db)):
    """Fire any sequence steps that are due. Idempotent — running twice in the
    same minute won't double-send because next_send_at is advanced after each step."""
    _check_key(request)

    now = datetime.now(timezone.utc)
    due_enrollments = (
        db.query(SequenceEnrollment)
        .filter(
            SequenceEnrollment.status == "active",
            SequenceEnrollment.next_send_at.isnot(None),
            SequenceEnrollment.next_send_at <= now,
        )
        .all()
    )

    sent = 0
    completed = 0
    failed = 0
    for enr in due_enrollments:
        seq = db.get(EmailSequence, enr.sequence_id)
        if not seq or not seq.is_active:
            enr.status = "stopped"
            continue
        lead = db.get(Lead, enr.lead_id)
        if not lead or not lead.email:
            enr.status = "stopped"
            failed += 1
            continue
        # current_step is 0-indexed for the *next* step to send
        steps = sorted(seq.steps, key=lambda s: s.step_number)
        if enr.current_step >= len(steps):
            enr.status = "completed"
            enr.completed_at = now
            completed += 1
            continue
        step = steps[enr.current_step]
        subject = _render(step.subject, lead)
        body = _render(step.body, lead)

        # Pick sender: lead owner's Graph mailbox, else SMTP
        from app.routers.mail import _pick_sender_account, _record_email_activity
        owner_account = _pick_sender_account(db, lead, lead.owner_id or 0)
        try:
            if owner_account:
                await send_mail(db, owner_account, [lead.email], None, subject, body, None, lead.id, None)
            elif is_smtp_configured():
                await asyncio.to_thread(smtp_send, [lead.email], None, subject, body)
                _record_email_activity(db, lead, lead.owner_id or enr.enrolled_by_id or 0, subject, body)
            else:
                failed += 1
                continue
        except Exception as e:
            print(f"[cron] sequence send to {lead.email} failed: {e}")
            failed += 1
            continue

        sent += 1
        enr.current_step += 1
        enr.last_step_at = now
        if enr.current_step >= len(steps):
            enr.status = "completed"
            enr.completed_at = now
            enr.next_send_at = None
            completed += 1
        else:
            next_step = steps[enr.current_step]
            enr.next_send_at = now + timedelta(days=next_step.delay_days)
    db.commit()

    return {"sent": sent, "completed": completed, "failed": failed, "checked": len(due_enrollments)}
