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
