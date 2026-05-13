"""AI email composer using Google Gemini (free tier).

Get a key at https://aistudio.google.com → 'Get API key'. Set GEMINI_API_KEY
in env. Free tier: 15 RPM / 1500 RPD on gemini-2.0-flash, no card needed.

The model is asked to return JSON with subject + body, and to include
placeholders like {{first_name}} / {{company}} so the result still
personalizes per recipient when sent through the existing bulk-send flow.
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

SYSTEM_PROMPT = """You are a sales-email assistant for a B2B CRM. The user will describe what they want; you return ONE email template (subject + body) that the CRM will personalize per recipient.

Rules:
- Use these placeholders in the output where appropriate — they are replaced per contact at send time: {{first_name}}, {{name}}, {{company}}, {{title}}, {{email}}
- Keep the email concise: 3-6 short sentences in the body.
- Friendly, direct, professional. No exclamation marks, no emojis, no "I hope this finds you well", no overselling.
- Subject should be under 60 characters and look like a real human wrote it.
- Sign off with "Best," then a new line then "[Your name]" — the CRM doesn't currently substitute the sender name, so leave it as literal "[Your name]".
- Return ONLY a JSON object with keys "subject" and "body". The body should be plain text, line breaks as \\n.
"""


def is_gemini_configured() -> bool:
    return bool(get_settings().gemini_api_key)


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


async def generate_email(
    prompt: str,
    contact: dict[str, Any],
    web_context: str = "",
    user_name: str = "",
) -> dict[str, str]:
    """Call Gemini and return {"subject": ..., "body": ...}.

    Raises RuntimeError if the API call fails or the response is unparseable
    — the caller should catch and surface to the user."""
    settings = get_settings()
    if not settings.gemini_api_key:
        raise RuntimeError("GEMINI_API_KEY is not set")

    user_message_parts = [f"User's request: {prompt.strip()}"]
    contact_lines = []
    for k in ("name", "first_name", "company", "title", "email"):
        v = (contact or {}).get(k)
        if v:
            contact_lines.append(f"  {k}: {v}")
    if contact_lines:
        user_message_parts.append("Sample contact:\n" + "\n".join(contact_lines))
    if web_context:
        user_message_parts.append(web_context)
    if user_name:
        user_message_parts.append(f"Sender's name: {user_name}")

    user_message = "\n\n".join(user_message_parts)

    payload = {
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": user_message}]}],
        "generationConfig": {
            "temperature": 0.7,
            "responseMimeType": "application/json",
            "responseSchema": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string"},
                    "body": {"type": "string"},
                },
                "required": ["subject", "body"],
            },
        },
    }

    url = GEMINI_URL.format(model=settings.gemini_model)
    async with httpx.AsyncClient(timeout=20) as client:
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
        raise RuntimeError(f"Unexpected Gemini response shape: {e}")

    try:
        parsed = _parse_json_strict(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Could not parse model response as JSON: {e}")

    subject = str(parsed.get("subject", "")).strip()
    body = str(parsed.get("body", "")).strip()
    if not subject or not body:
        raise RuntimeError("Model returned empty subject or body")
    return {"subject": subject, "body": body}


async def _gemini_json(system: str, user: str, schema: dict) -> dict:
    """Generic Gemini structured-output call. Caller supplies system prompt,
    user message, and a JSON schema; returns parsed JSON."""
    settings = get_settings()
    if not settings.gemini_api_key:
        raise RuntimeError("GEMINI_API_KEY is not set")
    payload = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {
            "temperature": 0.3,
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
        raise RuntimeError(f"Could not parse model response as JSON: {e}")


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


async def suggest_next_step(deal_context: str) -> dict:
    """deal_context is a free-text summary of the deal's state — caller assembles it."""
    return await _gemini_json(NEXT_STEP_SYSTEM, deal_context, NEXT_STEP_SCHEMA)


# ── Meeting summarizer / MOM generator ───────────────────────────────────────

MEETING_SYSTEM = """You convert rough meeting notes from a staffing-sales conversation into a structured Minutes-of-Meeting (MOM).

Return JSON with:
  - summary: 2-3 sentence executive summary
  - attendees: list of names mentioned (best guess from notes)
  - key_points: list of 3-7 short bullets covering what was discussed
  - action_items: list of {owner, action, due_in_days} — owner is a name or "Us"/"Client"; due_in_days is your best estimate from context (default 7)
  - requirements: list of {skill_or_role, count, location, rate, urgency} for any hiring/staffing requirements mentioned (empty list if none)
"""

MEETING_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "attendees": {"type": "array", "items": {"type": "string"}},
        "key_points": {"type": "array", "items": {"type": "string"}},
        "action_items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "action": {"type": "string"},
                    "due_in_days": {"type": "integer"},
                },
                "required": ["action"],
            },
        },
        "requirements": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "skill_or_role": {"type": "string"},
                    "count": {"type": "string"},
                    "location": {"type": "string"},
                    "rate": {"type": "string"},
                    "urgency": {"type": "string"},
                },
            },
        },
    },
    "required": ["summary", "key_points", "action_items"],
}


async def summarize_meeting(notes: str) -> dict:
    return await _gemini_json(MEETING_SYSTEM, f"Meeting notes:\n\n{notes}", MEETING_SCHEMA)


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


async def analyze_jd(jd_text: str) -> dict:
    return await _gemini_json(JD_SYSTEM, f"Job description:\n\n{jd_text}", JD_SCHEMA)
