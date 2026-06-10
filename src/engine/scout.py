"""
Scout engine — LLM verification via Cerebras (OpenAI-compatible endpoint).

Implements:
  - verify_scholarship_llama(): full 12-field schema LLM call
  - All Phase 5 post-processing: URL sanitization, hallucination guard,
    start date estimation, status safety net
  - CerebrasQuotaExceededException for quota abort signalling to runner
"""

import os
import json
import time
import logging
import urllib.parse
import requests
from datetime import date, timedelta
from typing import Optional, List, Dict, Any

from dotenv import load_dotenv

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ScoutEngine")

load_dotenv()


class CerebrasQuotaExceededException(Exception):
    """Raised when the Cerebras API returns a rate-limit or quota-exceeded response."""
    pass


# ── LLM system prompt ──────────────────────────────────────────────────────────
_SYSTEM_INSTRUCTION = """You are an advanced Automated Academic Scout and Data Verification Agent.
Your task is to analyze raw, scraped web text data and candidate links provided to you, verify the real-time application status of a specific scholarship, and output deterministic JSON data structures.

You must output a JSON object with the following fields:
1. "scholarship_name": The exact name of the scholarship processed (string)
2. "status": Strictly choose one: 'OPEN' | 'CLOSED' | 'NOT_YET_OPENED'
3. "application_start_date": String in YYYY-MM-DD format, or null if unknown
4. "application_deadline": String in YYYY-MM-DD format, or null if unknown
5. "official_source_url": The primary official info URL (government/embassy/university/foundation page). Must NOT be a third-party news site.
6. "official_registration_url": The verified submission/registration portal URL (must be different from official_source_url), or null.
7. "supplementary_source_url": If the PRIMARY source has no current cycle dates but you found an official government/embassy/ministry announcement page (same official domain, e.g. /nasional/, /news/, /berita/) with more up-to-date or extended deadline info — output that announcement URL here. Only use this for truly official domain posts, NOT for third-party media. Output null if not applicable.
8. "url_verification_fallback_used": true (boolean) if no web content was successfully scraped and the output relies on LLM training knowledge alone; false (boolean) if scraped page content was used.
9. "confidence_score": Float between 0.0 to 1.0 reflecting source reliability based on the text context
10. "processing_method_detected": Detect if registration requires 'Online', 'Offline/Mail-in', 'Hybrid', or 'Register First, Upload Later' (string)
11. "remarks": A concise summary. If dates come from two different sources (primary page vs. announcement post), explain the discrepancy. Always cite which page URL the dates were extracted from.
12. "date_precision": Strictly one of: 'exact' | 'monthly' | 'quarterly' | 'unknown'
    - 'exact'     : Specific YYYY-MM-DD dates found in the source
    - 'monthly'   : Only month names or ranges stated (e.g. "December - January")
    - 'quarterly' : Quarter or semester mentioned (e.g. "Q1 2026", "Semester 1")
    - 'unknown'   : No date information found at all

CRITICAL RULES FOR LINKS — READ CAREFULLY AND FOLLOW STRICTLY:
- "official_source_url" (Info Link) MUST be a URL from the OFFICIAL scholarship body — a government ministry, embassy, university, or foundation. NEVER cite a news article, blog, or third-party media site (e.g. kompas.com, detik.com, tribunnews.com, liputan6.com, etc.) as the official_source_url. This is an absolute rule.
- "official_registration_url" (Registration Link) MUST be a DIFFERENT URL that directly allows the user to register, log in, or submit an online application (e.g., a portal login page, Google Form, or direct submission URL).
- ABSOLUTELY FORBIDDEN: Do NOT output the same URL for both "official_source_url" and "official_registration_url" unless the registration form is literally embedded directly on the info page itself. This is a hard rule with zero exceptions.
- If you cannot find a distinct registration link, output null for "official_registration_url". Do NOT copy the info URL and do NOT invent a URL.
- If you cannot find a verified official source URL, output null for "official_source_url". Do NOT use a news article URL as a fallback.
- Always prefer specific sub-page URLs (e.g., /apply, /register, /form, /pendaftaran, /burse-2026, /news/2026-application) over generic homepage URLs.
- For date extraction: Parse application dates precisely from the page text. Look for explicit open/close/deadline date ranges labelled as 'important date', 'deadline', 'application period', 'batas waktu', or similar. Output in YYYY-MM-DD format.

SYSTEM LOGIC & ANALYSIS STRATEGY:
1. PHASE 1: Scan all PAGE URL sections in the scraped web context. Identify which pages belong to official scholarship bodies (government, embassy, university, foundation) vs. news/blog sites.
2. PHASE 2: Find the most current application window dates from the scraped text. Look for explicit date ranges in sections labelled 'Important Date', 'Application Period', 'Deadline', or similar. Dates found on official pages take priority over dates found on news/blog pages.
3. PHASE 3: Select distinct, specific URLs for info and registration from the CANDIDATE LINKS lists — never duplicates.
4. URL INTEGRITY RULE (STRICT): Only output URLs that were literally present in:
   a) The scraped page text (shown in the PAGE URL sections below)
   b) The CANDIDATE INFO LINKS or CANDIDATE REGISTRATION LINKS lists
   NEVER construct, infer, guess, or modify URL paths. If no valid URL is found, output null.
5. STATUS RULE (STRICT): Compare the application deadline you find against TODAY'S DATE (provided in the user prompt).
   - If today's date is BEFORE the application start date -> status = 'NOT_YET_OPENED'
   - If today's date is WITHIN start and end date -> status = 'OPEN'
   - If today's date is AFTER the application deadline -> status = 'CLOSED'
   - If no explicit deadline is found in the scraped text -> ASSUME status = 'CLOSED' (conservative). Do NOT output 'OPEN' without an explicit future deadline date. This is a hard rule.
   - NEVER output 'OPEN' if today's date is after the deadline. This is a hard rule.
6. DATE PRIORITY RULE: If the scraped page text contains explicit, precise dates (e.g. '2026-11-01 to 2027-01-31'), use those as ground truth. Only use context clues or estimates if NO explicit dates appear anywhere in the scraped content.

DATE INFERENCE FOR MONTH-RANGE SOURCES:
If dates are stated as month name ranges only (e.g. "December - January" or "Jun - Jul"):
  - application_start_date = first day of start month -> YYYY-MM-01
  - application_deadline   = last day of end month   -> use calendar (Jan=31, Apr=30, Feb=28, etc.)
  - Use the nearest upcoming cycle year. Example: if today is June 2026 and the source says "Dec - Jan", use Dec 2026 - Jan 2027.
  - Set date_precision = 'monthly'
For quarters: infer first/last day of the quarter. Set date_precision = 'quarterly'.

SOURCE AUTHORITY HIERARCHY — apply in strict descending priority:
1. Official embassy/consulate page for the applicant's home country
   (e.g. id.emb-japan.go.jp for Indonesian applicants to MEXT)
2. Issuing government ministry or national agency
   (e.g. niied.go.kr, mext.go.jp, hea.ie, bolashak.gov.kz)
3. Official scholarship foundation website
   (e.g. gksscholarship.com, chevening.org, cmkfoundation-globalscholarship.org)
4. Official university or institution admission page
   (for uni-to-uni scholarships: this becomes PRIORITY #1 — see UNI-TO-UNI note if present)
5. Study-abroad portals (e.g. studyinjapan.go.jp, studyinkorea.go.kr)
   -> USE ONLY if no higher-priority source exists in the scraped content
6. News articles, aggregator blogs, third-party media -> FORBIDDEN as official_source_url
When sources contradict each other, ALWAYS use the higher-priority source's dates.
"""


