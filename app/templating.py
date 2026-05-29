from datetime import datetime, timezone

from fastapi.templating import Jinja2Templates

from app.models.deal import STAGE_LABELS, PIPELINE_STAGES, OPEN_STAGES, DealStage
from app.flash import get_flash

templates = Jinja2Templates(directory="app/templates")


def _format_currency(value, currency: str = "USD") -> str:
    if value is None:
        return ""
    try:
        return f"{currency} {float(value):,.2f}"
    except (TypeError, ValueError):
        return str(value)


def _format_date(value) -> str:
    if not value:
        return "—"
    if isinstance(value, datetime):
        return value.strftime("%b %d, %Y")
    return value.strftime("%b %d, %Y")


def _humanize_status(value) -> str:
    if value is None:
        return ""
    s = value.value if hasattr(value, "value") else str(value)
    return s.replace("_", " ").title()


def _days_since(value) -> int | None:
    if not value:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - value).days


templates.env.filters["currency"] = _format_currency
templates.env.filters["date"] = _format_date
templates.env.filters["humanize"] = _humanize_status
templates.env.filters["days_since"] = _days_since

templates.env.globals["STAGE_LABELS"] = STAGE_LABELS
templates.env.globals["PIPELINE_STAGES"] = PIPELINE_STAGES
templates.env.globals["OPEN_STAGES"] = OPEN_STAGES
templates.env.globals["DealStage"] = DealStage
templates.env.globals["get_flash"] = get_flash
templates.env.globals["now"] = lambda: datetime.now(timezone.utc).timestamp()
