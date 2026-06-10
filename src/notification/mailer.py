"""
Mailer — HTML email report builder and dispatcher for SchReminder Scout.

Updated to use the prototype's richer email template:
  - search_status-based colour coding (NET ERR, BLOCKED, NO DATA, BYPASS, OPEN/CLOSED)
  - UNI-TO-UNI badge on scholarship name
  - ~date prefix for monthly/quarterly precision estimates
  - Supplementary announcement link in Info cell
  - Quota-exceeded alert banner
"""

import os
import smtplib
import logging
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import List, Dict, Any
from dotenv import load_dotenv

from src.engine.name_parser import parse_scholarship_name

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("Mailer")

load_dotenv()


def compile_html_report(results: List[Dict[str, Any]], quota_exceeded: bool = False) -> str:
    """
    Compiles a rich HTML table from the verification results.

    Each result dict must contain:
      - "row_idx": int
      - "search_status": str  (SUCCESS | LOCKED | BYPASS | NETWORK_FAILURE | BLOCKED | NO_RESULTS)
      - "verified_data": dict with all 12 LLM output fields
    """
    model_name = os.getenv("OPENAI_MODEL", os.getenv("OPENAI_MODEL_2", "Cerebras LLM"))

    rows = []
    for r in results:
        data = r.get("verified_data", {})
        search_status_val = r.get("search_status", "SUCCESS")
        status = data.get("status", "CLOSED")

        # Determine status label and colour based on search_status first
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
        elif search_status_val == "FALLBACK":
            # Search engine failed but preferred_urls recovered the result
            status_color = (
                "#2ecc71" if status == "OPEN"
                else "#f39c12" if status == "NOT_YET_OPENED"
                else "#e74c3c"
            )
            _label = "NOT YET OPEN" if status == "NOT_YET_OPENED" else status
            status_label = f"{_label} ⚙️"  # small gear icon signals fallback mode
        else:
            status_color = (
                "#2ecc71" if status == "OPEN"
                else "#f39c12" if status == "NOT_YET_OPENED"
                else "#e74c3c"
            )
            status_label = "NOT YET OPEN" if status == "NOT_YET_OPENED" else status

        info_url = data.get("official_source_url")
        supp_url = data.get("supplementary_source_url")
        reg_url  = data.get("official_registration_url")

        # Build info link cell — primary + optional supplementary announcement link
        if info_url:
            info_cell = f'<a href="{info_url}" style="color: #3498db; text-decoration: none;">Info Link</a>'
        else:
            info_cell = '<span style="color: #bdc3c7;">-</span>'
        if supp_url:
            info_cell += (
                f' &nbsp;<a href="{supp_url}" style="color: #8e44ad; text-decoration: none; font-size: 11px;">'
                f'[Announcement ↗]</a>'
            )

        reg_cell = (
            f'<a href="{reg_url}" style="color: #2ecc71; text-decoration: none; font-weight: bold;">Reg Link</a>'
            if reg_url else '<span style="color: #bdc3c7;">-</span>'
        )

        # UNI-TO-UNI badge on scholarship name
        _parsed_name = parse_scholarship_name(data.get("scholarship_name", ""))
        if _parsed_name["type"] == "uni_to_uni":
            name_display = (
                f'{data.get("scholarship_name")} '
                f'<span style="background:#8e44ad;color:white;font-size:10px;'
                f'padding:1px 5px;border-radius:3px;vertical-align:middle;">UNI-TO-UNI</span>'
            )
        else:
            name_display = data.get("scholarship_name", "")

        # ~date prefix for monthly/quarterly precision estimates
        date_precision = data.get("date_precision", "exact")
        start_display = data.get("application_start_date") or "N/A"
        end_display   = data.get("application_deadline")    or "N/A"
        if date_precision in ("monthly", "quarterly") and start_display != "N/A":
            start_display = f"~{start_display}"
        if date_precision in ("monthly", "quarterly") and end_display != "N/A":
            end_display = f"~{end_display}"

        method  = data.get("processing_method_detected") or "-"
        remarks = data.get("remarks") or ""

        rows.append(f"""
        <tr style="border-bottom: 1px solid #dddddd;">
            <td style="padding: 12px 15px; font-weight: bold; color: #333333;">{name_display}</td>
            <td style="padding: 12px 15px; font-weight: bold; color: {status_color}; text-decoration: none; white-space: nowrap;">{status_label}</td>
            <td style="padding: 12px 15px; color: #555555;">{start_display}</td>
            <td style="padding: 12px 15px; color: #555555;">{end_display}</td>
            <td style="padding: 12px 15px; font-size: 12px;">{info_cell}</td>
            <td style="padding: 12px 15px; font-size: 12px;">{reg_cell}</td>
            <td style="padding: 12px 15px; color: #555555; font-size: 12px;">{method}</td>
            <td style="padding: 12px 15px; color: #7f8c8d; font-size: 13px;">{remarks}</td>
        </tr>
        """)

    formatted_rows = "\n".join(rows)

    alert_banner = ""
    if quota_exceeded:
        alert_banner = """
        <div style="background-color: #fce4e4; border: 1px solid #f5c6cb; color: #721c24; padding: 15px; border-radius: 4px; margin-bottom: 20px; font-family: Arial, sans-serif;">
            <strong>⚠️ RUN INTERRUPTED:</strong> The Cerebras API daily quota limit was hit. The scouting process aborted early and has saved/reported the results gathered up to that point.
        </div>
        """

    html_content = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Scholarship Verification Run Report</title>
