"""
Orchestrator runner — batch pipeline for SchReminder Scout.

Processes ALL active scholarship rows from Google Sheets in a single run.

Pipeline per row:
  1. A4 bypass check (Status=T + Verified=F): email sheet data directly, skip search/LLM.
  2. Read per-scholarship config overrides (preferred_query, locked_urls, context_hint, etc.)
  3. Parse scholarship name (uni-to-uni vs centralized)
  4. Build search query (config query OR auto-generated)
  5. Locked mode (locked_urls set): skip search engine, scrape only pre-configured URLs.
  6. Normal mode: two-round DDG+Yahoo search -> build scrape queue -> scrape + branch sub-links
  7. Construct LLM context string + candidate link lists
  8. Call Cerebras LLM (verify_scholarship_llama) with all injected context notes
  9. Post-process: sanitize links, hallucination guard, start-date estimation, status safety net
  10. On CerebrasQuotaExceededException: abort loop, emit partial results
  11. After all rows: batch write to Google Sheets + send HTML email report + save result JSON
"""

import os
import sys
import time
import json
import re
import datetime
import logging
import urllib.parse
import random
from typing import List, Dict, Any, Optional

from dotenv import load_dotenv

# ── Imports from src modules ───────────────────────────────────────────────────
from src.spreadsheet.google_sheets import GoogleSheetsConnector
from src.search.crawler import (
    search_scholarship_with_retry,
    fetch_webpage_content,
    clean_html,
    extract_hyperlinks,
    filter_candidate_links,
    translate_text,
    is_news_domain,
    is_official_domain,
    OFFICIAL_DOMAINS,
)
from src.engine.scout import (
    verify_scholarship_llama,
    post_process_result,
    CerebrasQuotaExceededException,
)
from src.engine.name_parser import parse_scholarship_name
from scholarship_config.scholarship_config import get_scholarship_config
from src.notification.mailer import send_daily_email_report

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("Orchestrator")

# ── ANSI colors for console output ────────────────────────────────────────────
class Colors:
    HEADER  = "\033[95m"
    BLUE    = "\033[94m"
    CYAN    = "\033[96m"
    GREEN   = "\033[92m"
    WARNING = "\033[93m"
    FAIL    = "\033[91m"
    END     = "\033[0m"
    BOLD    = "\033[1m"

# ── Result persistence ─────────────────────────────────────────────────────────
def save_result_json(run_ts: str, model_used: str, processed_results: list) -> None:
    """Saves the full batch run result to scratch/result/ as a timestamped JSON file."""
    results_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "scratch", "result"
    )
    os.makedirs(results_dir, exist_ok=True)
    ts       = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    filename = f"{ts}_batch_run.json"
    filepath = os.path.join(results_dir, filename)
    payload  = {
        "run_ts":    run_ts,
        "model_used": model_used,
        "count":     len(processed_results),
        "results":   processed_results,
    }
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    logger.info(f"Batch result saved -> {filepath}")

# ── Branching constants ────────────────────────────────────────────────────────
BINARY_EXTENSIONS = (".pdf", ".xlsx", ".xls", ".docx", ".doc", ".ppt", ".pptx", ".zip", ".rar")

# Scholarship name prefixes (inside the opening parenthesis) that should be
# SKIPPED entirely — these are internally-funded scholarships with no public
# application portal that can be scraped or verified by the scout engine.
# E.g. "(Uni-Funded) Leiden Excellence Scholarship" -> skip it.
SKIPPED_PREFIXES = {
    "uni-funded",   # University-funded internal grants
}
BRANCHING_KEYWORDS = [
    "research", "rs", "graduate", "indonesia", "tahap",
    "announcement", "guideline", "scholar", "2026", "2025",
    "deadline", "apply", "apply-now", "schedule", "timeline",
    "gks", "kgsp", "niied",
    "news", "application", "open", "program", "burse",
    "scholarship", "grant", "award", "selection", "intake",
    "period", "cycle", "applic", "eligib", "require"
]
USELESS_PATH_PATTERNS = (
    "/search/label/", "/search/tag/", "/tag/", "/category/",
    "/contact", "/about", "/privacy", "/terms", "/faq",
    "/p/contact", "/p/about", "/sitemap",
)
USELESS_DOMAINS = (
    "www.google.com", "google.com", "translate.google.com",
    "twitter.com", "x.com", "instagram.com", "facebook.com",
    "linkedin.com", "youtube.com", "t.me", "wa.me",
)
MAX_BRANCHES = 4


