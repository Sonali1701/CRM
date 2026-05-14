"""AI composer with automatic provider fallback.

Two free providers are supported. Configure one OR both — calls go to
Gemini first; on rate-limit / quota errors the chain falls back to Groq.

  - Gemini    (https://aistudio.google.com/app/apikey)   15 RPM, 1500 RPD
  - Groq      (https://console.groq.com/keys)            ~14,400 RPD, very fast

All structured outputs (email templates, MOMs, JD extracts, deal next-step
suggestions) go through the same _structured_call() entrypoint so they
share the same provider fallback chain.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from app.config import get_settings


GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

# Error fragments that mean "this provider is rate-limited; try the next one"
_QUOTA_ERROR_HINTS = ("429", "rate limit", "rate_limit", "quota", "exhausted", "exceeded", "resource_exhausted")

SYSTEM_PROMPT = """You are a sales-email assistant for a B2B CRM. The user will describe what they want; you return ONE email template (subject + body) that the CRM will personalize per recipient.

CRITICAL: Follow the user's prompt precisely. If they ask for a 3-line email, write 3 lines. If they specify a hook or angle, use it. Your job is to execute their intent, not invent your own.

Rules:
- Use these placeholders in the output where appropriate — they are replaced per contact at send time: {{first_name}}, {{name}}, {{company}}, {{title}}, {{email}}
- Keep it concise: typically 3-6 short sentences unless the user asks otherwise.
- Friendly, direct, professional. No exclamation marks, no emojis, no "I hope this finds you well", no overselling.
- Subject should be under 60 characters and look like a real human wrote it.
- If an email signature is provided in the "About us" block, end with that exact signature. Otherwise sign off with "Best," then a new line then the sender's name if given, else "[Your name]".
- If "About us" context is provided, write as that company and accurately reference its real services.
- If "Recent saved templates" are provided, match their voice and rhythm — do not copy phrasing.
- If "Previous emails already sent to this recipient" are provided, do NOT repeat the same opening or pitch — build on prior context.
- Return ONLY a JSON object with keys "subject" and "body". The body should be plain text, line breaks as \\n.
"""


def is_gemini_configured() -> bool:
    return bool(get_settings().gemini_api_key)


def is_groq_configured() -> bool:
    return bool(get_settings().groq_api_key)


def is_ai_configured() -> bool:
    """True if at least one provider is configured."""
    s = get_settings()
    return bool(s.gemini_api_key or s.groq_api_key)


def _is_quota_error(err: Exception) -> bool:
    msg = str(err).lower()
    return any(h in msg for h in _QUOTA_ERROR_HINTS)


async def fetch_company_context(website: str) -> str:
    """Fetch the company's homepage and return a short text excerpt for the prompt.
    Returns an empty string on any error — the AI call still proceeds without it."""
    if not website:
        return ""
    url = website if website.startswith(("http://", "https://")) else f"https://{website}"
    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=6,
            headers={"User-Agent": "Mozilla/5.0 (compatible; RadixsolCRM/1.0)"},
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        # Prefer meta description, then first ~800 chars of body text
        meta = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", property="og:description")
        meta_text = (meta.get("content") or "").strip() if meta else ""
        body_text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))[:800]
        host = urlparse(url).netloc
        parts = [f"Company website ({host}):"]
        if meta_text:
            parts.append(meta_text)
        if body_text:
            parts.append(body_text)
        return "\n".join(parts)[:1500]
    except Exception:
        return ""


def _parse_json_strict(text: str) -> dict[str, Any]:
    """Gemini usually returns clean JSON when responseMimeType is set, but if a
    model wraps it in ```json ... ``` fences, strip those before parsing."""
    text = text.strip()
    if text.startswith("```"):
        # Strip leading ```json\n or ```\n
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


async def _call_gemini(system: str, user: str, schema: dict, temperature: float = 0.3) -> dict:
    """Single Gemini call with native structured-output. Raises RuntimeError on any failure."""
    settings = get_settings()
    if not settings.gemini_api_key:
        raise RuntimeError("Gemini not configured")
    payload = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {
            "temperature": temperature,
            "responseMimeType": "application/json",
            "responseSchema": schema,
        },
    }
    url = GEMINI_URL.format(model=settings.gemini_model)
    async with httpx.AsyncClient(timeout=25) as client:
        resp = await client.post(
            url,
            params={"key": settings.gemini_api_key},
            json=payload,
            headers={"Content-Type": "application/json"},
        )
        if resp.status_code >= 400:
            try:
                err = resp.json().get("error", {}).get("message", resp.text)
            except Exception:
                err = resp.text
            raise RuntimeError(f"Gemini API error ({resp.status_code}): {err}")
        data = resp.json()
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError(f"Unexpected Gemini response: {e}")
    try:
        return _parse_json_strict(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Could not parse Gemini response: {e}")


async def _call_groq(system: str, user: str, schema: dict, temperature: float = 0.3) -> dict:
    """Single Groq call. Groq's OpenAI-compatible API only supports json_object mode
    (not full JSON Schema), so we inline the schema in the system prompt to coach
    the model into the right shape."""
    settings = get_settings()
    if not settings.groq_api_key:
        raise RuntimeError("Groq not configured")

    schema_hint = (
        "Return ONLY a JSON object that matches this schema. No prose, no markdown fences:\n"
        + json.dumps(schema, indent=2)
    )
    payload = {
        "model": settings.groq_model,
        "messages": [
            {"role": "system", "content": f"{system}\n\n{schema_hint}"},
            {"role": "user", "content": user},
        ],
        "response_format": {"type": "json_object"},
        "temperature": temperature,
    }
    async with httpx.AsyncClient(timeout=25) as client:
        resp = await client.post(
            GROQ_URL,
            headers={
                "Authorization": f"Bearer {settings.groq_api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        if resp.status_code >= 400:
            try:
                err = resp.json().get("error", {}).get("message", resp.text)
            except Exception:
                err = resp.text
            raise RuntimeError(f"Groq API error ({resp.status_code}): {err}")
        data = resp.json()
    try:
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError(f"Unexpected Groq response: {e}")
    try:
        return _parse_json_strict(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Could not parse Groq response: {e}")


async def _structured_call(system: str, user: str, schema: dict, temperature: float = 0.3) -> dict:
    """Unified provider chain: Gemini → Groq on quota errors. Caller doesn't care
    which one served the request; they just get parsed JSON."""
    settings = get_settings()
    last_err: Exception | None = None

    if settings.gemini_api_key:
        try:
            return await _call_gemini(system, user, schema, temperature)
        except Exception as e:
            last_err = e
            # Only fall back on quota / rate-limit errors. Other errors (schema
            # mismatch, model down, etc.) are surfaced immediately unless Groq
            # is configured too — in which case we always try it as backup.
            if not _is_quota_error(e) and not settings.groq_api_key:
                raise

    if settings.groq_api_key:
        try:
            return await _call_groq(system, user, schema, temperature)
        except Exception as e:
            last_err = e

    if last_err:
        raise last_err
    raise RuntimeError(
        "No AI provider configured. Set GEMINI_API_KEY (aistudio.google.com) "
        "and/or GROQ_API_KEY (console.groq.com) — both have free tiers."
    )


async def generate_email(
    prompt: str,
    contact: dict[str, Any],
    web_context: str = "",
    user_name: str = "",
    db: Any = None,
    lead_id: int | None = None,
) -> dict[str, str]:
    """Generate a personalized email template. Returns {"subject", "body"}.

    If `db` is passed, the company profile is injected into the system prompt,
    and recent templates + previous emails to this contact are injected into
    the user message for tone/continuity grounding."""
    if not is_ai_configured():
        raise RuntimeError("No AI provider configured")

    # System prompt = base rules + company profile (RAG: who we are)
    system = SYSTEM_PROMPT
    if db is not None:
        from app.services.company_context import get_company_profile, build_company_block
        try:
            profile = get_company_profile(db)
            company_block = build_company_block(profile)
            if company_block:
                system = SYSTEM_PROMPT + "\n\n" + company_block
        except Exception:
            pass  # context is best-effort; never block the send

    # User message = prompt (loud) + recipient + web + history
    user_message_parts = [f"USER PROMPT (follow precisely):\n{prompt.strip()}"]
    contact_lines = []
    for k in ("name", "first_name", "company", "title", "email"):
        v = (contact or {}).get(k)
        if v:
            contact_lines.append(f"  {k}: {v}")
    if contact_lines:
        user_message_parts.append("Sample recipient:\n" + "\n".join(contact_lines))
    if user_name:
        user_message_parts.append(f"Sender's name: {user_name}")
    if web_context:
        user_message_parts.append(web_context)
    if db is not None:
        from app.services.company_context import build_email_writer_history
        try:
            hist = build_email_writer_history(db, lead_id)
            if hist:
                user_message_parts.append(hist)
        except Exception:
            pass

    schema = {
        "type": "object",
        "properties": {
            "subject": {"type": "string"},
            "body": {"type": "string"},
        },
        "required": ["subject", "body"],
    }
    parsed = await _structured_call(
        system, "\n\n".join(user_message_parts), schema, temperature=0.7,
    )
    subject = str(parsed.get("subject", "")).strip()
    body = str(parsed.get("body", "")).strip()
    if not subject or not body:
        raise RuntimeError("Model returned empty subject or body")
    return {"subject": subject, "body": body}




# ── Deal next-step recommendation ────────────────────────────────────────────

NEXT_STEP_SYSTEM = """You are a B2B sales coach for a staffing company. Given a deal's state
(stage, age, last activity, value, contact info, recent notes), recommend
ONE concrete next step the sales rep should take.

