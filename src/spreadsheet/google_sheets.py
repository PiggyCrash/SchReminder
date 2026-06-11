"""
Google Sheets connector for SchReminder Scout.

Handles read/write operations using gspread.
Enforces dynamic header mapping, hyperlink extraction, and consolidated batch updates.

Updated to support:
  - Col D (Verified) read for A4 bypass check
  - supplementary_source_url output column
"""

import os
import json
import logging
import re
from typing import List, Dict, Any, Tuple, Optional
import gspread
from gspread import Cell
from dotenv import load_dotenv

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("GoogleSheets")

load_dotenv()

class GoogleSheetsConnector:
    """
    Handles read/write operations with Google Sheets using gspread.
    Enforces dynamic header mapping, hyperlink extraction, and consolidated batch updates.
    """
    def __init__(self):
        self.spreadsheet_id = os.getenv("SPREADSHEET_ID")
        self.service_account_json_str = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
        self.client: Optional[gspread.Client] = None
        self.wks: Optional[gspread.Worksheet] = None
        self.headers: List[str] = []
        self.col_map: Dict[str, int] = {}

        # Exact column mapping for 'Scholarship List'
        self.expected_inputs = {
            "note":             ["Note"],                                                                         # Col B
            "scholarship_name": ["Name", "Scholarship Name", "Scholarship"],
            "active_status":    ["Status"],                                                                       # Col C
            "verified":         ["Verified"],                                                                     # Col D
            "country_region":   ["Country/Region", "Country", "Region"],
            "historical_method": ["Reg. Path", "Processing Method (Historical)", "Processing Method", "Method"],
            "historical_info_link": ["Info Link", "Info Link (Historical)", "Link Info", "Historical Info Link"],
            "historical_reg_link": ["Reg. Link", "Registration Link (Historical)", "Link Daftar", "Historical Registration Link"],
            "estimated_timeline": ["Est. Date", "Estimated Timeline", "Timeline"]
        }

        # Use 'Verified Status' for output to avoid overwriting their manual tracking column 'Status'
        self.expected_outputs = {
            "status":           ["Verified Status", "Scout Status"],
            "start_date":       ["Verified Start Date", "Start Date", "Application Start Date"],
            "deadline":         ["Verified Deadline", "Deadline", "Application Deadline"],
            "verified_info_url":  ["Verified Info Link", "Verified Source Link", "Verified Info URL"],
            "supplementary_url":  ["Supplementary Link", "Supplementary Source URL", "Announcement Link"],
            "verified_reg_url":   ["Verified Reg Link", "Verified Registration Link", "Verified Reg URL"],
            "fallback_used":    ["Fallback Used", "Url Verification Fallback Used"],
            "confidence":       ["Confidence Score", "Confidence"],
            "detected_method":  ["Detected Method", "Processing Method Detected"],
            "remarks":          ["Remarks", "Notes", "Summary"]
        }

    def connect(self, read_only: bool = False) -> None:
        """
        Authenticate with Google Sheets API and open the tracking sheet.

        Args:
            read_only: When True, skips auto-creating missing output header columns.
                       Use this in prototype/testing context to avoid touching the sheet.
        """
        if not self.spreadsheet_id:
            logger.error("SPREADSHEET_ID is not configured in .env")
            raise ValueError("Missing SPREADSHEET_ID in environment variables.")

        if not self.service_account_json_str:
            logger.error("GOOGLE_SERVICE_ACCOUNT_JSON is not configured in .env")
            raise ValueError("Missing GOOGLE_SERVICE_ACCOUNT_JSON in environment variables.")

        try:
            logger.info("Parsing Google Service Account credentials...")
            creds_dict = json.loads(self.service_account_json_str)

            logger.info(f"Authenticating via Google Service Account: {creds_dict.get('client_email')}")
            self.client = gspread.service_account_from_dict(creds_dict)

            logger.info(f"Opening Spreadsheet ID: '{self.spreadsheet_id}'")
            sh = self.client.open_by_key(self.spreadsheet_id)

            # Select first worksheet by default ('Scholarship List')
            self.wks = sh.get_worksheet(0)
            logger.info(f"Selected worksheet: '{self.wks.title}'")

            # Retrieve header row (row 1)
            self.headers = [h.strip() for h in self.wks.row_values(1)]
            logger.info(f"Retrieved sheet headers: {self.headers}")

            self._map_and_initialize_headers(read_only=read_only)

        except json.JSONDecodeError as je:
            logger.critical("GOOGLE_SERVICE_ACCOUNT_JSON in .env is not a valid JSON string!")
            raise je
        except gspread.exceptions.SpreadsheetNotFound as sne:
            service_email = json.loads(self.service_account_json_str).get("client_email")
            logger.critical(
                f"Spreadsheet not found or access denied for Google Service Account. "
                f"CRITICAL REMINDER: You must invite '{service_email}' as an 'Editor' on your Google Sheet!"
            )
            raise sne
        except Exception as e:
            logger.critical(f"Failed to connect to Google Sheets API: {str(e)}", exc_info=True)
            raise e

    def _map_and_initialize_headers(self, read_only: bool = False) -> None:
        """
        Dynamically maps input/output headers and automatically creates missing output headers.

        Args:
            read_only: When True, missing output columns are silently skipped instead of
                       being appended to the sheet. No write operations are performed.
        """
        self.col_map = {}
        headers_lower = [h.lower() for h in self.headers]

        def find_col_idx(aliases: List[str]) -> Optional[int]:
            for alias in aliases:
                alias_lower = alias.lower()
                if alias_lower in headers_lower:
                    return headers_lower.index(alias_lower) + 1
            return None

        # Map input columns
        for key, aliases in self.expected_inputs.items():
            idx = find_col_idx(aliases)
            if idx:
                self.col_map[key] = idx
            else:
                logger.warning(f"Could not find input field '{key}' (checked: {aliases})")

        # Check for required inputs
        required_inputs = ["scholarship_name", "historical_info_link", "historical_reg_link"]
        missing_inputs = [req for req in required_inputs if req not in self.col_map]
        if missing_inputs:
            raise ValueError(f"Spreadsheet is missing required input columns: {missing_inputs}. Check your headers!")

        # Map output columns
        headers_to_add = []
        for key, aliases in self.expected_outputs.items():
            idx = find_col_idx(aliases)
            if idx:
                self.col_map[key] = idx
            else:
                if read_only:
                    logger.debug(f"read_only mode: skipping missing output column '{aliases[0]}'")
                else:
                    primary_name = aliases[0]
                    new_col_idx = len(self.headers) + len(headers_to_add) + 1
                    headers_to_add.append((primary_name, new_col_idx))
                    self.col_map[key] = new_col_idx
                    logger.info(f"Will create output column '{primary_name}' at index {new_col_idx}")

        # If we have missing output headers (production mode only), batch-append them to Row 1
        if headers_to_add:
            max_new_col = max(col_idx for _, col_idx in headers_to_add)
            # Expand the sheet grid if it doesn't have enough columns yet.
            # gspread raises APIError 400 if you write beyond the current grid dimensions.
            try:
                sheet_props = self.wks.spreadsheet.fetch_sheet_metadata()
                for s in sheet_props.get("sheets", []):
                    if s["properties"]["sheetId"] == self.wks.id:
                        current_cols = s["properties"]["gridProperties"]["columnCount"]
                        if max_new_col > current_cols:
                            logger.info(
                                f"Expanding sheet from {current_cols} to {max_new_col} columns "
                                f"to accommodate new output headers..."
                            )
                            self.wks.spreadsheet.batch_update({
                                "requests": [{
                                    "updateSheetProperties": {
                                        "properties": {
                                            "sheetId": self.wks.id,
                                            "gridProperties": {"columnCount": max_new_col}
                                        },
                                        "fields": "gridProperties.columnCount"
                                    }
                                }]
                            })
                        break
            except Exception as expand_err:
                logger.warning(f"Could not pre-expand sheet columns (will try writing anyway): {expand_err}")

            logger.info(f"Appending {len(headers_to_add)} missing output header columns to Row 1...")
            cells_to_add = []
            for name, col_idx in headers_to_add:
                cells_to_add.append(Cell(row=1, col=col_idx, value=name))
                self.headers.append(name)
            self.wks.update_cells(cells_to_add, value_input_option='USER_ENTERED')
            logger.info("Successfully initialized missing output headers.")

    def extract_hyperlink(self, cell_data: dict) -> str:
        """
        Robustly extracts hyperlink target from cell metadata.
        Handles both cell.hyperlink metadata and formula-based =HYPERLINK("url", "label") values.
        """
        # 1. Embedded hyperlink field (Ctrl+K or Insert Link)
        if "hyperlink" in cell_data:
            return cell_data["hyperlink"]

        # 2. Formula-based hyperlink (e.g. =HYPERLINK("url", "label"))
        user_val = cell_data.get("userEnteredValue", {})
        if "formulaValue" in user_val:
            formula = user_val["formulaValue"]
            if formula.upper().startswith("=HYPERLINK("):
                match = re.search(r'=HYPERLINK\(\s*["\']([^"\']+)["\']', formula, re.IGNORECASE)
                if match:
                    return match.group(1)

        # 3. Direct text starting with http/https
        formatted_val = cell_data.get("formattedValue", "").strip()
        if formatted_val.startswith("http://") or formatted_val.startswith("https://"):
            return formatted_val

        return ""

    def read_scholarships(self) -> List[Dict[str, Any]]:
        """
        Read scholarship rows using the raw spreadsheets.get endpoint.
        Extracts visible text AND underlying hyperlink URLs from cells.
        Returns only rows where Status column = 'T'.

        Each returned dict contains:
          row_idx, scholarship_name, active_status, verified,
          historical_method, historical_info_link, historical_reg_link,
          estimated_timeline, note
        """
        if not self.wks:
            raise RuntimeError("Sheets API is not connected. Call connect() first.")

        logger.info("Reading scholarship rows and extracting cell hyperlinks...")

        try:
            range_name = f"'{self.wks.title}'!A1:T500"
            response = self.wks.spreadsheet.client.request(
                'get',
                f"https://sheets.googleapis.com/v4/spreadsheets/{self.spreadsheet_id}",
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
                logger.warning("Spreadsheet contains no data rows.")
                return []

            data_rows = row_data[1:]  # Skip header row
            scholarships = []

            for idx, r in enumerate(data_rows, start=2):
                cells = r.get("values", [])

                def get_cell_text(field_key: str) -> str:
                    col_idx = self.col_map.get(field_key)
                    if col_idx and col_idx <= len(cells):
                        cell = cells[col_idx - 1]
                        return cell.get("formattedValue", "").strip()
                    return ""

                def get_cell_link(field_key: str) -> str:
                    col_idx = self.col_map.get(field_key)
                    if col_idx and col_idx <= len(cells):
                        cell = cells[col_idx - 1]
                        return self.extract_hyperlink(cell)
                    return ""

                name = get_cell_text("scholarship_name")
                if not name:
                    continue

                # Skip rows where Status is not 'T' / 't'
                active_status = get_cell_text("active_status").strip().upper()
                if active_status != "T":
                    logger.info(f"Skipping row {idx} ('{name}') because active status is '{active_status}' (not 'T')")
                    continue

                # Read Col D (Verified) for A4 bypass check
                verified = get_cell_text("verified").strip().upper()

                # Extract link targets for Info Link and Reg Link
                info_link = get_cell_link("historical_info_link")
                reg_link  = get_cell_link("historical_reg_link")

                # If hyperlink was not extracted, fallback to cell text
                if not info_link:
                    info_link = get_cell_text("historical_info_link")
                if not reg_link:
                    reg_link = get_cell_text("historical_reg_link")

                hist_method = get_cell_text("historical_method")
                if not hist_method:
                    hist_method = "Online"

                scholarships.append({
                    "row_idx":              idx,
                    "scholarship_name":     name,
                    "active_status":        active_status,
                    "verified":             verified,
                    "historical_method":    hist_method,
                    "historical_info_link": info_link,
                    "historical_reg_link":  reg_link,
                    "estimated_timeline":   get_cell_text("estimated_timeline"),
                    "note":                 get_cell_text("note"),
                })

            logger.info(f"Loaded {len(scholarships)} active scholarship records from sheet.")
            return scholarships

        except Exception as e:
            logger.error(f"Failed to read sheet data via REST API: {str(e)}", exc_info=True)
            raise e

    def batch_write_results(self, verification_results: List[Dict[str, Any]]) -> None:
        """
        Performs a single consolidated batch write back to Google Sheets.
        Writes all output columns including the new supplementary_source_url.
        """
        if not self.wks:
            raise RuntimeError("Sheets API is not connected. Call connect() first.")

        if not verification_results:
            logger.info("No verification results to write back.")
            return

        logger.info(f"Preparing batch sheet commit for {len(verification_results)} rows...")
        cells_to_update = []

        for res in verification_results:
            row  = res["row_idx"]
            data = res["verified_data"]

            info_url = data.get("official_source_url", "")
            supp_url = data.get("supplementary_source_url", "")
            reg_url  = data.get("official_registration_url", "")

            info_formula = f'=HYPERLINK("{info_url}", "Link Info")' if info_url else ""
            supp_formula = f'=HYPERLINK("{supp_url}", "Announcement")' if supp_url else ""
            reg_formula  = f'=HYPERLINK("{reg_url}", "Link Daftar")' if reg_url else ""

            field_mappings = {
                "status":          data.get("status"),
                "start_date":      data.get("application_start_date"),
                "deadline":        data.get("application_deadline"),
                "verified_info_url":  info_formula,
                "supplementary_url":  supp_formula,
                "verified_reg_url":   reg_formula,
                "fallback_used":   "TRUE" if data.get("url_verification_fallback_used") else "FALSE",
                "confidence":      data.get("confidence_score"),
                "detected_method": data.get("processing_method_detected"),
                "remarks":         data.get("remarks")
            }

            for field, value in field_mappings.items():
                col_idx = self.col_map.get(field)
                if col_idx:
                    val_str = "" if value is None else str(value)
                    cells_to_update.append(Cell(row=row, col=col_idx, value=val_str))

        if not cells_to_update:
            logger.warning("No cells matched expected output columns for writing.")
            return

        try:
            logger.info(f"Executing bulk write of {len(cells_to_update)} cells...")
            self.wks.update_cells(cells_to_update, value_input_option='USER_ENTERED')
            logger.info("Successfully committed all verification results back to Google Sheet.")
        except Exception as e:
            logger.error(f"Google Sheets batch write failed: {str(e)}", exc_info=True)
            raise e
