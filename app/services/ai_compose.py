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


# ── Capability statement drafter ─────────────────────────────────────────────

CAPABILITY_SYSTEM = """You write a tailored staffing capability statement for an enterprise prospect. The user gives you their target client's industry/domain and optionally a specific use-case or hiring need; you return a structured pitch that explains how WE help that domain.

Rules:
- Pull from the About-us block — never invent services we don't actually offer.
- Concrete > generic. If we have a relevant service, name it.
- 4-6 bullet "key strengths" max; 3-5 sample deliverables.
- Tone matches our brand voice if provided.

Return JSON with:
  - capability_summary: 2-3 sentence opening paragraph (positions us for THIS domain)
  - key_strengths: list of 4-6 short bullets — capabilities most relevant to the prospect's domain
  - sample_deliverables: list of 3-5 concrete things we'd deliver in a typical engagement for this domain
  - why_us: ONE crisp differentiator-sentence (why pick us over an obvious competitor)
"""

CAPABILITY_SCHEMA = {
    "type": "object",
    "properties": {
        "capability_summary": {"type": "string"},
        "key_strengths": {"type": "array", "items": {"type": "string"}},
        "sample_deliverables": {"type": "array", "items": {"type": "string"}},
        "why_us": {"type": "string"},
    },
    "required": ["capability_summary", "key_strengths", "sample_deliverables"],
}


async def draft_capability(client_domain: str, use_case: str = "", db: Any = None) -> dict:
    system = CAPABILITY_SYSTEM
    if db is not None:
        from app.services.company_context import get_company_profile, build_company_block
        try:
            block = build_company_block(get_company_profile(db))
            if block:
                system = CAPABILITY_SYSTEM + "\n\n" + block
        except Exception:
            pass
    parts = [f"Target prospect's industry/domain: {client_domain.strip()}"]
    if use_case.strip():
        parts.append(f"Specific use-case or hiring need: {use_case.strip()}")
    return await _structured_call(system, "\n\n".join(parts), CAPABILITY_SCHEMA, temperature=0.5)


# ── Proposal generator ──────────────────────────────────────────────────────

PROPOSAL_SYSTEM = """You draft a staffing proposal for a deal in progress. The user gives you the deal state, the client, what was discussed in meetings (including extracted requirements), and our company profile. You return a structured proposal the rep can refine.

Rules:
- Ground EVERYTHING in the data provided. Don't invent skills/services/numbers.
- If requirement details (rate, duration, location, count) are missing, write placeholders like [TBD: rate per resource] — don't fabricate.
- Engagement model + timeline should match what's reasonable for the requirements.
- Use OUR services (from About-us) as the basis for "our approach".
- Length: ~250-500 words total across all sections.

Return JSON with:
  - executive_summary: 2-3 sentence opening that names the client and the headline ask
  - our_understanding: list of 3-5 bullets restating what we heard from the client
  - our_approach: 2-3 sentence description of how we'll solve it, grounded in our services
  - delivery_model: object {engagement_type, team_composition, timeline} — short strings; engagement_type is one of contract / contract-to-hire / managed-team / direct-hire / mixed
  - sample_profile: a 3-5 line description of the kind of resource(s) we'd put forward
  - commercials_outline: list of 3-5 bullets on commercials structure (rates as placeholders if unknown)
  - next_steps: list of 3-5 concrete next actions in order
"""