Return JSON with:
  - action: the specific next action (one short imperative sentence)
  - reason: why this is the best next step right now (1-2 sentences)
  - urgency: "high", "medium", or "low"
"""

NEXT_STEP_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string"},
        "reason": {"type": "string"},
        "urgency": {"type": "string", "enum": ["high", "medium", "low"]},
    },
    "required": ["action", "reason", "urgency"],
}


async def suggest_next_step(deal_context: str, db: Any = None) -> dict:
    """deal_context is a free-text summary of the deal's state — caller assembles it.
    If db is passed, the company profile is added to the system prompt so the
    suggestion is grounded in what we actually sell."""
    system = NEXT_STEP_SYSTEM
    if db is not None:
        from app.services.company_context import get_company_profile, build_company_block
        try:
            block = build_company_block(get_company_profile(db))
            if block:
                system = NEXT_STEP_SYSTEM + "\n\n" + block
        except Exception:
            pass
    return await _structured_call(system, deal_context, NEXT_STEP_SCHEMA)


# ── Meeting summarizer / MOM generator ───────────────────────────────────────

MEETING_SYSTEM = """You convert rough notes from a staffing-sales meeting into a structured MOM. You do TWO jobs at once: (a) extract hiring requirements with full detail and (b) produce the meeting-of-minutes itself.

