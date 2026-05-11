from __future__ import annotations

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
from app.templating import templates


router = APIRouter()


@router.get("")
def mail_inbox(
    request: Request,
    folder: str = "inbox",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    account = (
        db.query(MailAccount)
        .filter_by(user_id=user.id, provider="microsoft_graph")
        .first()
    )
    messages = []
    if account:
        messages = (
            db.query(EmailMessage)
            .filter_by(mail_account_id=account.id, folder=folder)
            .order_by(EmailMessage.received_at.desc(), EmailMessage.sent_at.desc())
            .limit(100)
            .all()
        )
    return templates.TemplateResponse(request, "mail/inbox.html", {
        "user": user,
        "flash": get_flash(request),
        "account": account,
        "messages": messages,
        "folder": folder,
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
        raise HTTPException(400, "Connect your Outlook mailbox first")

    to_list = [addr.strip() for addr in to.split(";") if addr.strip()]
    cc_list = [addr.strip() for addr in cc.split(";") if addr.strip()] if cc else None

    cid = int(client_id) if client_id.strip().isdigit() else None
    lid = int(lead_id) if lead_id.strip().isdigit() else None
    did = int(deal_id) if deal_id.strip().isdigit() else None

    await send_mail(db, account, to_list, cc_list, subject, body_html, cid, lid, did)

    # Trigger a quick sync of sent items so the email appears in CRM soon
    from app.services.mail_sync import sync_mail_folder
    import asyncio
    asyncio.create_task(sync_mail_folder(db, account, folder="sentItems"))

    return flash(RedirectResponse(redirect_to, 303), "Email sent")


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


# Public endpoint for uptime/automation; protected by a shared key for security
@router.post("/sync-all")
async def mail_sync_all(
    request: Request,
    db: Session = Depends(get_db),
):
    settings = get_settings()
    key = request.headers.get("X-Sync-Key")
    if not settings.mail_sync_key or key != settings.mail_sync_key:
        raise HTTPException(403)

    accounts = db.query(MailAccount).all()
    for acc in accounts:
        try:
            await sync_all_folders(db, acc)
            link_messages_to_activities(db, acc.user_id)
        except Exception as e:
            # Log error but continue processing others
            print(f"[mail_sync_all] Failed for {acc.mailbox}: {e}")

    return Response("OK", status_code=200)
