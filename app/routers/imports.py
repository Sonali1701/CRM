"""
Smart import router — 2-step upload → map → import.
Supports CSV, XLSX (Excel), and Google Sheets CSV exports.
"""
import csv
import io
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any

import openpyxl
from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import require_user
from app.flash import get_flash, flash
from app.models import User, Lead, Client
from app.models.client import ClientType
from app.models.lead import LeadStatus
from app.templating import templates

router = APIRouter()

# ── In-memory session store (single-process; fine for Render single-worker) ──
_sessions: dict[str, dict] = {}
_SESSION_TTL = timedelta(minutes=30)


def _put_session(entity: str, headers: list[str], rows: list[dict]) -> str:
    sid = str(uuid.uuid4())
    _sessions[sid] = {
        "entity": entity,
        "headers": headers,
        "rows": rows,
        "expires": datetime.now(timezone.utc) + _SESSION_TTL,
    }
    _evict_old_sessions()
    return sid


def _get_session(sid: str) -> dict | None:
    s = _sessions.get(sid)
    if not s or s["expires"] < datetime.now(timezone.utc):
        _sessions.pop(sid, None)
        return None
    return s


def _evict_old_sessions():
    now = datetime.now(timezone.utc)
    expired = [k for k, v in _sessions.items() if v["expires"] < now]
    for k in expired:
        del _sessions[k]


# ── CRM field definitions ─────────────────────────────────────────────────────

LEAD_FIELDS = [
    {"key": "name",    "label": "Name",    "required": True},
    {"key": "company", "label": "Company", "required": False},
    {"key": "email",   "label": "Email",   "required": False},
    {"key": "phone",   "label": "Phone",   "required": False},
    {"key": "source",  "label": "Source",  "required": False},
    {"key": "notes",   "label": "Notes",   "required": False},
]

CLIENT_FIELDS = [
    {"key": "name",     "label": "Company Name", "required": True},
    {"key": "type",     "label": "Type (direct/msp/partner/other)", "required": False},
    {"key": "industry", "label": "Industry",     "required": False},
    {"key": "website",  "label": "Website",      "required": False},
    {"key": "notes",    "label": "Notes",        "required": False},
]

# Auto-mapping aliases: crm_field → [possible column names]
_LEAD_ALIASES: dict[str, list[str]] = {
    "name":    ["name", "full name", "fullname", "contact name", "lead name",
                "first name", "firstname", "person"],
    "company": ["company", "company name", "organization", "organisation",
                "account", "account name", "firm", "employer"],
    "email":   ["email", "email address", "e-mail", "mail", "email id"],
    "phone":   ["phone", "phone number", "mobile", "mobile number", "cell",
                "telephone", "contact number", "ph"],
    "source":  ["source", "lead source", "channel", "origin", "how did you hear"],
    "notes":   ["notes", "note", "comments", "comment", "description", "remarks"],
}

_CLIENT_ALIASES: dict[str, list[str]] = {
    "name":     ["name", "company", "company name", "account name", "organization",
                 "organisation", "firm", "client name"],
    "type":     ["type", "client type", "account type", "category", "tier"],
    "industry": ["industry", "sector", "vertical", "domain"],
    "website":  ["website", "url", "web", "site", "domain", "homepage"],
    "notes":    ["notes", "note", "comments", "description", "remarks"],
}


def _auto_map(headers: list[str], aliases: dict[str, list[str]]) -> dict[str, str]:
    """Return {crm_field: matched_header} best-effort auto-mapping."""
    normalised = {h.strip().lower(): h for h in headers}
    mapping = {}
    for field, candidates in aliases.items():
        for candidate in candidates:
            if candidate in normalised:
                mapping[field] = normalised[candidate]
                break
    return mapping


# ── File parsing ──────────────────────────────────────────────────────────────

MAX_BYTES = 5 * 1024 * 1024  # 5 MB


async def _parse_file(file: UploadFile) -> tuple[list[str], list[dict[str, Any]]] | str:
    """Return (headers, rows) or an error string."""
    raw = await file.read()
    if len(raw) > MAX_BYTES:
        return "File too large (max 5 MB)."

    fname = (file.filename or "").lower()

    if fname.endswith(".xlsx") or fname.endswith(".xls") or \
            file.content_type in ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                  "application/vnd.ms-excel"):
        return _parse_excel(raw)
    else:
        return _parse_csv(raw)