Return JSON with:
  - summary: 2-3 sentence executive summary
  - attendees: list of {name, organisation, role} — leave fields blank if not stated; "organisation" should be "Us" / "Client" / actual company name when known
  - priorities: list of 1-5 short bullets ranked from most-important to least, capturing what the client cares about most (e.g. "Onboard 3 backend devs in 2 weeks", "Reduce vendor count")
  - key_points: list of 3-7 short bullets covering what was discussed (separate from priorities — these are notable facts, not asks)
  - action_items: list of {owner, action, due_in_days, priority} — owner is a name or "Us"/"Client"; due_in_days your best estimate (default 7); priority is "high" | "medium" | "low"
  - requirements: list of staffing/hiring requirements raised. Each is {role, skills, experience_years, count, location, rate, contract_type, duration, start_date, urgency}. Fields explained:
      * role: the job title (e.g. "Senior Java Developer")
      * skills: list of must-have technical/domain skills
      * experience_years: integer years of experience (0 if unspecified)
      * count: number of positions as a string (e.g. "3", "2-3", "unknown")
      * location: city/country/remote/hybrid
      * rate: extracted compensation or empty string
      * contract_type: "contract" | "full_time" | "contract_to_hire" | "unspecified"
      * duration: contract duration or empty string (e.g. "6 months", "12 months extendable")
      * start_date: when they want to start (e.g. "ASAP", "2 weeks", "Q3 2026")
      * urgency: "high" | "medium" | "low"
    Empty list if no hiring requirements were discussed.
  - next_meeting: object {scheduled, agenda} if a follow-up was agreed (else null). "scheduled" is the date/time mentioned, "agenda" is a one-line topic.
