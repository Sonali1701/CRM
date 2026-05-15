from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.deps import require_user
from app.flash import flash, get_flash
from app.models import User, MailAccount, EmailMessage, Activity, Deal, Client, Lead
from app.models.activity import ActivityType
from app.services.ms_graph_mail import (
    build_authorize_url,
    exchange_code_for_token,
    validate_oauth_state,
)
from app.services.mail_sync import (
    sync_all_folders,
    link_messages_to_activities,
    send_mail,
)
from app.services.smtp_mail import is_smtp_configured, smtp_send
from app.services.ai_compose import is_ai_configured, generate_email, fetch_company_context
from app.templating import templates


router = APIRouter()


def _render_lead_template(text: str, lead: Lead) -> str:
    """Substitute {{placeholder}} tokens with this lead's fields."""
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


def _pick_sender_account(db: Session, lead: Lead, fallback_user_id: int) -> MailAccount | None:
    """Return the lead owner's mailbox; fall back to the fallback user's mailbox."""
    if lead.owner_id:
        owner_acc = (
            db.query(MailAccount)
            .filter_by(user_id=lead.owner_id, provider="microsoft_graph")
            .first()
        )
        if owner_acc:
            return owner_acc
    return (
        db.query(MailAccount)
        .filter_by(user_id=fallback_user_id, provider="microsoft_graph")
        .first()
    )


def _record_email_activity(db: Session, lead: Lead, user_id: int, subject: str, body: str) -> Activity:
    """Log a sent email as an Activity so it shows up in:
      - /activities (filtered by created_by_id)
      - the contact's company page (filtered by client_id or deal_id)
      - the linked deal's page + the pipeline (via deal.last_activity_at)

    We also try to attach the activity to the first open deal for the lead's
    company so the pipeline reflects the touch."""
    deal_id = None
    if lead.client_id:
        from app.models.deal import OPEN_STAGES
        open_deal = (
            db.query(Deal)
            .filter(Deal.client_id == lead.client_id, Deal.stage.in_(OPEN_STAGES))
            .order_by(Deal.created_at.desc())
            .first()
        )
        if open_deal:
            deal_id = open_deal.id
            open_deal.last_activity_at = datetime.now(timezone.utc)

    activity = Activity(
        type=ActivityType.EMAIL,
        subject=subject or "(no subject)",
        body=(body or "")[:2000],
        client_id=lead.client_id,
        lead_id=lead.id,
        deal_id=deal_id,
        created_by_id=user_id,
        completed=True,
        completed_at=datetime.now(timezone.utc),
    )
    db.add(activity)
    db.commit()
    return activity


def _create_followup_task(
    db: Session, lead: Lead, owner_user_id: int,
    original_subject: str, days: int,
) -> Activity | None:
    """Schedule a follow-up Task activity N days from now for this lead.
    Lives in Upcoming follow-ups, daily digest, and the contact/company/deal timelines."""
    days = max(1, min(60, int(days)))  # clamp to 1-60d
    due_at = datetime.now(timezone.utc) + timedelta(days=days)
    deal_id = None
    if lead.client_id:
        from app.models.deal import OPEN_STAGES
        open_deal = (
            db.query(Deal)
            .filter(Deal.client_id == lead.client_id, Deal.stage.in_(OPEN_STAGES))
            .order_by(Deal.created_at.desc())
            .first()
        )
        if open_deal:
            deal_id = open_deal.id

    subj_short = (original_subject or "email")[:200]
    task = Activity(
        type=ActivityType.TASK,
        subject=f"Follow up with {lead.name}: {subj_short}",
        body=f"Auto-scheduled {days} days after sending email \"{subj_short}\". Check for reply and decide next step.",
        client_id=lead.client_id,
        lead_id=lead.id,
        deal_id=deal_id,
        created_by_id=owner_user_id,
        due_at=due_at,
        completed=False,
    )
    db.add(task)
    db.commit()
    return task


