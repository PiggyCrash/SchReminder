# 🎓 Automated Academic Scout & Google Sheets Sync

An advanced, highly resilient academic scout agent that tracks, crawls, and verifies scholarship application windows in real-time. It integrates a **4-engine search crawler**, OpenAI-compatible **Llama model verifications (via Cerebras)**, automated **Google Sheets sync orchestration**, and styled **HTML digest reports** emailed directly to your inbox.

Designed to handle real-time web search limitations, network blocks, API rate limits, and language barriers with maximum autonomy.

---

## Stacks 

| Component | Technology | Description |
| :--- | :--- | :--- |
| **Language** | Python 3.x | Core programming language |
| **API Framework** | FastAPI + Uvicorn | High-performance asynchronous API endpoints and web server |
| **LLM Inference** | Cerebras Inference API | High-speed inference using Llama 3.x models via OpenAI-compatible HTTP requests |
| **Search Crawler** | DuckDuckGo, Yahoo, Bing, SearXNG | 4-engine fallback web search scheduler |
| **Web Scraping** | `requests` + `beautifulsoup4` | HTTP client and HTML parsing for deep crawling target domains |
| **Spreadsheet Integration** | Google Sheets API (`gspread`) | Read-only access to tracking sheet data using Google Service Account |
| **Email Service** | SMTP (`smtplib`) | Automated HTML digest reporting with SSL (465) / STARTTLS (587) support |
| **Translation Engine** | MyMemory API | Automated free translation for local/non-English scholarship websites |
| **Configuration / Env** | `python-dotenv` | Secure loading of API keys and server variables |
| **Data Validation** | `pydantic` | Typed schemas for configuration overrides and structured LLM outputs |

## 🛠️ Premium Architectural Features

### 1. Robust Multi-Engine Crawler (Anti-CAPTCHA & Failover)
* **4-Engine Search Schedule**: Queries **DuckDuckGo ➔ Yahoo ➔ Bing ➔ SearXNG** sequentially. The first engine to return results wins, avoiding reliance on a single provider.
* **3-Round Aggressive Retry**: If all engines fail, the crawler retries after exponential delays (**0s ➔ 30s ➔ 60s**) with dynamic **User-Agent rotation** (Chrome, Safari, Firefox, Edge) and header spoofing to bypass Web Application Firewalls (WAF).
* **Targeted Deep Crawling**: Crawls program index pages and automatically branches into child links (e.g. `/news`, `/announcements`, `/deadline`) matching structural keywords to locate text-based timeline notices.

### 2. Config-Driven Scholarship Overrides
Located in `schreminder/src/config/scholarship_config.py`, this engine overrides crawler behavior for complex sites:
* `preferred_query` / `preferred_urls`: Directs searches to the exact sub-pages.
* `locked_urls`: Skips search engines entirely to scrape only target portals (e.g. for GKS).
* `date_source_domain`: Enforces date authority, preventing LLM from pulling dates from third-party blogs or wrong embassy regions.
* `context_hint`: Injects operator-verified knowledge (e.g., "deadlines vary by school") to help the LLM draw accurate conclusions.
* `needs_translation`: Detects non-English sites (under 5% ASCII ratio) and automatically translates page texts via MyMemory API.

### 3. Balanced Name Parser for University-Specific recommendations
* Handled in `schreminder/src/engine/name_parser.py`, it detects parenthesized tags like `(Scholarship Body) University Name` (e.g., `(MEXT Scholarship) Hokkaido University`) and automatically generates queries targeting specific recommendation guidelines.

### 4. Enterprise API Congestion Resilience
* **Queue Congestion Retry**: Automatically detects Cerebras `429 queue_exceeded` server loads and retries with backoff delays (**10s ➔ 20s ➔ 30s**).
* **Per-Row Error Sentinels**: If an LLM call exhausts all retries, the system logs a `QUOTA_EXCEEDED` status for that row and **continues the batch** instead of aborting the pipeline. 

