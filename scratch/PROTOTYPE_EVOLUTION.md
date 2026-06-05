# Scholarship Scout: System Evolution & Logic History

This document chronicles the development of the automated scholarship scouting and verification system from the very first production implementation to the current resilient testing prototype.

---

## 🛠️ The Architecture Evolution

```mermaid
graph TD
    V1[Version 1: Production Engine] -->|Batch-sync, Gemini API, DDG| V1_Issues{Issues: Wrong dates/links/remarks, 429 API rate limits}
    V1_Issues -->|Create test-bed for single scholarship| V2[Version 2: Prototyping Phase]
    V2 -->|Compare models, test isolated rows| V2_Evolution{Pipeline & Robustness Enhancements}
    V2_Evolution -->|1. Resilient Scraping & Yahoo Fallback| V2_Evolution
    V2_Evolution -->|2. Error/Captcha Handling| V2_Evolution
    V2_Evolution -->|3. Current Date Injection & Strict Status Rules| V2_Evolution
    V2_Evolution -->|4. Country Context Queries & Branching Link Tracing| V2_Evolution
    V2_Evolution -->|5. Binary PDF Domain Swapping| V2_Evolution
    V2_Evolution -->|6. Safe Sheet Integration read_only| V2_Evolution
    V2_Evolution -->|7. News Blocklist & Official Whitelist| V2_Evolution
    V2_Evolution -->|8. URL Integrity & Hallucination Guard| V2_Evolution
    V2_Evolution -->|9. Supplementary Announcement Links| V2_Evolution
    V2_Evolution -->|Targeted verification of complex cycles| Success[Success: High confidence status, timelines, and links verified]
```

---

## 📅 Version 1: Batch-Sync Production Engine ([runner.py](file:///c:/Work/schreminder/src/runner.py), [scout.py](file:///c:/Work/schreminder/src/engine/scout.py))

### Method/Logic
The initial release was designed to run as an automated batch pipeline (via CLI or FastAPI endpoints like `/sync`). It performed the following steps:
1. **Google Sheets Connection**: Pulled all active scholarship tracking rows in a batch.
2. **Search Harvesting**: Searched DuckDuckGo for each scholarship name.
3. **Gemini NLP Verification**: Submitted search snippets and historical row data to the Gemini API (`gemini-2.5-flash`).
4. **Batch Commitment**: Saved the results into the spreadsheet in a single batch write call.
5. **Notification**: Sent a styled HTML digest report email.

### Identified Problems & Failures
* **Wrong Timeline / Dates**: The Gemini model had no awareness of the current date, causing it to hallucinate active dates or incorrectly mark expired cycles (e.g. from 2024 or 2025) as `OPEN`.
* **Wrong Info / Registration Links**: The scraper often matched generic homepages, and the LLM frequently hallucinated duplicate URLs for both the Info Link and the Registration Link, violating the requirement for distinct links.
* **Wrong Remarks / Summaries**: Outputted generic remarks that did not reflect actual application requirements or portal movements.
* **Gemini API Limit Hits (429 Rate Limits)**: Because the engine looped through the entire spreadsheet, it quickly hit the 15 RPM limit on Gemini's free tier, throwing `RESOURCE_EXHAUSTED` errors and terminating the sync midway.

---

## 🧪 Version 2: The Prototyping Phase ([sch_prototype.py](file:///c:/Work/schreminder/scratch/sch_prototype.py))

### Method/Logic
To resolve Version 1's issues without constantly wasting Google Sheets and Gemini API quotas, a prototyping file ([sch_prototype.py](file:///c:/Work/schreminder/scratch/sch_prototype.py)) was introduced to:
* **Isolate Testing**: Focus on a single scholarship at a time (`TEST_SCHOLARSHIP_NAME`) instead of batch checking the entire sheet.
* **Compare Models**: Switch the LLM provider to Cerebras (`zai-glm-4.7` or `gpt-oss-120b`) via an OpenAI-compatible endpoint.

