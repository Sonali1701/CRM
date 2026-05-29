"""Reporting dashboard: pipeline value by stage, win rate, rep leaderboard,
conversion funnel, recent activity volume.

Managers see org-wide numbers; reps see only their own."""

from datetime import datetime, timedelta, timezone, date
from collections import defaultdict

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import require_user
from app.flash import get_flash
from app.models import User, Deal, Activity, Lead, Client
from app.models.deal import DealStage, OPEN_STAGES, STAGE_LABELS
from app.models.activity import ActivityType
from app.models.lead import LeadStatus
from app.templating import templates


router = APIRouter()


# Stage-based win probability used for revenue forecasting when a deal
# doesn't have its own per-deal probability set.
_STAGE_PROBABILITY = {
    DealStage.LEAD_GENERATED: 5,
    DealStage.QUALIFIED: 15,
    DealStage.DISCOVERY_DONE: 30,
    DealStage.REQUIREMENT_RECEIVED: 45,
    DealStage.PROPOSAL_SHARED: 60,
    DealStage.NEGOTIATION: 80,
}


def _deal_probability(d: Deal) -> int:
    """Use the per-deal probability if the user set one; otherwise fall back
    to the stage-based default."""
    if d.probability and d.probability > 0:
        return int(d.probability)
    return _STAGE_PROBABILITY.get(d.stage, 0)


