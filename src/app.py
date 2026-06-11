"""
FastAPI app — interactive test portal for SchReminder Scout.

Endpoints:
  GET  /         -> health check
  POST /verify   -> manually verify a single scholarship (search + LLM)
  POST /sync     -> trigger full batch pipeline sync
"""

import logging
from typing import Optional
from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel, Field

from src.runner import run_scout_pipeline

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("FastAPI-App")

app = FastAPI(
    title="Daily Academic Scout & Google Sheets Sync API",
    description=(
        "Interactive test portal to manually trigger scholarship search harvests, "
        "LLM verifications, and spreadsheet syncs. "
        "Uses Cerebras LLM (OpenAI-compatible) for verification."
    ),
    version="2.0.0"
)

class VerificationRequest(BaseModel):
    scholarship_name: str = Field(..., example="Gates Cambridge Scholarship")


@app.get("/")
def read_root():
    return {
        "status": "online",
        "description": "Daily Academic Scout & Sheets Sync API is running.",
        "documentation": "/docs"
    }


@app.post("/verify", summary="Verify a Single Scholarship")
def verify_single(request: VerificationRequest):
    """
    Manually triggers the full pipeline for a single scholarship by name.

    The scholarship name must match exactly what's in the Google Sheet.
    The engine will:
      1. Look up config overrides from scholarship_config.py
      2. Run search (DDG -> Yahoo) or locked mode
      3. Scrape pages + follow sub-links
      4. Call Cerebras LLM
      5. Return the full verification result JSON
    """
    from src.search.crawler import search_scholarship_with_retry, fetch_webpage_content, clean_html, extract_hyperlinks, filter_candidate_links, is_news_domain, OFFICIAL_DOMAINS
    from src.engine.scout import verify_scholarship_llama, post_process_result, CerebrasQuotaExceededException
    from src.engine.name_parser import parse_scholarship_name
    from src.config.scholarship_config import get_scholarship_config
    from src.runner import _process_single_scholarship
    import os

    logger.info(f"Received manual verification request for '{request.scholarship_name}'")

    # Build a minimal 's' dict to pass to the single-scholarship processor
    s = {
        "row_idx":              0,
        "scholarship_name":     request.scholarship_name,
        "active_status":        "T",
        "verified":             "",   # not a bypass — let engine run
        "historical_method":    "Online",
        "historical_info_link": "",
        "historical_reg_link":  "",
        "estimated_timeline":   "",
        "note":                 "",
    }

    model_name = os.getenv("OPENAI_MODEL", os.getenv("OPENAI_MODEL_2", "gpt-oss-120b"))

    try:
        result = _process_single_scholarship(s, model_name)
        return result["verified_data"]
    except CerebrasQuotaExceededException as qe:
        raise HTTPException(status_code=429, detail=f"Cerebras API quota exceeded: {str(qe)}")
    except Exception as e:
        logger.error(f"Error during manual verification: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Verification failed: {str(e)}")


@app.post("/sync", summary="Trigger Full Google Sheets Sync")
def trigger_sync():
    """
    Triggers the full batch pipeline sync synchronously.
    Reads all active rows from Google Sheet, verifies each scholarship,
    updates the sheet, and dispatches the HTML report email.
    """
    logger.info("Manual Sheets sync triggered via FastAPI /sync endpoint.")
    success = run_scout_pipeline()
    if success:
        return {
            "status": "success",
            "message": "Full sync pipeline executed successfully. Check Google Sheets and your inbox."
        }
    else:
        raise HTTPException(
            status_code=500,
            detail="Pipeline execution completed with errors or was interrupted by quota limit. Check console logs."
        )