### Evolutionary Path & Pipeline Fixes

#### 1. Page Content Scraping
* **Problem**: Simple search snippets did not contain detailed embassy or university guidelines.
* **Fix**: Added BeautifulSoup to fetch and clean the raw HTML of target pages, extracting the top 5,000 characters.

#### 2. DuckDuckGo SSL Handshake & Yahoo Fallback
* **Problem**: DuckDuckGo search requests consistently raised `SSLV3_ALERT_HANDSHAKE_FAILURE`. Switching to Bing returned a JavaScript-only blank shell with 0 results.
* **Fix**: Integrated **Yahoo Search** (`search.yahoo.com`), which returned clean, server-side rendered HTML results. Added 3 retries with a 5-second sleep specifically for Yahoo HTTP 500 errors.

#### 3. Connection Reset Handling
* **Problem**: Government and university domains frequently refused scrapers, causing connection resets (HTTP 10054). The script slept for 180 seconds on any exception, causing huge delays.
* **Fix**: Coded the crawler to instantly skip network/SSL errors without triggering the 180-second rate-limit sleep.

#### 4. False Captcha Trigger Prevention
* **Problem**: Normal pages with words like "robotics", or scripts containing WordPress plugin names like `silentcaptcha`, were falsely identified as captcha pages, trigger-blocking the program.
* **Fix**: Refined the block keywords to specific phrases (e.g. `"prove you are human"`, `"i am not a robot"`) and stripped `<script>` / `<style>` tags *before* running checks.

#### 5. Context Size & LLM Timeout Management
* **Problem**: Large scraped contexts caused Cerebras to time out after 30 seconds.
* **Fix**: Capped the total context payload at 12,000 characters, increased LLM client timeouts to 90 seconds, and added a single retry on timeout.

#### 6. Today's Date Injection & Strict Status Rules
* **Problem**: The LLM still struggled to correctly match status state against deadlines.
* **Fix**: Injected `TODAY'S DATE: {current_date}` into the prompt and enforced strict status evaluation criteria (`OPEN` if today is between start and end, `CLOSED` if today is after the deadline).

