# SchReminder Scout — Implementation Plan (Rev 4)

## Background

Full-batch test of `sch_prototype.py` across ~30 scholarships revealed problems in 9 areas and 8 enhancement notes. Root cause analysis identified **three distinct failure modes** that were previously indistinguishable. This plan addresses all of them in 4 phases (A → D), consolidating every item from Rev 3 plus new findings, and is the authoritative plan going forward.

---

## Confirmed Spreadsheet Column Map

| Col | Header | `col_map` key |
|-----|--------|--------------|
| A | `Periode` | _(not used by engine)_ |
| B | `Note` | `"note"` ← **NEW** |
| C | `Status` | `"active_status"` ✅ |
| D | `Verified` | `"verified"` ← **NEW** |
| E | `Name` | `"scholarship_name"` ✅ |
| F | `Country/Region` | `"country_region"` ✅ |
| G | `Est. Date` | `"estimated_timeline"` ✅ |
| H | `Reg. Path` | `"historical_method"` ✅ |
| I | `Info Link` | `"historical_info_link"` ✅ |
| J | `Reg. Link` | `"historical_reg_link"` ✅ |
| K | `Status` | `"status"` (scout output) ✅ |

---

## Root Cause Summary (from full batch test)

Three distinct failure modes identified. Previously all silently reported as
`"No web context or candidate..."` in Remarks — impossible to distinguish.
Each has a different fix.

| Failure Mode | What Happens | Scholarships Affected |
|---|---|---|
| **NETWORK_FAILURE** | DuckDuckGo SSL error + Yahoo unreachable. Scraper gets 0 URLs. LLM receives empty context. | Inpex, BIM, Sultan Qaboos, HDR, EGYAID (transient) |
| **BLOCKED** | Search engine returns captcha/rate-limit page. Same result but different root cause (IP rate-limited). | Varies by run |
| **NO_RESULTS** | Search succeeds but 0 parseable `algo` elements returned. | Rare |

---

## Confirmed Decisions

| Topic | Decision |
|-------|----------|
| T+F bypass trigger | `Status=T` AND `Verified=F` → skip search+LLM, email sheet data |
| T+T behaviour | Full pipeline (search + LLM + email) |
| ANSO split | Two separate sheet rows: `(ANSO Scholarship) UCAS` and `(ANSO Scholarship) USTC` |
| LPDP names/Any Scholarship with has different registration window (Usage of words Tahap / Phase) | `LPDP STEM Industri Strategis (Tahap 1)` and `LPDP STEM Industri Strategis (Tahap 2)` |
| Uni-to-uni date conflict | University page date wins in email; scholarship body date shown in Remarks |
| Uni-to-uni method field | LLM detects from page content (not hardcoded) |
| Result JSON | One file per run, stored in `scratch/result/` — filename: `{timestamp}_{slug}.json` |
| Network remark distinction | `NETWORK_FAILURE` and `BLOCKED` get different remarks AND different email cell color |

---

## User Review Required

> [!IMPORTANT]
> All design questions from the previous revision are answered. Review the new sections before approving execution:
> - **A3 (Network Error)**: Now includes both remark differentiation AND a proper second-round retry and `search_status` field that flows through to email color (grey/orange/light-grey cells)
> - **B1 (Scholarship Config)**: MTCP entry added with preferred sub-page URLs (not just a note)
> - **Phase ordering changed**: A1 → A3 → A2 → A4 → B1 → B2 → B3 → C1 → C2 → D

> [!WARNING]
> **Phase B3 (Uni-to-Uni)** adds a name-parsing step that changes search behaviour for any entry matching the `(X) Y` pattern. Confirmed safe — regex requires parenthesised part to be at the **start** of the name. `(Beasiswa Indonesia Bangkit (BIB) - LPDP)` is not affected. Blocklist `_UNI_TO_UNI_SKIP_PREFIXES` handles `(Uni-Funded)` prefix entries.

---

## Phase A — Quick Wins
*Additive changes only. Zero pipeline risk. Execute in order: A1 → A3 → A2 → A4.*

---

### A1 — Result Persistence: `scratch/result/` JSON Folder (Note #8)

**Problem**: Every run's result disappears after the terminal closes. No history to compare across runs or debug regressions.

##### [MODIFY] [sch_prototype.py](file:///c:/Work/schreminder/scratch/sch_prototype.py)

Add `save_result_json()` helper and call it at the end of `run_comparison()`.
One file per run. Filename: `{timestamp}_{slug}.json`.

```python
import datetime
import re

def save_result_json(scholarship_name: str, model_used: str,
                     search_status: str, processed_results: list) -> None:
    results_dir = os.path.join("c:/Work/schreminder/scratch/result")
    os.makedirs(results_dir, exist_ok=True)

    slug = re.sub(r'[^a-z0-9_]', '_', scholarship_name.lower().strip())[:40]
    ts   = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    filename = f"{ts}_{slug}.json"
    filepath = os.path.join(results_dir, filename)

    payload = {
        "run_ts":           datetime.datetime.now().isoformat(),
        "scholarship_name": scholarship_name,
        "model_used":       model_used,
        "search_status":    search_status,   # 'SUCCESS' | 'NETWORK_FAILURE' | 'BLOCKED' | 'NO_RESULTS' | 'BYPASS'
        "results":          processed_results,
    }
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    logger.info(f"Result saved → {filepath}")
```

