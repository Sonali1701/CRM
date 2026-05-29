"""
Fetch an Excel/Google Sheets file from a URL and run the existing
_sync_leads_from_excel logic. Called both from the background worker
and from the "Run Now" button.
"""
import hashlib
import json
import re
import threading
import time
from datetime import datetime, timezone

import httpx

from app.database import SessionLocal
from app.models.auto_sync import AutoSyncConfig


# ── URL helpers ───────────────────────────────────────────────────────────────

def resolve_download_url(url: str) -> str:
    """
    Convert cloud share URLs to direct download URLs.

    Supports:
      - Google Drive file share links  → direct download (with confirm skip)
      - Google Sheets share/edit links → xlsx export
      - Dropbox share links            → force-download variant
      - OneDrive share links           → embed→download transform
      - Plain direct URLs              → unchanged
    """
    url = url.strip()

    # Google Sheets: https://docs.google.com/spreadsheets/d/{ID}/...
    m = re.search(r"spreadsheets/d/([a-zA-Z0-9_-]+)", url)
    if m:
        return f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=xlsx"

    # Google Drive file: https://drive.google.com/file/d/{ID}/view...
    # Use uc?export=download which bypasses the confirmation page
    m = re.search(r"drive\.google\.com/file/d/([a-zA-Z0-9_-]+)", url)
    if m:
        return f"https://drive.google.com/uc?export=download&confirm=t&id={m.group(1)}"

    # Google Drive open link: https://drive.google.com/open?id={ID}
    m = re.search(r"drive\.google\.com/open\?id=([a-zA-Z0-9_-]+)", url)
    if m:
        return f"https://drive.google.com/uc?export=download&confirm=t&id={m.group(1)}"

    # Dropbox: change dl=0 → dl=1 (or add dl=1)
    if "dropbox.com" in url:
        url = re.sub(r"[?&]dl=0", "", url)
        sep = "&" if "?" in url else "?"
        return url + sep + "dl=1"

    # OneDrive personal share: https://1drv.ms/... or https://onedrive.live.com/...
    # These need a redirect follow — httpx handles it with follow_redirects=True
    # For OneDrive for Business / SharePoint direct links they work as-is
    return url


def fetch_excel_bytes(url: str) -> bytes:
    """Download file from URL, raise on error."""
    download_url = resolve_download_url(url)
    resp = httpx.get(download_url, timeout=30, follow_redirects=True)
    resp.raise_for_status()
    return resp.content


# ── Single sync run ───────────────────────────────────────────────────────────

def run_sync(config: AutoSyncConfig, db) -> dict:
    """
    Fetch the configured URL, parse the Excel file, run the sync,
    update config.last_synced_at / last_result / last_error.
    Returns the result dict. If file hash unchanged, skips sync.
    """
    from app.routers.imports import (
        _parse_excel_all_sheets, _sync_leads_from_excel, _SYNC_ALIASES, _auto_map,
    )

    try:
        raw = fetch_excel_bytes(config.url)
    except Exception as e:
        error_str = str(e)
        if "Content_Types" in error_str:
            error_str += " — the link returned HTML, not Excel. Check the share link is correct."
        config.last_error = f"Fetch failed: {error_str}"
        config.last_synced_at = datetime.now(timezone.utc)
        db.commit()
        return {}

    # Calculate file hash for idempotency
    file_hash = hashlib.sha256(raw).hexdigest()
    if config.last_file_hash == file_hash:
        # File unchanged since last sync
        summary = {"status": "unchanged", "created": 0, "updated": 0, "activities": 0, "skipped": 0}
        config.last_result = json.dumps(summary)
        config.last_error = None
        config.last_synced_at = datetime.now(timezone.utc)
        db.commit()
        return summary

    sheets = _parse_excel_all_sheets(raw)
    if isinstance(sheets, str):
        config.last_error = sheets
        config.last_synced_at = datetime.now(timezone.utc)
        db.commit()
        return {}

    sheet_name = config.sheet_name
    if sheet_name and sheet_name in sheets:
        headers, rows = sheets[sheet_name]
    elif sheets:
        # fallback to first sheet
        sheet_name, (headers, rows) = next(iter(sheets.items()))
    else:
        config.last_error = "No data sheets found in file."
        config.last_synced_at = datetime.now(timezone.utc)
        db.commit()
        return {}

    mapping = config.mapping_dict
    if not mapping:
        # auto-map on first run (no saved mapping yet)
        mapping = _auto_map(headers, _SYNC_ALIASES)

    result = _sync_leads_from_excel(rows, mapping, config.owner, db)
    summary = {
        "created": len(result.get("created", [])),
        "updated": len(result.get("updated", [])),
        "activities": len(result.get("activities", [])),
        "skipped": len(result.get("skipped", [])),
    }
    config.last_result = json.dumps(summary)
    config.last_error = None
    config.last_file_hash = file_hash
    config.last_synced_at = datetime.now(timezone.utc)
    db.commit()
    return summary


# ── Background worker ─────────────────────────────────────────────────────────

def _worker_loop():
    """Daemon thread: check every 60 s, run any configs that are due."""
    from datetime import timedelta

    while True:
        time.sleep(60)
        try:
            db = SessionLocal()
            try:
                configs = db.query(AutoSyncConfig).filter(
                    AutoSyncConfig.enabled == True  # noqa: E712
                ).all()
                now = datetime.now(timezone.utc)
                for cfg in configs:
                    due_after = (
                        cfg.last_synced_at + timedelta(hours=cfg.sync_interval_hours)
                        if cfg.last_synced_at
                        else now  # never synced → run immediately
                    )
                    if now >= due_after:
                        # eagerly mark so concurrent workers skip it
                        cfg.last_synced_at = now
                        db.commit()
                        try:
                            run_sync(cfg, db)
                        except Exception as e:
                            cfg.last_error = str(e)
                            cfg.last_synced_at = now
                            db.commit()
            finally:
                db.close()
        except Exception:
            pass  # never crash the daemon


def start_background_worker():
    t = threading.Thread(target=_worker_loop, daemon=True, name="auto-sync-worker")
    t.start()
