from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, Form, Request, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import require_user
from app.flash import flash, get_flash
from app.models import User
from app.models.daily_report import DailyReport
from app.templating import templates

router = APIRouter()


def _report_query(db: Session, user: User):
    q = db.query(DailyReport)
    if not user.is_manager:
        q = q.filter(DailyReport.user_id == user.id)
    return q


def _get_report(report_id: int, user: User, db: Session) -> DailyReport:
    q = db.query(DailyReport).filter(DailyReport.id == report_id)
    if not user.is_manager:
        q = q.filter(DailyReport.user_id == user.id)
    report = q.first()
    if not report:
        raise HTTPException(404, "Report not found")
    return report


@router.get("")
def daily_reports_list(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    q = _report_query(db, user)
    reports = q.order_by(DailyReport.report_date.desc()).all()
    return templates.TemplateResponse(request, "daily_reports/list.html", {
        "user": user, "flash": get_flash(request),
        "reports": reports,
    })


@router.get("/new")
def daily_report_new(request: Request, user: User = Depends(require_user)):
    return templates.TemplateResponse(request, "daily_reports/form.html", {
        "user": user, "report": None, "today": date.today(),
    })


@router.post("")
def daily_report_create(
    request: Request,
    report_date: str = Form(...),
    client_name: str = Form(""),
    accounts_worked: str = Form("0"),
    emails_sent: str = Form("0"),
    calls_dialed: str = Form("0"),
    meetings_set: str = Form("0"),
    meetings_attended: str = Form("0"),
    linkedin_requests_sent: str = Form("0"),
    linkedin_connections: str = Form("0"),
    important_conversations: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    try:
        rdate = datetime.strptime(report_date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(400, "Invalid date format")

    def _int(s: str, default: int = 0) -> int:
        try:
            return max(0, int(s.strip() or default))
        except ValueError:
            return default

    report = DailyReport(
        user_id=user.id,
        report_date=rdate,
        client_name=client_name.strip() or None,
        accounts_worked=_int(accounts_worked),
        emails_sent=_int(emails_sent),
        calls_dialed=_int(calls_dialed),
        meetings_set=_int(meetings_set),
        meetings_attended=_int(meetings_attended),
        linkedin_requests_sent=_int(linkedin_requests_sent),
        linkedin_connections=_int(linkedin_connections),
        important_conversations=important_conversations.strip() or None,
    )
    db.add(report)
    db.commit()
    return flash(RedirectResponse(f"/daily-reports/{report.id}", 303), "Daily report created.")


@router.get("/{report_id}")
def daily_report_detail(report_id: int, request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    report = _get_report(report_id, user, db)
    return templates.TemplateResponse(request, "daily_reports/detail.html", {
        "user": user, "flash": get_flash(request),
        "report": report,
    })


@router.get("/{report_id}/edit")
def daily_report_edit(report_id: int, request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    report = _get_report(report_id, user, db)
    return templates.TemplateResponse(request, "daily_reports/form.html", {
        "user": user, "report": report,
    })


@router.post("/{report_id}")
def daily_report_update(
    report_id: int,
    request: Request,
    report_date: str = Form(...),
    client_name: str = Form(""),
    accounts_worked: str = Form("0"),
    emails_sent: str = Form("0"),
    calls_dialed: str = Form("0"),
    meetings_set: str = Form("0"),
    meetings_attended: str = Form("0"),
    linkedin_requests_sent: str = Form("0"),
    linkedin_connections: str = Form("0"),
    important_conversations: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    report = _get_report(report_id, user, db)
    try:
        rdate = datetime.strptime(report_date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(400, "Invalid date format")

    def _int(s: str, default: int = 0) -> int:
        try:
            return max(0, int(s.strip() or default))
        except ValueError:
            return default

    report.report_date = rdate
    report.client_name = client_name.strip() or None
    report.accounts_worked = _int(accounts_worked)
    report.emails_sent = _int(emails_sent)
    report.calls_dialed = _int(calls_dialed)
    report.meetings_set = _int(meetings_set)
    report.meetings_attended = _int(meetings_attended)
    report.linkedin_requests_sent = _int(linkedin_requests_sent)
    report.linkedin_connections = _int(linkedin_connections)
    report.important_conversations = important_conversations.strip() or None
    report.updated_at = datetime.now(timezone.utc)
    db.commit()
    return flash(RedirectResponse(f"/daily-reports/{report_id}", 303), "Daily report updated.")


@router.post("/{report_id}/delete")
def daily_report_delete(report_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    report = _get_report(report_id, user, db)
    db.delete(report)
    db.commit()
    return flash(RedirectResponse("/daily-reports", 303), "Report deleted.", "error")