Call at end of `run_comparison()`:
```python
save_result_json(sch_name, model_name, search_status, processed_results)
```

Files are never auto-deleted. Full history is retained.

---

### A3 — Network Error Differentiation + Smarter Retry (Issue #7 / Note #7)

**Problem**: All failure modes produce the same `"No web context found."` remark and identical email appearance. Impossible to distinguish a network outage from a bad search result. Transient failures (EGYAID) fail permanently when a 60-second wait would have fixed them.

**Solution**: Two-part fix — (1) proper second-round retry with jitter sleep, (2) `search_status` enum that flows search → remarks → email rendering.

##### [MODIFY] [sch_prototype.py](file:///c:/Work/schreminder/scratch/sch_prototype.py)

**Part 1 — `search_status` Enum**

`search_scholarship_with_retry()` signature changes to return a tuple:
```python
def search_scholarship_with_retry(query: str, ...) -> tuple[Optional[list], str]:
    """Returns (results, search_status)"""
```

| `search_status` value | Meaning |
|-----------------------|---------|
| `"SUCCESS"` | ≥1 page scraped successfully |
| `"NETWORK_FAILURE"` | Both DDG + Yahoo raised connection/SSL/timeout errors |
| `"BLOCKED"` | Both DDG + Yahoo returned captcha/rate-limit response |
| `"NO_RESULTS"` | Responses received but 0 parseable result elements |

**Part 2 — Bonus Retry Round**

After both DDG + Yahoo fail on Round 1, sleep `60 + random.uniform(-5, 5)` seconds then try the full DDG → Yahoo sequence once more before giving up.

```
Round 1:  DDG → fail → Yahoo → fail
          sleep(60 ± 5s)
Round 2:  DDG → fail → Yahoo → fail → set search_status, abort
```

```python
import random

def search_scholarship_with_retry(query: str, max_results: int = 5) -> tuple:
    last_error_type = "NETWORK_FAILURE"

    for round_num in range(1, 3):  # 2 full rounds
        # --- try DuckDuckGo ---
        try:
            ddg_result = _try_duckduckgo(query, max_results)
            if ddg_result is not None:
                return (ddg_result, "SUCCESS")
            last_error_type = "BLOCKED"
        except (requests.exceptions.SSLError,
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as e:
            logger.warning(f"DDG network error (round {round_num}): {e}")
            last_error_type = "NETWORK_FAILURE"

        # --- try Yahoo fallback ---
        yahoo_result = _try_yahoo(query, max_results)
        if yahoo_result:
            return (yahoo_result, "SUCCESS")

        # --- both failed this round ---
        if round_num == 1:
            sleep_s = 60 + random.uniform(-5, 5)
            logger.info(f"Both engines failed. Sleeping {sleep_s:.0f}s then retrying (round 2)...")
            time.sleep(sleep_s)

    return (None, last_error_type)
```

**Part 3 — What happens when `search_status != "SUCCESS"`**

Skip the LLM entirely (empty context = wasted tokens). Build a synthetic result
with a differentiated remark, then save + email immediately:

```python
search_results, search_status = search_scholarship_with_retry(search_query)

if search_status != "SUCCESS" or not search_results:
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
            "scholarship_config.py."
        ),
    }
    remark = remark_map.get(search_status, "[UNKNOWN SEARCH FAILURE]")

    processed_results.append({
        "row_idx":      matched_row["row_idx"],
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
            "remarks":                    remark,
        }
    })
    save_result_json(sch_name, model_name, search_status, processed_results)
    send_scout_report_email(processed_results, quota_exceeded)
    return
```

**Part 4 — Email rendering for `search_status`**

In `send_scout_report_email()`, read `search_status` from the result dict:

```python
search_status_val = r.get("search_status", "SUCCESS")
status            = data.get("status", "CLOSED")

if search_status_val == "NETWORK_FAILURE":
    status_color = "#95a5a6"    # grey
    status_label = "⚡ NET ERR"
elif search_status_val == "BLOCKED":
    status_color = "#e67e22"    # dark orange
    status_label = "🚫 BLOCKED"
elif search_status_val == "NO_RESULTS":
    status_color = "#bdc3c7"    # light grey
    status_label = "❓ NO DATA"
elif search_status_val == "BYPASS":
    status_color = "#8e44ad"    # purple
    status_label = "✅ VERIFIED"
else:
    status_color = "#2ecc71" if status == "OPEN" else (
        "#f39c12" if status == "NOT_YET_OPENED" else "#e74c3c"
    )
    status_label = status
```

The email cell shows a clearly distinct colour per failure mode — no more all-red CLOSED for network issues.

---

### A2 — Start Date Estimation from End Date Only (Note #2)