PROPOSAL_SCHEMA = {
    "type": "object",
    "properties": {
        "executive_summary": {"type": "string"},
        "our_understanding": {"type": "array", "items": {"type": "string"}},
        "our_approach": {"type": "string"},
        "delivery_model": {
            "type": "object",
            "properties": {
                "engagement_type": {"type": "string"},
                "team_composition": {"type": "string"},
                "timeline": {"type": "string"},
            },
        },
        "sample_profile": {"type": "string"},
        "commercials_outline": {"type": "array", "items": {"type": "string"}},
        "next_steps": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["executive_summary", "our_understanding", "our_approach", "next_steps"],
}


async def generate_proposal(deal_context: str, db: Any = None) -> dict:
    system = PROPOSAL_SYSTEM
    if db is not None:
        from app.services.company_context import get_company_profile, build_company_block
        try:
            block = build_company_block(get_company_profile(db))
            if block:
                system = PROPOSAL_SYSTEM + "\n\n" + block
        except Exception:
            pass
    return await _structured_call(system, deal_context, PROPOSAL_SCHEMA, temperature=0.4)


# ── Deal risk explainer (called per deal on demand) ─────────────────────────

RISK_SYSTEM = """A deal in our staffing CRM has been flagged as at risk by rules (no activity / slow stage progression). Explain in plain English (1-2 sentences) WHY it's at risk and recommend ONE concrete action to recover it.

Return JSON with:
  - reason: why this deal is at risk (1-2 sentences referencing the actual signals)
  - action: one short imperative recovery action
  - urgency: "high" | "medium" | "low"
"""

RISK_SCHEMA = {
    "type": "object",
    "properties": {
        "reason": {"type": "string"},
        "action": {"type": "string"},
        "urgency": {"type": "string", "enum": ["high", "medium", "low"]},
    },
    "required": ["reason", "action", "urgency"],
}


async def explain_deal_risk(risk_context: str, db: Any = None) -> dict:
    system = RISK_SYSTEM
    if db is not None:
        from app.services.company_context import get_company_profile, build_company_block
        try:
            block = build_company_block(get_company_profile(db))
            if block:
                system = RISK_SYSTEM + "\n\n" + block
        except Exception:
            pass
    return await _structured_call(system, risk_context, RISK_SCHEMA, temperature=0.3)


# ── Daily focus (Smart Daily Briefing) ──────────────────────────────────────

DAILY_FOCUS_SYSTEM = """You are the rep's morning sales coach. Given their pipeline state, overdue tasks, today's tasks, and recent hot accounts, return ONE focused paragraph (2-4 sentences) telling them what to focus on TODAY.

Rules:
- Lead with the most-urgent thing.
- Reference specific account/deal names from the data — don't be generic.
- End with a "do this first" recommendation.
- No fluff, no greetings — straight to the point. The greeting is already in the email.

Return JSON with:
  - focus: the paragraph (2-4 sentences)
  - top_action: one short imperative ("Call Acme — 12 days no contact")
"""

DAILY_FOCUS_SCHEMA = {
    "type": "object",
    "properties": {
        "focus": {"type": "string"},
        "top_action": {"type": "string"},
    },
    "required": ["focus"],
}


async def daily_focus(briefing_context: str, db: Any = None) -> dict:
    system = DAILY_FOCUS_SYSTEM
    if db is not None:
        from app.services.company_context import get_company_profile, build_company_block
        try:
            block = build_company_block(get_company_profile(db))
            if block:
                system = DAILY_FOCUS_SYSTEM + "\n\n" + block
        except Exception:
            pass
    return await _structured_call(system, briefing_context, DAILY_FOCUS_SCHEMA, temperature=0.4)


# ── Stakeholder Mapper ──────────────────────────────────────────────────────

STAKEHOLDER_SYSTEM = """You map a staffing-sales account's contacts into decision-making roles. Classify each contact into one of:
- "economic_buyer" (procurement, vendor management, finance)
- "technical_buyer" (delivery managers, engineering heads, hiring managers)
- "user" (recruiters, TA leads, team leads who will work with the resources)
- "champion" (internal advocate, generally any senior who's responsive)
- "influencer" (executives or peers who shape the decision)
- "unknown"

Also rate engagement_level "hot" / "warm" / "cold" based on signals provided.

Return JSON:
  - stakeholders: array of {name, role_label, role_category, engagement_level, why}
  - gaps: array of strings — what stakeholder types are missing or under-engaged
  - top_action: one short imperative ("Find the procurement contact at Acme")
"""

STAKEHOLDER_SCHEMA = {
    "type": "object",
    "properties": {
        "stakeholders": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "role_label": {"type": "string"},
                    "role_category": {"type": "string", "enum": ["economic_buyer", "technical_buyer", "user", "champion", "influencer", "unknown"]},
                    "engagement_level": {"type": "string", "enum": ["hot", "warm", "cold"]},
                    "why": {"type": "string"},
                },
                "required": ["name", "role_category", "engagement_level"],
            },
        },
        "gaps": {"type": "array", "items": {"type": "string"}},
        "top_action": {"type": "string"},
    },
    "required": ["stakeholders"],
}