async def _send_to_lead(
    db: Session,
    lead: Lead,
    current_user_id: int,
    subject: str,
    body_html: str,
    cc_list: list[str] | None,
    followup_days: int = 0,
) -> tuple[str | None, str | None]:
    """Try Graph (owner's mailbox → current user's mailbox), then SMTP.
    If followup_days > 0, schedule a follow-up Task activity for the lead.
    Returns (method, error). method is None on failure."""
    if not lead.email:
        return None, "no email"

    method: str | None = None
    err: str | None = None
    sender_id = current_user_id
    account = _pick_sender_account(db, lead, current_user_id)
    if account:
        try:
            await send_mail(db, account, [lead.email], cc_list, subject, body_html, None, lead.id, None)
            # Sender = the owner of the mailbox we used, not necessarily the
            # logged-in user; falls back to current user when no owner mailbox.
            sender_id = account.user_id or current_user_id
            _record_email_activity(db, lead, sender_id, subject, body_html)
            method = "graph"
        except Exception as e:
            err = str(e)
    elif is_smtp_configured():
        try:
            await asyncio.to_thread(smtp_send, [lead.email], cc_list, subject, body_html)
            _record_email_activity(db, lead, current_user_id, subject, body_html)
            method = "smtp"
        except Exception as e:
            err = str(e)

    if method:
        if followup_days > 0:
            try:
                _create_followup_task(db, lead, sender_id, subject, followup_days)
            except Exception as e:
                # Never fail the send because the follow-up task couldn't be created
                print(f"[mail] follow-up task for {lead.email} failed: {e}")
        return method, None
    if err is not None:
        return None, err

    # Helpful error naming the user who needs to connect
    owner_name = "owner"
    if lead.owner_id:
        owner = db.get(User, lead.owner_id)
        if owner:
            owner_name = owner.full_name
    else:
        me = db.get(User, current_user_id)
        if me:
            owner_name = me.full_name
    return None, f"{owner_name} needs to connect Outlook in Profile"