def _parse_excel(raw: bytes) -> tuple[list[str], list[dict]] | str:
    try:
        wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
        wb.close()
    except Exception as e:
        return f"Could not read Excel file: {e}"

    if not rows:
        return "Excel file is empty."

    headers = [str(c).strip() if c is not None else "" for c in rows[0]]
    headers = [h for h in headers if h]  # drop trailing empty cols
    if not headers:
        return "First row has no headers."

    data_rows = []
    for row in rows[1:]:
        values = [str(c).strip() if c is not None else "" for c in row]
        if not any(values):
            continue
        data_rows.append(dict(zip(headers, values)))

    return headers, data_rows


def _parse_csv(raw: bytes) -> tuple[list[str], list[dict]] | str:
    try:
        text = raw.decode("utf-8-sig", errors="replace")
    except Exception:
        return "Could not decode file as text."

    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel

    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    if not reader.fieldnames:
        return "CSV has no headers."

    headers = [h.strip() for h in reader.fieldnames if h and h.strip()]
    rows = []
    for row in reader:
        cleaned = {k.strip(): (v or "").strip() for k, v in row.items() if k and k.strip()}
        if not any(cleaned.values()):
            continue
        rows.append(cleaned)

    return headers, rows


# ── Template downloads ────────────────────────────────────────────────────────