@router.get("/reports")
async def reports(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    thirty_days_ago = now - timedelta(days=30)

    # Scope: managers see all, reps see only their own
    deals_q = db.query(Deal)
    acts_q = db.query(Activity)
    leads_q = db.query(Lead)
    if not user.is_manager:
        deals_q = deals_q.filter(Deal.owner_id == user.id)
        acts_q = acts_q.filter(Activity.created_by_id == user.id)
        leads_q = leads_q.filter(Lead.owner_id == user.id)

    all_deals = deals_q.all()
    open_deals = [d for d in all_deals if d.stage in OPEN_STAGES]
    def _aware(dt):
        if dt is None:
            return None
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt

    won = [d for d in all_deals if d.stage == DealStage.CLOSED_WON]
    lost = [d for d in all_deals if d.stage == DealStage.CLOSED_LOST]
    won_this_month = [d for d in won if _aware(d.updated_at) and _aware(d.updated_at) >= month_start]

    pipeline_value = sum(float(d.value or 0) for d in open_deals)
    won_value_total = sum(float(d.value or 0) for d in won)
    won_value_month = sum(float(d.value or 0) for d in won_this_month)

    closed_total = len(won) + len(lost)
    win_rate = (len(won) / closed_total * 100) if closed_total else 0

    # Pipeline by stage (count + value)
    by_stage = []
    max_stage_value = 1
    for stage in OPEN_STAGES:
        ds = [d for d in open_deals if d.stage == stage]
        v = sum(float(d.value or 0) for d in ds)
        max_stage_value = max(max_stage_value, v)
        by_stage.append({
            "label": STAGE_LABELS[stage],
            "stage": stage.value,
            "count": len(ds),
            "value": v,
        })
    for row in by_stage:
        row["pct"] = int(row["value"] / max_stage_value * 100) if max_stage_value else 0

    # Funnel: leads → qualified → won (respecting user scope)
    all_leads = leads_q.all()
    lead_counts = {
        "total_leads": len(all_leads),
        "qualified": len([l for l in all_leads if l.status == LeadStatus.QUALIFIED]),
        "converted": len([l for l in all_leads if l.status == LeadStatus.CONVERTED]),
        "won_deals": len(won),
    }

    # Rep leaderboard (managers only) — open pipeline, won deals (count + value), activities (30d)
    rep_rows = []
    if user.is_manager:
        reps = db.query(User).filter(User.is_active == True).all()
        # Group deals by owner
        deals_by_owner = defaultdict(list)
        for d in all_deals:
            if d.owner_id:
                deals_by_owner[d.owner_id].append(d)
        # Activity counts last 30 days
        rows = (
            db.query(Activity.created_by_id, func.count(Activity.id))
            .filter(Activity.created_at >= thirty_days_ago)
            .group_by(Activity.created_by_id)
            .all()
        )
        act_count_by_user = {row[0]: row[1] for row in rows if row[0]}
        for rep in reps:
            owned = deals_by_owner.get(rep.id, [])
            owned_open = [d for d in owned if d.stage in OPEN_STAGES]
            owned_won = [d for d in owned if d.stage == DealStage.CLOSED_WON]
            rep_rows.append({
                "rep": rep,
                "pipeline_value": sum(float(d.value or 0) for d in owned_open),
                "open_count": len(owned_open),
                "won_count": len(owned_won),
                "won_value": sum(float(d.value or 0) for d in owned_won),
                "activity_30d": act_count_by_user.get(rep.id, 0),
            })
        rep_rows.sort(key=lambda r: r["pipeline_value"], reverse=True)

    # Activity volume by type (last 30 days)
    recent_acts = acts_q.filter(Activity.created_at >= thirty_days_ago).all()
    activity_by_type = {t.value: 0 for t in ActivityType}
    for a in recent_acts:
        activity_by_type[a.type.value] = activity_by_type.get(a.type.value, 0) + 1

    # AI narration — best-effort, never blocks the page if AI is down or unconfigured
    narration = None
    from app.services.ai_compose import is_ai_configured, narrate_pipeline
    if is_ai_configured():
        try:
            metrics_for_ai = {
                "Open pipeline value": f"{int(pipeline_value):,}",
                "Open deal count": len(open_deals),
                "Won this month value": f"{int(won_value_month):,}",
                "Won deals (all time)": len(won),
                "Lost deals (all time)": len(lost),
                "Win rate %": f"{win_rate:.0f}",
                "Pipeline by stage (count + value)": ", ".join(
                    f"{s['label']} = {s['count']} deals / {int(s['value']):,}" for s in by_stage
                ),
                "Activities last 30d": len(recent_acts),
                "Email vs call vs meeting (30d)": (
                    f"{activity_by_type.get('email', 0)} email · "
                    f"{activity_by_type.get('call', 0)} call · "
                    f"{activity_by_type.get('meeting', 0)} meeting"
                ),
                "Leads in funnel": lead_counts["total_leads"],
                "Qualified leads": lead_counts["qualified"],
            }
            narration = await narrate_pipeline(metrics_for_ai, db=db)
        except Exception as e:
            print(f"[reports] narration failed: {e}")

    # Revenue forecast: probability-weighted open pipeline by close month
    forecast_rows: list[dict] = []
    forecast_buckets: dict[str, dict] = {}
    forecast_unscheduled = {"label": "No close date", "value": 0.0, "weighted": 0.0, "count": 0}
    for d in open_deals:
        val = float(d.value or 0)
        prob = _deal_probability(d)
        weighted = val * prob / 100.0
        if d.expected_close_date:
            key = d.expected_close_date.strftime("%Y-%m")
            label = d.expected_close_date.strftime("%b %Y")
            b = forecast_buckets.setdefault(key, {"label": label, "value": 0.0, "weighted": 0.0, "count": 0})
            b["value"] += val
            b["weighted"] += weighted
            b["count"] += 1
        else:
            forecast_unscheduled["value"] += val
            forecast_unscheduled["weighted"] += weighted
            forecast_unscheduled["count"] += 1
    for key in sorted(forecast_buckets.keys()):
        forecast_rows.append(forecast_buckets[key])
    if forecast_unscheduled["count"]:
        forecast_rows.append(forecast_unscheduled)
    forecast_total_weighted = sum(r["weighted"] for r in forecast_rows)

    # Industry heatmap: open pipeline and win count by industry
    industry_stats: dict[str, dict] = {}
    for d in all_deals:
        ind = (d.client.industry if d.client and d.client.industry else "Unknown")
        s = industry_stats.setdefault(ind, {"industry": ind, "open_value": 0.0, "open_count": 0, "won_count": 0, "won_value": 0.0, "lost_count": 0})
        if d.stage in OPEN_STAGES:
            s["open_value"] += float(d.value or 0)
            s["open_count"] += 1
        elif d.stage == DealStage.CLOSED_WON:
            s["won_value"] += float(d.value or 0)
            s["won_count"] += 1
        elif d.stage == DealStage.CLOSED_LOST:
            s["lost_count"] += 1
    industry_rows = sorted(industry_stats.values(), key=lambda r: -r["open_value"])
    max_industry_value = max((r["open_value"] for r in industry_rows), default=1) or 1
    for r in industry_rows:
        r["pct"] = int(r["open_value"] / max_industry_value * 100) if max_industry_value else 0
        closed = r["won_count"] + r["lost_count"]
        r["win_rate"] = (r["won_count"] / closed * 100) if closed else 0

    # Pipeline monitoring: stalled deals (no activity in 14+ days) + inactive
    # accounts (clients with open deals but no activity in 30+ days)
    fourteen_days_ago = now - timedelta(days=14)
    thirty_days_ago_for_acct = now - timedelta(days=30)
    stalled_deals = []
    for d in open_deals:
        last = _aware(d.last_activity_at)
        if last is None or last < fourteen_days_ago:
            stalled_deals.append({
                "deal": d,
                "last_activity_at": last,
                "days_quiet": (now - last).days if last else None,
            })
    stalled_deals.sort(key=lambda r: (r["days_quiet"] if r["days_quiet"] is not None else 9999), reverse=True)
    stalled_deals = stalled_deals[:15]

    # Inactive accounts: clients with at least one open deal whose most-recent
    # activity (any type) is older than 30 days or absent.
    client_ids_with_open = {d.client_id for d in open_deals if d.client_id}
    inactive_accounts: list[dict] = []
    if client_ids_with_open:
        last_act_rows = (
            db.query(Activity.client_id, func.max(Activity.created_at))
            .filter(Activity.client_id.in_(client_ids_with_open))
            .group_by(Activity.client_id)
            .all()
        )
        last_by_client = {row[0]: row[1] for row in last_act_rows}
        for cid in client_ids_with_open:
            last = _aware(last_by_client.get(cid))
            if last is None or last < thirty_days_ago_for_acct:
                client = db.get(Client, cid)
                if client:
                    inactive_accounts.append({
                        "client": client,
                        "last_activity_at": last,
                        "days_quiet": (now - last).days if last else None,
                    })
        inactive_accounts.sort(key=lambda r: (r["days_quiet"] if r["days_quiet"] is not None else 9999), reverse=True)
        inactive_accounts = inactive_accounts[:10]

    return templates.TemplateResponse(request, "reports/index.html", {
        "user": user,
        "flash": get_flash(request),
        "pipeline_value": pipeline_value,
        "won_value_total": won_value_total,
        "won_value_month": won_value_month,
        "won_count": len(won),
        "lost_count": len(lost),
        "open_count": len(open_deals),
        "win_rate": win_rate,
        "by_stage": by_stage,
        "lead_counts": lead_counts,
        "rep_rows": rep_rows,
        "activity_by_type": activity_by_type,
        "activity_total_30d": len(recent_acts),
        "narration": narration,
        "forecast_rows": forecast_rows,
        "forecast_total_weighted": forecast_total_weighted,
        "industry_rows": industry_rows,
        "stalled_deals": stalled_deals,
        "inactive_accounts": inactive_accounts,
    })