**Problem**: Many pages only show the deadline. Status correctly shows `CLOSED` if deadline passed — but if deadline is in the future, the user has no start-date reference. Also previously, a scholarship with only an end date would show `CLOSED` (conservative) even if it was currently open.

**Solution**: Post-processing after LLM call. Subtract 90 days from deadline to estimate start.

##### [MODIFY] [sch_prototype.py](file:///c:/Work/schreminder/scratch/sch_prototype.py)

```python
from datetime import date, timedelta

# ── START DATE ESTIMATION ────────────────────────────────────────────────────
start_date = verified_data.get("application_start_date")
end_date   = verified_data.get("application_deadline")

if start_date is None and end_date is not None:
    try:
        end_dt   = date.fromisoformat(end_date)
        start_dt = end_dt - timedelta(days=90)
        verified_data["application_start_date"] = start_dt.isoformat()
        verified_data["remarks"] = (
            (verified_data.get("remarks") or "") +
            " [Start date estimated: only deadline found — start = deadline − 90 days.]"
        ).strip()
        logger.info(
            f"Start date estimated: {start_dt.isoformat()} "
            f"(deadline − 90 days from {end_date})"
        )
    except ValueError:
        pass  # unparseable end_date — leave start as None
```

> [!NOTE]
> Status re-evaluation runs inside the LLM. This estimation adds a `start_date` **after** the LLM call — it does NOT override the LLM's status decision. The status safety net (OPEN without a deadline → forced CLOSED) remains unchanged. After estimation, the status safety net re-runs to check if the estimated window makes the scholarship OPEN.

---

### A4 — Status=T + Verified=F → Direct Email Bypass (Note #1)

**Problem**: Scholarships with `Status=T` (active) and `Verified=F` (manually confirmed data) should not waste API calls on a web search. The data is already known-good in the sheet.

**Logic Table:**

| Col C `Status` | Col D `Verified` | Action |
|----------------|-----------------|--------|
| `T` | `T` | ✅ Full pipeline — search + LLM + email |
| `T` | `F` | ⏭️ **Bypass** — skip search & LLM, read sheet cols, email only |
| not `T` | any | Already skipped by existing guard |

##### [MODIFY] [google_sheets.py](file:///c:/Work/schreminder/src/spreadsheet/google_sheets.py)

Add new keys to `expected_inputs`:
```python
"verified": ["Verified"],   # Col D
"note":     ["Note"],       # Col B
```

##### [MODIFY] [sch_prototype.py](file:///c:/Work/schreminder/scratch/sch_prototype.py)

Add `get_cell_link()` helper alongside existing `get_cell_text()`:
```python
def get_cell_link(field_key: str) -> Optional[str]:
    col_idx = conn.col_map.get(field_key)
    if col_idx and col_idx <= len(cells):
        cell = cells[col_idx - 1]
        return cell.get("hyperlink") or cell.get("formattedValue") or None
    return None
```

Bypass block — add immediately after row match, before search:
```python
col_c_val = get_cell_text("active_status")   # Col C
col_d_val = get_cell_text("verified")        # Col D

if col_c_val.upper() == "T" and col_d_val.upper() == "F":
    logger.info(
        f"[BYPASS] '{sch_name}': Status=T, Verified=F → "
        f"emailing sheet data directly (no search/LLM)."
    )
    bypass_data = {
        "scholarship_name":           sch_name,
        "status":                     "VERIFIED (MANUAL)",
        "application_start_date":     None,
        "application_deadline":       get_cell_text("estimated_timeline"),     # Col G verbatim
        "official_source_url":        get_cell_link("historical_info_link"),   # Col I
        "official_registration_url":  get_cell_link("historical_reg_link"),    # Col J
        "processing_method_detected": get_cell_text("historical_method"),      # Col H
        "supplementary_source_url":   None,
        "url_verification_fallback_used": False,
        "confidence_score":           1.0,
        "remarks":                    get_cell_text("note"),                   # Col B verbatim
    }
    processed_results.append({
        "row_idx":       matched_row["row_idx"],
        "search_status": "BYPASS",
        "verified_data": bypass_data,
    })
    save_result_json(sch_name, model_name, "BYPASS", processed_results)
    send_scout_report_email(processed_results, quota_exceeded)
    return
```

> [!NOTE]
> Col G (`Est. Date`) is placed verbatim in `application_deadline`. No date parsing attempted for bypass rows. Col B (`Note`) is copied as-is into `remarks`. Email renders purple `✅ VERIFIED` cell (see A3 Part 4 email renderer).

---

## Phase B — Per-Scholarship Config Table

**What is the config table?**
A new Python dict file (`scholarship_config.py`) where each key is a scholarship name from the spreadsheet. The engine checks this dict *before* running its normal search. If a match is found, it overrides the search query and/or injects specific URLs at the front of the scrape queue. Scholarships with no config entry run the normal pipeline — nothing changes for them.

---

### B1 — New File: `scholarship_config.py`

##### [NEW] [scholarship_config.py](file:///c:/Work/schreminder/scratch/scholarship_config.py)