def _process_single_scholarship(
    s: Dict[str, Any],
    model_name: str,
) -> Dict[str, Any]:
    """
    Full pipeline for a single scholarship row.
    Returns a result dict suitable for appending to processed_results.

    Dict schema:
      row_idx, search_status, verified_data
    """
    sch_name = s["scholarship_name"]
    row_idx  = s["row_idx"]

    # ── SKIP: Internally-funded scholarships with no public portal ────────────
    # Names like "(Uni-Funded) Leiden Excellence" are university internal grants.
    # They have no public application portal to scrape, so we skip them entirely.
    _stripped = sch_name.strip()
    if _stripped.startswith("("):
        import re as _re
        _m = _re.match(r'^\(([^)]+)\)', _stripped)
        if _m and _m.group(1).strip().lower() in SKIPPED_PREFIXES:
            logger.info(f"[SKIP] '{sch_name}': prefix '({_m.group(1)})' is in SKIPPED_PREFIXES — skipping.")
            return None  # caller must handle None

    # ── A4: Bypass check (Status=T + Verified=F) ──────────────────────────────
    if s.get("active_status", "").upper() == "T" and s.get("verified", "").upper() == "F":
        logger.info(f"[BYPASS] '{sch_name}': Status=T, Verified=F - passing sheet data directly.")
        bypass_data = {
            "scholarship_name":           sch_name,
            "status":                     "VERIFIED (MANUAL)",
            "application_start_date":     None,
            "application_deadline":       s.get("estimated_timeline"),
            "official_source_url":        s.get("historical_info_link"),
            "official_registration_url":  s.get("historical_reg_link"),
            "supplementary_source_url":   None,
            "processing_method_detected": s.get("historical_method", "Online"),
            "url_verification_fallback_used": False,
            "confidence_score":           1.0,
            "date_precision":             "unknown",
            "remarks":                    s.get("note", "Manually verified — scout bypassed."),
        }
        return {"row_idx": row_idx, "search_status": "BYPASS", "verified_data": bypass_data}

    # ── B2: Config-driven query + URL queue ───────────────────────────────────
    sch_cfg     = get_scholarship_config(sch_name)
    name_parsed = parse_scholarship_name(sch_name)

    if sch_cfg.get("preferred_query"):
        search_query = sch_cfg["preferred_query"]
        logger.info(f"[CONFIG] Using preferred query: {search_query}")
    elif name_parsed["type"] == "uni_to_uni":
        _cur_year = datetime.datetime.now().year
        _adm_year = _cur_year + 1
        search_query = (
            f"{name_parsed['university']} {name_parsed['scholarship']} "
            f"university recommendation application {_cur_year} OR {_adm_year} deadline"
        )
        logger.info(f"[UNI-TO-UNI] Auto query (deadline={_cur_year}, admission={_adm_year}): {search_query}")
    else:
        search_query = f"{sch_name} important date deadline {time.strftime('%Y')}"
    logger.info(f"Search query: '{search_query}'")

    # Per-run domain allowlist — don't mutate the global OFFICIAL_DOMAINS
    run_official_domains = set(OFFICIAL_DOMAINS)
    if sch_cfg.get("preferred_domains"):
        run_official_domains.update(sch_cfg["preferred_domains"])

    # ── B4: Locked mode ───────────────────────────────────────────────────────
    is_locked         = bool(sch_cfg.get("locked_urls"))
    locked_source_note = ""
    search_status     = "SUCCESS"

    if is_locked:
        cur_year    = datetime.datetime.now().year
        locked_urls = [u.format(year=cur_year) for u in sch_cfg["locked_urls"]]
        urls_to_scrape = [(u, "Locked URL") for u in locked_urls]
        search_status  = "LOCKED"
        locked_source_note = (
            "[LOCKED SOURCE] Search engine skipped. Scraped only: "
            + ", ".join(locked_urls)
        )
        logger.info(f"[LOCKED] Skipping search engine. Locked URLs: {locked_urls}")
    else:
        urls_to_scrape  = []
        search_results, search_status = search_scholarship_with_retry(search_query)

        # If search completely failed AND no preferred_urls configured — emit failure result
        if not search_results:
            fallback_preferred = sch_cfg.get("preferred_urls", [])
            if fallback_preferred:
                logger.warning(
                    f"Search failed ({search_status}) but {len(fallback_preferred)} preferred_url(s) "
                    f"configured — falling back to scraping preferred URLs only."
                )
                urls_to_scrape = [(u, "Config Preferred URL (search-failed fallback)") for u in fallback_preferred]
                # Override search_status: we recovered via preferred_urls — don't show NET ERR in email
                search_status = "FALLBACK"
            else:
                remark_map = {
                    "NETWORK_FAILURE": (
                        "[NETWORK FAILURE] Both DuckDuckGo and Yahoo were unreachable "
                        "(SSL/connection error). No web context retrieved. "
                        "Retry manually in a few minutes."
                    ),
                    "BLOCKED": (
                        "[SEARCH BLOCKED] Search engines returned captcha/rate-limit. "
                        "Wait ~5 minutes and re-run."
                    ),
                    "NO_RESULTS": (
                        "[NO RESULTS] Search engines responded but returned 0 parseable "
                        "result links. The scholarship name may need a config override in "
                        "scholarship_config/scholarship_config.py."
                    ),
                }
                remark = remark_map.get(search_status, "[UNKNOWN SEARCH FAILURE]")
                logger.warning(f"Search failed ({search_status}) for '{sch_name}'.")
                return {
                    "row_idx":       row_idx,
                    "search_status": search_status,
                    "verified_data": {
                        "scholarship_name":           sch_name,
                        "status":                     "UNKNOWN",
                        "application_start_date":     None,
                        "application_deadline":       None,
                        "official_source_url":        None,
                        "official_registration_url":  None,
                        "supplementary_source_url":   None,
                        "url_verification_fallback_used": True,
                        "confidence_score":           0.0,
                        "processing_method_detected": "Unknown",
                        "date_precision":             "unknown",
                        "remarks":                    remark,
                    }
                }

        if not urls_to_scrape:
            search_results = search_results or []

        # Build scrape queue: preferred URLs first, then search results
        if not urls_to_scrape:
            preferred_entries = [
                (u, "Config Preferred URL") for u in sch_cfg.get("preferred_urls", [])
            ]
            search_entries = [
                (r["url"], "Search Result") for r in search_results[:5]
            ]
            preferred_set  = {u for u, _ in preferred_entries}
            search_entries = [e for e in search_entries if e[0] not in preferred_set]

            official_entries = [(u, t) for u, t in search_entries if not is_news_domain(u)]
            news_entries     = [(u, t) for u, t in search_entries if is_news_domain(u)]

            urls_to_scrape = preferred_entries + official_entries + news_entries

    # ── Deep scraping & link extraction ──────────────────────────────────────
    scraped_pages        = []
    all_candidate_info   = []
    all_candidate_reg    = []
    fetched_urls         = set()
    branching_count      = 0

    for url, url_type in urls_to_scrape:
        if not url or url in fetched_urls:
            continue

        # Skip binary files — substitute root domain instead
        url_path = urllib.parse.urlparse(url).path.lower()
        if any(url_path.endswith(ext) for ext in BINARY_EXTENSIONS):
            parsed   = urllib.parse.urlparse(url)
            root_url = f"{parsed.scheme}://{parsed.netloc}/"
            logger.info(f"Binary URL detected ({url_path[-4:]}), substituting root domain: {root_url}")
            if root_url not in fetched_urls:
                urls_to_scrape.append((root_url, f"{url_type} (root domain)"))
            continue

        fetched_urls.add(url)
        html = fetch_webpage_content(url)
        if not html:
            continue

        cleaned_text = clean_html(html)
        _char_limit  = sch_cfg.get("scrape_char_limit", 5000)
        truncated_text = cleaned_text[:_char_limit]

        # Translation sub-step for non-English pages
        if sch_cfg.get("needs_translation") and cleaned_text:
            lang_hint   = sch_cfg.get("translation_lang", "auto")
            ascii_ratio = sum(
                1 for c in cleaned_text if c.isascii() and c.isalpha()
            ) / max(len(cleaned_text), 1)
            if ascii_ratio < 0.05:
                logger.info(
                    f"Non-English content (ASCII ratio {ascii_ratio:.2f}). "
                    f"Translating excerpt (lang_hint={lang_hint})..."
                )
                translated   = translate_text(cleaned_text[:500], source_lang=lang_hint)
                cleaned_text = f"[TRANSLATED EXCERPT]:\n{translated}\n\n[ORIGINAL]:\n{cleaned_text}"
                truncated_text = cleaned_text[:_char_limit]

        links      = extract_hyperlinks(html, url)
        candidates = filter_candidate_links(links, sch_name)
        all_candidate_info.extend(candidates["info"])
        all_candidate_reg.extend(candidates["reg"])

        scraped_pages.append({"url": url, "type": url_type, "content": truncated_text})

        # ── Branching: follow sub-links within budget ─────────────────────────
        current_netloc = urllib.parse.urlparse(url).netloc.lower()

        _name_words = [w.lower() for w in sch_name.split() if len(w) > 3]

        def _branch_priority(u: str) -> int:
            p = urllib.parse.urlparse(u).path.lower()
            if any(kw in p for kw in BRANCHING_KEYWORDS):
                return 0
            if any(w in u.lower() for w in _name_words):
                return 1
            return 2

        for sub_url in sorted(candidates["info"], key=_branch_priority):
            if branching_count >= MAX_BRANCHES:
                break
            if sub_url == url or sub_url in fetched_urls:
                continue

            sub_parsed = urllib.parse.urlparse(sub_url)
            sub_netloc = sub_parsed.netloc.lower()
            sub_path   = sub_parsed.path.lower()

            # Skip binary files
            if any(sub_path.endswith(ext) for ext in BINARY_EXTENSIONS):
                logger.debug(f"Skipping binary sub-link: {sub_url}")
                continue

            # Skip known useless URL patterns and noise domains
            if sub_netloc in USELESS_DOMAINS:
                continue
            if any(pat in sub_path for pat in USELESS_PATH_PATTERNS):
                continue

            # Domain restriction: only branch into same domain or known official domains
            is_same_domain = (sub_netloc == current_netloc)
            is_official    = is_official_domain(sub_url)
            if not is_same_domain and not is_official:
                continue

            passes_path_keywords = any(kw in sub_path for kw in BRANCHING_KEYWORDS)
            if passes_path_keywords or is_official:
                fetched_urls.add(sub_url)
                logger.info(f"Following branching sub-link ({branching_count + 1}/{MAX_BRANCHES}): {sub_url}")
                sub_html = fetch_webpage_content(sub_url)
                if sub_html:
                    sub_text       = clean_html(sub_html)[:5000]
                    sub_links      = extract_hyperlinks(sub_html, sub_url)
                    sub_candidates = filter_candidate_links(sub_links, sch_name)
                    all_candidate_info.extend(sub_candidates["info"])
                    all_candidate_reg.extend(sub_candidates["reg"])
                    scraped_pages.append({
                        "url":     sub_url,
                        "type":    "Branching Sub-link",
                        "content": sub_text
                    })
                    branching_count += 1

    # ── Build LLM context string ──────────────────────────────────────────────
    context_str = ""
    for page in scraped_pages:
        context_str += f"\n--- PAGE URL: {page['url']} ({page['type']}) ---\n"
        context_str += page["content"]
        context_str += f"\n{'-'*60}\n"

    if not context_str:
        context_str = "No web context found."
    elif len(context_str) > 12000:
        context_str = context_str[:12000] + "\n... [context truncated for API limits]"

    unique_candidate_info = list(set(all_candidate_info))[:15]
    unique_candidate_reg  = list(set(all_candidate_reg))[:15]

    all_known_urls = set(fetched_urls)
    all_known_urls.update(all_candidate_info)
    all_known_urls.update(all_candidate_reg)

    # ── Build LLM context notes ───────────────────────────────────────────────
    uni_context_note = ""

    if name_parsed["type"] == "uni_to_uni":
        uni_context_note = f"""
IMPORTANT CONTEXT - UNI-TO-UNI SCHOLARSHIP:
This is a UNI-TO-UNI entry. '{name_parsed["scholarship"]}' is being checked specifically
for '{name_parsed["university"]}'. This university manages its own application window -
it may differ from the scholarship body's central portal dates.

RULES FOR THIS ENTRY:
1. For official_source_url and dates: PRIORITISE the university's own page.
2. If the central scholarship body's dates are also found: include them in
   'remarks' (e.g. "Central body deadline: YYYY-MM-DD. University page: YYYY-MM-DD").
3. The university page date is what the user will act on - use it as the primary result.
"""

    date_domain = sch_cfg.get("date_source_domain", "")
    if date_domain:
        uni_context_note += f"""
HARD DATE SOURCE CONSTRAINT - READ AND FOLLOW STRICTLY:
The dates for this scholarship MUST ONLY come from pages on the domain: {date_domain}
  - If you find dates on pages from other domains, you MUST ignore those dates entirely.
  - The official_source_url MUST be a URL on {date_domain}.
  - If NO date information was found on {date_domain} pages in the scraped content, then:
      * Set application_start_date = null
      * Set application_deadline = null
      * Set status = 'CLOSED' (conservative)
      * Set url_verification_fallback_used = true
      * In remarks, state exactly: "REQUIRED SOURCE ({date_domain}) was not accessible or had no dates."
  - DO NOT substitute dates from any other domain, even if that domain has dates.
  - This constraint is ABSOLUTE. There are no exceptions.
"""

    if is_locked:
        uni_context_note += """
LOCKED SOURCE MODE - READ AND FOLLOW STRICTLY:
The pages in the scraped context below are the ONLY sources available for this scholarship.
No search engine was used. These pre-configured URLs are the designated authoritative sources:
  - Treat them as official scholarship pages, NOT as third-party blogs or unofficial sites.
  - Extract all date, status, and link information exclusively from these pages.
  - Do NOT downgrade or dismiss these pages based on their domain name or writing style.
  - These are the definitive sources the operator has verified for this scholarship.
"""

    if sch_cfg.get("context_hint"):
        uni_context_note += f"\n\nADDITIONAL CONTEXT (operator-verified):\n{sch_cfg['context_hint']}\n"

    # ── LLM call ─────────────────────────────────────────────────────────────
    # Note: CerebrasQuotaExceededException intentionally NOT caught here.
    # The caller (run_scout_pipeline) catches it to abort the batch.
    verified_data = verify_scholarship_llama(
        scholarship_name=sch_name,
        scraped_web_text=context_str,
        candidate_info_links=unique_candidate_info,
        candidate_reg_links=unique_candidate_reg,
        model_name=model_name,
        uni_context_note=uni_context_note,
    )

    # ── Post-processing ───────────────────────────────────────────────────────
    verified_data = post_process_result(
        verified_data=verified_data,
        all_known_urls=all_known_urls,
        is_locked=is_locked,
        locked_source_note=locked_source_note,
    )

    return {"row_idx": row_idx, "search_status": search_status, "verified_data": verified_data}


