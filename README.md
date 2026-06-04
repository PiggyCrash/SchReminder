# 🎓 Automated Academic Scout & Google Sheets Sync (FastAPI Manual Test Version)

An advanced verification agent that crawls real-time web search results (protected by robust anti-CAPTCHA strategies), verifies application windows via the **Google Gemini API**, writes updates back to your Google sheet in a single batch, and emails a daily status summary digest to your inbox.

To help you test this manually and interactively, this version includes a local **FastAPI** web server with built-in interactive Swagger UI.

---

## 🛠️ Key Production Safeguards

### 1. Robust Search Harvesting (Anti-CAPTCHA Defenses)
- Programmatic DuckDuckGo search integration utilizes the `duckduckgo_search` library to manage VQD cookies and internal API token validation, preventing automated IP blocks.
- Try-except crawler blocks catch query failures and gracefully fall back to historical info/registration links instead of crashing the workflow.

### 2. Gemini API 15 RPM Rate Limit Compliance
- Synchronous `time.sleep(5)` pacing intervals ensure sheet-sync executions do not exceed 12 requests per minute, staying safely below the strict 15 RPM ceiling.

### 3. Consolidated State Commit (Batch Sheet Writes)
- All processed results are accumulated in-memory and committed back using `wks.update_cells()` in a **single, consolidated batch update API call**, saving Google Sheets API write quota.

---

## 📋 Google Spreadsheet Setup

### 1. Spreadsheet Format & Headers
Ensure your Google Sheet contains headers in **Row 1**. The program dynamically maps columns by searching case-insensitively for these keywords or their common aliases:

| Field | Primary Header Target | Common Supported Aliases | Type |
|---|---|---|---|
| **Scholarship Name (Required)** | `Scholarship Name` | `Name`, `Scholarship` | Input |
| **Historical Method** | `Processing Method (Historical)` | `Processing Method`, `Method` | Input |
| **Historical Info Link (Required)** | `Info Link (Historical)` | `Info Link`, `Historical Info Link` | Input |
| **Historical Reg Link (Required)** | `Registration Link (Historical)` | `Registration Link`, `Historical Registration Link` | Input |
| **Estimated Timeline** | `Estimated Timeline` | `Timeline`, `Est Timeline` | Input |
| **Status** | `Status` | `Live Status` | Output |
| **Start Date** | `Start Date` | `Application Start Date` | Output |
| **Deadline** | `Deadline` | `Application Deadline` | Output |
| **Verified Info Link** | `Verified Info Link` | `Verified Source Link`, `Source URL` | Output |
| **Verified Reg Link** | `Verified Reg Link` | `Verified Registration Link` | Output |
| **Fallback Used** | `Fallback Used` | `Url Verification Fallback Used` | Output |
| **Confidence** | `Confidence` | `Confidence Score` | Output |
| **Detected Method** | `Detected Method` | `Processing Method Detected` | Output |
| **Remarks** | `Remarks` | `Notes`, `Summary` | Output |

---

## 🔑 Google Cloud API Access Control (IAM)

To prevent `403 Sheet Not Found` authorization errors:
1. Open your **Google Cloud IAM Console** and navigate to your Service Account dashboard.
2. Locate and copy the generated **Service Account Email** (typically formatted like `your-service-account@project-id.iam.gserviceaccount.com`).
3. Open your Tracking Google Spreadsheet, click **Share** at the top right, and invite the copied service account email as an **"Editor"**.

---

## 💻 Local Quickstart & Manual Testing

### 1. Installation
Clone the repository and install requirements:
```bash
pip install -r requirements.txt
```

### 2. Environment Setup
Copy the `.env.example` file to `.env`:
```bash
cp .env.example .env
```
Fill in the appropriate credentials, spreadsheet ID, and SMTP mail configuration.

### 3. Run FastAPI Web Server
To launch the FastAPI server locally:
```bash
uvicorn src.app:app --reload --host 127.0.0.1 --port 8000
```

### 4. Test Manually via Swagger UI
Open your browser and navigate to:
👉 **[http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)**

From this interactive dashboard, you can trigger two manual endpoints:
1. **`POST /verify`**: Manually verify a single scholarship. Provide a scholarship name, and the API will crawl DuckDuckGo, query Gemini, and return the exact verified status and deadlines in real-time JSON format.
2. **`POST /sync`**: Manually trigger the full sheet sync. This will run the orchestrator loop, update your spreadsheet rows, and send the styled report email immediately.

### 5. Run via CLI (Alternative)
```bash
python src/main.py
```
This runs the daily cron pipeline once in your terminal.
