"""
Auto-sync: schedule periodic Excel pulls from a cloud URL (Google Drive,
Dropbox, OneDrive, or any direct .xlsx link).

Setup wizard:
  GET/POST  /auto-sync/setup          — step 1: paste URL, fetch sheets
  GET/POST  /auto-sync/setup/map/{sid}— step 2: map columns, save config
  GET       /auto-sync                — list all configs
  POST      /auto-sync/{id}/run       — manual trigger
  POST      /auto-sync/{id}/toggle    — enable / disable
  POST      /auto-sync/{id}/delete    — delete
"""
import json
import uuid

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import require_user
from app.flash import flash, get_flash
from app.models import User
from app.models.auto_sync import AutoSyncConfig
from app.routers.imports import (
    SYNC_LEAD_FIELDS, _SYNC_ALIASES, _auto_map, _parse_excel_all_sheets,
)
from app.services.sheet_sync import fetch_excel_bytes, run_sync
from app.templating import templates

router = APIRouter(tags=["auto-sync"])

# ── in-memory setup session (same pattern as import wizard) ───────────────────
_SESSIONS: dict[str, dict] = {}


# ── List ──────────────────────────────────────────────────────────────────────

@router.get("/auto-sync")
def auto_sync_list(request: Request, user: User = Depends(require_user),
                   db: Session = Depends(get_db)):
    configs = db.query(AutoSyncConfig).filter(
        AutoSyncConfig.owner_id == user.id
    ).order_by(AutoSyncConfig.created_at.desc()).all()
    return templates.TemplateResponse(request, "auto_sync/list.html", {
        "user": user, "configs": configs, "flash": get_flash(request),
    })


# ── Setup step 1: URL input ───────────────────────────────────────────────────

@router.get("/auto-sync/setup")
def auto_sync_setup_page(request: Request, user: User = Depends(require_user)):
    return templates.TemplateResponse(request, "auto_sync/setup_url.html", {
        "user": user, "error": None,
    })


@router.post("/auto-sync/setup")
async def auto_sync_setup_post(
    request: Request,
    url: str = Form(...),
    name: str = Form(...),
    sync_interval_hours: int = Form(1),
    user: User = Depends(require_user),
):
    url = url.strip()
    if not url:
        return templates.TemplateResponse(request, "auto_sync/setup_url.html", {
            "user": user, "error": "Please enter a URL.",
        })

    try:
        raw = fetch_excel_bytes(url)
    except Exception as e:
        error_msg = str(e)
        hint = ""
        if "Content_Types" in error_msg:
            hint = " (The link returned HTML instead of Excel. For Google Drive: right-click file → Share → Anyone with link can view → copy link. Make sure the link ends with /view, not just an ID.)"
        elif "404" in error_msg:
            hint = " (File not found. Check the link is correct and still shared.)"
        return templates.TemplateResponse(request, "auto_sync/setup_url.html", {
            "user": user,
            "error": f"Could not fetch the file: {e}.{hint}",
        })

    sheets = _parse_excel_all_sheets(raw)
    if isinstance(sheets, str):
        return templates.TemplateResponse(request, "auto_sync/setup_url.html", {
            "user": user, "error": sheets,
        })

    # if only one sheet, skip sheet selection
    sheet_names = list(sheets.keys())
    if len(sheet_names) == 1:
        chosen = sheet_names[0]
        headers, rows = sheets[chosen]
        auto_map = _auto_map(headers, _SYNC_ALIASES)
        preview = rows[:3]
        sid = str(uuid.uuid4())
        _SESSIONS[sid] = {
            "url": url, "name": name, "interval": sync_interval_hours,
            "sheet_name": chosen, "headers": headers,
            "auto_map": auto_map, "preview": preview,
        }
        return RedirectResponse(f"/auto-sync/setup/map/{sid}", status_code=303)

    # multiple sheets — let user pick
    sheet_info = [
        {"name": n, "count": len(sheets[n][1]), "headers": sheets[n][0][:6]}
        for n in sheet_names
    ]
    sid = str(uuid.uuid4())
    _SESSIONS[sid] = {
        "url": url, "name": name, "interval": sync_interval_hours,
        "sheets_data": {n: (h, r) for n, (h, r) in sheets.items()},
    }
    return templates.TemplateResponse(request, "auto_sync/setup_sheets.html", {
        "user": user, "sheets": sheet_info, "sid": sid, "error": None,
    })