def run_scout_pipeline() -> bool:
    """
    Executes the full daily automated scout pipeline:
      1. Reads ALL active tracking rows (Status=T) from Google Sheet (READ-ONLY).
      2. For each row: A4 bypass check -> config -> search -> scrape -> LLM -> post-process.
      3. On quota exceeded: skips that row, continues batch.
      4. Sends HTML email report.
      5. Saves JSON to scratch/result/.

    The Google Sheet is NEVER written to. Results go to email + JSON only.
    """
    load_dotenv()
    run_ts     = datetime.datetime.now().isoformat()
    model_name = os.getenv("OPENAI_MODEL", os.getenv("OPENAI_MODEL_2", "gpt-oss-120b"))
    start_time = time.time()
    print(f"\n{Colors.BOLD}{Colors.HEADER}======================================================================")
    print("        LAUNCHING AUTOMATED ACADEMIC SCOUT (SHEET: READ-ONLY)")
    print(f"======================================================================{Colors.END}\n")

    # 1. Connect to Google Sheets
    sheet_connector = GoogleSheetsConnector()
    try:
        sheet_connector.connect()
    except Exception as e:
        logger.critical(f"Aborting pipeline. Connection to Google Sheet failed: {str(e)}")
        print(f"\n{Colors.FAIL}[Critical Error] Connection to Google Sheets failed.{Colors.END}\n")
        return False

    scholarships = sheet_connector.read_scholarships()
    total_count  = len(scholarships)

    if total_count == 0:
        logger.warning("No tracking rows found in Google Sheet. Exiting.")
        print(f"{Colors.WARNING}[Warning] Pipeline completed with 0 scholarships loaded.{Colors.END}\n")
        return True

    print(f"{Colors.BOLD}{Colors.CYAN}[Start] Successfully loaded {total_count} scholarship records to verify.{Colors.END}\n")

    processed_results: List[Dict[str, Any]] = []
    quota_exceeded = False

    # 2. Iterate and process each scholarship
    for i, s in enumerate(scholarships, 1):
        row_idx  = s["row_idx"]
        sch_name = s["scholarship_name"]

        # Fast-path skip for internally-funded scholarships (e.g. (Uni-Funded) ...)
        _sn = sch_name.strip()
        if _sn.startswith("("):
            import re as _ire
            _sm = _ire.match(r'^\(([^)]+)\)', _sn)
            if _sm and _sm.group(1).strip().lower() in SKIPPED_PREFIXES:
                print(f"{Colors.BOLD}----------------------------------------------------------------------")
                print(f"[{i}/{total_count}] {Colors.WARNING}[SKIP]{Colors.END} Row {row_idx}: {sch_name}")
                print(f"        Reason: '({_sm.group(1)})' prefix -> no public portal to scrape.")
                print(f"----------------------------------------------------------------------")
                continue

        print(f"{Colors.BOLD}----------------------------------------------------------------------")
        print(f"[{i}/{total_count}] Processing Row {row_idx}: {Colors.CYAN}{sch_name}{Colors.END}")
        print(f"----------------------------------------------------------------------")

        try:
            result = _process_single_scholarship(s, model_name)
            if result is None:
                # _process_single_scholarship returned None -> was skipped internally
                continue

            # Console summary
            vd     = result["verified_data"]
            status = vd.get("status", "?")
            search_status = result.get("search_status", "SUCCESS")
            status_color  = Colors.GREEN if status == "OPEN" else (
                Colors.WARNING if status == "NOT_YET_OPENED" else Colors.FAIL
            )
            print(f"Search Status:   {Colors.BOLD}{search_status}{Colors.END}")
            print(f"Verified Status: {status_color}{Colors.BOLD}{status}{Colors.END}")
            print(f"Start Date:      {Colors.BOLD}{vd.get('application_start_date') or 'Unknown'}{Colors.END}")
            print(f"Deadline:        {Colors.BOLD}{vd.get('application_deadline') or 'Unknown'}{Colors.END}")
            print(f"Info URL:        {vd.get('official_source_url') or '-'}")
            print(f"Reg URL:         {vd.get('official_registration_url') or '-'}")
            print(f"Confidence:      {vd.get('confidence_score')}")
            print(f"Remarks:         {vd.get('remarks')}")

            processed_results.append(result)

        except CerebrasQuotaExceededException as qe:
            logger.critical(
                f"Cerebras queue still exceeded for '{sch_name}' after all retries: {str(qe)}"
            )
            print(
                f"{Colors.FAIL}[QUOTA EXHAUSTED] All retries failed for '{sch_name}' — "
                f"skipping this row and continuing batch.{Colors.END}"
            )
            processed_results.append({
                "row_idx":       row_idx,
                "search_status": "QUOTA_EXCEEDED",
                "verified_data": {
                    "scholarship_name":               sch_name,
                    "status":                         "UNKNOWN",
                    "application_start_date":         None,
                    "application_deadline":           None,
                    "official_source_url":            None,
                    "official_registration_url":      None,
                    "supplementary_source_url":       None,
                    "url_verification_fallback_used": True,
                    "confidence_score":               0.0,
                    "processing_method_detected":     "Unknown",
                    "date_precision":                 "unknown",
                    "remarks": (
                        "[QUOTA EXCEEDED] Cerebras server queue was still congested after "
                        "4 retries (10s + 20s + 30s backoff). Re-run this scholarship "
                        "later, ideally between 1PM-7PM WIB when US server traffic is lowest."
                    )
                }
            })

        except Exception as ex:
            logger.error(f"Failed to process '{sch_name}' (row {row_idx}): {str(ex)}", exc_info=True)
            print(f"{Colors.FAIL}[Failed] Verification step failed for '{sch_name}': {str(ex)}{Colors.END}")
            # Store an error sentinel so the row still appears in the email
            processed_results.append({
                "row_idx":       row_idx,
                "search_status": "ERROR",
                "verified_data": {
                    "scholarship_name":           sch_name,
                    "status":                     "UNKNOWN",
                    "application_start_date":     None,
                    "application_deadline":       None,
                    "official_source_url":        None,
                    "official_registration_url":  None,
                    "supplementary_source_url":   None,
                    "url_verification_fallback_used": True,
                    "confidence_score":           0.0,
                    "processing_method_detected": "Unknown",
                    "date_precision":             "unknown",
                    "remarks":                    f"System error: {str(ex)}"
                }
            })

        # Courtesy delay between rows to avoid hammering search engines
        if i < total_count and not quota_exceeded:
            time.sleep(2)

    print(f"\n{Colors.BOLD}{Colors.GREEN}======================================================================")
    print(f"       ALL {len(processed_results)}/{total_count} ROWS PROCESSED")
    print(f"======================================================================{Colors.END}\n")

    # 3. Dispatch email report
    try:
        print(f"{Colors.BLUE}Compiling and dispatching styled HTML digest report email...{Colors.END}")
        email_sent = send_daily_email_report(processed_results, quota_exceeded=quota_exceeded)
        if email_sent:
            print(f"{Colors.GREEN}[Success] Daily report email sent successfully.{Colors.END}\n")
        else:
            print(f"{Colors.WARNING}[Warning] Email skipped or failed. Verify SMTP settings.{Colors.END}\n")
    except Exception as mail_err:
        logger.error(f"Email dispatch failed: {str(mail_err)}")
        print(f"{Colors.FAIL}[Failed] Email Dispatch Failure: {str(mail_err)}{Colors.END}\n")

    # 5. Save result JSON
    if processed_results:
        save_result_json(run_ts, model_name, processed_results)

    quota_rows = sum(1 for r in processed_results if r.get("search_status") == "QUOTA_EXCEEDED")
    duration = (time.time() - start_time) / 60
    print(f"{Colors.BOLD}{Colors.HEADER}======================================================================")
    print(f"      PIPELINE RUN FINISHED IN {duration:.2f} MINUTES!")
    if quota_rows:
        print(f"      NOTE: {quota_rows} row(s) skipped — Cerebras still congested after all retries.")
        print(f"      Best time to re-run skipped rows: 1PM-7PM WIB (low US server traffic).")
    print(f"======================================================================{Colors.END}\n")

    return True  # individual row quota failures are not a fatal pipeline error


if __name__ == "__main__":
    success = run_scout_pipeline()
    sys.exit(0 if success else 1)
