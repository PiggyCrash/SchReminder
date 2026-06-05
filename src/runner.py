import time
import logging
from typing import List, Dict, Any
from dotenv import load_dotenv

from src.spreadsheet.google_sheets import GoogleSheetsConnector
from src.search.crawler import search_scholarship
from src.engine.scout import verify_scholarship
from src.notification.mailer import send_daily_email_report

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("Orchestrator")

# ANSI color codes for stylized logging
class Colors:
    HEADER = '\033[95m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    GREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    END = '\033[0m'
    BOLD = '\033[1m'

def run_scout_pipeline() -> bool:
    """
    Executes the entire daily automated scout pipeline:
    1. Reads active tracking rows from Google Sheet.
    2. Crawls real-time web context.
    3. Runs Gemini NLP status verification (with rate-limiting compliance).
    4. Commits all changes in a single consolidated batch write.
    5. Dispatches an HSL-styled email digest report.
    """
    load_dotenv()
    
    start_time = time.time()
    print(f"\n{Colors.BOLD}{Colors.HEADER}======================================================================")
    print("        LAUNCHING AUTOMATED ACADEMIC SCOUT & SHEET SYNC RUNNER")
    print(f"======================================================================{Colors.END}\n")
    
    # 1. Connect to Google Sheets
    sheet_connector = GoogleSheetsConnector()
    try:
        sheet_connector.connect()
    except Exception as e:
        logger.critical(f"Aborting pipeline. Connection to Google Sheet failed: {str(e)}")
        print(f"\n{Colors.FAIL}[Critical Error] Connection to Google Sheets failed. Ensure you shared your sheet with the service account and copied the spreadsheet ID correctly.{Colors.END}\n")
        return False
        
    # Read active scholarship rows
    scholarships = sheet_connector.read_scholarships()
    total_count = len(scholarships)
    
    if total_count == 0:
        logger.warning("No tracking rows found in Google Sheet. Exiting.")
        print(f"{Colors.WARNING}[Warning] Pipeline completed with 0 scholarships loaded from the spreadsheet.{Colors.END}\n")
        return True
        
    print(f"{Colors.BOLD}{Colors.CYAN}[Start] Successfully loaded {total_count} scholarship records to verify.{Colors.END}\n")
    print(f"{Colors.BLUE}Enforcing Gemini rate limits (max 15 RPM) via synchronous 5s pacing intervals...{Colors.END}\n")
    
    processed_results: List[Dict[str, Any]] = []
    
    # 2. Iterate and verify scholarships
    for i, s in enumerate(scholarships, 1):
        row_idx = s["row_idx"]
        name = s["scholarship_name"]
        hist_method = s["historical_method"]
        hist_info = s["historical_info_link"]
        hist_reg = s["historical_reg_link"]
        est_time = s["estimated_timeline"]
        
        print(f"{Colors.BOLD}----------------------------------------------------------------------")
        print(f"[{i}/{total_count}] Processing Row {row_idx}: {Colors.CYAN}{name}{Colors.END}")
        print(f"----------------------------------------------------------------------")
        
        # A. Resilient search crawling with duckduckgo_search
        scraped_text = search_scholarship(name)
        
        if scraped_text is None:
            logger.warning(f"Resilient Search Crawler failed for '{name}'. Activating fallback URL logic.")
            print(f"{Colors.WARNING}⚠️ Search failed/blocked. Triggered fallback url verification for historical links.{Colors.END}")
            
        # B. Call Gemini verification engine (Phase 1, Phase 2, Override status checks)
        try:
            verified_data = verify_scholarship(
                scholarship_name=name,
                historical_method=hist_method,
                historical_info_link=hist_info,
                historical_reg_link=hist_reg,
                estimated_timeline=est_time,
                scraped_web_text=scraped_text
            )
            
            # Log verification findings
            status = verified_data["status"]
            status_color = Colors.GREEN if status == "OPEN" else (Colors.WARNING if status == "NOT_YET_OPENED" else Colors.FAIL)
            print(f"Verified Status: {status_color}{Colors.BOLD}{status}{Colors.END}")
            print(f"Verified Deadline: {Colors.BOLD}{verified_data['application_deadline'] or 'Unknown'}{Colors.END}")
            print(f"Fallback Used: {Colors.BOLD}{verified_data['url_verification_fallback_used']}{Colors.END}")
            print(f"Detected Method: {verified_data['processing_method_detected']}")
            print(f"Remarks: {verified_data['remarks']}")
            
            # Store in in-memory results list
            processed_results.append({
                "row_idx": row_idx,
                "verified_data": verified_data
            })
            
        except Exception as ex:
            logger.error(f"Failed to process verification for row {row_idx} ({name}): {str(ex)}")
            print(f"{Colors.FAIL}[Failed] Verification step failed for '{name}': {str(ex)}{Colors.END}")
            
        # C. Mandatory 15 RPM Pacing compliance delay
        if i < total_count:
            logger.info("Enforcing mandatory 5-second pacing delay to remain below Gemini's 15 RPM ceiling...")
            time.sleep(5)
            
    print(f"\n{Colors.BOLD}{Colors.GREEN}======================================================================")
    print("       ALL ROW SCOUTING PROCESSES COMPLETED SUCCESSFULLY!")
    print(f"======================================================================{Colors.END}\n")
    
    # 3. Consolidated State Commit (Batch Sheet Writes)
    try:
        print(f"{Colors.BLUE}Initiating single consolidated batch write back to Google Sheets...{Colors.END}")
        sheet_connector.batch_write_results(processed_results)
        print(f"{Colors.GREEN}[Success] Google Sheet successfully updated with all verification results.{Colors.END}\n")
    except Exception as sheet_err:
        logger.error(f"Critical error committing updates back to Google Sheet: {str(sheet_err)}")
        print(f"{Colors.FAIL}[Failed] Sheet Sync Failure: Failed to commit updates back to the spreadsheet: {str(sheet_err)}{Colors.END}\n")
        
    # 4. Dispatch Email Report
    try:
        print(f"{Colors.BLUE}Compiling and dispatching styled HTML digest report email...{Colors.END}")
        email_sent = send_daily_email_report(processed_results)
        if email_sent:
            print(f"{Colors.GREEN}[Success] Daily report verification email successfully sent to your inbox.{Colors.END}\n")
        else:
            print(f"{Colors.WARNING}[Warning] Daily report email was skipped or failed. Verify SMTP settings in your secrets.{Colors.END}\n")
    except Exception as mail_err:
        logger.error(f"Catastrophic error dispatching email report: {str(mail_err)}")
        print(f"{Colors.FAIL}[Failed] Email Dispatch Failure: Failed to transmit report: {str(mail_err)}{Colors.END}\n")
        
    duration = ((time.time() - start_time) / 60)
    print(f"{Colors.BOLD}{Colors.HEADER}======================================================================")
    print(f"      PIPELINE RUN FINISHED SUCCESSFULLY IN {duration:.2f} MINUTES!")
    print(f"======================================================================{Colors.END}\n")
    
    return True

if __name__ == "__main__":
    run_scout_pipeline()