```python
"""
Per-scholarship configuration overrides.
Key   = scholarship name exactly as written in spreadsheet (case-insensitive match at runtime).
Value = dict of overrides (all optional):
  preferred_query          : str   — replaces the auto-generated search query
  preferred_urls           : list  — injected at the FRONT of the scrape queue
  preferred_domains        : list  — temporarily added to OFFICIAL_DOMAINS for this run only
  needs_translation        : bool  — translate scraped non-English text before LLM
  translation_lang         : str   — source language code hint (e.g. "kk", "ru")
  date_precision_expected  : str   — hint to email renderer ("monthly", "quarterly")
  notes                    : str   — human-readable note (ignored by engine)
"""

SCHOLARSHIP_CONFIG = {

    # ── JAPAN ──────────────────────────────────────────────────────────────────
    "MEXT (Monbukagakusho) - Research Student": {
        "preferred_query":   "MEXT Research Student Scholarship 2026 Indonesia embassy deadline application",
        "preferred_urls":    ["https://www.id.emb-japan.go.jp/itpr_id/sch_rs.html"],
        "preferred_domains": ["id.emb-japan.go.jp"],
        "notes": (
            "Always scrape Indonesian embassy. studyinjapan.go.jp is a generic global portal, "
            "NOT Indonesia-specific. Embassy page is authoritative for Indonesian applicants."
        ),
    },

    # ── KOREA ──────────────────────────────────────────────────────────────────
    "Global Korea Scholarship (GKS) - Graduate": {
        "preferred_query":   "GKS Global Korea Scholarship 2026 Indonesia graduate deadline niied",
        "preferred_urls":    [
            "https://gksscholarship.com/",
            "https://www.niied.go.kr/",
        ],
        "preferred_domains": ["gksscholarship.com", "niied.go.kr"],
        "notes": (
            "studyinkorea.go.kr returns Korean-language national archive pages. "
            "gksscholarship.com has Indonesia-specific cycle dates. NIIED is the issuing body."
        ),
    },

    # ── IRELAND (GOI-IES) ─────────────────────────────────────────────────────
    "Government of Ireland International Education Scholarship (GOI-IES)": {
        "preferred_query":   "Government of Ireland International Education Scholarship HEA 2026 deadline",
        "preferred_urls":    ["https://hea.ie/policy/internationalisation/goi-ies/"],
        "preferred_domains": ["hea.ie"],
        "notes": (
            "HEA Ireland (Higher Education Authority) is the issuer. "
            "Ranks ~#5 in generic searches. Must inject directly."
        ),
    },

    # ── IRELAND (GO-PSP) ──────────────────────────────────────────────────────
    "Government of Ireland Postgraduate Scholarship Programme (GO-PSP)": {
        "preferred_query":   "Government of Ireland Postgraduate Scholarship Programme 2026 IRC deadline",
        "preferred_urls":    ["https://research.ie/funding/goipg/"],
        "preferred_domains": ["research.ie"],
        "notes": "Irish Research Council (IRC) is the issuer. Different from GOI-IES.",
    },

    # ── KAZAKHSTAN ────────────────────────────────────────────────────────────
    "Kazakhstan Government Scholarship (Bolashak)": {
        "preferred_query":    "Bolashak Kazakhstan Government Scholarship 2026 deadline English",
        "preferred_urls":     [
            "https://www.bolashak.gov.kz/en/scholarship-program",
            "https://konkurs.bolashak.gov.kz/",
        ],
        "preferred_domains":  ["bolashak.gov.kz", "konkurs.bolashak.gov.kz"],
        "needs_translation":  True,
        "translation_lang":   "ru",
        "notes": (
            "Site defaults to Kazakh/Russian. Try English subdomain first. "
            "konkurs.bolashak.gov.kz is the registration portal. "
            "Translation sub-step fires if ASCII ratio < 5%."
        ),
    },

    # ── MALAYSIA (MTCP) ───────────────────────────────────────────────────────
    "MTCP Scholarship": {
        "preferred_query":   "MTCP Malaysia Technical Cooperation Programme scholarship 2026 deadline application",
        "preferred_urls":    [
            "https://mtcp.kln.gov.my/scholarship",
            "https://mtcp.kln.gov.my/news",
            "https://mtcp.kln.gov.my/announcement",
        ],
        "preferred_domains": ["mtcp.kln.gov.my"],
        "notes": (
            "Main page (mtcp.kln.gov.my/scholarship) embeds dates in IMAGES — "
            "scraper cannot read them. Branching into /news and /announcement sub-pages "
            "may find text-based deadline notices. If still no dates found, "
            "remark will explicitly say [NO_RESULTS] — not silently empty."
        ),
    },

    # ── GERMANY / DAAD ────────────────────────────────────────────────────────
    "DAAD STEM Discipline": {
        "preferred_query":   "DAAD STEM scholarship 2026 deadline Germany engineering sciences application",
        "preferred_urls":    [
            "https://www2.daad.de/deutschland/stipendium/datenbank/en/21148-scholarship-database/?origin=5&status=3&subjectGrps=&daad=&q=&page=1&detail=57742130#voraussetzungen",
        ],
        "preferred_domains": ["daad.de"],
        "notes": (
            "Param-based DB URL (detail=57742130) is NEVER indexed by search engines. "
            "Direct injection required. daad.org/en/2025/... is a news page, not the official DB entry."
        ),
    },

    "DAAD EPOS": {
        "preferred_query":   "DAAD EPOS scholarship 2026 deadline postgraduate application Germany",
        "preferred_urls":    [
            "https://www2.daad.de/deutschland/stipendium/datenbank/en/21148-scholarship-database/?origin=5&status=3&subjectGrps=&daad=",
            "https://www.daad.de/en/",
        ],
        "preferred_domains": ["daad.de"],
        "notes": "DAAD EPOS — use scholarship database, not news articles.",
    },

    # ── HYUNDAI CMK ───────────────────────────────────────────────────────────
    "Hyundai Motor Chung Mong-Koo Global Scholarship": {
        "preferred_query":         "Hyundai CMK Foundation Global Scholarship 2026 graduate deadline",
        "preferred_urls":          ["https://www.cmkfoundation-globalscholarship.org/work/graduates"],
        "preferred_domains":       ["cmkfoundation-globalscholarship.org"],
        "date_precision_expected": "monthly",
        "notes": (
            "Dates published as month ranges only (e.g. Dec-Jan, Jun-Jul). "
            "LLM date_precision_expected hint tells it to infer first/last of month."
        ),
    },

    # ── LPDP ─────────────────────────────────────────────────────────────────
    "LPDP STEM Industri Strategis (Tahap 1)": {
        "preferred_query":   "LPDP STEM Industri Strategis Tahap 1 2026 jadwal pendaftaran timeline",
        "preferred_urls":    ["https://beasiswalpdp.kemenkeu.go.id/"],
        "preferred_domains": ["beasiswalpdp.kemenkeu.go.id", "lpdp.kemenkeu.go.id"],
    },
    "LPDP STEM Industri Strategis (Tahap 2)": {
        "preferred_query":   "LPDP STEM Industri Strategis Tahap 2 2026 jadwal pendaftaran timeline",
        "preferred_urls":    ["https://beasiswalpdp.kemenkeu.go.id/"],
        "preferred_domains": ["beasiswalpdp.kemenkeu.go.id", "lpdp.kemenkeu.go.id"],
    },

    # ── ANSO (uni-to-uni — separate rows, handled by B3 parser) ──────────────
    "(ANSO Scholarship) UCAS": {
        "preferred_urls":    ["https://english.ucas.ac.cn/index.php/admission/international-students/deadline"],
        "preferred_domains": ["ucas.ac.cn", "anso.org.cn"],
        "notes": "CAS-ANSO via UCAS. University page is the authoritative source for deadline.",
    },
    "(ANSO Scholarship) USTC": {
        "preferred_urls":    [
            "https://en.ustc.edu.cn/",
            "https://en.ustc.edu.cn/info/1043/3098.htm",
        ],
        "preferred_domains": ["ustc.edu.cn", "anso.org.cn"],
        "notes": "ANSO via University of Science and Technology of China. Second URL is the known ANSO admissions page.",
    },

    # ── ADB-JSP (uni-to-uni — handled by B3 parser) ───────────────────────────
    "(ADB-JSP Scholarship) Institute of Science Tokyo": {
        "preferred_urls":    ["https://www.isct.ac.jp/en/"],
        "preferred_domains": ["isct.ac.jp", "adb.org"],
        "notes": "ADB-JSP at Institute of Science Tokyo (formerly Tokyo Tech). Check English admissions page.",
    },
    "(ADB-JSP Scholarship) Keio University": {
        "preferred_urls":    ["https://www.keio.ac.jp/en/"],
        "preferred_domains": ["keio.ac.jp", "adb.org"],
        "notes": "ADB-JSP at Keio University. Check English graduate admissions page.",
    },
}


def get_scholarship_config(name: str) -> dict:
    """Case-insensitive exact-name lookup. Returns {} if no config found."""
    name_lower = name.strip().lower()
    for key, cfg in SCHOLARSHIP_CONFIG.items():
        if key.strip().lower() == name_lower:
            return cfg
    return {}
```

