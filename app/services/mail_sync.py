from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import MailAccount, EmailMessage, Activity, ActivityType, Deal, Client, Lead
from app.services.ms_graph_mail import ensure_account_access_token, graph_get, graph_post


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_message_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _clean_emails(value: list[dict[str, Any]]) -> str:
    return ";".join(e.get("emailAddress", {}).get("address", "") for e in value)


def _map_message_to_db(msg: dict[str, Any], mail_account_id: int, folder: str) -> dict[str, Any]:
    payload = {
        "mail_account_id": mail_account_id,
        "folder": folder,
        "provider_message_id": msg.get("id"),
        "internet_message_id": msg.get("internetMessageId"),
        "conversation_id": msg.get("conversationId"),
        "subject": msg.get("subject"),
        "body_preview": msg.get("bodyPreview"),
        "body_content": msg.get("body", {}).get("content"),
        "from_email": msg.get("from", {}).get("emailAddress", {}).get("address"),
        "to_emails": _clean_emails(msg.get("toRecipients", [])),
        "cc_emails": _clean_emails(msg.get("ccRecipients", [])),
        "is_inbound": folder != "sentItems",
        "sent_at": _parse_message_time(msg.get("sentDateTime")),
        "received_at": _parse_message_time(msg.get("receivedDateTime")),
    }
    return payload


async def sync_mail_folder(db: Session, account: MailAccount, folder: str = "inbox") -> None:
    token = await ensure_account_access_token(db, account)
    delta_link = account.inbox_delta_link if folder == "inbox" else account.sent_delta_link

    if not delta_link:
        # Initial sync: get first page and @odata.nextLink for delta
        initial = await graph_get(
            token,
            f"/me/mailFolders/{folder}/messages?$select=id,internetMessageId,conversationId,subject,bodyPreview,body,from,toRecipients,ccRecipients,sentDateTime,receivedDateTime&$orderby=receivedDateTime desc&$top=50"
        )
        messages = initial.get("value", [])
        delta_link = initial.get("@odata.nextLink")
    else:
        # Delta sync using stored link
        resp = await graph_get(token, delta_link)
        messages = resp.get("value", [])
        delta_link = resp.get("@odata.nextLink")

    added = 0
    for msg in messages:
        provider_id = msg.get("id")
        if not provider_id:
            continue
        existing = (
            db.query(EmailMessage)
            .filter_by(mail_account_id=account.id, provider_message_id=provider_id)
            .first()
        )
        if existing:
            continue

        payload = _map_message_to_db(msg, account.id, folder)
        db.add(EmailMessage(**payload))
        added += 1

    if folder == "inbox":
        account.inbox_delta_link = delta_link
    else:
        account.sent_delta_link = delta_link

    account.last_sync_at = _now()
    db.commit()


async def sync_all_folders(db: Session, account: MailAccount) -> None:
    await sync_mail_folder(db, account, folder="inbox")
    await sync_mail_folder(db, account, folder="sentItems")


def _match_to_crm_record(db: Session, message: EmailMessage) -> tuple[int | None, int | None, int | None]:
    client_id = None
    lead_id = None
    deal_id = None

    email = message.from_email or ""
    to = message.to_emails or ""

    # Try to match by email to leads
    lead = db.query(Lead).filter(Lead.email.ilike(email)).first()
    if lead:
        lead_id = lead.id

    # Try to match company email
    client = db.query(Client).filter(Client.email.ilike(email)).first()
    if client:
        client_id = client.id

    # If not matched, try to match by domain in to/from for company
    if not client_id and email:
        domain = email.split("@")[-1].lower()
        client_by_domain = db.query(Client).filter(Client.email.ilike(f"%@{domain}%")).first()
        if client_by_domain:
            client_id = client_by_domain.id

    # If linked to a client, optionally link to its open deals
    if client_id:
        open_deal = db.query(Deal).filter(
            Deal.client_id == client_id,
            Deal.stage.in_(["lead_generated", "qualified", "discovery_done", "requirement_received", "proposal_shared", "negotiation"]),
        ).first()
        if open_deal:
            deal_id = open_deal.id

    return client_id, lead_id, deal_id


def create_activity_for_message(db: Session, message: EmailMessage, user_id: int) -> Activity | None:
    if message.activity_id:
        return None

    client_id, lead_id, deal_id = _match_to_crm_record(db, message)

    activity = Activity(
        type=ActivityType.EMAIL,
        subject=message.subject or "(No subject)",
        body=message.body_preview or "",
        client_id=client_id,
        lead_id=lead_id,
        deal_id=deal_id,
        created_by_id=user_id,
    )
    db.add(activity)
    db.flush()
    message.activity_id = activity.id

    # Update deal last_activity_at if linked
    if deal_id:
        deal = db.get(Deal, deal_id)
        if deal:
            deal.last_activity_at = _now()

    return activity


def link_messages_to_activities(db: Session, user_id: int) -> None:
    # Link newly synced messages to activities if not already linked
    messages = (
        db.query(EmailMessage)
        .filter(EmailMessage.activity_id.is_(None))
        .filter(EmailMessage.created_at > _now() - timedelta(hours=24))
        .all()
    )
    for msg in messages:
        create_activity_for_message(db, msg, user_id)
    db.commit()


async def send_mail(db: Session, account: MailAccount, to: list[str], cc: list[str] | None, subject: str, body_html: str, client_id: int | None, lead_id: int | None, deal_id: int | None) -> str | None:
    token = await ensure_account_access_token(db, account)

    payload = {
        "message": {
            "subject": subject,
            "body": {"contentType": "HTML", "content": body_html},
            "toRecipients": [{"emailAddress": {"address": addr}} for addr in to],
        }
    }

    if cc:
        payload["message"]["ccRecipients"] = [{"emailAddress": {"address": addr}} for addr in cc]

    # Graph /me/sendMail returns 202 with no body; graph_post returns None for empty responses.
    await graph_post(token, "/me/sendMail", payload)
    return None
