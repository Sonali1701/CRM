"""
Website enrichment: fetch a company's homepage and extract as much as possible.
Returns a dict — all fields are optional strings, empty string means not found.
"""
import re
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

TIMEOUT = 8
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; RadixsolCRM/1.0; +https://radixsol.com)",
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

# Rough keyword → industry map (first match wins)
_INDUSTRY_HINTS: list[tuple[list[str], str]] = [
    (["hospital", "clinic", "healthcare", "medical", "health care", "nursing", "patient"], "Healthcare"),
    (["staffing", "recruitment", "talent", "workforce", "placement", "hiring"], "Staffing"),
    (["software", "saas", "cloud", "platform", "api", "developer", "tech"], "Technology"),
    (["insurance", "insurer", "coverage", "policy", "underwriting"], "Insurance"),
    (["bank", "finance", "financial", "investment", "wealth", "capital"], "Finance"),
    (["logistics", "supply chain", "freight", "shipping", "warehouse"], "Logistics"),
    (["retail", "e-commerce", "ecommerce", "store", "shop", "marketplace"], "Retail"),
    (["education", "university", "college", "school", "learning", "edtech"], "Education"),
    (["real estate", "property", "realty", "mortgage"], "Real Estate"),
    (["construction", "contractor", "engineering", "infrastructure"], "Construction"),
    (["legal", "law firm", "attorney", "counsel"], "Legal"),
    (["marketing", "advertising", "agency", "branding", "media"], "Marketing"),
    (["manufacturing", "factory", "production", "industrial"], "Manufacturing"),
    (["pharma", "pharmaceutical", "biotech", "life sciences", "drug"], "Pharma"),
]

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
_PHONE_RE = re.compile(r"""
    (?:(?:\+1[\s\-.]?)?\(?\d{3}\)?[\s\-.]?\d{3}[\s\-.]?\d{4})  # US
    |(?:\+\d{1,3}[\s\-.]?\d{6,12})                               # International
""", re.VERBOSE)


def _normalise_url(url: str) -> str:
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def _clean_title(title: str, domain: str) -> str:
    """Strip common site name suffixes like '| Company Name' from page titles."""
    for sep in [" | ", " - ", " – ", " — ", " :: "]:
        if sep in title:
            parts = title.split(sep)
            # Pick the shortest non-empty part (usually the company name)
            title = min((p.strip() for p in parts if p.strip()), key=len, default=title)
    # Fall back to domain root if title is still generic
    generic = {"home", "homepage", "welcome", "index"}
    if title.lower() in generic or not title:
        title = domain.replace("www.", "").split(".")[0].title()
    return title


def _guess_industry(text: str) -> str:
    lower = text.lower()
    for keywords, industry in _INDUSTRY_HINTS:
        if any(kw in lower for kw in keywords):
            return industry
    return ""


def _find_emails(soup: BeautifulSoup, text: str) -> list[str]:
    found: set[str] = set()
    # href="mailto:..."
    for a in soup.find_all("a", href=re.compile(r"^mailto:", re.I)):
        m = _EMAIL_RE.search(a["href"])
        if m:
            found.add(m.group().lower())
    # Plain text (limit to avoid spam traps)
    for m in _EMAIL_RE.finditer(text):
        email = m.group().lower()
        if not any(skip in email for skip in ["example.", "sentry.", "wixpress", "schema"]):
            found.add(email)
    # Exclude image/asset emails
    return sorted(e for e in found if not e.endswith((".png", ".jpg", ".svg")))[:5]


def _find_phones(text: str) -> list[str]:
    found = []
    seen: set[str] = set()
    for m in _PHONE_RE.finditer(text):
        digits = re.sub(r"\D", "", m.group())
        if digits not in seen and len(digits) >= 10:
            seen.add(digits)
            found.append(m.group().strip())
    return found[:3]


def _find_social(soup: BeautifulSoup) -> dict[str, str]:
    social: dict[str, str] = {}
    patterns = {
        "linkedin": re.compile(r"linkedin\.com/(company|in)/[^\"'\s/]+", re.I),
        "twitter":  re.compile(r"(?:twitter|x)\.com/[^\"'\s/]+", re.I),
        "facebook": re.compile(r"facebook\.com/[^\"'\s/]+", re.I),
    }
    for a in soup.find_all("a", href=True):
        href = a["href"]
        for key, pat in patterns.items():
            if key not in social and pat.search(href):
                # make absolute if relative
                social[key] = href if href.startswith("http") else urljoin("https://", href)
    return social


async def enrich(url: str) -> dict:
    url = _normalise_url(url)
    domain = urlparse(url).netloc

    result = {
        "url": url,
        "name": "",
        "description": "",
        "industry": "",
        "emails": [],
        "phones": [],
        "social": {},
        "error": "",
    }

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=TIMEOUT, headers=HEADERS) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            html = resp.text
    except httpx.TimeoutException:
        result["error"] = "Request timed out — the website took too long to respond."
        return result
    except httpx.HTTPStatusError as e:
        result["error"] = f"Website returned {e.response.status_code}."
        return result
    except Exception as e:
        result["error"] = f"Could not reach website: {e}"
        return result

    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(separator=" ", strip=True)

    # Name
    og_site = soup.find("meta", property="og:site_name")
    og_title = soup.find("meta", property="og:title")
    raw_title = soup.title.string.strip() if soup.title and soup.title.string else ""
    if og_site and og_site.get("content"):
        result["name"] = og_site["content"].strip()
    elif og_title and og_title.get("content"):
        result["name"] = _clean_title(og_title["content"].strip(), domain)
    elif raw_title:
        result["name"] = _clean_title(raw_title, domain)
    else:
        result["name"] = domain.replace("www.", "").split(".")[0].title()

    # Description
    og_desc = soup.find("meta", property="og:description")
    meta_desc = soup.find("meta", attrs={"name": "description"})
    if og_desc and og_desc.get("content"):
        result["description"] = og_desc["content"].strip()[:500]
    elif meta_desc and meta_desc.get("content"):
        result["description"] = meta_desc["content"].strip()[:500]

    # Industry
    combined = f"{result['name']} {result['description']} {text[:2000]}"
    result["industry"] = _guess_industry(combined)

    # Contacts
    result["emails"] = _find_emails(soup, text)
    result["phones"] = _find_phones(text)
    result["social"] = _find_social(soup)

    return result