async def map_stakeholders(account_context: str, db: Any = None) -> dict:
    system = STAKEHOLDER_SYSTEM
    if db is not None:
        from app.services.company_context import get_company_profile, build_company_block
        try:
            block = build_company_block(get_company_profile(db))
            if block:
                system = STAKEHOLDER_SYSTEM + "\n\n" + block
        except Exception:
            pass
    return await _structured_call(system, account_context, STAKEHOLDER_SCHEMA, temperature=0.3)


# ── LinkedIn / WhatsApp message drafter ─────────────────────────────────────

SOCIAL_SYSTEM = """You draft a short message for a staffing sales rep to send via LinkedIn DM or WhatsApp. Tone is professional but conversational — NOT a formal email. Length: 2-4 short sentences max for LinkedIn, 1-3 sentences for WhatsApp. No "Dear", no signoff, no signature — these are chat messages.

Use placeholders like {{first_name}}, {{company}} where it improves the message.

Return JSON:
  - linkedin: short LinkedIn DM (2-4 sentences)
  - whatsapp: short WhatsApp message (1-3 sentences)
"""

SOCIAL_SCHEMA = {
    "type": "object",
    "properties": {
        "linkedin": {"type": "string"},
        "whatsapp": {"type": "string"},
    },
    "required": ["linkedin", "whatsapp"],
}


async def draft_social_messages(intent: str, contact_context: str = "", db: Any = None) -> dict:
    system = SOCIAL_SYSTEM
    if db is not None:
        from app.services.company_context import get_company_profile, build_company_block
        try:
            block = build_company_block(get_company_profile(db))
            if block:
                system = SOCIAL_SYSTEM + "\n\n" + block
        except Exception:
            pass
    user = f"Intent: {intent}"
    if contact_context:
        user += f"\n\nContact context:\n{contact_context}"
    return await _structured_call(system, user, SOCIAL_SCHEMA, temperature=0.5)


# ── SOW / MSA Draft Assistant ───────────────────────────────────────────────

SOW_SYSTEM = """You draft a Statement of Work (SOW) outline for a staffing engagement. Use ONLY information given — never invent rates, dates, or scope. If something is missing, use a clearly-bracketed placeholder like [insert duration].

Return JSON with these sections:
  - title: "SOW: <project name>"
  - parties: 1-2 sentences naming the customer and supplier (supplier is from company profile if given)
  - background: 2-3 sentences on context — why this work is happening
  - scope_of_work: array of 4-7 bulleted scope items
  - resources: array of objects {role, count, skills, location} describing the resources to be provided
  - deliverables: array of 3-5 bullets
  - duration_and_timeline: 1-2 sentences
  - commercials: 2-3 sentences on engagement model, rates (placeholders if unknown), invoicing cadence
  - assumptions: array of 3-5 bullets
  - acceptance_criteria: array of 3-5 bullets
"""

SOW_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "parties": {"type": "string"},
        "background": {"type": "string"},
        "scope_of_work": {"type": "array", "items": {"type": "string"}},
        "resources": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "role": {"type": "string"},
                    "count": {"type": "string"},
                    "skills": {"type": "string"},
                    "location": {"type": "string"},
                },
                "required": ["role"],
            },
        },
        "deliverables": {"type": "array", "items": {"type": "string"}},
        "duration_and_timeline": {"type": "string"},
        "commercials": {"type": "string"},
        "assumptions": {"type": "array", "items": {"type": "string"}},
        "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "parties", "scope_of_work"],
}


async def draft_sow(deal_context: str, db: Any = None) -> dict:
    system = SOW_SYSTEM
    if db is not None:
        from app.services.company_context import get_company_profile, build_company_block
        try:
            block = build_company_block(get_company_profile(db))
            if block:
                system = SOW_SYSTEM + "\n\n" + block
        except Exception:
            pass
    return await _structured_call(system, deal_context, SOW_SCHEMA, temperature=0.3)


# ── Win/Loss Analyzer ───────────────────────────────────────────────────────

WINLOSS_SYSTEM = """You analyze a list of recently closed staffing deals (won AND lost) and identify patterns. Use ONLY the data given.

Return JSON:
  - win_themes: array of 3-5 strings — common patterns in WON deals
  - loss_themes: array of 3-5 strings — common patterns in LOST deals
  - top_risk_signals: array of 3-5 strings — early signals of a loss you'd watch for
  - recommendations: array of 3-5 strings — concrete actions to improve win rate
"""

