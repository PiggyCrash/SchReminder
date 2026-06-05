import logging
from typing import Optional
from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel, Field

from src.search.crawler import search_scholarship
from src.engine.scout import verify_scholarship, ScholarshipVerification
from src.runner import run_scout_pipeline

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("FastAPI-App")

app = FastAPI(
    title="Daily Academic Scout & Google Sheets Sync API",
    description="Interactive test portal to manually trigger scholarship search harvests, Gemini verifications, and spreadsheet syncs.",
    version="1.0.0"
)

class VerificationRequest(BaseModel):
    scholarship_name: str = Field(..., example="Gates Cambridge Scholarship")
    historical_method: str = Field(default="Online", example="Online")
    historical_info_link: str = Field(default="", example="https://www.gatescambridge.org")
    historical_reg_link: str = Field(default="", example="https://www.gatescambridge.org/apply")
    estimated_timeline: str = Field(default="", example="October 2026")
    scraped_web_text: Optional[str] = Field(
        default=None, 
        description="Optional pre-scraped search text. If omitted, the program will crawl DuckDuckGo in real-time."
    )

@app.get("/")
def read_root():
    return {
        "status": "online",
        "description": "Daily Academic Scout & Sheets Sync API is running.",
        "documentation": "/docs"
    }

@app.post("/verify", response_model=ScholarshipVerification, summary="Verify a Single Scholarship")
def verify_single(request: VerificationRequest):
    """
    Manually triggers verification for a single scholarship.
    If `scraped_web_text` is not provided, the API crawls DuckDuckGo in real-time.
    """
    logger.info(f"Received manual verification request for '{request.scholarship_name}'")
    
    web_context = request.scraped_web_text
    if not web_context or web_context.strip() == "":
        logger.info(f"Scraped web text not provided. Harvesting live search results for '{request.scholarship_name}'...")
        web_context = search_scholarship(request.scholarship_name)
        if not web_context:
            logger.warning(f"Live search harvesting returned 0 results for '{request.scholarship_name}'")
            # We don't fail here, we let the verifier run with empty context to trigger fallback URLs
            
    try:
        result = verify_scholarship(
            scholarship_name=request.scholarship_name,
            historical_method=request.historical_method,
            historical_info_link=request.historical_info_link,
            historical_reg_link=request.historical_reg_link,
            estimated_timeline=request.estimated_timeline,
            scraped_web_text=web_context
        )
        return result
    except Exception as e:
        logger.error(f"Error during manual verification: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Verification failed: {str(e)}")

@app.post("/sync", summary="Trigger Full Google Sheets Sync")
def trigger_sync(background_tasks: BackgroundTasks):
    """
    Triggers the full pipeline sync synchronously to return immediate execution status.
    Updates the Google Sheet, commits batch writes, and dispatches the HTML report email.
    """
    logger.info("Manual Sheets sync triggered via FastAPI /sync endpoint.")
    
    # We can run it synchronously to let the user see the exact execution result in Swagger
    success = run_scout_pipeline()
    if success:
        return {
            "status": "success",
            "message": "Full sync pipeline executed successfully. Check Google Sheets and your inbox."
        }
    else:
        raise HTTPException(
            status_code=500, 
            detail="Pipeline execution failed. Check console logs for Sheets authentication or connectivity issues."
        )
