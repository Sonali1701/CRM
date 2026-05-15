from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, Form, Request, HTTPException
from fastapi.responses import RedirectResponse, JSONResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import require_user
from app.flash import flash, get_flash
from app.models import (
    User, Deal, Client, Activity,
    DealQualification, ClosePlan, ClosePlanStep, ClosePlanStepStatus,
)
from app.models.deal import DealStage, OPEN_STAGES, PIPELINE_STAGES, STAGE_LABELS
from app.models.sales_planning import MEDDIC_DIMENSIONS
from app.templating import templates

router = APIRouter()


def _deal_query(db: Session, user: User):
    q = db.query(Deal)
    if not user.is_manager:
        q = q.filter(Deal.owner_id == user.id)
    return q


@router.get("")
def deals_list(request: Request, stage: str = "", search: str = "", user: User = Depends(require_user), db: Session = Depends(get_db)):
    q = _deal_query(db, user)
    if stage:
        q = q.filter(Deal.stage == stage)
    if search:
        q = q.filter(Deal.title.ilike(f"%{search}%"))
    deals = q.order_by(Deal.updated_at.desc()).all()
    clients = db.query(Client).order_by(Client.name).all()
    reps = db.query(User).filter(User.is_active == True).all() if user.is_manager else []
    # Per-deal risk score keyed by id so the template can lookup with risk_by_id[deal.id]
    from app.services.deal_risk import compute_risk
    risk_by_id = {d.id: compute_risk(d) for d in deals}
    return templates.TemplateResponse(request, "deals/list.html", {
        "user": user, "flash": get_flash(request),
        "deals": deals, "clients": clients, "reps": reps,
        "stages": PIPELINE_STAGES, "stage_labels": STAGE_LABELS,
        "filter_stage": stage, "search": search,
        "risk_by_id": risk_by_id,
    })


@router.get("/new")
def deal_new(request: Request, client_id: int = None, user: User = Depends(require_user), db: Session = Depends(get_db)):
    clients = db.query(Client).order_by(Client.name).all()
    reps = db.query(User).filter(User.is_active == True).all() if user.is_manager else []
    return templates.TemplateResponse(request, "deals/form.html", {
        "user": user, "deal": None, "clients": clients, "reps": reps,
        "stages": OPEN_STAGES, "stage_labels": STAGE_LABELS,
        "prefill_client_id": client_id,
    })