---

### B2 — Integration into `sch_prototype.py` (Config-Driven Scraping)

##### [MODIFY] [sch_prototype.py](file:///c:/Work/schreminder/scratch/sch_prototype.py)

In `run_comparison()`, after bypass check, before search call:

```python
from scholarship_config import get_scholarship_config

sch_cfg = get_scholarship_config(sch_name)
parsed  = parse_scholarship_name(sch_name)   # from Phase B3

# 1. Build search query
if sch_cfg.get("preferred_query"):
    search_query = sch_cfg["preferred_query"]
    logger.info(f"[CONFIG] Using preferred query: {search_query}")
elif parsed["type"] == "uni_to_uni":
    search_query = (
        f"{parsed['scholarship']} {parsed['university']} "
        f"2026 deadline application scholarship"
    )
    logger.info(f"[UNI-TO-UNI] Auto query: {search_query}")
else:
    search_query = f"{sch_name} important date deadline {time.strftime('%Y')}"

# 2. Per-run domain allowlist (don't mutate the global OFFICIAL_DOMAINS)
run_official_domains = set(OFFICIAL_DOMAINS)
if sch_cfg.get("preferred_domains"):
    run_official_domains.update(sch_cfg["preferred_domains"])

# 3. Search (now returns tuple)
search_results, search_status = search_scholarship_with_retry(search_query)

# 4. Build scrape queue — preferred URLs FIRST, then search results
preferred_entries = [
    (u, "Config Preferred URL") for u in sch_cfg.get("preferred_urls", [])
]
search_entries = [
    (r["url"], "Search Result") for r in (search_results or [])[:5]
]
# Deduplicate: don't re-scrape preferred URLs if search also returned them
preferred_set  = {u for u, _ in preferred_entries}
search_entries = [e for e in search_entries if e[0] not in preferred_set]

# Official-first ordering among search entries (existing logic)
official_entries = [(u, t) for u, t in search_entries if not is_news_domain(u)]
news_entries     = [(u, t) for u, t in search_entries if is_news_domain(u)]

urls_to_scrape = preferred_entries + official_entries + news_entries
```