def verify_scholarship_llama(
    scholarship_name: str,
    scraped_web_text: str,
    candidate_info_links: List[str],
    candidate_reg_links: List[str],
    model_name: Optional[str] = None,
    uni_context_note: str = "",
) -> Dict[str, Any]:
    """
    Calls the OpenAI-compatible endpoint (Cerebras/Llama) using requests.
    Only the scholarship name and independently scraped web content are
    passed — no spreadsheet historical data. The LLM must discover all
    links, dates, and status from the web context alone.

    Args:
        scholarship_name:    Name of the scholarship to verify.
        scraped_web_text:    Combined text from all scraped pages.
        candidate_info_links: List of candidate info URLs found during scraping.
        candidate_reg_links:  List of candidate registration URLs found during scraping.
        model_name:          Optional override for LLM model name.
        uni_context_note:    Additional context injected for uni-to-uni entries,
                             locked mode, date_source_domain, and context_hint.

    Returns:
        Dict with all 12 LLM output fields plus a 'latency' key.

    Raises:
        CerebrasQuotaExceededException: If the API quota/rate limit is exceeded.
        RuntimeError: For other API failures.
    """
    api_key  = os.getenv("OPENAI_API_KEY")
    base_url = os.getenv("OPENAI_BASE_URL", "https://api.cerebras.ai/v1")
    if not model_name:
        model_name = os.getenv("OPENAI_MODEL", "gpt-oss-120b")

    if not api_key:
        raise ValueError("Missing OPENAI_API_KEY in environment variables.")

    candidate_info_str = "\n".join([f"- {u}" for u in candidate_info_links]) if candidate_info_links else "None found."
    candidate_reg_str  = "\n".join([f"- {u}" for u in candidate_reg_links])  if candidate_reg_links  else "None found."

    user_prompt = f"""
{uni_context_note}
TODAY'S DATE: {time.strftime('%Y-%m-%d')} (use this to determine if the scholarship is currently OPEN, CLOSED, or NOT_YET_OPENED)

SCHOLARSHIP NAME TO VERIFY: {scholarship_name}

RAW SCRAPED WEB CONTEXT:
{scraped_web_text}

CANDIDATE INFO LINKS FOUND on webpages:
{candidate_info_str}

CANDIDATE REGISTRATION LINKS FOUND on webpages:
{candidate_reg_str}
"""

    endpoint = f"{base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": _SYSTEM_INSTRUCTION},
            {"role": "user",   "content": user_prompt}
        ],
        "temperature": 0.0,
        "response_format": {"type": "json_object"}
    }

    logger.info(f"Submitting verification request to Llama ({model_name}) for: '{scholarship_name}'")
    start_time = time.time()

    # Retry loop: handles both Timeout and 429 queue_exceeded with exponential backoff.
    # Up to 4 total attempts. Wait schedule: 10s -> 20s -> 30s between consecutive attempts.
    # "queue_exceeded" 429 = transient server congestion, safe to retry.
    # Any other 429 (hard rate/quota limit) is raised immediately without retry.
    _LLM_RETRY_WAITS = (10, 20, 30)  # seconds to wait before attempt 2, 3, 4
    response = None
    for llm_attempt in range(1, 5):  # up to 4 attempts total
        try:
            response = requests.post(endpoint, json=payload, headers=headers, timeout=90)
        except requests.exceptions.Timeout:
            if llm_attempt < 4:
                wait_s = _LLM_RETRY_WAITS[llm_attempt - 1]
                logger.warning(
                    f"Cerebras API timed out (attempt {llm_attempt}/4) - "
                    f"retrying in {wait_s}s..."
                )
                time.sleep(wait_s)
                continue
            else:
                raise  # all 4 attempts timed out

        # Got a response — check if it's a retryable queue_exceeded (transient congestion)
        if response.status_code == 429 and "queue_exceeded" in response.text:
            if llm_attempt < 4:
                wait_s = _LLM_RETRY_WAITS[llm_attempt - 1]
                logger.warning(
                    f"Cerebras server queue exceeded (attempt {llm_attempt}/4) - "
                    f"server is temporarily congested. Retrying in {wait_s}s..."
                )
                time.sleep(wait_s)
                continue
            else:
                # All 4 attempts exhausted on queue congestion — give up on this row
                raise CerebrasQuotaExceededException(
                    f"Cerebras API limit/quota hit: {response.status_code} - {response.text}"
                )

        break  # non-timeout, non-queue_exceeded response — proceed

    latency = time.time() - start_time

    # Gate error keyword scan behind non-OK HTTP status.
    # IMPORTANT: A 200 OK response contains the model's JSON output (which may
    # legitimately contain words like "quota" from scholarship content, e.g.
    # "special-quota.php") and must NEVER be scanned for API error keywords.
    if response.status_code == 429:
        raise CerebrasQuotaExceededException(
            f"Cerebras API limit/quota hit: {response.status_code} - {response.text}"
        )
    if not response.ok:
        err_text = response.text
        if "RESOURCE_EXHAUSTED" in err_text or "quota" in err_text.lower() or "limit exceeded" in err_text.lower():
            raise CerebrasQuotaExceededException(
                f"Cerebras API limit/quota hit: {response.status_code} - {err_text}"
            )
        raise RuntimeError(f"Llama API failed: {response.status_code} - {response.text}")

    res_json = response.json()
    content_str = res_json["choices"][0]["message"]["content"]
    parsed_data = json.loads(content_str)
    parsed_data["latency"] = latency
    logger.info(f"Cerebras responded in {latency:.1f}s for '{scholarship_name}'")
    return parsed_data