#### 7. Country/Region Contextual Queries
* **Problem**: Yahoo search returned global or non-Indonesian target pages, causing inaccurate date tracking.
* **Fix**: Expanded the search query to include the region: `{Scholarship Name} Indonesia deadline {Year}`. Extended [google_sheets.py](file:///c:/Work/schreminder/src/spreadsheet/google_sheets.py) to extract `country_region` to support this search parameter.

#### 8. Binary PDF Swapping
* **Problem**: Search links pointing directly to `.pdf` guidelines broke the HTML text parser.
* **Fix**: Programmed the crawler to detect binary file extensions and fallback to scanning the root domain instead.

#### 9. Keyword-Filtered Branching Links
* **Problem**: Scrapers followed irrelevant internal links (e.g. `/contact`, `/about`) while missing the actual timelines page.
* **Fix**: Whitelisted keyword terms (`timeline`, `schedule`, `gks`, `apply`, `2026`) and traced deep branching child-links.

#### 10. Safe Sheet Integration & Read-Only Mode
* **Problem**: Running prototype tests could overwrite or corrupt production sheet cells, or fail with `APIError: [400] Range exceeds grid limits` because output columns were not initialized.
* **Fix**: Integrated `read_only=True` parameter in `conn.connect()` and updated the core spreadsheet library [google_sheets.py](file:///c:/Work/schreminder/src/spreadsheet/google_sheets.py). Disabled spreadsheet writing in the prototype completely, routing all reports to email logs.

#### 11. Refined Search Queries & Sanitized Links
* **Problem**: Queries like `{name} scholarship` were too broad. Also, placeholder string values like `"None"`, `"null"`, `"-"`, or `"n/a"` returned by the LLM caused broken HTML links (e.g. `<a href="None">`) in output reports.
* **Fix**: Appended `"important date deadline"` to the search query. Added a Python `sanitize_link()` function that normalizes all variations of null strings to Python `None`, rendering as a clean `—` (em-dash) in output summaries.

#### 12. News/Media Domain Blocklist
* **Problem**: The scraper followed and extracted links from third-party Indonesian news portals (e.g., Kompas, Detik, Tribunnews), causing the LLM to cite news articles instead of the official scholarship website.
* **Fix**: Added a `NEWS_MEDIA_DOMAINS` blacklist and a helper `is_news_domain()` to filter news URLs from candidate links before sending context to the LLM. Also injected system instructions strictly forbidding the citation of news portals as official sources.

#### 13. Official Domain Whitelist Protection
* **Problem**: Authoritative government news/announcement sub-pages (e.g. `kemenag.go.id/nasional/`) resemble news sites and were erroneously blocked by the news filter.
* **Fix**: Introduced an `OFFICIAL_DOMAINS` list (including `.go.id`, `.go.jp`, embassy, and academic domains) and `is_official_domain()`. Government, embassy, and university pages are exempt from the news blocklist even if their paths contain news-related keywords.

#### 14. URL Integrity & Hallucination Guard
* **Problem**: The LLM frequently hallucinated or reconstructed registration URLs (e.g., modifying paths like `/register`, `/apply` on official domains).
* **Fix**: Enforced a URL Integrity Rule in the system prompt. Added a post-processing URL Hallucination Guard in Python: if the returned registration URL is not in the set of all known fetched or candidate links, the system reverts to the verified historical registration link.

#### 15. Status Safety Net Override
* **Problem**: LLMs occasionally set the status to `OPEN` even when no application deadline could be extracted, presenting a false positive risk.
* **Fix**: Added a post-processing safety net: if status is `OPEN` but the verified deadline is `None`, the status is forced to `CLOSED` and a note is appended to the remarks.

#### 16. Supplementary Source URLs (Official Announcements)
* **Problem**: When a scholarship cycle closes, official primary portals often delete deadline references entirely, but official announcement pages (e.g., LPDP/Ministry news boards) still contain active extension notices.
* **Fix**: Added `supplementary_source_url` to the LLM JSON output. If the primary page lacks dates but an official announcement post on the same domain lists active timelines, the LLM captures the announcement URL. Python validates it, and the HTML email renders it as a separate purple `[Announcement ↗]` link next to the main Info Link.

---

## 🏗️ Phase 3: Zero-Historical-Dependency Architecture

### Design Principle Change
> *"No cheating. The program reads only the scholarship name from the spreadsheet. Everything else — links, dates, processing method — must be discovered independently through web scraping."*

This was a deliberate integrity constraint enforced after tests showed the pipeline was using historical spreadsheet links as a "cheat sheet" rather than genuinely discovering data. The historical info/reg links remain in the spreadsheet for the **user's own validation**, not for the engine.

### Problems Found (triggering this phase)

| Symptom | Root Cause |
|---------|-----------|
| GKS/ARICE/MEXT: regression to all-null results on re-test | Search query hardcoded `"Indonesia"` — for Hungary, Romania, Japan scholarships, this made Yahoo return Indonesian news articles instead of official sites, which were then blocked by the news filter |
| MEXT: never returns any link or date | Embassy server blocks scraper; historical link was the only fallback — now removed, so LLM gets blank context |
| GKS remarks say "Feb–March" when actual is February only | LLM was blending the `estimated_timeline` cell value from the sheet into its answer |
| `branching_count` reset per URL | Bug: counter was inside the outer loop, so each top-level page got 2 fresh branches. Max effective branches = 2 × #URLs, not 2 total |
| Stipendium dates wrong (future cycle) | Date buried 2 clicks deep; homepage has no dates. Branching keyword list lacked `"news"`, `"application"`, `"open"` so the news article sub-link was never followed |
| `processing_method_detected` not in email | Email HTML table had no Method column |

### Changes Implemented

#### 17. Removed All Historical Data from LLM Pipeline
* **What changed**: `verify_scholarship_llama()` parameters `historical_method`, `historical_info_link`, `historical_reg_link`, `estimated_timeline` were removed entirely. `matched_row` now only contains `row_idx` and `scholarship_name`.
* **Why**: The historical links were being used as a crutch. The engine should earn its results from the web. The spreadsheet links are the user's reference, not the engine's input.
* **Impact**: LLM system prompt rewritten — Phase 1 (historical link priority) removed. URL Integrity Rule no longer includes "(b) historical links from user". Field 8 (`url_verification_fallback_used`) redefined as "true if LLM relied on training knowledge, not scraping."

#### 18. Dynamic Search Query (No Hardcoded Country)
* **Problem**: `"Indonesia"` was hardcoded in the search string — ruining non-Indonesian scholarships (Hungary, Romania, Japan) because Yahoo returned Indonesian-language news articles about those scholarships instead of the official foreign sites.
* **Fix**: Query simplified to `"{name} important date deadline {year}"`. The search engine will find the official site naturally. Country is no longer read from the spreadsheet.

#### 19. Official-First URL Ordering
* **Problem**: News sites could appear first in the scraping queue, wasting branching budget on non-authoritative content.
* **Fix**: After Yahoo returns results, split into `official_results` (not news domain) and `news_results`, then `urls_to_scrape = official_results + news_results`. Official sites always scraped and branched first.

#### 20. Global Branching Counter (`branching_count` bug fixed)
* **Problem**: `branching_count = 0` was reset inside the outer scraping loop — meaning every top-level URL got 2 fresh branch attempts, not 2 total.
* **Fix**: Moved `branching_count = 0` and `MAX_BRANCHES = 4` to before the outer loop. Now globally capped at 4 sub-page fetches per scholarship run.

#### 21. Expanded Branching Keyword List
* **Problem**: The old keyword list lacked terms like `"news"`, `"application"`, `"open"`, `"burse"`, `"program"` — so announcement sub-pages (e.g. `/news/2026-application`) were never followed.
* **Fix**: Added 13 new branching keywords covering announcement, news, program, selection, intake, period, cycle, eligib, require, burse, grant, award, open.

#### 22. Info URL Hallucination Guard Added
* **Problem**: Only the registration URL was validated against scraped URLs. The LLM could still hallucinate an info URL from a domain never visited.
* **Fix**: Added a second guard for `official_source_url`. If the domain was never visited → set to null. If the domain was visited but the exact path differs → allowed (legitimate sub-page of a scraped official domain).

#### 23. Hallucination Guard Fallback = `None` (not historical link)
* **Problem**: Old guard fell back to `matched_row["historical_reg_link"]` — which no longer exists in the data model.
* **Fix**: Both reg and info guards now set to `None` on rejection. No spreadsheet data is used as fallback.

#### 24. Processing Method Column Added to Email
* **Fix**: `Method` column added to the HTML email table (`<th>` in header, `<td>` in data row) between Reg. Link and Remarks.

---

## 🎯 Current Success State
With the robust prototyping upgrades, the scouting engine has been successfully validated across several major targets:
1. **GKS (Global Korea Scholarship)**: Correctly verified as `CLOSED` (Feb 12 to Feb 25, 2026), successfully navigating the Indonesia-specific branch link and emailing the report.
2. **Zuyd ZES - Reguler**: Correctly verified as `CLOSED` (deadline: May 1) based on the official university portal data.
3. **Beasiswa Indonesia Bangkit (BIB) LPDP**: Correctly verified as `CLOSED` (original dates 2025-03-28 to 2025-06-07). The URL Hallucination Guard successfully intercepted a hallucinated LPDP registration path and reverted it to the historical link.

> ⚠️ **Re-testing required** after Phase 3 changes for: Stipendium Hungaricum, MEXT, GKS, ARICE Romania, Akebono Foundation.