</head>
<body style="background-color: #f9f9f9; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; margin: 0; padding: 20px; color: #333333;">
    <div style="max-width: 1100px; margin: 0 auto; background-color: #ffffff; padding: 30px; border-radius: 8px; box-shadow: 0 4px 10px rgba(0,0,0,0.05); border-top: 5px solid #3498db;">

        {alert_banner}

        <h2 style="font-size: 22px; font-weight: 600; margin-top: 0; color: #2c3e50;">🎓 Scholarship Verification Report</h2>
        <p style="font-size: 14px; color: #7f8c8d; margin-bottom: 25px;">
            This report contains the latest verification details compiled by the automated Scout pipeline
            running model <strong>{model_name}</strong>.
            Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}
        </p>

        <div style="overflow-x: auto; -webkit-overflow-scrolling: touch;">
        <table style="border-collapse: collapse; min-width: 800px; width: 100%; text-align: left; font-size: 14px;">
            <thead>
                <tr style="background-color: #f8f9fa; border-bottom: 2px solid #3498db; color: #2c3e50;">
                    <th style="padding: 12px 15px; white-space: nowrap;">Scholarship Name</th>
                    <th style="padding: 12px 15px; white-space: nowrap;">Status</th>
                    <th style="padding: 12px 15px; white-space: nowrap;">Start Date</th>
                    <th style="padding: 12px 15px; white-space: nowrap;">Deadline</th>
                    <th style="padding: 12px 15px; white-space: nowrap;">Info Link</th>
                    <th style="padding: 12px 15px; white-space: nowrap;">Reg. Link</th>
                    <th style="padding: 12px 15px; white-space: nowrap;">Method</th>
                    <th style="padding: 12px 15px;">Remarks</th>
                </tr>
            </thead>
            <tbody>
                {formatted_rows}
            </tbody>
        </table>
        </div>

        <div style="margin-top: 30px; padding-top: 20px; border-top: 1px solid #eeeeee; font-size: 12px; color: #95a5a6; text-align: center;">
            Academic Scout Automated System &bull; {len(results)} scholarships processed
        </div>
    </div>
</body>
</html>
"""
    return html_content


def send_daily_email_report(results: List[Dict[str, Any]], quota_exceeded: bool = False) -> bool:
    """
    Sends the compiled daily HTML digest to your receiver email using SMTP settings.

    Args:
        results:        List of processed result dicts.
        quota_exceeded: When True, shows quota-exceeded alert banner in the email.
    """
    host     = os.getenv("SMTP_HOST")
    port     = os.getenv("SMTP_PORT")
    user     = os.getenv("SMTP_USER")
    passwd   = os.getenv("SMTP_PASS")
    sender   = os.getenv("SENDER_EMAIL")
    receiver = os.getenv("RECEIVER_EMAIL")

    if not all([host, port, user, passwd, sender, receiver]):
        logger.warning("SMTP configuration is incomplete in .env. Cannot send report email.")
        return False

    try:
        port_int = int(port)
    except ValueError:
        logger.error(f"Invalid SMTP_PORT: '{port}'. Must be an integer.")
        return False

    logger.info(f"Assembling verification digest email for: '{receiver}'")

    try:
        msg = MIMEMultipart("alternative")
        subject = f"🎓 Scholarship Verification Report - {len(results)} Processed"
        if quota_exceeded:
            subject = "⚠️ " + subject + " (API Limit Hit)"
        msg["Subject"] = subject
        msg["From"]    = f"Academic Scout Agent <{sender}>"
        msg["To"]      = receiver

        html_report = compile_html_report(results, quota_exceeded=quota_exceeded)
        msg.attach(MIMEText(html_report, "html"))

        logger.info(f"Connecting to SMTP server {host}:{port_int}...")
        if port_int == 465:
            server = smtplib.SMTP_SSL(host, port_int, timeout=15)
        else:
            server = smtplib.SMTP(host, port_int, timeout=15)
            server.starttls()

        logger.info("SMTP connection established. Logging in...")
        server.login(user, passwd)
        logger.info(f"Sending email from '{sender}' to '{receiver}'...")
        server.sendmail(sender, receiver, msg.as_string())
        server.quit()

        logger.info("Daily email report successfully dispatched!")
        return True

    except Exception as e:
        logger.error(f"Failed to dispatch daily email report: {str(e)}", exc_info=True)
        return False
