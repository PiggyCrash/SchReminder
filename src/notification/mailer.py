import os
import smtplib
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import List, Dict, Any
from dotenv import load_dotenv

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("Mailer")

load_dotenv()

def compile_html_report(results: List[Dict[str, Any]]) -> str:
    """
    Compiles a simple, clean white-background HTML table from the verification results.
    """
    # Generate rows
    table_rows = []
    for idx, r in enumerate(results, 1):
        v = r["verified_data"]
        
        status = v["status"]
        if status == "OPEN":
            status_html = '<strong style="color: #16a34a;">OPEN</strong>'
        elif status == "NOT_YET_OPENED":
            status_html = '<strong style="color: #d97706;">NOT YET OPENED</strong>'
        else:
            status_html = '<span style="color: #dc2626;">CLOSED</span>'
            
        deadline = v["application_deadline"] if v["application_deadline"] else '-'
        start_date = v["application_start_date"] if v["application_start_date"] else '-'
        
        # Links
        links_list = []
        if v["official_source_url"]:
            links_list.append(f'<a href="{v["official_source_url"]}" target="_blank">Info Portal</a>')
        if v["official_registration_url"]:
            links_list.append(f'<a href="{v["official_registration_url"]}" target="_blank">Register</a>')
        links_html = " | ".join(links_list) if links_list else "-"
        
        fallback_note = " (Fallback)" if v["url_verification_fallback_used"] else ""
        
        table_rows.append(f"""
        <tr>
            <td style="border: 1px solid #cccccc; text-align: center; font-size: 13px; padding: 8px; color: #333333;">{idx}</td>
            <td style="border: 1px solid #cccccc; font-size: 13px; padding: 8px; font-weight: bold; color: #111111;">{v["scholarship_name"]}</td>
            <td style="border: 1px solid #cccccc; text-align: center; font-size: 13px; padding: 8px;">{status_html}{fallback_note}</td>
            <td style="border: 1px solid #cccccc; text-align: center; font-size: 13px; padding: 8px; color: #333333;">{start_date}</td>
            <td style="border: 1px solid #cccccc; text-align: center; font-size: 13px; padding: 8px; color: #333333;">{deadline}</td>
            <td style="border: 1px solid #cccccc; text-align: center; font-size: 13px; padding: 8px;">{links_html}</td>
            <td style="border: 1px solid #cccccc; font-size: 13px; padding: 8px; line-height: 1.4; color: #333333;">{v["remarks"]}</td>
        </tr>
        """)
        
    formatted_rows = "\n".join(table_rows)

    html_content = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Scholarship Verification Report</title>
</head>
<body style="background-color: #ffffff; font-family: Arial, sans-serif; margin: 20px; color: #333333;">
    <h2 style="font-size: 20px; font-weight: bold; margin-bottom: 10px; color: #111111;">Daily Scholarship Verification Report</h2>
    <p style="font-size: 14px; margin-bottom: 20px; color: #666666;">
        Below is the daily status of active scholarships updated on your tracking sheet.
    </p>
    <table border="1" cellpadding="8" cellspacing="0" style="border-collapse: collapse; width: 100%; font-family: Arial, sans-serif; background-color: #ffffff; border: 1px solid #cccccc;">
        <thead>
            <tr style="background-color: #f2f2f2; font-size: 13px; color: #111111;">
                <th style="border: 1px solid #cccccc; text-align: center; padding: 8px; width: 40px;">No.</th>
                <th style="border: 1px solid #cccccc; text-align: left; padding: 8px;">Scholarship Name</th>
                <th style="border: 1px solid #cccccc; text-align: center; padding: 8px; width: 120px;">Live Status</th>
                <th style="border: 1px solid #cccccc; text-align: center; padding: 8px; width: 100px;">Start Date</th>
                <th style="border: 1px solid #cccccc; text-align: center; padding: 8px; width: 100px;">Deadline</th>
                <th style="border: 1px solid #cccccc; text-align: center; padding: 8px; width: 150px;">Official Links</th>
                <th style="border: 1px solid #cccccc; text-align: left; padding: 8px;">Remarks & Notes</th>
            </tr>
        </thead>
        <tbody>
            {formatted_rows}
        </tbody>
    </table>
</body>
</html>
"""
    return html_content

def send_daily_email_report(results: List[Dict[str, Any]]) -> bool:
    """
    Sends the compiled daily HTML digest to your receiver email using SMTP settings.
    """
    host = os.getenv("SMTP_HOST")
    port = os.getenv("SMTP_PORT")
    user = os.getenv("SMTP_USER")
    passwd = os.getenv("SMTP_PASS")
    sender = os.getenv("SENDER_EMAIL")
    receiver = os.getenv("RECEIVER_EMAIL")
    
    # Check if configurations are set
    if not all([host, port, user, passwd, sender, receiver]):
        logger.warning("SMTP email credentials are incomplete in .env. Skipping email report dispatch.")
        return False
        
    try:
        port_int = int(port)
    except ValueError:
        logger.error(f"Invalid SMTP_PORT: '{port}'. Must be an integer.")
        return False
        
    logger.info(f"Assembling verification digest email for: '{receiver}'")
    
    try:
        # Build multipart message
        msg = MIMEMultipart("alternative")
        msg["Subject"] = "🎓 [Daily Digest] Scholarship Verification Sync Report"
        msg["From"] = f"Academic Scout Agent <{sender}>"
        msg["To"] = receiver
        
        # Compile HTML template
        html_report = compile_html_report(results)
        
        # Record MIME text type html
        part = MIMEText(html_report, "html")
        msg.attach(part)
        
        # Connect to SMTP server
        logger.info(f"Connecting to SMTP server {host}:{port_int}...")
        
        # standard SSL connection for port 465, STARTTLS for 587
        if port_int == 465:
            server = smtplib.SMTP_SSL(host, port_int, timeout=15)
        else:
            server = smtplib.SMTP(host, port_int, timeout=15)
            server.starttls()
            
        logger.info("SMTP connection established. Logging in...")
        server.login(user, passwd)
        
        logger.info(f"Sending email alert from '{sender}' to '{receiver}'...")
        server.sendmail(sender, receiver, msg.as_string())
        server.quit()
        
        logger.info("Daily email report successfully dispatched!")
        return True
        
    except Exception as e:
        logger.error(f"Failed to dispatch daily email report: {str(e)}", exc_info=True)
        # Safe return so the process doesn't fail catastrophically
        return False

if __name__ == "__main__":
    # Local quick compile test
    test_results = [
        {
            "row_idx": 2,
            "verified_data": {
                "scholarship_name": "Gates Cambridge Scholarship",
                "status": "OPEN",
                "application_start_date": "2026-09-01",
                "application_deadline": "2026-12-05",
                "official_source_url": "https://www.gatescambridge.org",
                "official_registration_url": "https://www.gatescambridge.org/apply",
                "url_verification_fallback_used": False,
                "confidence_score": 0.98,
                "processing_method_detected": "Online",
                "remarks": "Verified active cycle open for September 2026 intake. Portal is accessible."
            }
        }
    ]
    html = compile_html_report(test_results)
    print("HTML Report Compiled Successfully (length: {})".format(len(html)))