@router.post("/new")
def deal_create(
    request: Request,
    title: str = Form(...), client_id: str = Form(""), owner_id: str = Form(""),
    value: float = Form(0), stage: str = Form("lead_generated"),
    expected_close_date: str = Form(""), probability: int = Form(0),
    notes: str = Form(""),
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    cid = int(client_id) if client_id.strip().isdigit() else None
    oid = int(owner_id) if owner_id.strip().isdigit() else None
    deal = Deal(
        title=title, client_id=cid,
        owner_id=oid if user.is_manager else user.id,
        value=value, stage=DealStage(stage),
        expected_close_date=datetime.strptime(expected_close_date, "%Y-%m-%d").date() if expected_close_date else None,
        probability=probability, notes=notes or None,
    )
    db.add(deal); db.commit()
    return flash(RedirectResponse(f"/deals/{deal.id}", 303), "Deal created.")


@router.get("/{deal_id}")
def deal_detail(deal_id: int, request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    deal = _get_deal(deal_id, user, db)
    clients = db.query(Client).order_by(Client.name).all()
    reps = db.query(User).filter(User.is_active == True).all() if user.is_manager else []
    activities = db.query(Activity).filter(Activity.deal_id == deal_id).order_by(Activity.created_at.desc()).all()
    qualification = db.query(DealQualification).filter_by(deal_id=deal_id).first()
    close_plan = db.query(ClosePlan).filter_by(deal_id=deal_id).first()
    from app.models.activity import ActivityType
    return templates.TemplateResponse(request, "deals/detail.html", {
        "user": user, "flash": get_flash(request),
        "deal": deal, "clients": clients, "reps": reps, "activities": activities,
        "stages": PIPELINE_STAGES, "stage_labels": STAGE_LABELS,
        "ActivityType": ActivityType,
        "qualification": qualification,
        "close_plan": close_plan,
        "meddic_dimensions": MEDDIC_DIMENSIONS,
        "close_step_statuses": list(ClosePlanStepStatus),
    })


@router.post("/{deal_id}")
def deal_update(
    deal_id: int,
    title: str = Form(...), client_id: str = Form(""), owner_id: str = Form(""),
    value: float = Form(0), stage: str = Form(...),
    expected_close_date: str = Form(""), probability: int = Form(0),
    notes: str = Form(""),
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    deal = _get_deal(deal_id, user, db)
    deal.title = title
    deal.client_id = int(client_id) if client_id.strip().isdigit() else None
    deal.value = value; deal.stage = DealStage(stage)
    deal.expected_close_date = datetime.strptime(expected_close_date, "%Y-%m-%d").date() if expected_close_date else None
    deal.probability = probability; deal.notes = notes or None
    if user.is_manager:
        deal.owner_id = int(owner_id) if owner_id.strip().isdigit() else None
    db.commit()
    return flash(RedirectResponse(f"/deals/{deal_id}", 303), "Deal updated.")


@router.post("/{deal_id}/stage")
async def deal_move_stage(deal_id: int, request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    """HTMX/JSON endpoint for Kanban drag-drop stage change."""
    data = await request.json()
    new_stage = data.get("stage")
    if new_stage not in [s.value for s in DealStage]:
        raise HTTPException(400, "Invalid stage")
    deal = _get_deal(deal_id, user, db)
    deal.stage = DealStage(new_stage)
    db.commit()
    return JSONResponse({"ok": True, "stage": new_stage, "label": STAGE_LABELS[deal.stage]})


@router.post("/{deal_id}/delete")
def deal_delete(deal_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    deal = _get_deal(deal_id, user, db)
    db.delete(deal); db.commit()
    return flash(RedirectResponse("/deals", 303), "Deal deleted.", "error")


@router.post("/{deal_id}/suggest-next-step")
async def deal_suggest_next_step(deal_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Returns JSON {action, reason, urgency} suggested by Gemini based on deal state."""
    from app.services.ai_compose import is_ai_configured, suggest_next_step
    if not is_ai_configured():
        raise HTTPException(400, "AI is not configured. Set GEMINI_API_KEY and/or GROQ_API_KEY in env.")

    deal = _get_deal(deal_id, user, db)

    # Build a compact textual description of the deal for the model
    now = datetime.now(timezone.utc)
    created = deal.created_at.replace(tzinfo=timezone.utc) if deal.created_at and deal.created_at.tzinfo is None else deal.created_at
    age_days = (now - created).days if created else None
    last = deal.last_activity_at
    if last and last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    days_since_activity = (now - last).days if last else None

    recent_activities = (
        db.query(Activity)
        .filter(Activity.deal_id == deal_id)
        .order_by(Activity.created_at.desc())
        .limit(5)
        .all()
    )
    activity_lines = [
        f"  - {a.type.value}: {a.subject} ({a.created_at.strftime('%Y-%m-%d')})"
        for a in recent_activities
    ]

    context_parts = [
        f"Deal: {deal.title}",
        f"Company: {deal.client.name if deal.client else 'none'}",
        f"Stage: {STAGE_LABELS.get(deal.stage, deal.stage.value)}",
        f"Value: {deal.value} {deal.currency}",
        f"Probability: {deal.probability}%",
        f"Owner: {deal.owner.full_name if deal.owner else 'unassigned'}",
        f"Expected close: {deal.expected_close_date or 'not set'}",
        f"Deal age: {age_days} days" if age_days is not None else "Deal age: unknown",
        f"Days since last activity: {days_since_activity}" if days_since_activity is not None else "No activities logged yet",
    ]
    if deal.notes:
        context_parts.append(f"Notes: {deal.notes[:500]}")
    if activity_lines:
        context_parts.append("Recent activities:\n" + "\n".join(activity_lines))
    context = "\n".join(context_parts)

    try:
        result = await suggest_next_step(context, db=db)
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    return JSONResponse(result)


def _get_deal(deal_id: int, user: User, db: Session) -> Deal:
    q = db.query(Deal).filter(Deal.id == deal_id)
    if not user.is_manager:
        q = q.filter(Deal.owner_id == user.id)
    deal = q.first()
    if not deal:
        raise HTTPException(404, "Deal not found")
    return deal


# ─── MEDDIC qualification ────────────────────────────────────────────────────

def _get_or_create_qualification(db: Session, deal: Deal) -> DealQualification:
    q = db.query(DealQualification).filter_by(deal_id=deal.id).first()
    if q:
        return q
    q = DealQualification(deal_id=deal.id)
    db.add(q)
    db.flush()
    return q


def _build_meddic_context(db: Session, deal: Deal) -> str:
    """Plain-text dump of everything we know about the deal — feeds the MEDDIC LLM."""
    lines = [f"# Deal: {deal.title}",
             f"Stage: {deal.stage_label}",
             f"Value: {deal.value} {deal.currency}"]
    if deal.expected_close_date:
        lines.append(f"Target close: {deal.expected_close_date}")
    if deal.notes:
        lines.append(f"\nDeal notes:\n{deal.notes}")
    if deal.client_id:
        client = db.get(Client, deal.client_id)
        if client:
            lines.append(f"\n## Client: {client.name}")
            if client.industry: lines.append(f"Industry: {client.industry}")
            if client.description: lines.append(f"\n{client.description}")
    acts = (
        db.query(Activity).filter(Activity.deal_id == deal.id)
        .order_by(Activity.created_at.desc()).limit(15).all()
    )
    if acts:
        lines.append("\n## Recent activity")
        for a in acts:
            when = a.created_at.strftime("%Y-%m-%d") if a.created_at else "?"
            body = (a.body or "")[:500].replace("\n", " ")
            lines.append(f"- {when} · {a.type.value} · {a.subject} — {body}")
    return "\n".join(lines)


@router.post("/{deal_id}/qualification")
def deal_save_qualification(
    deal_id: int,
    metrics_score: int = Form(0), metrics_notes: str = Form(""),
    economic_buyer_score: int = Form(0), economic_buyer_notes: str = Form(""),
    decision_criteria_score: int = Form(0), decision_criteria_notes: str = Form(""),
    decision_process_score: int = Form(0), decision_process_notes: str = Form(""),
    identify_pain_score: int = Form(0), identify_pain_notes: str = Form(""),
    champion_score: int = Form(0), champion_notes: str = Form(""),
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    """Manual save of MEDDIC scores."""
    deal = _get_deal(deal_id, user, db)
    q = _get_or_create_qualification(db, deal)

    def _clamp(v: int) -> int:
        return max(0, min(3, int(v) if v is not None else 0))

    q.metrics_score = _clamp(metrics_score); q.metrics_notes = metrics_notes or None
    q.economic_buyer_score = _clamp(economic_buyer_score); q.economic_buyer_notes = economic_buyer_notes or None
    q.decision_criteria_score = _clamp(decision_criteria_score); q.decision_criteria_notes = decision_criteria_notes or None
    q.decision_process_score = _clamp(decision_process_score); q.decision_process_notes = decision_process_notes or None
    q.identify_pain_score = _clamp(identify_pain_score); q.identify_pain_notes = identify_pain_notes or None
    q.champion_score = _clamp(champion_score); q.champion_notes = champion_notes or None
    q.last_scored_by_id = user.id
    q.last_scored_at = datetime.now(timezone.utc)
    q.last_scored_by_ai = 0
    db.commit()
    return flash(RedirectResponse(f"/deals/{deal_id}", 303), "Qualification saved.")


@router.post("/{deal_id}/qualification/ai-score")
async def deal_ai_score_qualification(
    deal_id: int,
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    """Use AI to score MEDDIC from deal notes + activity history."""
    from app.services.ai_compose import is_ai_configured, score_meddic
    if not is_ai_configured():
        raise HTTPException(400, "AI not configured")
    deal = _get_deal(deal_id, user, db)
    context = _build_meddic_context(db, deal)
    try:
        result = await score_meddic(context, db=db)
    except RuntimeError as e:
        raise HTTPException(502, str(e))

    q = _get_or_create_qualification(db, deal)
    for dim in MEDDIC_DIMENSIONS:
        block = result.get(dim) or {}
        score = max(0, min(3, int(block.get("score", 0))))
        reasoning = (block.get("reasoning") or "").strip() or None
        setattr(q, f"{dim}_score", score)
        setattr(q, f"{dim}_notes", reasoning)
    q.last_scored_by_id = user.id
    q.last_scored_at = datetime.now(timezone.utc)
    q.last_scored_by_ai = 1
    db.commit()
    return flash(RedirectResponse(f"/deals/{deal_id}", 303),
                 f"AI MEDDIC score updated. Top gap: {result.get('top_gap', 'unknown')}")


# ─── Close Plan ──────────────────────────────────────────────────────────────

def _get_or_create_close_plan(db: Session, deal: Deal, user_id: int) -> ClosePlan:
    cp = db.query(ClosePlan).filter_by(deal_id=deal.id).first()
    if cp:
        return cp
    cp = ClosePlan(deal_id=deal.id, created_by_id=user_id)
    db.add(cp)
    db.flush()
    return cp


@router.post("/{deal_id}/close-plan")
def close_plan_save(
    deal_id: int,
    summary: str = Form(""),
    target_close_date: str = Form(""),
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    deal = _get_deal(deal_id, user, db)
    cp = _get_or_create_close_plan(db, deal, user.id)
    cp.summary = summary or None
    cp.target_close_date = datetime.strptime(target_close_date, "%Y-%m-%d").date() if target_close_date else None
    db.commit()
    return flash(RedirectResponse(f"/deals/{deal_id}", 303), "Close plan saved.")


@router.post("/{deal_id}/close-plan/steps")
def close_plan_add_step(
    deal_id: int,
    title: str = Form(...),
    owner_label: str = Form(""),
    due_date: str = Form(""),
    notes: str = Form(""),
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    deal = _get_deal(deal_id, user, db)
    cp = _get_or_create_close_plan(db, deal, user.id)
    max_pos = max((s.position for s in cp.steps), default=-1)
    step = ClosePlanStep(
        close_plan_id=cp.id,
        position=max_pos + 1,
        title=title.strip(),
        owner_label=owner_label.strip() or None,
        due_date=datetime.strptime(due_date, "%Y-%m-%d").date() if due_date else None,
        notes=notes.strip() or None,
        status=ClosePlanStepStatus.PENDING,
    )
    db.add(step)
    db.commit()
    return flash(RedirectResponse(f"/deals/{deal_id}", 303), "Step added.")


@router.post("/{deal_id}/close-plan/steps/{step_id}/status")
def close_plan_set_status(
    deal_id: int, step_id: int,
    status: str = Form(...),
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    deal = _get_deal(deal_id, user, db)
    step = db.get(ClosePlanStep, step_id)
    if not step or not step.plan or step.plan.deal_id != deal.id:
        raise HTTPException(404)
    try:
        step.status = ClosePlanStepStatus(status)
    except ValueError:
        raise HTTPException(400, "Invalid status")
    db.commit()
    return flash(RedirectResponse(f"/deals/{deal_id}", 303), "Step updated.")


@router.post("/{deal_id}/close-plan/steps/{step_id}/delete")
def close_plan_delete_step(
    deal_id: int, step_id: int,
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    deal = _get_deal(deal_id, user, db)
    step = db.get(ClosePlanStep, step_id)
    if not step or not step.plan or step.plan.deal_id != deal.id:
        raise HTTPException(404)
    db.delete(step)
    db.commit()
    return flash(RedirectResponse(f"/deals/{deal_id}", 303), "Step removed.", "error")


@router.post("/{deal_id}/close-plan/ai-draft")
async def close_plan_ai_draft(
    deal_id: int,
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    """Generate a starter close plan from deal + activity context. Skips if
    there are already steps — won't clobber manual work."""
    from app.services.ai_compose import is_ai_configured, draft_close_plan
    if not is_ai_configured():
        raise HTTPException(400, "AI not configured")
    deal = _get_deal(deal_id, user, db)
    cp = _get_or_create_close_plan(db, deal, user.id)
    if cp.steps:
        return flash(RedirectResponse(f"/deals/{deal_id}", 303),
                     "Close plan already has steps — clear them first to regenerate.", "error")
    context = _build_meddic_context(db, deal)
    try:
        result = await draft_close_plan(context, db=db)
    except RuntimeError as e:
        raise HTTPException(502, str(e))

    cp.summary = result.get("summary") or None
    target = (result.get("target_close_date") or "").strip()
    if target:
        try:
            cp.target_close_date = datetime.strptime(target, "%Y-%m-%d").date()
        except ValueError:
            pass
    today = datetime.now(timezone.utc).date()
    for i, raw in enumerate(result.get("steps") or []):
        title = (raw.get("title") or "").strip()
        if not title:
            continue
        due_in = raw.get("due_in_days")
        due = today + timedelta(days=int(due_in)) if isinstance(due_in, int) else None
        db.add(ClosePlanStep(
            close_plan_id=cp.id,
            position=i,
            title=title[:500],
            owner_label=(raw.get("owner_label") or "").strip()[:120] or None,
            due_date=due,
            status=ClosePlanStepStatus.PENDING,
            notes=(raw.get("notes") or "").strip() or None,
        ))
    db.commit()
    return flash(RedirectResponse(f"/deals/{deal_id}", 303), "AI close plan drafted.")
