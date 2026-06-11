import sys
import os
sys.path.insert(0, 'c:/Work/schreminder')

import logging
from src.spreadsheet.google_sheets import GoogleSheetsConnector
from src.notification.mailer import send_daily_email_report

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("TestEmailSender")

def send_existing_results_email():
    logger.info("Connecting to Google Sheets...")
    conn = GoogleSheetsConnector()
    conn.connect()
    
    logger.info("Reading spreadsheet raw grid data to extract cells and hyperlinks...")
    # Query up to column T, row 500
    range_name = f"'{conn.wks.title}'!A1:T500"
    response = conn.wks.spreadsheet.client.request(
        'get',
        f"https://sheets.googleapis.com/v4/spreadsheets/{conn.spreadsheet_id}",
        params={
            'ranges': range_name,
            'includeGridData': 'true',
            'fields': 'sheets/data/rowData/values(formattedValue,userEnteredValue,hyperlink)'
        }
    )
    
    response_json = response.json()
    sheet_data = response_json.get("sheets", [{}])[0].get("data", [{}])[0]
    row_data = sheet_data.get("rowData", [])
    
    if len(row_data) <= 1:
        logger.warning("No data rows found in spreadsheet.")
        return
        
    data_rows = row_data[1:] # Skip header
    results = []
    cells_to_update = []
    
    for idx, r in enumerate(data_rows, start=2): # Data starts at row 2
        cells = r.get("values", [])
        
        # Helper to get cell value or hyperlink target safely
        def get_cell_text(field_key: str) -> str:
            col_idx = conn.col_map.get(field_key)
            if col_idx and col_idx <= len(cells):
                cell = cells[col_idx - 1]
                return cell.get("formattedValue", "").strip()
            return ""

        def get_cell_link(field_key: str) -> str:
            col_idx = conn.col_map.get(field_key)
            if col_idx and col_idx <= len(cells):
                cell = cells[col_idx - 1]
                return conn.extract_hyperlink(cell)
            return ""
            
        name = get_cell_text("scholarship_name")
        if not name:
            continue
            
        # Check active status (Col 3, "Status")
        active_status = get_cell_text("active_status").strip().upper()
        if active_status != "T":
            # Skip inactive rows
            continue
            
        # Extract output fields written to sheet
        status = get_cell_text("status")
        if not status:
            status = "CLOSED"  # Default if not set or failed
            
        start_date = get_cell_text("start_date")
        deadline = get_cell_text("deadline")
        
        # Extract links
        official_source_url = get_cell_link("verified_info_url")
        if not official_source_url:
            official_source_url = get_cell_text("verified_info_url")
            
        official_registration_url = get_cell_link("verified_reg_url")
        if not official_registration_url:
            official_registration_url = get_cell_text("verified_reg_url")
            
        # Fallback used boolean
        fallback_used_str = get_cell_text("fallback_used").strip().upper()
        url_verification_fallback_used = (fallback_used_str in ["TRUE", "T", "YES"])
        
        # Parse confidence score
        try:
            conf_str = get_cell_text("confidence").replace("%", "").strip()
            confidence_score = float(conf_str)
            if confidence_score > 1.0:
                confidence_score /= 100.0
        except Exception:
            confidence_score = 0.0
            
        processing_method_detected = get_cell_text("detected_method")
        if not processing_method_detected:
            processing_method_detected = "Online"
            
        remarks = get_cell_text("remarks")
        original_remarks = remarks
        
        # Clean up old raw tracebacks for the email report and sheet
        if "RESOURCE_EXHAUSTED" in remarks or "429" in remarks:
            remarks = "Gemini API daily free limit reached. Please check manually or upgrade to Pay-as-you-go."
        elif "UNAVAILABLE" in remarks or "503" in remarks:
            remarks = "Gemini API is temporarily busy. Please try again later."
            
        if remarks != original_remarks:
            col_idx = conn.col_map.get("remarks")
            if col_idx:
                from gspread import Cell
                cells_to_update.append(Cell(row=idx, col=col_idx, value=remarks))
            
        results.append({
            "row_idx": idx,
            "verified_data": {
                "scholarship_name": name,
                "status": status,
                "application_start_date": start_date,
                "application_deadline": deadline,
                "official_source_url": official_source_url,
                "official_registration_url": official_registration_url,
                "url_verification_fallback_used": url_verification_fallback_used,
                "confidence_score": confidence_score,
                "processing_method_detected": processing_method_detected,
                "remarks": remarks
            }
        })
        
    if cells_to_update:
        logger.info(f"Cleaning up {len(cells_to_update)} raw error remarks on the Google Sheet...")
        try:
            conn.wks.update_cells(cells_to_update, value_input_option='USER_ENTERED')
            logger.info("Successfully updated remarks on Google Sheet.")
        except Exception as e:
            logger.warning(f"Failed to update remarks on Google Sheet: {e}")
            
    logger.info(f"Loaded {len(results)} active verified rows. Compiling and sending email...")
    success = send_daily_email_report(results)
    if success:
        logger.info("Email sent successfully!")
        print("\n[SUCCESS] Email sent successfully using current results on Google Sheets!")
    else:
        logger.error("Failed to send email.")
        print("\n[ERROR] Email failed to send. Check your .env settings.")

if __name__ == "__main__":
    send_existing_results_email()