WINLOSS_SCHEMA = {
    "type": "object",
    "properties": {
        "win_themes": {"type": "array", "items": {"type": "string"}},
        "loss_themes": {"type": "array", "items": {"type": "string"}},
        "top_risk_signals": {"type": "array", "items": {"type": "string"}},
        "recommendations": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["win_themes", "loss_themes", "recommendations"],
}


async def analyze_winloss(deals_context: str, db: Any = None) -> dict:
    return await _structured_call(WINLOSS_SYSTEM, deals_context, WINLOSS_SCHEMA, temperature=0.3)


# ── Enterprise Strategy Advisor ─────────────────────────────────────────────

STRATEGY_SYSTEM = """You advise a staffing sales leader on which industries and accounts to prioritize, based on their current pipeline data. Use ONLY the data given.

Return JSON:
  - top_industries: array of {industry, why, opportunity_size} — 3-5 industries to focus on
  - top_accounts: array of {account, why, action} — 3-5 existing accounts to invest in
  - underleveraged: array of strings — segments/industries currently under-represented but worth exploring (1-3)
  - guidance: 2-3 sentence executive paragraph summarizing the strategy
"""

STRATEGY_SCHEMA = {
    "type": "object",
    "properties": {
        "top_industries": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "industry": {"type": "string"},
                    "why": {"type": "string"},
                    "opportunity_size": {"type": "string"},
                },
                "required": ["industry"],
            },
        },
        "top_accounts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "account": {"type": "string"},
                    "why": {"type": "string"},
                    "action": {"type": "string"},
                },
                "required": ["account"],
            },
        },
        "underleveraged": {"type": "array", "items": {"type": "string"}},
        "guidance": {"type": "string"},
    },
    "required": ["guidance"],
}


async def advise_strategy(pipeline_context: str, db: Any = None) -> dict:
    system = STRATEGY_SYSTEM
    if db is not None:
        from app.services.company_context import get_company_profile, build_company_block
        try:
            block = build_company_block(get_company_profile(db))
            if block:
                system = STRATEGY_SYSTEM + "\n\n" + block
        except Exception:
            pass
    return await _structured_call(system, pipeline_context, STRATEGY_SCHEMA, temperature=0.4)


# ── Sales Coach ─────────────────────────────────────────────────────────────

COACH_SYSTEM = """You coach a staffing sales rep based on their recent activity patterns. Use ONLY the data given. Be specific (reference numbers and account names), not generic.

Return JSON:
  - strengths: array of 2-4 strings — what they're doing well
  - gaps: array of 2-4 strings — what's missing or inconsistent
  - this_week: array of 3-5 strings — concrete actions to take this week
"""

