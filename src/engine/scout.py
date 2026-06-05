import os
import json
import logging
from typing import Optional, Literal, Dict, Any
from pydantic import BaseModel, Field
from dotenv import load_dotenv

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ScoutEngine")

# Load environment variables
load_dotenv()

# Define the Pydantic schema for strict verification outputs
class ScholarshipVerification(BaseModel):
    scholarship_name: str = Field(
        description="The exact name of the scholarship processed"
    )
    status: Literal['OPEN', 'CLOSED', 'NOT_YET_OPENED'] = Field(
        description="Strictly choose one: 'OPEN' | 'CLOSED' | 'NOT_YET_OPENED'"
    )
    application_start_date: Optional[str] = Field(
        None, 
        description="String in YYYY-MM-DD format, or null/None if unknown"
    )
    application_deadline: Optional[str] = Field(
        None, 
        description="String in YYYY-MM-DD format, or null/None if unknown"
    )
    official_source_url: str = Field(
        description="The verified information link found or validated from the text"
    )
    official_registration_url: str = Field(
        description="The verified submission/registration link found or validated from the text"
    )
    url_verification_fallback_used: bool = Field(
        description="true if the independent scraped text was insufficient and you had to rely strictly on the user's historical links, false if the scraped text found cleaner/newer active links"
    )
    confidence_score: float = Field(
        description="Float between 0.0 to 1.0 reflecting source reliability based on the text context",
        ge=0.0,
        le=1.0
    )
    processing_method_detected: Literal['Online', 'Offline/Mail-in', 'Hybrid', 'Register First, Upload Later'] = Field(
        description="Detect if registration requires 'Online', 'Offline/Mail-in', 'Hybrid', or 'Register First, Upload Later' based on current findings"
    )
    remarks: str = Field(
        description="A brief, concise summary of the findings or notes to map directly into the tracking spreadsheet and email alert"
    )

# Try importing the new Google GenAI SDK, fallback to old google-generativeai SDK if needed
try:
    from google import genai
    from google.genai import types
    USE_NEW_SDK = True
    logger.info("Using new 'google-genai' SDK for Gemini verification.")
except ImportError:
    try:
        import google.generativeai as genai_legacy
        USE_NEW_SDK = False
        logger.info("Using legacy 'google-generativeai' SDK for Gemini verification.")
    except ImportError as e:
        logger.critical("Failed to import both 'google-genai' and 'google-generativeai'. Please install dependencies.")
        raise e