**Translation sub-step** (for `needs_translation: True` config entries):

```python
def translate_text(text: str, source_lang: str = "auto", target_lang: str = "en") -> str:
    try:
        resp = requests.get(
            "https://api.mymemory.translated.net/get",
            params={"q": text, "langpair": f"{source_lang}|{target_lang}"},
            timeout=10
        )
        if resp.ok:
            return resp.json().get("responseData", {}).get("translatedText", text)
    except Exception as e:
        logger.warning(f"Translation failed ({e}). Using original text.")
    return text
```

Applied per page after `clean_html()`:
```python
if sch_cfg.get("needs_translation") and cleaned_text:
    lang_hint   = sch_cfg.get("translation_lang", "auto")
    ascii_ratio = sum(1 for c in cleaned_text if c.isascii() and c.isalpha()) / max(len(cleaned_text), 1)
    if ascii_ratio < 0.05:
        logger.info(f"Non-English content (ASCII ratio {ascii_ratio:.2f}). Translating excerpt...")
        translated   = translate_text(cleaned_text[:500], source_lang=lang_hint)
        cleaned_text = f"[TRANSLATED EXCERPT]:\n{translated}\n\n[ORIGINAL]:\n{cleaned_text}"
```

---

### B3 — Uni-to-Uni Schema

Full support for scholarships that require checking individual university websites rather than a central scholarship portal.

#### Naming Convention

```
(Scholarship Name) University Name
```

Examples:
```
(ANSO Scholarship) UCAS
(ANSO Scholarship) USTC
(ADB-JSP Scholarship) Institute of Science Tokyo
(ADB-JSP Scholarship) Keio University
```

Normal scholarships never start with `(` — detection is unambiguous.

#### B3a — Name Parser Function

##### [MODIFY] [sch_prototype.py](file:///c:/Work/schreminder/scratch/sch_prototype.py)

New helper added near the top of the file (after imports):

```python
import re as _re

# Parenthesised prefixes that are category tags, NOT scholarship body names.
_UNI_TO_UNI_SKIP_PREFIXES = {
    "uni-funded",   # e.g. (Uni-Funded) Leiden University Excellence Scholarships
}

def parse_scholarship_name(name: str) -> dict:
    """
    Detects uni-to-uni naming pattern: (Scholarship Name) University Name

    Returns:
      { "type": "centralized", "display_name": name }
      { "type": "uni_to_uni", "scholarship": "ANSO Scholarship",
        "university": "UCAS", "display_name": name }
    """
    match = _re.match(r'^\((.+?)\)\s+(.+)$', name.strip())
    if match:
        prefix = match.group(1).strip().lower()
        if prefix not in _UNI_TO_UNI_SKIP_PREFIXES:
            return {
                "type":        "uni_to_uni",
                "scholarship": match.group(1).strip(),
                "university":  match.group(2).strip(),
                "display_name": name,
            }
    return {"type": "centralized", "display_name": name}
```

Adding new category-tag prefixes in the future only requires appending to `_UNI_TO_UNI_SKIP_PREFIXES`.

#### B3b — LLM Prompt Addition for Uni-to-Uni Context

When `parsed["type"] == "uni_to_uni"`, inject a context note into the LLM user prompt:

```python
if parsed["type"] == "uni_to_uni":
    uni_context_note = f"""
IMPORTANT CONTEXT — UNI-TO-UNI SCHOLARSHIP:
This is a UNI-TO-UNI entry. '{parsed["scholarship"]}' is being checked specifically
for '{parsed["university"]}'. This university manages its own application window —
it may differ from the scholarship body's central portal dates.

RULES FOR THIS ENTRY:
1. For official_source_url and dates: PRIORITISE the university's own page.
2. If the central scholarship body's dates are also found: include them in
   'remarks' (e.g. "Central body deadline: YYYY-MM-DD. University page: YYYY-MM-DD").
3. The university page date is what the user will act on — use it as the primary result.
"""
else:
    uni_context_note = ""
```