def post_process_result(
    verified_data: Dict[str, Any],
    all_known_urls: set,
    is_locked: bool,
    locked_source_note: str,
) -> Dict[str, Any]:
    """
    Applies all post-processing steps to the raw LLM output:
      1. Sanitize link values (empty/None strings -> Python None)
      2. Discard supplementary_source_url if it's a news/media domain
      3. Hallucination guard: reject reg URL if its domain was never scraped
      4. Hallucination guard: reject info URL if its domain was never scraped
      5. Start date estimation: if deadline found but no start date, estimate deadline - 90 days
      6. Status safety net: OPEN without deadline -> CLOSED
      7. Prepend locked source note to remarks

    Imports from crawler to avoid circular imports.
    """
    from src.search.crawler import is_news_domain

    # 1. Sanitize links
    def _sanitize(val):
        if not val or str(val).strip().lower() in ("none", "", "null", "-", "n/a"):
            return None
        return val

    verified_data["official_source_url"]       = _sanitize(verified_data.get("official_source_url"))
    verified_data["official_registration_url"] = _sanitize(verified_data.get("official_registration_url"))
    verified_data["supplementary_source_url"]  = _sanitize(verified_data.get("supplementary_source_url"))

    # 2. Discard supplementary_source_url if news/media
    supp_url = verified_data.get("supplementary_source_url")
    if supp_url and is_news_domain(supp_url):
        logger.warning(f"Supplementary URL '{supp_url}' is a news/media site - discarding.")
        verified_data["supplementary_source_url"] = None

    def _domain_seen(url: str) -> bool:
        if not url:
            return False
        domain = urllib.parse.urlparse(url).netloc
        return any(urllib.parse.urlparse(u).netloc == domain for u in all_known_urls if u)

    # 3. Hallucination guard — registration URL
    reg_url = verified_data.get("official_registration_url")
    if reg_url and reg_url not in all_known_urls:
        if not _domain_seen(reg_url):
            logger.warning(f"Reg URL '{reg_url}' domain never scraped - likely hallucinated. Setting to null.")
        else:
            logger.warning(f"Reg URL '{reg_url}' has a known domain but unseen path - setting to null.")
        verified_data["official_registration_url"] = None

    # 4. Hallucination guard — info URL (domain must be known; sub-pages of known domains are OK)
    info_url = verified_data.get("official_source_url")
    if info_url and info_url not in all_known_urls:
        if _domain_seen(info_url):
            logger.info(f"Info URL '{info_url}' path not in exact list but domain is known - keeping.")
        else:
            logger.warning(f"Info URL '{info_url}' domain never scraped - likely hallucinated. Setting to null.")
            verified_data["official_source_url"] = None

    # 5. Start date estimation: deadline - 90 days
    _start = verified_data.get("application_start_date")
    _end   = verified_data.get("application_deadline")
    if _start is None and _end is not None:
        try:
            _end_dt   = date.fromisoformat(_end)
            _start_dt = _end_dt - timedelta(days=90)
            verified_data["application_start_date"] = _start_dt.isoformat()
            verified_data["remarks"] = (
                (verified_data.get("remarks") or "") +
                " [Start date estimated: only deadline found - start = deadline - 90 days.]"
            ).strip()
            logger.info(f"Start date estimated: {_start_dt.isoformat()} (deadline - 90 days from {_end})")
        except ValueError:
            pass  # unparseable end_date — leave start as None

    # 6. Status safety net: OPEN without deadline -> CLOSED
    if verified_data.get("status") == "OPEN" and verified_data.get("application_deadline") is None:
        logger.warning("Status is OPEN but no deadline was found - cannot confirm. Forcing to CLOSED.")
        verified_data["status"] = "CLOSED"
        verified_data["remarks"] = (
            (verified_data.get("remarks") or "") +
            " [Status overridden to CLOSED: no explicit deadline date found to confirm OPEN.]"
        ).strip()

    # 7. Prepend locked source note to remarks
    if is_locked and locked_source_note:
        verified_data["remarks"] = (
            locked_source_note + " | " + (verified_data.get("remarks") or "")
        ).strip(" |").strip()

    return verified_data