@router.get("/leads/template.csv")
def leads_template():
    lines = ["name,company,email,phone,source,notes",
             "John Smith,Acme Corp,john@acme.com,555-1234,LinkedIn,Met at conference"]
    return StreamingResponse(io.BytesIO("\n".join(lines).encode()),
                             media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=leads_template.csv"})


@router.get("/clients/template.csv")
def clients_template():
    lines = ["name,type,industry,website,notes",
             "Acme Healthcare,direct,Healthcare,https://acme.com,Key account",
             "MedForce MSP,msp,Staffing,https://medforce.com,"]
    return StreamingResponse(io.BytesIO("\n".join(lines).encode()),
                             media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=clients_template.csv"})


# ── STEP 1 — Upload pages ─────────────────────────────────────────────────────

@router.get("/leads/import")
def leads_upload_page(request: Request, user: User = Depends(require_user)):
    return templates.TemplateResponse(request, "imports/upload.html", {
        "user": user, "flash": get_flash(request),
        "entity": "leads", "entity_label": "Leads",
        "back_url": "/leads",
        "fields": LEAD_FIELDS,
    })


@router.get("/clients/import")
def clients_upload_page(request: Request, user: User = Depends(require_user)):
    return templates.TemplateResponse(request, "imports/upload.html", {
        "user": user, "flash": get_flash(request),
        "entity": "clients", "entity_label": "Clients",
        "back_url": "/clients",
        "fields": CLIENT_FIELDS,
    })


@router.post("/leads/import")
async def leads_upload(
    request: Request,
    file: UploadFile = File(...),
    user: User = Depends(require_user),
):
    result = await _parse_file(file)
    if isinstance(result, str):
        return templates.TemplateResponse(request, "imports/upload.html", {
            "user": user, "entity": "leads", "entity_label": "Leads",
            "back_url": "/leads", "fields": LEAD_FIELDS, "error": result,
        }, status_code=400)
    headers, rows = result
    sid = _put_session("leads", headers, rows)
    return RedirectResponse(f"/import/leads/map/{sid}", status_code=303)


@router.post("/clients/import")
async def clients_upload(
    request: Request,
    file: UploadFile = File(...),
    user: User = Depends(require_user),
):
    result = await _parse_file(file)
    if isinstance(result, str):
        return templates.TemplateResponse(request, "imports/upload.html", {
            "user": user, "entity": "clients", "entity_label": "Clients",
            "back_url": "/clients", "fields": CLIENT_FIELDS, "error": result,
        }, status_code=400)
    headers, rows = result
    sid = _put_session("clients", headers, rows)
    return RedirectResponse(f"/import/clients/map/{sid}", status_code=303)


# ── STEP 2 — Mapping pages ────────────────────────────────────────────────────

@router.get("/leads/map/{sid}")
def leads_map_page(sid: str, request: Request, user: User = Depends(require_user)):
    session = _get_session(sid)
    if not session:
        return flash(RedirectResponse("/import/leads/import", 303), "Session expired — please re-upload.", "error")
    auto = _auto_map(session["headers"], _LEAD_ALIASES)
    return templates.TemplateResponse(request, "imports/map.html", {
        "user": user, "sid": sid, "entity": "leads", "entity_label": "Leads",
        "fields": LEAD_FIELDS, "file_headers": session["headers"],
        "auto_map": auto, "preview": session["rows"][:3],
        "total_rows": len(session["rows"]),
        "back_url": "/import/leads/import",
        "confirm_url": f"/import/leads/confirm/{sid}",
    })


@router.get("/clients/map/{sid}")
def clients_map_page(sid: str, request: Request, user: User = Depends(require_user)):
    session = _get_session(sid)
    if not session:
        return flash(RedirectResponse("/import/clients/import", 303), "Session expired — please re-upload.", "error")
    auto = _auto_map(session["headers"], _CLIENT_ALIASES)
    return templates.TemplateResponse(request, "imports/map.html", {
        "user": user, "sid": sid, "entity": "clients", "entity_label": "Clients",
        "fields": CLIENT_FIELDS, "file_headers": session["headers"],
        "auto_map": auto, "preview": session["rows"][:3],
        "total_rows": len(session["rows"]),
        "back_url": "/import/clients/import",
        "confirm_url": f"/import/clients/confirm/{sid}",
    })


# ── STEP 3 — Confirm + import ─────────────────────────────────────────────────

@router.post("/leads/confirm/{sid}")
async def leads_confirm(
    sid: str, request: Request,
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    session = _get_session(sid)
    if not session:
        return flash(RedirectResponse("/import/leads/import", 303), "Session expired — please re-upload.", "error")
    form = await request.form()
    mapping = {f["key"]: form.get(f"map_{f['key']}", "") for f in LEAD_FIELDS}
    results = _import_leads(session["rows"], mapping, user, db)
    del _sessions[sid]
    return templates.TemplateResponse(request, "imports/results.html", {
        "user": user, "entity": "Leads", "back_url": "/leads",
        "imported": results["imported"], "skipped": results["skipped"],
    })


@router.post("/clients/confirm/{sid}")
async def clients_confirm(
    sid: str, request: Request,
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    session = _get_session(sid)
    if not session:
        return flash(RedirectResponse("/import/clients/import", 303), "Session expired — please re-upload.", "error")
    form = await request.form()
    mapping = {f["key"]: form.get(f"map_{f['key']}", "") for f in CLIENT_FIELDS}
    results = _import_clients(session["rows"], mapping, user, db)
    del _sessions[sid]
    return templates.TemplateResponse(request, "imports/results.html", {
        "user": user, "entity": "Clients", "back_url": "/clients",
        "imported": results["imported"], "skipped": results["skipped"],
    })


# ── Import logic ──────────────────────────────────────────────────────────────

def _get_val(row: dict, col: str) -> str:
    return row.get(col, "").strip() if col else ""


def _import_leads(rows: list[dict], mapping: dict, user: User, db: Session) -> dict:
    imported, skipped = [], []
    name_col = mapping.get("name", "")

    for i, row in enumerate(rows, start=2):
        name = _get_val(row, name_col)
        if not name:
            skipped.append({"row": i, "data": row, "reason": "Name is empty"})
            continue
        lead = Lead(
            name=name,
            company=_get_val(row, mapping.get("company", "")) or None,
            email=_get_val(row, mapping.get("email", "")) or None,
            phone=_get_val(row, mapping.get("phone", "")) or None,
            source=_get_val(row, mapping.get("source", "")) or None,
            notes=_get_val(row, mapping.get("notes", "")) or None,
            status=LeadStatus.NEW,
            owner_id=user.id,
        )
        db.add(lead)
        imported.append({"row": i, "name": name, "company": _get_val(row, mapping.get("company", ""))})

    db.commit()
    return {"imported": imported, "skipped": skipped}


VALID_CLIENT_TYPES = {t.value for t in ClientType}


def _import_clients(rows: list[dict], mapping: dict, user: User, db: Session) -> dict:
    imported, skipped = [], []
    name_col = mapping.get("name", "")

    for i, row in enumerate(rows, start=2):
        name = _get_val(row, name_col)
        if not name:
            skipped.append({"row": i, "data": row, "reason": "Name is empty"})
            continue

        type_raw = _get_val(row, mapping.get("type", "")).lower() or "direct"
        if type_raw not in VALID_CLIENT_TYPES:
            type_raw = "direct"

        if db.query(Client).filter(Client.name == name).first():
            skipped.append({"row": i, "data": row, "reason": f"'{name}' already exists"})
            continue

        client = Client(
            name=name,
            type=ClientType(type_raw),
            industry=_get_val(row, mapping.get("industry", "")) or None,
            website=_get_val(row, mapping.get("website", "")) or None,
            notes=_get_val(row, mapping.get("notes", "")) or None,
            owner_id=user.id,
        )
        db.add(client)
        imported.append({"row": i, "name": name, "type": type_raw})

    db.commit()
    return {"imported": imported, "skipped": skipped}