def verify_scholarship(
    scholarship_name: str,
    historical_method: str,
    historical_info_link: str,
    historical_reg_link: str,
    estimated_timeline: str,
    scraped_web_text: Optional[str]
) -> Dict[str, Any]:
    """
    Submits scholarship details and scraped text to Gemini for state verification.
    Applies Phase 1, Phase 2, and the Override rules, returning a validated dictionary.
    """
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        logger.error("GEMINI_API_KEY environment variable is not set!")
        raise ValueError("Missing GEMINI_API_KEY in environment.")

    # Base system instructions implementing your exact logical workflow
    system_instruction = """You are an advanced Automated Academic Scout and Data Verification Agent. Your task is to analyze raw, scraped web text data provided to you, verify the real-time application status of a specific scholarship, and output deterministic data structures.

SYSTEM LOGIC & ANALYSIS STRATEGY (CRITICAL COUNTERMEASURE):
Analyze the provided web search context using the following fallback logic:
1. PHASE 1 (Analyze Scraped Search Data): Read and analyze the scraped search text to find the most current, active application window for the current cycle.
2. PHASE 2 (Cross-Reference & Validation):
   - Cross-reference any discovered timelines or links with the historical links provided by the user.
   - Determine if the official domain has changed or if the registration has moved to a new portal based on the scraped text.
3. OVERRIDE RULE (Prioritize Open Status): Even if the scraped text does not contain a 100% authoritative dot-gov/dot-edu domain, if the text provides reliable announcements/evidence indicating that the scholarship is currently OPEN, you MUST prioritize marking it as "OPEN".

CONTEXT & TIMELINE FLEXIBILITY:
- Do not restrict your analysis to a hardcoded calendar year. Determine the status based on the current active or upcoming application cycle found within the text.
- Verify if the application window is currently accessible for new applicants.

URL FALLBACK DETECT:
- Set `url_verification_fallback_used` = true if the independent scraped text was insufficient to find active links and you had to rely strictly on the user's historical links.
- Set `url_verification_fallback_used` = false if the scraped text found cleaner or newer active links.

CONFIDENCE SCORE RULES:
- High (0.8 - 1.0): Official announcements, verified portal matching the name, authoritative deadlines.
- Medium (0.5 - 0.7): Secondary source directories, slightly ambiguous announcements but clear indicators.
- Low (0.0 - 0.4): Missing official sources, contradictory dates, highly unauthoritative/unreliable scraped results.

JSON OUTPUT COMPLIANCE:
- You must output data strictly adhering to the requested JSON schema.
- All date fields must be 'YYYY-MM-DD' or null if unknown. Do not include year-only or range formats.
- Fill the 'remarks' field with a very concise description of your findings (e.g. 'Verified open till June 15, portal changed to new domain', or 'Closed for 2026 cycle since April 1st').
"""

    # If crawler returned None or was empty, provide a fallback notification context
    if not scraped_web_text or scraped_web_text.strip() == "":
        logger.warning(f"No scraped context provided for '{scholarship_name}'. Triggering fallback to historical links.")
        scraped_web_text = "WARNING: Search harvesting returned zero results. Please verify using historical links."
        fallback_active = True
    else:
        fallback_active = False

    # Structure the prompt input data
    user_prompt = f"""
INPUT SPREADSHEET ROW DETAILS:
- Scholarship Name: {scholarship_name}
- Processing Method (Historical): {historical_method}
- Info Link (Historical): {historical_info_link}
- Registration Link (Historical): {historical_reg_link}
- Estimated Timeline: {estimated_timeline}

RAW SCRAPED WEB CONTEXT (DUCKDUCKGO RESULTS):
{scraped_web_text}
"""

    logger.info(f"Submitting verification request to Gemini API for: '{scholarship_name}'")
    
    try:
        if USE_NEW_SDK:
            # Using latest google-genai client
            client = genai.Client(api_key=api_key)
            response = client.models.generate_content(
                model='gemini-2.5-flash',
                contents=user_prompt,
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    response_mime_type="application/json",
                    response_schema=ScholarshipVerification,
                    temperature=0.0, # High determinism
                ),
            )
            response_text = response.text
        else:
            # Using legacy google-generativeai SDK
            genai_legacy.configure(api_key=api_key)
            model = genai_legacy.GenerativeModel(
                model_name='gemini-2.5-flash',
                system_instruction=system_instruction
            )
            response = model.generate_content(
                user_prompt,
                generation_config=genai_legacy.GenerationConfig(
                    response_mime_type="application/json",
                    response_schema=ScholarshipVerification,
                    temperature=0.0,
                )
            )
            response_text = response.text

        logger.info(f"Gemini API responded successfully for '{scholarship_name}'")
        
        # Safe JSON parse to verify schema
        parsed_data = json.loads(response_text)
        
        # Validate using Pydantic (ensures Literal matching and types)
        validated_obj = ScholarshipVerification(**parsed_data)
        
        # Convert validated Pydantic object to dict
        result_dict = validated_obj.model_dump()
        
        # Override adjustment check if fallback active
        if fallback_active:
            result_dict["url_verification_fallback_used"] = True
            
        return result_dict

    except Exception as e:
        logger.error(f"Gemini API verification failed for '{scholarship_name}' due to exception: {str(e)}", exc_info=True)
        
        # Categorize exception message for user-friendly remarks
        err_msg = str(e)
        if "RESOURCE_EXHAUSTED" in err_msg or "429" in err_msg:
            user_remarks = "Gemini API daily free limit reached. Please check manually or upgrade to Pay-as-you-go."
        elif "UNAVAILABLE" in err_msg or "503" in err_msg:
            user_remarks = "Gemini API is temporarily busy. Please try again later."
        else:
            user_remarks = "Could not verify details due to a temporary system connection issue. Please check manually."
            
        # Construct a safe error-state dictionary so the orchestrator doesn't crash
        return {
            "scholarship_name": scholarship_name,
            "status": "CLOSED",  # Safe fallback
            "application_start_date": None,
            "application_deadline": None,
            "official_source_url": historical_info_link,
            "official_registration_url": historical_reg_link,
            "url_verification_fallback_used": True,
            "confidence_score": 0.0,
            "processing_method_detected": historical_method if historical_method in ['Online', 'Offline/Mail-in', 'Hybrid', 'Register First, Upload Later'] else 'Online',
            "remarks": user_remarks
        }