COACH_SCHEMA = {
    "type": "object",
    "properties": {
        "strengths": {"type": "array", "items": {"type": "string"}},
        "gaps": {"type": "array", "items": {"type": "string"}},
        "this_week": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["this_week"],
}


async def coach_rep(activity_context: str, db: Any = None) -> dict:
    return await _structured_call(COACH_SYSTEM, activity_context, COACH_SCHEMA, temperature=0.4)


# ── MEDDIC qualification scorer ─────────────────────────────────────────────

MEDDIC_SYSTEM = """You qualify a B2B sales deal using the MEDDIC framework. Score each of the six dimensions on a scale of 0-3 using ONLY the data the user provides — if a dimension has no signal, score it 0 (unknown). Never invent.

Scoring rubric (apply to every dimension):
- 0 = no signal / unknown
- 1 = weak / red flag
- 2 = partial / progressing
- 3 = strong / confirmed

Dimensions:
- metrics: Has the client quantified the business impact of solving this? (Revenue, cost saved, time saved.)
- economic_buyer: Have we identified and engaged the person with budget authority?
- decision_criteria: Do we know the explicit criteria they will use to choose a vendor?
- decision_process: Do we know the steps, owners, and timeline of their procurement process?
- identify_pain: Have we confirmed a specific, named pain that this purchase resolves?
- champion: Do we have an internal advocate selling for us when we are not in the room?

For each dimension, give:
  - score (0-3, integer)
  - reasoning: one short sentence citing actual evidence from the data, OR "No data" if nothing was provided.

Also return:
  - overall_summary: 1-2 sentences capturing the deal's qualification state
  - top_gap: the single most important dimension to improve next
"""

MEDDIC_SCHEMA = {
    "type": "object",
    "properties": {
        "metrics": {"type": "object", "properties": {"score": {"type": "integer"}, "reasoning": {"type": "string"}}, "required": ["score"]},
        "economic_buyer": {"type": "object", "properties": {"score": {"type": "integer"}, "reasoning": {"type": "string"}}, "required": ["score"]},
        "decision_criteria": {"type": "object", "properties": {"score": {"type": "integer"}, "reasoning": {"type": "string"}}, "required": ["score"]},
        "decision_process": {"type": "object", "properties": {"score": {"type": "integer"}, "reasoning": {"type": "string"}}, "required": ["score"]},
        "identify_pain": {"type": "object", "properties": {"score": {"type": "integer"}, "reasoning": {"type": "string"}}, "required": ["score"]},
        "champion": {"type": "object", "properties": {"score": {"type": "integer"}, "reasoning": {"type": "string"}}, "required": ["score"]},
        "overall_summary": {"type": "string"},
        "top_gap": {"type": "string"},
    },
    "required": ["metrics", "economic_buyer", "decision_criteria", "decision_process", "identify_pain", "champion"],
}


async def score_meddic(deal_context: str, db: Any = None) -> dict:
    return await _structured_call(MEDDIC_SYSTEM, deal_context, MEDDIC_SCHEMA, temperature=0.2)


# ── Account Plan drafter ────────────────────────────────────────────────────

ACCOUNT_PLAN_SYSTEM = """You draft a strategic account plan for a sales rep covering a key B2B account. Use ONLY the data given. If a section has no signal, write a short prompt like "Discover during next conversation" rather than inventing details.

Return JSON with these sections (each as plain text, 2-5 sentences):
  - business_goals: what the client is trying to achieve (their goals, not ours)
  - whitespace: where we can expand inside this account — services we don't yet sell here, teams we haven't engaged
  - key_stakeholders: who matters and what we know about them
  - threats_risks: competitive threats, account-side risks (budget freeze, restructure), execution risks
  - next_90d_actions: 3-5 concrete actions for the next 90 days, as a short bulleted text
  - success_metrics: how we'll know this account plan is working
"""

ACCOUNT_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "business_goals": {"type": "string"},
        "whitespace": {"type": "string"},
        "key_stakeholders": {"type": "string"},
        "threats_risks": {"type": "string"},
        "next_90d_actions": {"type": "string"},
        "success_metrics": {"type": "string"},
    },
    "required": ["business_goals", "next_90d_actions"],
}


async def draft_account_plan(account_context: str, db: Any = None) -> dict:
    system = ACCOUNT_PLAN_SYSTEM
    if db is not None:
        from app.services.company_context import get_company_profile, build_company_block
        try:
            block = build_company_block(get_company_profile(db))
            if block:
                system = ACCOUNT_PLAN_SYSTEM + "\n\n" + block
        except Exception:
            pass
    return await _structured_call(system, account_context, ACCOUNT_PLAN_SCHEMA, temperature=0.4)


# ── Mutual Close Plan drafter ───────────────────────────────────────────────

CLOSE_PLAN_SYSTEM = """You draft a mutual close plan (also called Mutual Action Plan or MAP) for a B2B sales deal. This is a step-by-step roadmap to signature, shared with the client.

Use ONLY the data given. If the target close date is unknown, propose a reasonable one based on stage + typical sales cycle.

Return JSON:
  - summary: 1-2 sentence framing of what this plan covers
  - target_close_date: ISO date string (YYYY-MM-DD) OR empty string if unclear
  - steps: array of 5-9 ordered steps. Each step = {title, owner_label ("us"/"client"/"both"), due_in_days (int, days from today), notes (short)}
"""

CLOSE_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "target_close_date": {"type": "string"},
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "owner_label": {"type": "string"},
                    "due_in_days": {"type": "integer"},
                    "notes": {"type": "string"},
                },
                "required": ["title"],
            },
        },
    },
    "required": ["summary", "steps"],
}


async def draft_close_plan(deal_context: str, db: Any = None) -> dict:
    system = CLOSE_PLAN_SYSTEM
    if db is not None:
        from app.services.company_context import get_company_profile, build_company_block
        try:
            block = build_company_block(get_company_profile(db))
            if block:
                system = CLOSE_PLAN_SYSTEM + "\n\n" + block
        except Exception:
            pass
    return await _structured_call(system, deal_context, CLOSE_PLAN_SCHEMA, temperature=0.3)