"""

MEETING_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "attendees": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "organisation": {"type": "string"},
                    "role": {"type": "string"},
                },
            },
        },
        "priorities": {"type": "array", "items": {"type": "string"}},
        "key_points": {"type": "array", "items": {"type": "string"}},
        "action_items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "action": {"type": "string"},
                    "due_in_days": {"type": "integer"},
                    "priority": {"type": "string"},
                },
                "required": ["action"],
            },
        },
        "requirements": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "role": {"type": "string"},
                    "skills": {"type": "array", "items": {"type": "string"}},
                    "experience_years": {"type": "integer"},
                    "count": {"type": "string"},
                    "location": {"type": "string"},
                    "rate": {"type": "string"},
                    "contract_type": {"type": "string"},
                    "duration": {"type": "string"},
                    "start_date": {"type": "string"},
                    "urgency": {"type": "string"},
                },
            },
        },
        "next_meeting": {
            "type": "object",
            "properties": {
                "scheduled": {"type": "string"},
                "agenda": {"type": "string"},
            },
        },
    },
    "required": ["summary", "key_points", "action_items"],
}


async def summarize_meeting(notes: str, db: Any = None) -> dict:
    system = MEETING_SYSTEM
    if db is not None:
        from app.services.company_context import get_company_profile, build_company_block
        try:
            block = build_company_block(get_company_profile(db))
            if block:
                system = MEETING_SYSTEM + "\n\n" + block
        except Exception:
            pass
    return await _structured_call(system, f"Meeting notes:\n\n{notes}", MEETING_SCHEMA)


# ── JD / Requirement analyzer ────────────────────────────────────────────────

JD_SYSTEM = """You extract structured requirements from a job description / staffing requirement text. The user is a staffing-sales rep who needs to quickly understand the asks.

Return JSON with:
  - role: the job title (e.g. "Senior Java Developer")
  - seniority: "junior" / "mid" / "senior" / "lead" / "manager" / "director" / "unspecified"
  - skills: list of must-have technical skills
  - nice_to_haves: list of nice-to-have skills (empty list if none)
  - experience_years: integer years of experience required (0 if unspecified)
  - location: string (city/country/remote/hybrid)
  - employment_type: "contract" / "full_time" / "contract_to_hire" / "unspecified"
  - rate_or_salary: extracted compensation, or empty string
  - duration: contract duration or empty string
  - urgency: "high" / "medium" / "low" based on language used
  - summary: 1-2 sentence plain-English summary of the role
"""

JD_SCHEMA = {
    "type": "object",
    "properties": {
        "role": {"type": "string"},
        "seniority": {"type": "string"},
        "skills": {"type": "array", "items": {"type": "string"}},
        "nice_to_haves": {"type": "array", "items": {"type": "string"}},
        "experience_years": {"type": "integer"},
        "location": {"type": "string"},
        "employment_type": {"type": "string"},
        "rate_or_salary": {"type": "string"},
        "duration": {"type": "string"},
        "urgency": {"type": "string"},
        "summary": {"type": "string"},
    },
    "required": ["role", "skills", "summary"],
}


async def analyze_jd(jd_text: str, db: Any = None) -> dict:
    system = JD_SYSTEM
    if db is not None:
        from app.services.company_context import get_company_profile, build_company_block
        try:
            block = build_company_block(get_company_profile(db))
            if block:
                system = JD_SYSTEM + "\n\n" + block
        except Exception:
            pass
    return await _structured_call(system, f"Job description:\n\n{jd_text}", JD_SCHEMA)


# ── Dashboard narrator ───────────────────────────────────────────────────────

NARRATOR_SYSTEM = """You explain CRM pipeline numbers to a sales leader in plain English. Given the metrics, write ONE concise paragraph (2-4 sentences) that:
- leads with the most important signal (positive OR negative)
- calls out one risk to watch
- calls out one opportunity to act on this week
Reference the actual numbers. No fluff, no preamble like "Here's a summary".

Return JSON with:
  - headline: a 6-10 word tagline of the pipeline state
  - narrative: the paragraph"""

NARRATOR_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "narrative": {"type": "string"},
    },
    "required": ["narrative"],
}


async def narrate_pipeline(metrics: dict, db: Any = None) -> dict:
    system = NARRATOR_SYSTEM
    if db is not None:
        from app.services.company_context import get_company_profile, build_company_block
        try:
            block = build_company_block(get_company_profile(db))
            if block:
                system = NARRATOR_SYSTEM + "\n\n" + block
        except Exception:
            pass
    user = "Pipeline metrics:\n" + "\n".join(f"- {k}: {v}" for k, v in metrics.items())
    return await _structured_call(system, user, NARRATOR_SCHEMA, temperature=0.4)


# ── Objection handler ────────────────────────────────────────────────────────

OBJECTION_SYSTEM = """You are a B2B sales coach for a staffing firm. The user shares an objection a prospect/client raised; you coach them on how to respond.

Rules:
- Never be defensive. Reframe the objection around the client's underlying concern.
- Tie the response to OUR ACTUAL SERVICES (use the About-us block) — do not invent capabilities.
- Use our brand voice/tone if provided.
- Keep responses concise and practical, written so the rep can paraphrase verbatim.