@router.get("")
def mail_inbox(
    request: Request,
    folder: str = "inbox",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Show messages from BOTH sources so users see their sent mail
    regardless of whether they're on Graph OAuth or SMTP:
      - EmailMessage rows synced from Microsoft Graph (inbox + sent)
      - Activity rows of type=EMAIL that DON'T have a Graph parent
        (SMTP sends, plus Graph sends still waiting for sync to catch up)
    """
    from sqlalchemy import select
    from app.models.activity import ActivityType

    account = (
        db.query(MailAccount)
        .filter_by(user_id=user.id, provider="microsoft_graph")
        .first()
    )

    # 1) Graph-synced messages
    graph_msgs = []
    if account:
        graph_msgs = (
            db.query(EmailMessage)
            .filter_by(mail_account_id=account.id, folder=folder)
            .order_by(
                EmailMessage.received_at.desc().nullslast(),
                EmailMessage.sent_at.desc().nullslast(),
            )
            .limit(200)
            .all()
        )

    # 2) Activity-only emails (SMTP sends, or Graph sends not yet synced).
    # Only relevant for the Sent tab — inbox is Graph-only.
    activity_msgs: list[Activity] = []
    if folder == "sentItems":
        synced_ids = select(EmailMessage.activity_id).where(EmailMessage.activity_id.isnot(None))
        q = db.query(Activity).filter(
            Activity.type == ActivityType.EMAIL,
            Activity.id.notin_(synced_ids),
        )
        if not user.is_manager:
            q = q.filter(Activity.created_by_id == user.id)
        activity_msgs = q.order_by(Activity.created_at.desc()).limit(200).all()

    # Unified items list
    items: list[dict] = []
    for m in graph_msgs:
        items.append({
            "subject": m.subject or "(No subject)",
            "from_label": m.from_email or "—",
            "to_label": (m.to_emails or "").replace(";", ", "),
            "preview": (m.body_preview or "")[:200],
            "when": m.sent_at or m.received_at,
            "lead_id": m.lead_id,
            "client_id": m.client_id,
            "deal_id": m.deal_id,
            "source": "Outlook",
        })
    for a in activity_msgs:
        recipient_bits = []
        if a.lead:
            recipient_bits.append(a.lead.name)
            if a.lead.email:
                recipient_bits.append(f"<{a.lead.email}>")
        to_label = " ".join(recipient_bits) or "—"
        from_label = a.created_by.full_name if a.created_by else "—"
        if a.created_by_id == user.id:
            from_label += " (you)"
        items.append({
            "subject": a.subject or "(No subject)",
            "from_label": from_label,
            "to_label": to_label,
            "preview": (a.body or "")[:200],
            "when": a.created_at,
            "lead_id": a.lead_id,
            "client_id": a.client_id,
            "deal_id": a.deal_id,
            "source": "SMTP",
        })

    items.sort(
        key=lambda x: x["when"] or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    items = items[:100]

    return templates.TemplateResponse(request, "mail/inbox.html", {
        "user": user,
        "flash": get_flash(request),
        "account": account,
        "items": items,
        "folder": folder,
        "smtp_only": (account is None and len(items) > 0),
    })


@router.get("/connect")
def mail_connect(request: Request, user: User = Depends(require_user)):
    settings = get_settings()
    if not settings.ms_client_id or not settings.ms_redirect_uri:
        raise HTTPException(503, "Microsoft OAuth not configured")

    from app.services.ms_graph_mail import create_oauth_state
    state = create_oauth_state(user.id)
    auth_url = build_authorize_url(state)
    return RedirectResponse(auth_url)


@router.get("/callback")
async def mail_callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
    db: Session = Depends(get_db),
):
    if error:
        raise HTTPException(400, f"OAuth error: {error}")

    try:
        user_id = validate_oauth_state(state)
    except Exception as e:
        raise HTTPException(400, f"Invalid state: {e}")

    token = await exchange_code_for_token(code)

    # Fetch user profile to get mailbox address
    from app.services.ms_graph_mail import graph_get
    profile = await graph_get(token.access_token, "/me?$select=mail,userPrincipalName")
    mailbox = profile.get("mail") or profile.get("userPrincipalName") or ""

    # Create or update mail account
    account = (
        db.query(MailAccount)
        .filter_by(user_id=user_id, provider="microsoft_graph")
        .first()
    )
    if not account:
        account = MailAccount(user_id=user_id, provider="microsoft_graph", mailbox=mailbox)
        db.add(account)

    from app.crypto import encrypt_str
    account.access_token_enc = encrypt_str(token.access_token)
    if token.refresh_token:
        account.refresh_token_enc = encrypt_str(token.refresh_token)
    account.token_expires_at = token.expires_at
    db.commit()

    return RedirectResponse("/users/profile?flash=Mail%20connected", status_code=303)


@router.post("/send")
async def mail_send(
    request: Request,
    to: str = Form(...),
    cc: str = Form(""),
    subject: str = Form(...),
    body_html: str = Form(...),
    client_id: str = Form(""),
    lead_id: str = Form(""),
    deal_id: str = Form(""),
    create_followup: str = Form("1"),
    followup_days: str = Form("5"),
    redirect_to: str = Form("/"),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    to_list = [addr.strip() for addr in to.split(";") if addr.strip()]
    cc_list = [addr.strip() for addr in cc.split(";") if addr.strip()] if cc else None

    cid = int(client_id) if client_id.strip().isdigit() else None
    lid = int(lead_id) if lead_id.strip().isdigit() else None
    did = int(deal_id) if deal_id.strip().isdigit() else None

    # Default-on: send a follow-up reminder unless caller explicitly disables it
    # by sending create_followup=0/off/empty.
    follow_days = 0
    if create_followup and create_followup.lower() not in ("0", "off", "false", "no"):
        try:
            follow_days = max(1, min(60, int(followup_days)))
        except (ValueError, TypeError):
            follow_days = 5

    def _maybe_schedule_followup(sender_id: int) -> str:
        if not (follow_days and lid):
            return ""
        lead = db.get(Lead, lid)
        if not lead:
            return ""
        try:
            _create_followup_task(db, lead, sender_id, subject, follow_days)
            due = (datetime.now(timezone.utc) + timedelta(days=follow_days)).strftime("%b %d")
            return f" Follow-up scheduled for {due}."
        except Exception as e:
            print(f"[mail] follow-up task failed: {e}")
            return ""

    # Try Microsoft Graph: current user's mailbox, then lead owner's mailbox
    account = (
        db.query(MailAccount)
        .filter_by(user_id=user.id, provider="microsoft_graph")
        .first()
    )
    if not account and lid:
        lead = db.get(Lead, lid)
        if lead:
            account = _pick_sender_account(db, lead, user.id)

    if account:
        await send_mail(db, account, to_list, cc_list, subject, body_html, cid, lid, did)
        sender_id = account.user_id or user.id
        if lid:
            lead = db.get(Lead, lid)
            if lead:
                _record_email_activity(db, lead, sender_id, subject, body_html)
        return flash(RedirectResponse(redirect_to, 303), "Email sent." + _maybe_schedule_followup(sender_id))

    # SMTP fallback
    if is_smtp_configured():
        await asyncio.to_thread(smtp_send, to_list, cc_list, subject, body_html)
        if lid:
            lead = db.get(Lead, lid)
            if lead:
                _record_email_activity(db, lead, user.id, subject, body_html)
        return flash(RedirectResponse(redirect_to, 303), "Email sent (via SMTP)." + _maybe_schedule_followup(user.id))

    raise HTTPException(400, "No mailbox configured. Connect Outlook in Profile, or set SMTP_* env vars.")


@router.post("/bulk-send")
async def mail_bulk_send(
    request: Request,
    lead_ids: str = Form(""),
    subject: str = Form(...),
    body_html: str = Form(...),
    cc: str = Form(""),
    create_followup: str = Form(""),
    followup_days: str = Form("5"),
    redirect_to: str = Form("/leads"),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Send a personalized email to each selected contact, using each contact's
    assigned owner's mailbox (falling back to the current user's mailbox)."""
    ids = [int(x) for x in lead_ids.split(",") if x.strip().isdigit()]
    if not ids:
        raise HTTPException(400, "No contacts selected")

    q = db.query(Lead).filter(Lead.id.in_(ids))
    if not user.is_manager:
        q = q.filter(Lead.owner_id == user.id)
    leads = q.all()

    cc_list = [addr.strip() for addr in cc.split(";") if addr.strip()] if cc else None

    # Follow-up scheduling — checkbox unchecked → 0 (skip)
    follow_days = 0
    if create_followup:
        try:
            follow_days = max(1, min(60, int(followup_days)))
        except (ValueError, TypeError):
            follow_days = 5

    sent = 0
    skipped: list[str] = []
    # Microsoft Graph throttles /sendMail to ~30/min per user mailbox. A 1.2s
    # gap between sends keeps us comfortably under that even if every email
    # goes from the same mailbox, and only adds a few seconds to typical bulk
    # sends. Skip the delay after the very last message.
    for idx, lead in enumerate(leads):
        method, err = await _send_to_lead(
            db, lead, user.id,
            _render_lead_template(subject, lead),
            _render_lead_template(body_html, lead),
            cc_list,
            followup_days=follow_days,
        )
        if method:
            sent += 1
        else:
            skipped.append(f"{lead.name} ({err})")
        if idx < len(leads) - 1:
            await asyncio.sleep(1.2)

    msg = f"Sent {sent} email(s)."
    if sent and follow_days > 0:
        due = (datetime.now(timezone.utc) + timedelta(days=follow_days)).strftime("%b %d")
        msg += f" Scheduled {sent} follow-up task(s) for {due}."
    if skipped:
        msg += f" Skipped {len(skipped)}: {'; '.join(skipped[:3])}"
        if len(skipped) > 3:
            msg += f" …+{len(skipped) - 3} more"
    return flash(RedirectResponse(redirect_to, 303), msg, "success" if sent else "error")


@router.post("/generate")
async def mail_generate(
    request: Request,
    prompt: str = Form(...),
    lead_id: str = Form(""),
    use_web: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Generate a subject + body email template from a natural-language prompt.

    Uses one sample contact's data (the first selected lead) as context for the
    model so the generated text matches the recipient style; the result still
    contains {{first_name}}/{{company}} placeholders that the bulk-send flow
    personalizes per recipient at send time."""
    if not is_ai_configured():
        raise HTTPException(
            400,
            "AI compose needs a free provider key. Set GEMINI_API_KEY "
            "(aistudio.google.com) and/or GROQ_API_KEY (console.groq.com) in env.",
        )
    if not prompt.strip():
        raise HTTPException(400, "Prompt is empty")

    contact: dict = {}
    web_context = ""

    lid = int(lead_id) if lead_id.strip().isdigit() else None
    if lid:
        lead = db.get(Lead, lid)
        if lead and (user.is_manager or lead.owner_id == user.id):
            contact = {
                "name": lead.name,
                "first_name": lead.first_name,
                "company": lead.company or "",
                "title": lead.job_title or "",
                "email": lead.email or "",
            }
            if use_web and lead.client_id:
                client = db.get(Client, lead.client_id)
                if client and client.website:
                    web_context = await fetch_company_context(client.website)

    try:
        result = await generate_email(
            prompt, contact, web_context,
            user_name=user.full_name, db=db, lead_id=lid,
        )
    except RuntimeError as e:
        raise HTTPException(502, str(e))

    return {
        "subject": result["subject"],
        "body": result["body"],
        "web_context_used": bool(web_context),
    }


@router.post("/sync")
async def mail_sync(
    request: Request,
    redirect_to: str = Form("/"),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    account = (
        db.query(MailAccount)
        .filter_by(user_id=user.id, provider="microsoft_graph")
        .first()
    )
    if not account:
        raise HTTPException(400, "No mailbox connected")

    now = datetime.now(timezone.utc)
    min_interval = timedelta(seconds=get_settings().mail_sync_min_interval_seconds)
    if account.last_sync_at and (now - account.last_sync_at) < min_interval:
        return flash(RedirectResponse(redirect_to, 303), "Sync throttled; try again later")

    await sync_all_folders(db, account)
    link_messages_to_activities(db, user.id)

    return flash(RedirectResponse(redirect_to, 303), "Mail synced")


# Public endpoint for uptime/automation; protected by a shared key for security.
# Accepts GET or POST; key via X-Sync-Key header OR ?key=... query param.
@router.api_route("/sync-all", methods=["GET", "POST"])
async def mail_sync_all(
    request: Request,
    db: Session = Depends(get_db),
):
    settings = get_settings()
    key = request.headers.get("X-Sync-Key") or request.query_params.get("key")
    if not settings.mail_sync_key or key != settings.mail_sync_key:
        raise HTTPException(403, "Bad or missing key — pass X-Sync-Key header or ?key=... query param")

    accounts = db.query(MailAccount).all()
    for acc in accounts:
        try:
            await sync_all_folders(db, acc)
            link_messages_to_activities(db, acc.user_id)
        except Exception as e:
            # Log error but continue processing others
            print(f"[mail_sync_all] Failed for {acc.mailbox}: {e}")

    return Response("OK", status_code=200)
