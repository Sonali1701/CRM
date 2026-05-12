from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import require_user
from app.flash import flash, get_flash
from app.models import (
    User, Lead,
    EmailSequence, SequenceStep, SequenceEnrollment,
)
from app.templating import templates


router = APIRouter()


@router.get("")
def sequences_list(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    rows = db.query(EmailSequence).order_by(EmailSequence.name).all()
    return templates.TemplateResponse(request, "sequences/list.html", {
        "user": user, "flash": get_flash(request), "sequences": rows,
    })


@router.get("/new")
def new_sequence(request: Request, user: User = Depends(require_user)):
    return templates.TemplateResponse(request, "sequences/form.html", {
        "user": user, "sequence": None,
    })


@router.post("/new")
def create_sequence(
    name: str = Form(...), description: str = Form(""),
    is_active: str = Form(""),
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    seq = EmailSequence(
        name=name.strip(),
        description=description or None,
        is_active=bool(is_active),
        created_by_id=user.id,
    )
    db.add(seq); db.commit()
    return flash(RedirectResponse(f"/sequences/{seq.id}", 303), "Sequence created. Add steps now.")


@router.get("/{sid}")
def sequence_detail(sid: int, request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    seq = _get(sid, db)
    active_enrollments = (
        db.query(SequenceEnrollment)
        .filter(SequenceEnrollment.sequence_id == sid)
        .order_by(SequenceEnrollment.enrolled_at.desc())
        .limit(50)
        .all()
    )
    return templates.TemplateResponse(request, "sequences/form.html", {
        "user": user, "flash": get_flash(request),
        "sequence": seq, "enrollments": active_enrollments,
    })


@router.post("/{sid}")
def update_sequence(
    sid: int,
    name: str = Form(...), description: str = Form(""),
    is_active: str = Form(""),
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    seq = _get(sid, db)
    seq.name = name.strip()
    seq.description = description or None
    seq.is_active = bool(is_active)
    db.commit()
    return flash(RedirectResponse(f"/sequences/{sid}", 303), "Sequence updated.")


@router.post("/{sid}/delete")
def delete_sequence(sid: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    seq = _get(sid, db)
    if not user.is_admin and seq.created_by_id != user.id:
        raise HTTPException(403, "Only the creator or an admin can delete")
    db.delete(seq); db.commit()
    return flash(RedirectResponse("/sequences", 303), "Sequence deleted.", "error")


# ── Steps ────────────────────────────────────────────────────────────────────

@router.post("/{sid}/steps/new")
def add_step(
    sid: int,
    delay_days: str = Form("0"),
    subject: str = Form(...),
    body: str = Form(...),
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    seq = _get(sid, db)
    existing = db.query(SequenceStep).filter(SequenceStep.sequence_id == sid).count()
    step = SequenceStep(
        sequence_id=sid,
        step_number=existing + 1,
        delay_days=int(delay_days) if delay_days.strip().isdigit() else 0,
        subject=subject,
        body=body,
    )
    db.add(step); db.commit()
    return flash(RedirectResponse(f"/sequences/{sid}", 303), "Step added.")


@router.post("/{sid}/steps/{step_id}/delete")
def delete_step(sid: int, step_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    step = db.get(SequenceStep, step_id)
    if not step or step.sequence_id != sid:
        raise HTTPException(404)
    db.delete(step)
    # Renumber remaining steps
    remaining = db.query(SequenceStep).filter(SequenceStep.sequence_id == sid).order_by(SequenceStep.step_number).all()
    for i, s in enumerate(remaining, start=1):
        if s.id != step_id:
            s.step_number = i
    db.commit()
    return flash(RedirectResponse(f"/sequences/{sid}", 303), "Step removed.", "error")


# ── Enroll leads ─────────────────────────────────────────────────────────────

@router.post("/enroll")
def enroll_leads(
    sequence_id: str = Form(...),
    lead_ids: str = Form(""),
    redirect_to: str = Form("/leads"),
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    sid = int(sequence_id) if sequence_id.strip().isdigit() else 0
    seq = db.get(EmailSequence, sid)
    if not seq or not seq.is_active:
        raise HTTPException(400, "Sequence not found or inactive")

    ids = [int(x) for x in lead_ids.split(",") if x.strip().isdigit()]
    if not ids:
        raise HTTPException(400, "No leads selected")

    leads = db.query(Lead).filter(Lead.id.in_(ids)).all()
    now = datetime.now(timezone.utc)
    steps = sorted(seq.steps, key=lambda s: s.step_number)
    if not steps:
        raise HTTPException(400, "This sequence has no steps yet")

    enrolled = 0
    skipped = 0
    for lead in leads:
        if not lead.email:
            skipped += 1
            continue
        existing = (
            db.query(SequenceEnrollment)
            .filter_by(sequence_id=sid, lead_id=lead.id)
            .first()
        )
        if existing:
            # Re-activate if previously stopped/completed
            existing.status = "active"
            existing.current_step = 0
            existing.next_send_at = now + timedelta(days=steps[0].delay_days)
            existing.last_step_at = None
            existing.completed_at = None
            existing.enrolled_by_id = user.id
        else:
            enr = SequenceEnrollment(
                sequence_id=sid,
                lead_id=lead.id,
                enrolled_by_id=user.id,
                next_send_at=now + timedelta(days=steps[0].delay_days),
            )
            db.add(enr)
        enrolled += 1
    db.commit()

    msg = f"Enrolled {enrolled} contact(s) in '{seq.name}'."
    if skipped:
        msg += f" Skipped {skipped} with no email."
    return flash(RedirectResponse(redirect_to, 303), msg)


@router.post("/enrollments/{eid}/stop")
def stop_enrollment(eid: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    enr = db.get(SequenceEnrollment, eid)
    if not enr:
        raise HTTPException(404)
    enr.status = "stopped"
    enr.next_send_at = None
    db.commit()
    return flash(RedirectResponse(f"/sequences/{enr.sequence_id}", 303), "Enrollment stopped.", "error")


def _get(sid: int, db: Session) -> EmailSequence:
    seq = db.get(EmailSequence, sid)
    if not seq:
        raise HTTPException(404, "Sequence not found")
    return seq
