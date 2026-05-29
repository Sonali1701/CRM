"""
Lightweight NLP classifier for outreach notes (no AI, no quota).
Uses keyword matching to classify engagement as: positive, decline, reconnect_later, etc.
"""


POSITIVE_KEYWORDS = {
    "interested", "budget", "timeline", "proposal", "meeting", "sample",
    "requirement", "need", "when", "how much", "send proposal", "meeting confirmed",
    "send info", "details", "pricing", "proposal", "demo", "trial", "test",
    "next step", "next week", "tomorrow", "ready", "confirm", "schedule",
    "let's", "can we", "shall we", "would love", "very interested", "keen",
    "excited", "great fit", "looking forward", "next steps", "move forward",
}

DECLINE_KEYWORDS = {
    "not interested", "no vendor", "internal", "no budget", "no need", "no requirement",
    "wrong person", "left company", "laid off", "not relevant", "not applicable",
    "not right fit", "decline", "pass", "reject", "rejected", "can't", "cannot",
    "no thank you", "not available", "not open", "no hiring", "not looking",
    "no opening", "no position", "no expansion", "closed to", "only internal",
}

RECONNECT_KEYWORDS = {
    "ping me later", "reach out later", "call me back", "contact me in",
    "after", "next month", "next quarter", "next year", "6 months",
    "in a few months", "revisit", "check back", "future", "later",
    "down the line", "when we", "if we", "once we", "when things change",
}

WRONG_POC_KEYWORDS = {
    "wrong person", "not the right person", "talk to", "speak with",
    "contact", "reach out to", "my colleague", "my manager", "my team",
    "refer you to", "you should talk to", "you need to speak with",
    "the person you need", "head of", "director of", "owner of",
}

OUT_OF_ORG_KEYWORDS = {
    "left", "no longer", "moved on", "left the company", "resigned",
    "laid off", "fired", "terminated", "separated", "retired", "quit",
}

INFO_SENT_KEYWORDS = {
    "send email", "send info", "send details", "send proposal", "send deck",
    "sent", "sharing", "share with", "will send", "sending", "attached",
}


def classify_notes(text: str) -> str:
    """
    Classify outreach notes into engagement category.
    Returns one of: positive, decline, reconnect_later, wrong_poc, out_of_org,
                    info_sent, in_house_only, no_outcome
    """
    if not text or not text.strip():
        return "no_outcome"

    text_lower = text.lower()
    words = set(text_lower.split())

    # Count keyword matches per category
    scores = {
        "positive": sum(1 for kw in POSITIVE_KEYWORDS if kw in text_lower),
        "decline": sum(1 for kw in DECLINE_KEYWORDS if kw in text_lower),
        "reconnect_later": sum(1 for kw in RECONNECT_KEYWORDS if kw in text_lower),
        "wrong_poc": sum(1 for kw in WRONG_POC_KEYWORDS if kw in text_lower),
        "out_of_org": sum(1 for kw in OUT_OF_ORG_KEYWORDS if kw in text_lower),
        "info_sent": sum(1 for kw in INFO_SENT_KEYWORDS if kw in text_lower),
        "in_house_only": text_lower.count("internal") + text_lower.count("in-house") + text_lower.count("inhouse"),
    }

    # Prioritize by specificity (some categories are stronger signals)
    if scores["out_of_org"] > 0:
        return "out_of_org"
    if scores["wrong_poc"] > 0:
        return "wrong_poc"
    if scores["reconnect_later"] > 0:
        return "reconnect_later"
    if scores["in_house_only"] > 0:
        return "in_house_only"
    if scores["decline"] > 0:
        return "decline"  # More general than "not_interested_now"
    if scores["info_sent"] > 0:
        return "info_sent"
    if scores["positive"] > 0:
        return "positive"

    return "no_outcome"


def get_engagement_category(category: str) -> str:
    """Map internal category to display label."""
    labels = {
        "positive": "Interested",
        "decline": "Not Interested",
        "reconnect_later": "Reconnect Later",
        "wrong_poc": "Wrong Person",
        "out_of_org": "Left Company",
        "info_sent": "Info Sent",
        "in_house_only": "In-House Only",
        "no_outcome": "No Outcome",
    }
    return labels.get(category, category)