Prepend `uni_context_note` to the `user_prompt` string before sending to LLM.

#### B3c — Email Display for Uni-to-Uni

In `send_scout_report_email()`, the scholarship name cell gets a small badge:

```python
parsed = parse_scholarship_name(data.get("scholarship_name", ""))
if parsed["type"] == "uni_to_uni":
    name_display = (
        f'{data.get("scholarship_name")} '
        f'<span style="background:#8e44ad;color:white;font-size:10px;'
        f'padding:1px 5px;border-radius:3px;vertical-align:middle;">UNI-TO-UNI</span>'
    )
else:
    name_display = data.get("scholarship_name", "")
```

---

### B Summary — What the Config Table Solves

| Scholarship | Root Cause | Config Fix |
|-------------|-----------|-----------|
| MEXT Research Student | Generic portal instead of embassy | `preferred_urls` → Indonesian embassy |
| GKS Graduate | Korean portal vs Indonesia-specific site | `preferred_urls` → gksscholarship.com |
| GOI-IES | Ranks #5+ in generic search | `preferred_urls` → hea.ie direct |
| Kazakhstan Bolashak | Kazakh/Russian language barrier | English subdomain + translation fallback |
| MTCP | Dates in images on main page | Branch into `/news`, `/announcement` sub-pages |
| DAAD STEM | Param URL never indexed | `preferred_urls` → hardcoded deep link |
| Hyundai CMK | Month-range dates only | `date_precision_expected: monthly` + C1 inference |
| LPDP Tahap 1 & 2 | Mixed dates from wrong cycle | Phase-specific queries per row |
| ANSO UCAS/USTC | Two institutions, one old name | Separate rows + uni-to-uni detection |
| ADB-JSP | Uni-to-uni, no central portal | Uni-to-uni detection + university URLs |

---

## Phase C — LLM Prompt Enhancements

### C1 — `date_precision` Field + Monthly Date Inference (Note #5)

##### [MODIFY] [sch_prototype.py](file:///c:/Work/schreminder/scratch/sch_prototype.py)

**New JSON field #12 added to system prompt:**
```
12. "date_precision": Strictly one of: 'exact' | 'monthly' | 'quarterly' | 'unknown'
    - 'exact'     : Specific YYYY-MM-DD dates found
    - 'monthly'   : Only month names or ranges stated (e.g. "December - January")
    - 'quarterly' : Quarter or semester mentioned (e.g. "Q1 2026", "Semester 1")
    - 'unknown'   : No date information found at all
```

**New system prompt instruction:**
```
DATE INFERENCE FOR MONTH-RANGE SOURCES:
If dates are stated as month name ranges only (e.g. "December - January" or "Jun - Jul"):
  - application_start_date = first day of start month  -> YYYY-MM-01
  - application_deadline   = last day of end month     -> use calendar (Jan=31, Apr=30, etc.)
  - Use the nearest upcoming cycle year. Example: if today is June 2026 and the source
    says "Dec - Jan", use Dec 2026 - Jan 2027.
  - Set date_precision = 'monthly'
For quarters: infer first/last day of the quarter. Set date_precision = 'quarterly'.
```

**Email rendering**: When `date_precision` is `monthly` or `quarterly`:
- Prefix both dates with `~` (e.g. `~2026-12-01`)
- Append `(month-range estimate)` to Remarks

---

### C2 — Source Authority Hierarchy (Note #4)

##### [MODIFY] [sch_prototype.py](file:///c:/Work/schreminder/scratch/sch_prototype.py)

**New system prompt section:**
```
SOURCE AUTHORITY HIERARCHY — apply in strict descending priority:
1. Official embassy/consulate page for the applicant's home country
   (e.g. id.emb-japan.go.jp for Indonesian applicants to MEXT)
2. Issuing government ministry or national agency
   (e.g. niied.go.kr, mext.go.jp, hea.ie, bolashak.gov.kz)
3. Official scholarship foundation website
   (e.g. gksscholarship.com, chevening.org, cmkfoundation-globalscholarship.org)
4. Official university or institution admission page
   (for uni-to-uni scholarships: this becomes PRIORITY #1 — see UNI-TO-UNI note)
5. Study-abroad portals (e.g. studyinjapan.go.jp, studyinkorea.go.kr)
   -> USE ONLY if no higher-priority source exists in the scraped content
6. News articles, aggregator blogs, third-party media -> FORBIDDEN as official_source_url

When sources contradict each other, ALWAYS use the higher-priority source's dates.
When a lower-priority source is the only one available, note it in 'remarks'.
```

---

## Phase D — `PROTOTYPE_EVOLUTION.md` Update

##### [MODIFY] [PROTOTYPE_EVOLUTION.md](file:///c:/Work/schreminder/scratch/PROTOTYPE_EVOLUTION.md)

Add **Phase 4** section documenting items #25–42:

| # | Enhancement | Phase |
|---|-------------|-------|
| 25 | Result persistence — `/result` JSON folder (one file per run) | A1 |
| 26 | Start date estimation (end − 90 days) when only deadline found | A2 |
| 27 | `search_status` enum: `SUCCESS / NETWORK_FAILURE / BLOCKED / NO_RESULTS` | A3 |
| 28 | Bonus retry round (60s jitter sleep between round 1 and round 2) | A3 |
| 29 | Remark differentiation: NETWORK_FAILURE vs BLOCKED vs NO_RESULTS — different text | A3 |
| 30 | Email: grey/dark-orange/light-grey cells for NET ERR / BLOCKED / NO DATA | A3 |
| 31 | T+F bypass (Status=T, Verified=F → direct email from sheet cols) | A4 |
| 32 | Email: purple cell + ✅ VERIFIED label for BYPASS entries | A4 |
| 33 | `google_sheets.py` col_map: added `verified` (Col D) and `note` (Col B) | A4 |
| 34 | Per-scholarship config table `scholarship_config.py` | B1 |
| 35 | Config-driven scraping (preferred URLs front-loaded + query override) | B2 |
| 36 | Translation sub-step (MyMemory API for non-English pages) | B2 |
| 37 | Uni-to-Uni schema detection (`parse_scholarship_name()`) | B3 |
| 38 | Uni-to-Uni LLM context injection + university page priority | B3 |
| 39 | Uni-to-Uni email badge (purple UNI-TO-UNI span) | B3 |
| 40 | `date_precision` field + monthly date inference rule in LLM prompt | C1 |
| 41 | Email: `~` prefix on estimated month-range dates + remark note | C1 |
| 42 | Source authority hierarchy in LLM system prompt | C2 |

Update mermaid diagram and "Current Success State" table.

---

## Verification Test Matrix

| Test Scholarship | What to Verify |
|-----------------|---------------|
| GOI-IES | `hea.ie` info link; correct date from HEA page |
| GO-PSP | `research.ie` info link; not confused with GOI-IES |
| Kazakhstan Bolashak | `bolashak.gov.kz/en/` info; `konkurs.bolashak.gov.kz` reg; translation note in remarks |
| MEXT Research Student | `id.emb-japan.go.jp` link, **not** `studyinjapan.go.jp` |
| GKS Graduate | Date from `gksscholarship.com`, **not** `studyinkorea.go.kr` |
| DAAD STEM Discipline | Deep DAAD param link, **not** news page |
| Hyundai CMK | `date_precision: monthly`; `~YYYY-MM-01` dates in email; month-range estimate in remarks |
| MTCP | Either text dates from `/news` sub-page OR explicit `[NO_RESULTS]` remark — never silent empty |
| LPDP Tahap 1 | Tahap 1 dates only (not Tahap 2) |
| LPDP Tahap 2 | Tahap 2 dates, different from Tahap 1 |
| (ANSO) UCAS | `ucas.ac.cn` link; `UNI-TO-UNI` badge in email |
| (ANSO) USTC | `ustc.edu.cn` link; uni page date wins; central body date in remarks |
| (ADB-JSP) Keio | `keio.ac.jp` link; `UNI-TO-UNI` badge |
| (ADB-JSP) IST | `isct.ac.jp` link; `UNI-TO-UNI` badge |
| Inpex / BIM / Sultan Qaboos / HDR | Email shows **grey `⚡ NET ERR`** cell — NOT blank red CLOSED |
| EGYAID (run 3x in a row) | Consistent result on all 3 — bonus retry absorbs transient failure |
| Any scholarship, end-date only | Start = end − 90 days; `[Start date estimated...]` note in remarks |
| Status=T, Verified=F (any row) | Skips LLM; purple `✅ VERIFIED` cell; emails Col B + G + H verbatim |
| Status=T, Verified=T (any row) | Full pipeline runs normally |

---

## Implementation Order & Time Estimate

```
Phase A1  (result JSON)              ~15 min   zero risk, additive
Phase A3  (network error + retry)    ~40 min   search function refactor, high value
Phase A2  (start date estimation)    ~15 min   post-processing only
Phase A4  (T+F bypass)               ~30 min   col_map + bypass block + email color
────────────────────────────────────────────────────────────────────────────────
Phase A subtotal                     ~100 min
────────────────────────────────────────────────────────────────────────────────
Phase B1  (scholarship_config.py)    ~25 min   new file, no pipeline changes
Phase B2  (config integration)       ~35 min   core pipeline addition + translate
Phase B3  (uni-to-uni feature)       ~40 min   parser + LLM prompt + email badge
────────────────────────────────────────────────────────────────────────────────
Phase B subtotal                     ~100 min
────────────────────────────────────────────────────────────────────────────────
Phase C1  (date_precision + monthly) ~20 min   prompt + email renderer
Phase C2  (source authority)         ~15 min   prompt string edit only
Phase D   (evolution doc)            ~15 min   documentation
────────────────────────────────────────────────────────────────────────────────
Total                                ~4h 10min
```