@router.post("/auto-sync/setup/sheets/{sid}")
async def auto_sync_pick_sheet(
    request: Request,
    sid: str,
    sheet: str = Form(...),
    user: User = Depends(require_user),
):
    session = _SESSIONS.get(sid)
    if not session:
        return RedirectResponse("/auto-sync/setup", status_code=303)

    sheets_data = session.get("sheets_data", {})
    if sheet not in sheets_data:
        return RedirectResponse("/auto-sync/setup", status_code=303)

    headers, rows = sheets_data[sheet]
    auto_map = _auto_map(headers, _SYNC_ALIASES)
    session["sheet_name"] = sheet
    session["headers"] = headers
    session["auto_map"] = auto_map
    session["preview"] = rows[:3]
    return RedirectResponse(f"/auto-sync/setup/map/{sid}", status_code=303)


# ── Setup step 2: column mapping ──────────────────────────────────────────────

@router.get("/auto-sync/setup/map/{sid}")
def auto_sync_map_page(request: Request, sid: str, user: User = Depends(require_user)):
    session = _SESSIONS.get(sid)
    if not session:
        return RedirectResponse("/auto-sync/setup", status_code=303)
    return templates.TemplateResponse(request, "auto_sync/setup_map.html", {
        "user": user,
        "sid": sid,
        "name": session["name"],
        "sheet_name": session["sheet_name"],
        "interval": session["interval"],
        "fields": SYNC_LEAD_FIELDS,
        "file_headers": session["headers"],
        "auto_map": session["auto_map"],
        "preview": session["preview"],
    })


@router.post("/auto-sync/setup/map/{sid}")
async def auto_sync_map_save(
    request: Request,
    sid: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    session = _SESSIONS.get(sid)
    if not session:
        return RedirectResponse("/auto-sync/setup", status_code=303)

    form = await request.form()
    mapping = {
        f["key"]: form.get(f"map_{f['key']}", "")
        for f in SYNC_LEAD_FIELDS
        if form.get(f"map_{f['key']}", "")
    }

    config = AutoSyncConfig(
        name=session["name"],
        url=session["url"],
        sheet_name=session["sheet_name"],
        column_mapping=json.dumps(mapping),
        sync_interval_hours=session["interval"],
        enabled=True,
        owner_id=user.id,
    )
    db.add(config)
    db.commit()
    _SESSIONS.pop(sid, None)
    return flash(RedirectResponse("/auto-sync", status_code=303),
                 f'Auto-sync "{config.name}" saved. First sync will run within 1 minute.')


# ── Actions ───────────────────────────────────────────────────────────────────

@router.post("/auto-sync/{config_id}/run")
def auto_sync_run_now(
    request: Request,
    config_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    config = db.query(AutoSyncConfig).filter(
        AutoSyncConfig.id == config_id,
        AutoSyncConfig.owner_id == user.id,
    ).first()
    if not config:
        return RedirectResponse("/auto-sync", status_code=303)

    try:
        summary = run_sync(config, db)
        msg = (f"Sync done - {summary.get('created', 0)} created, "
               f"{summary.get('updated', 0)} updated, "
               f"{summary.get('activities', 0)} activities.")
    except Exception as e:
        msg = f"Sync failed: {e}"
    return flash(RedirectResponse("/auto-sync", status_code=303), msg)


@router.post("/auto-sync/{config_id}/toggle")
def auto_sync_toggle(
    request: Request,
    config_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    config = db.query(AutoSyncConfig).filter(
        AutoSyncConfig.id == config_id,
        AutoSyncConfig.owner_id == user.id,
    ).first()
    if config:
        config.enabled = not config.enabled
        db.commit()
    return RedirectResponse("/auto-sync", status_code=303)


@router.post("/auto-sync/{config_id}/delete")
def auto_sync_delete(
    request: Request,
    config_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    config = db.query(AutoSyncConfig).filter(
        AutoSyncConfig.id == config_id,
        AutoSyncConfig.owner_id == user.id,
    ).first()
    if config:
        db.delete(config)
        db.commit()
        return flash(RedirectResponse("/auto-sync", status_code=303),
                     f'Auto-sync "{config.name}" deleted.')
    return RedirectResponse("/auto-sync", status_code=303)