Return JSON with:
  - response: a coached one-paragraph response (3-5 sentences) the rep can send/say
  - tactic: ONE short sentence on WHY this response works
  - alternatives: 2 alternative angles to try if the first doesn't land (each is one short sentence)"""

OBJECTION_SCHEMA = {
    "type": "object",
    "properties": {
        "response": {"type": "string"},
        "tactic": {"type": "string"},
        "alternatives": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["response", "tactic", "alternatives"],
}


async def handle_objection(objection: str, context: str = "", db: Any = None) -> dict:
    system = OBJECTION_SYSTEM
    if db is not None:
        from app.services.company_context import get_company_profile, build_company_block
        try:
            block = build_company_block(get_company_profile(db))
            if block:
                system = OBJECTION_SYSTEM + "\n\n" + block
        except Exception:
            pass
    parts = [f"Objection from the prospect/client:\n{objection.strip()}"]
    if context.strip():
        parts.append(f"Deal/account context:\n{context.strip()}")
    return await _structured_call(system, "\n\n".join(parts), OBJECTION_SCHEMA, temperature=0.5)


# ── Pre-meeting research brief ───────────────────────────────────────────────

BRIEF_SYSTEM = """You prepare a pre-meeting briefing about a CRM account for a staffing-sales rep. Use ONLY the data the user provides — never invent specifics. If the data is thin, say so in the overview.

Return JSON with:
  - overview: 2-3 sentence summary of who this is and where the relationship stands
  - talking_points: list of 3-5 specific things to discuss next (each tied to something in the data)
  - questions_to_ask: list of 3-5 discovery/qualification questions
  - watch_outs: list of 0-3 sensitivities or risks (empty list if none surface)
  - opportunities: list of 1-3 specific upsell / expansion / engagement ideas grounded in OUR services"""

BRIEF_SCHEMA = {
    "type": "object",
    "properties": {
        "overview": {"type": "string"},
        "talking_points": {"type": "array", "items": {"type": "string"}},
        "questions_to_ask": {"type": "array", "items": {"type": "string"}},
        "watch_outs": {"type": "array", "items": {"type": "string"}},
        "opportunities": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["overview", "talking_points", "questions_to_ask"],
}


async def generate_brief(account_context: str, db: Any = None) -> dict:
    system = BRIEF_SYSTEM
    if db is not None:
        from app.services.company_context import get_company_profile, build_company_block
        try:
            block = build_company_block(get_company_profile(db))
            if block:
                system = BRIEF_SYSTEM + "\n\n" + block
        except Exception:
            pass
    return await _structured_call(system, account_context, BRIEF_SCHEMA, temperature=0.4)


# ── Follow-up email drafter ──────────────────────────────────────────────────

FOLLOWUP_SYSTEM = """You draft a follow-up email after a logged sales activity (call / meeting / email). The user gives you the activity content and the recipient; you write the email.

Rules:
- Reference WHAT WAS DISCUSSED specifically — pull commitments, open questions, next steps from the activity
- Do not invent details that weren't in the activity
- Keep it 3-5 sentences
- Use placeholders for personalisation: {{first_name}}, {{name}}, {{company}}
- End with the sender's signature if provided in the About-us block, else "Best," then a new line then "[Your name]"
- Write in OUR brand voice

Return JSON with:
  - subject: under 60 chars, references the topic
  - body: plain text with \\n line breaks"""

FOLLOWUP_SCHEMA = {
    "type": "object",
    "properties": {
        "subject": {"type": "string"},
        "body": {"type": "string"},
    },
    "required": ["subject", "body"],
}


async def draft_followup(activity_summary: str, contact: dict | None = None, db: Any = None) -> dict:
    system = FOLLOWUP_SYSTEM
    if db is not None:
        from app.services.company_context import get_company_profile, build_company_block
        try:
            block = build_company_block(get_company_profile(db))
            if block:
                system = FOLLOWUP_SYSTEM + "\n\n" + block
        except Exception:
            pass
    parts = [f"Activity to follow up on:\n{activity_summary}"]
    if contact:
        lines = [f"  {k}: {v}" for k, v in (contact or {}).items() if v]
        if lines:
            parts.append("Recipient:\n" + "\n".join(lines))
    return await _structured_call(system, "\n\n".join(parts), FOLLOWUP_SCHEMA, temperature=0.5)