### 5. Consolidated Google Sheets Synchronization
* **Grid Pre-expansion**: Automatically expands Google Sheet dimensions before writing new output columns, avoiding grid-out-of-bounds `APIError 400`.
* **Manual Override (Bypass Mode)**: If a row has `active_status = "T"` and `verified = "F"`, the engine bypasses search/LLM entirely and preserves user-entered data as `VERIFIED (MANUAL)`.
* **Dry-Run Mode**: Setting `SCOUT_DRY_RUN=true` disables Google Sheet updates while still parsing web results, generating JSON outputs, and sending email digests.

---

## 📋 Google Spreadsheet Setup

The script dynamically maps column indices by matching Row 1 headers against case-insensitive keywords and aliases.

### 1. Expected Column Mapping

| Column Type | Script Key | Preferred Header | Supported Aliases / Alternative Headings |
| :--- | :--- | :--- | :--- |
| **Input** | `scholarship_name` | `Scholarship Name` | `Name`, `Scholarship` |
| **Input** | `active_status` | `Status` | *(Must contain `T` or `t` to process the row)* |
| **Input** | `verified` | `Verified` | *(If containing `F` or `f` while Status is `T`, triggers Manual Bypass)* |
| **Input** | `historical_method` | `Processing Method (Historical)` | `Method`, `Processing Method`, `Reg. Path` |
| **Input** | `historical_info_link`| `Info Link (Historical)` | `Info Link`, `Link Info`, `Historical Info Link` |
| **Input** | `historical_reg_link` | `Registration Link (Historical)`| `Reg. Link`, `Link Daftar`, `Historical Registration Link` |
| **Input** | `estimated_timeline` | `Estimated Timeline` | `Timeline`, `Est. Date` |
| **Input** | `note` | `Note` | *Optional operator remarks* |

### 2. Returning JSON values
| Column Type | Script Key | Preferred Header | Supported Aliases / Alternative Headings |
| :--- | :--- | :--- | :--- |
| **Output** | `status` | `Verified Status` | `Scout Status` |
| **Output** | `start_date` | `Verified Start Date` | `Start Date`, `Application Start Date` |
| **Output** | `deadline` | `Verified Deadline` | `Deadline`, `Application Deadline` |
| **Output** | `verified_info_url` | `Verified Info Link` | `Verified Source Link`, `Verified Info URL` |
| **Output** | `supplementary_url` | `Supplementary Link` | `Supplementary Source URL`, `Announcement Link` |
| **Output** | `verified_reg_url` | `Verified Reg Link` | `Verified Registration Link`, `Verified Reg URL` |
| **Output** | `fallback_used` | `Fallback Used` | `Url Verification Fallback Used` |
| **Output** | `confidence` | `Confidence Score` | `Confidence` |
| **Output** | `detected_method` | `Detected Method` | `Processing Method Detected` |
| **Output** | `remarks` | `Remarks` | `Notes`, `Summary` |

---

## 🔑 IAM API Authorization

To avoid Google API `403 Sheet Not Found` or authorization errors:
1. Open your **Google Cloud IAM Console** and navigate to your Service Account dashboard.
2. Locate and copy the generated **Service Account Email** (e.g. `scout-agent@project.iam.gserviceaccount.com`).
3. Open your Tracking Google Spreadsheet, click **Share** at the top right, and invite the email address as an **"Editor"**.

---

## 💻 Local Quickstart

### 1. Installation
Clone the repository and install requirements:
```bash
pip install -r requirements.txt
```

### 2. Configure Environment
Copy the example environment configuration to `.env`:
```bash
cp .env.example .env
```
Fill in the appropriate API keys, spreadsheet ID, service account JSON string, and SMTP credentials.

### 3. Run FastAPI Web Server
To launch the FastAPI server locally:
```bash
uvicorn src.app:app --reload --host 127.0.0.1 --port 8000
```
Then navigate to: 👉 **[http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)** to test via the Swagger UI.

* **`POST /verify`**: Manually verify a single scholarship row by name without running the full sync.
* **`POST /sync`**: Manually trigger the full Google Sheets sync, updating rows and sending the report email.

### 4. Run via CLI
```bash
python src/runner.py
```
