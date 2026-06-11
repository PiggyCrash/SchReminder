import sys
import os
sys.path.insert(0, 'c:/Work/schreminder')

import time
import json
import logging
import requests
import urllib.parse
from typing import Dict, Any, Optional, List
from bs4 import BeautifulSoup

try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path='c:/Work/schreminder/.env', override=True)
except ImportError:
    # Manual fallback to read .env if python-dotenv is not installed
    env_path = 'c:/Work/schreminder/.env'
    if os.path.exists(env_path):
        with open(env_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, val = line.split('=', 1)
                    val = val.strip().strip("'").strip('"')
                    os.environ[key.strip()] = val

from src.spreadsheet.google_sheets import GoogleSheetsConnector

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sch_prototype")

# Hardcoded scholarship name to test (change this to check different ones)
TEST_SCHOLARSHIP_NAME = "Beasiswa Indonesia Bangkit (BIB) - LPDP"

class CerebrasQuotaExceededException(Exception):
    pass

def clean_bing_url(raw_url: str) -> str:
    if not raw_url:
        return ""
    if "/ck/a?" not in raw_url:
        return raw_url
    try:
        parsed_url = urllib.parse.urlparse(raw_url)
        queries = urllib.parse.parse_qs(parsed_url.query)
        u_param = queries.get("u", [None])[0]
        if u_param:
            base64_str = u_param[2:]
            base64_str += "=" * ((4 - len(base64_str) % 4) % 4)
            import base64
            try:
                decoded_bytes = base64.b64decode(base64_str.encode('utf-8'))
            except Exception:
                decoded_bytes = base64.urlsafe_b64decode(base64_str.encode('utf-8'))
            decoded = decoded_bytes.decode('utf-8', errors='ignore')
            if decoded.startswith('http://') or decoded.startswith('https://'):
                return decoded
    except Exception as e:
        logger.debug(f"Failed to decode Bing URL: {raw_url}, error: {str(e)}")
    return raw_url

def clean_yahoo_url(redirect_url: str) -> str:
    """Decodes Yahoo's search redirect URL to get the actual destination URL."""
    if not redirect_url or "r.search.yahoo.com" not in redirect_url:
        return redirect_url
    try:
        # Yahoo encodes the real URL in the RU= parameter
        parsed = urllib.parse.urlparse(redirect_url)
        # The real URL is buried in the path after /RU=/
        path = parsed.path
        if "/RU=" in path:
            ru_part = path.split("/RU=")[1].split("/")[0]
            return urllib.parse.unquote(ru_part)
    except Exception:
        pass
    return redirect_url

def perform_bing_fallback_raw(query: str, max_results: int = 5) -> Optional[List[Dict[str, str]]]:
    """
    Fallback search using Yahoo (Bing returns a JS-only page on this machine).
    Yahoo returns real HTML results that are parseable without JavaScript.
    Retries up to 3 times on 5xx server errors (short 5s sleep between attempts).
    """
    logger.info(f"🌐 Falling back to Yahoo Search scraping for query: '{query}'")
    search_url = f"https://search.yahoo.com/search?p={urllib.parse.quote(query)}"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
    }
    for attempt in range(1, 4):  # up to 3 quick retries for server errors
        try:
            response = requests.get(search_url, headers=headers, timeout=15, allow_redirects=True)
            if response.status_code >= 500:
                logger.warning(f"Yahoo HTTP {response.status_code} (attempt {attempt}/3) — retrying in 5s...")
                if attempt < 3:
                    time.sleep(5)
                    continue
                else:
                    logger.error("Yahoo returned 5xx after 3 attempts.")
                    return None
            if not response.ok:
                logger.warning(f"Yahoo search returned HTTP {response.status_code}")
                return None
            soup = BeautifulSoup(response.text, 'html.parser')
            results = []
            algo_elements = soup.find_all(class_='algo')
            logger.info(f"Yahoo returned {len(algo_elements)} result elements")
            for el in algo_elements[:max_results]:
                anchors = el.find_all('a', href=True)
                if not anchors:
                    continue
                a = anchors[0]
                title = a.get_text(strip=True)
                raw_href = a.get('href', '')
                href = clean_yahoo_url(raw_href)
                
                snippet_div = el.find('div', class_='compText') or el.find('p')
                snippet = snippet_div.get_text(strip=True)[:300] if snippet_div else ""
                
                if title and href and href.startswith('http'):
                    results.append({
                        "title": title,
                        "url": href,
                        "snippet": snippet
                    })
            if not results:
                logger.warning("Yahoo returned 0 parseable results.")
                return None
            logger.info(f"Yahoo: harvested {len(results)} results successfully.")
            return results
        except Exception as e:
            logger.error(f"Yahoo fallback failed: {str(e)}")
            return None
    return None

def search_scholarship_with_retry(scholarship_name: str, max_results: int = 5, retries: int = 3, retry_delay: int = 180) -> Optional[List[Dict[str, str]]]:
    """
    Searches for scholarship on DuckDuckGo and retrieves top results.
    - Captcha/rate-limit (HTTP block): retries with 3-min sleep, then Bing fallback.
    - SSL/connection errors: immediately tries Bing fallback (no sleep needed).
    - Falls back to Bing Search on any terminal failure.
    """
    # If the caller already built a specific query (contains year or deadline), use it directly.
    # Otherwise append a generic suffix to help search engines find the right pages.
    already_specific = any(kw in scholarship_name.lower() for kw in ["2026", "2025", "deadline", "indonesia", "timeline"])
    if already_specific:
        query = scholarship_name.strip()
    else:
        query = f"{scholarship_name} scholarship deadline timeline 2026"
    url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
    }
    
    for attempt in range(1, retries + 1):
        try:
            logger.info(f"Searching DuckDuckGo (Attempt {attempt}/{retries}) for: '{scholarship_name}'")
            response = requests.get(url, headers=headers, timeout=15)
            
            is_blocked = False
            if not response.ok:
                is_blocked = True
            elif "ddg-captcha" in response.text or "robot" in response.text or "ddg-lms" in response.text:
                is_blocked = True
                
            if is_blocked:
                logger.warning("DuckDuckGo returned captcha/rate-limiting block. Attempting Bing Search...")
                bing_results = perform_bing_fallback_raw(query, max_results)
                if bing_results:
                    return bing_results
                    
                # If Bing also fails, wait and retry DuckDuckGo
                if attempt < retries:
                    logger.info(f"DuckDuckGo and Bing blocked. Sleeping for {retry_delay} seconds (3 mins) before retry...")
                    time.sleep(retry_delay)
                    continue
                else:
                    logger.error("DuckDuckGo and Bing search blocked after all retry attempts.")
                    return None
            
            soup = BeautifulSoup(response.text, 'html.parser')
            results = []
            result_divs = soup.find_all('div', class_='result')
            for div in result_divs[:max_results]:
                title_a = div.find('a', class_='result__url') or div.find('a', class_='result__title')
                if not title_a:
                    continue
                title = title_a.get_text(strip=True)
                href = title_a.get('href', '')
                
                if 'uddg=' in href:
                    try:
                        parsed_href = urllib.parse.urlparse(href)
                        queries = urllib.parse.parse_qs(parsed_href.query)
                        if 'uddg' in queries:
                            href = queries['uddg'][0]
                    except Exception:
                        pass
                elif href.startswith('//'):
                    href = 'https:' + href
                    
                snippet_div = div.find(class_='result__snippet')
                snippet = snippet_div.get_text(strip=True) if snippet_div else ""
                
                if title and href:
                    results.append({
                        "title": title,
                        "url": href,
                        "snippet": snippet
                    })
            return results
        except Exception as e:
            err_str = str(e)
            err_type = type(e).__name__
            logger.error(f"Search attempt raised exception ({err_type}): {err_str}")
            # SSL / connection errors: the host is unreachable, sleep won't help.
            # Skip the retry sleep entirely and go straight to Bing fallback.
            is_network_error = any(kw in err_type or kw in err_str for kw in [
                "SSL", "Connection", "timeout", "Timeout", "HANDSHAKE"
            ])
            if is_network_error:
                logger.warning("Network/SSL error — skipping retry sleep, immediately trying Bing fallback...")
                bing_results = perform_bing_fallback_raw(query, max_results)
                if bing_results:
                    return bing_results
                logger.error("Bing fallback also failed after DuckDuckGo network error.")
                return None
            # For other errors, retry with sleep if attempts remain
            if attempt < retries:
                logger.info(f"Sleeping for {retry_delay} seconds before retry...")
                time.sleep(retry_delay)
            else:
                return None
    return None

def clean_html(html_content: str) -> str:
    soup = BeautifulSoup(html_content, "html.parser")
    for element in soup(["script", "style", "nav", "header", "footer", "iframe", "aside"]):
        element.decompose()
    text = soup.get_text(separator=" ")
    cleaned_lines = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            cleaned_lines.append(line)
    return "\n".join(cleaned_lines)

def fetch_webpage_content(url: str, retries: int = 2, retry_delay: int = 180) -> Optional[str]:
    """
    Fetches URL HTML content.
    - HTTP 429/403 or captcha blocks: retries with 3-min sleep (genuine rate limits).
    - SSL/connection errors: skips immediately without sleep (server refused us).
    """
    if not url or not (url.startswith("http://") or url.startswith("https://")):
        return None
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
    }
    for attempt in range(1, retries + 1):
        try:
            logger.info(f"Fetching URL (Attempt {attempt}/{retries}): {url}")
            response = requests.get(url, headers=headers, timeout=15)
            
            is_blocked = False
            if response.status_code in [429, 403]:
                is_blocked = True
            else:
                # Strip scripts/styles before checking — JS variable names like 'silentcaptcha'
                # appear on many normal sites and must not be treated as captcha challenges
                from bs4 import BeautifulSoup as _BS
                _soup = _BS(response.text, 'html.parser')
                for _tag in _soup(['script', 'style']):
                    _tag.decompose()
                visible_text = _soup.get_text().lower()
                if any(phrase in visible_text for phrase in [
                    "ddg-captcha", "i am not a robot", "prove you are human",
                    "too many requests", "ddg-lms", "verify you are human"
                ]):
                    is_blocked = True
                    
            if is_blocked:
                logger.warning(f"⚠️ Rate limit or Captcha block detected on {url}. Status: {response.status_code}")
                if attempt < retries:
                    logger.info(f"Sleeping for {retry_delay} seconds (3 mins) before retry...")
                    time.sleep(retry_delay)
                    continue
                else:
                    logger.error(f"Failed to fetch {url} after {retries} attempts due to rate-limiting.")
                    return None
                    
            if response.ok:
                return response.text
            else:
                logger.warning(f"Non-OK response code: {response.status_code} for {url}")
                return None
        except Exception as e:
            err_str = str(e)
            err_type = type(e).__name__
            logger.error(f"Error fetching {url} ({err_type}): {err_str[:200]}")
            # Connection resets, SSL errors, timeouts = server refused us.
            # Sleeping 3 minutes and hitting the same server won't fix this — skip.
            is_network_error = any(kw in err_type or kw in err_str for kw in [
                "SSL", "Connection", "ConnectionReset", "timeout", "Timeout",
                "HANDSHAKE", "forcibly closed", "10054", "10061", "RemoteDisconnected"
            ])
            if is_network_error:
                logger.warning(f"Network error on {url} — skipping without retry sleep.")
                return None
            # For unexpected errors only, retry with sleep if attempts remain
            if attempt < retries:
                logger.info(f"Sleeping for {retry_delay} seconds before retry...")
                time.sleep(retry_delay)
            else:
                return None
    return None

def extract_hyperlinks(html_content: str, base_url: str) -> List[Dict[str, str]]:
    soup = BeautifulSoup(html_content, "html.parser")
    links = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        text = a.get_text(strip=True)
        if not href or href.startswith("javascript:") or href.startswith("#") or href.startswith("mailto:"):
            continue
        absolute_url = urllib.parse.urljoin(base_url, href)
        if absolute_url not in seen:
            seen.add(absolute_url)
            links.append({"text": text, "url": absolute_url})
    return links

def filter_candidate_links(links: List[Dict[str, str]], scholarship_name: str) -> Dict[str, List[str]]:
    keywords = [w.lower() for w in scholarship_name.split() if len(w) > 3]
    keywords_info = keywords + ["research", "rs", "student", "graduate", "announcement", "scholarship", "guideline", "guide", "tahap", "stem"]
    keywords_reg = ["apply", "register", "pendaftaran", "daftar", "login", "portal", "form", "online", "submit"]
    
    candidate_info = []
    candidate_reg = []
    
    for l in links:
        url_lower = l["url"].lower()
        text_lower = l["text"].lower()
        
        if any(kw in url_lower or kw in text_lower for kw in keywords_reg):
            candidate_reg.append(l["url"])
            
        if any(kw in url_lower or kw in text_lower for kw in keywords_info):
            candidate_info.append(l["url"])
            
    return {
        "info": list(set(candidate_info))[:10],
        "reg": list(set(candidate_reg))[:10]
    }

def verify_scholarship_llama(
    scholarship_name: str,
    historical_method: str,
    historical_info_link: str,
    historical_reg_link: str,
    estimated_timeline: str,
    scraped_web_text: str,
    candidate_info_links: List[str],
    candidate_reg_links: List[str],
    model_name: Optional[str] = None
) -> Dict[str, Any]:
    """
    Calls the OpenAI-compatible endpoint (Cerebras/Llama) using requests.
    """
    api_key = os.getenv("OPENAI_API_KEY")
    base_url = os.getenv("OPENAI_BASE_URL", "https://api.cerebras.ai/v1")
    if not model_name:
        model_name = os.getenv("OPENAI_MODEL", "gpt-oss-120b")
        
    if not api_key:
        raise ValueError("Missing OPENAI_API_KEY in environment variables.")
        
    system_instruction = """You are an advanced Automated Academic Scout and Data Verification Agent.
Your task is to analyze raw, scraped web text data and candidate links provided to you, verify the real-time application status of a specific scholarship, and output deterministic JSON data structures.

You must output a JSON object with the following fields:
1. "scholarship_name": The exact name of the scholarship processed (string)
2. "status": Strictly choose one: 'OPEN' | 'CLOSED' | 'NOT_YET_OPENED'
3. "application_start_date": String in YYYY-MM-DD format, or null if unknown
4. "application_deadline": String in YYYY-MM-DD format, or null if unknown
5. "official_source_url": The verified specific information link found or validated (string)
6. "official_registration_url": The verified specific submission/registration link found or validated (string)
7. "url_verification_fallback_used": true (boolean) if the independent scraped text was insufficient and you had to rely strictly on the user's historical links, false (boolean) if the scraped text found cleaner/newer active links
8. "confidence_score": Float between 0.0 to 1.0 reflecting source reliability based on the text context
9. "processing_method_detected": Detect if registration requires 'Online', 'Offline/Mail-in', 'Hybrid', or 'Register First, Upload Later' (string)
10. "remarks": A brief, concise summary of findings or notes (string)

CRITICAL RULES FOR LINKS — READ CAREFULLY AND FOLLOW STRICTLY:
- "official_source_url" (Info Link) MUST be a URL that leads to a page with scholarship details, announcement text, guidelines, or timeline information.
- "official_registration_url" (Registration Link) MUST be a DIFFERENT URL that directly allows the user to register, log in, or submit an online application (e.g., a portal login page, Google Form, or direct submission URL).
- ABSOLUTELY FORBIDDEN: Do NOT output the same URL for both "official_source_url" and "official_registration_url" unless the registration form is literally embedded directly on the info page itself. This is a hard rule with zero exceptions.
- If you cannot find a distinct registration link, set "official_registration_url" to the best candidate from the CANDIDATE REGISTRATION LINKS list, or null. Do NOT copy the info URL.
- Always prefer specific sub-page URLs (e.g., /apply, /register, /form, /pendaftaran) over generic homepage URLs for the registration field.
- Prioritize selecting from the provided lists of "CANDIDATE INFO LINKS" and "CANDIDATE REGISTRATION LINKS" if they are available and relevant.
- For date extraction: Parse application dates precisely from the page text. Look for explicit open/close/deadline date ranges. Output in YYYY-MM-DD format.

SYSTEM LOGIC & ANALYSIS STRATEGY:
1. PHASE 1: Find the most current application window dates from the scraped text. Look for explicit date ranges like 'February 12 to February 25, 2026'.
2. PHASE 2: Cross-reference discovered timelines/links with historical links.
3. PHASE 3: Select distinct, specific URLs for info and registration — never duplicates.
4. STATUS RULE (STRICT): Compare the application deadline you find against TODAY'S DATE (provided in the user prompt).
   - If today's date is BEFORE the application start date → status = 'NOT_YET_OPENED'
   - If today's date is WITHIN start and end date → status = 'OPEN'
   - If today's date is AFTER the application deadline → status = 'CLOSED'
   - If no dates found, use the estimated timeline and apply the same logic.
   - NEVER output 'OPEN' if today's date is after the deadline. This is a hard rule.
"""

    candidate_info_str = "\n".join([f"- {url}" for url in candidate_info_links]) if candidate_info_links else "None found."
    candidate_reg_str = "\n".join([f"- {url}" for url in candidate_reg_links]) if candidate_reg_links else "None found."

    user_prompt = f"""
TODAY'S DATE: {time.strftime('%Y-%m-%d')} (use this to determine if the scholarship is currently OPEN, CLOSED, or NOT_YET_OPENED)

INPUT SPREADSHEET ROW DETAILS:
- Scholarship Name: {scholarship_name}
- Processing Method (Historical): {historical_method}
- Info Link (Historical): {historical_info_link}
- Registration Link (Historical): {historical_reg_link}
- Estimated Timeline: {estimated_timeline}

RAW SCRAPED WEB CONTEXT:
{scraped_web_text}

CANDIDATE INFO LINKS FOUND on webpages:
{candidate_info_str}

CANDIDATE REGISTRATION LINKS FOUND on webpages:
{candidate_reg_str}
"""

    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": 0.0,
        "response_format": {"type": "json_object"}
    }
    
    logger.info(f"Submitting verification request to Llama ({model_name}) API for: '{scholarship_name}'")
    start_time = time.time()
    # Retry once on timeout — Cerebras can be slow with large contexts
    for llm_attempt in range(1, 3):
        try:
            response = requests.post(url, json=payload, headers=headers, timeout=90)
            break  # success
        except requests.exceptions.Timeout:
            if llm_attempt < 2:
                logger.warning(f"Cerebras API timed out (attempt {llm_attempt}/2) — retrying in 5s...")
                time.sleep(5)
            else:
                raise
    latency = time.time() - start_time
    
    # Catch API rate-limiting or quota limit hits
    if response.status_code == 429 or "RESOURCE_EXHAUSTED" in response.text or "quota" in response.text.lower() or "limit exceeded" in response.text.lower():
        raise CerebrasQuotaExceededException(f"Cerebras API limit/quota hit: {response.status_code} - {response.text}")
        
    if not response.ok:
        raise RuntimeError(f"Llama API failed: {response.status_code} - {response.text}")
        
    res_json = response.json()
    content_str = res_json["choices"][0]["message"]["content"]
    parsed_data = json.loads(content_str)
    parsed_data["latency"] = latency
    return parsed_data

def run_comparison():
    print(f"\n=========================================================")
    print(f"       LLM SCOUT & VERIFICATION TEST RUNNER")
    print(f"       Target: '{TEST_SCHOLARSHIP_NAME}'")
    print(f"=========================================================\n")
    
    logger.info("Connecting to Google Sheets...")
    conn = GoogleSheetsConnector()
    conn.connect()
    
    # Read spreadsheet data rows
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
    data_rows = row_data[1:] # Skip header
    
    matched_row = None
    for idx, r in enumerate(data_rows, start=2):
        cells = r.get("values", [])
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
        if name.strip().lower() == TEST_SCHOLARSHIP_NAME.strip().lower():
            info_link = get_cell_link("historical_info_link") or get_cell_text("historical_info_link")
            reg_link = get_cell_link("historical_reg_link") or get_cell_text("historical_reg_link")
            matched_row = {
                "row_idx": idx,
                "scholarship_name": name,
                "country_region": get_cell_text("country_region"),
                "historical_method": get_cell_text("historical_method") or "Online",
                "historical_info_link": info_link,
                "historical_reg_link": reg_link,
                "estimated_timeline": get_cell_text("estimated_timeline")
            }
            break
            
    if not matched_row:
        logger.error(f"Could not find scholarship '{TEST_SCHOLARSHIP_NAME}' in sheet!")
        return
        
    logger.info(f"Successfully matched sheet details: {matched_row}")
    
    processed_results = []
    model_name = os.getenv("OPENAI_MODEL_2", "zai-glm-4.7")
    
    quota_exceeded = False
    sch_name = TEST_SCHOLARSHIP_NAME
    country = matched_row.get("country_region", "").strip()
    today_str = time.strftime('%Y-%m-%d')  # e.g. '2026-06-05'

    search_query = f"{sch_name} Indonesia deadline {time.strftime('%Y')}".strip()
    search_results = search_scholarship_with_retry(search_query) or []
    
    # 2. Deep scraping & link extraction
    scraped_pages = []
    all_candidate_info = []
    all_candidate_reg = []
    fetched_urls = set()
    
    urls_to_scrape = []
    if matched_row["historical_info_link"]:
        urls_to_scrape.append((matched_row["historical_info_link"], "Historical Info Link"))
    if matched_row["historical_reg_link"]:
        urls_to_scrape.append((matched_row["historical_reg_link"], "Historical Registration Link"))
    for res in search_results[:3]:
        urls_to_scrape.append((res["url"], "Search Result"))
        
    BINARY_EXTENSIONS = (".pdf", ".xlsx", ".xls", ".docx", ".doc", ".ppt", ".pptx", ".zip", ".rar")
    
    for url, url_type in urls_to_scrape:
        if not url or url in fetched_urls:
            continue
        
        # If URL is a binary file (PDF, Excel, etc.), skip it and try the root domain instead
        url_path = urllib.parse.urlparse(url).path.lower()
        if any(url_path.endswith(ext) for ext in BINARY_EXTENSIONS):
            parsed = urllib.parse.urlparse(url)
            root_url = f"{parsed.scheme}://{parsed.netloc}/"
            logger.info(f"Binary URL detected ({url_path[-4:]}), substituting root domain: {root_url}")
            if root_url not in fetched_urls:
                urls_to_scrape.append((root_url, f"{url_type} (root domain)"))
            continue
        
        fetched_urls.add(url)
        
        html = fetch_webpage_content(url)
        if not html:
            continue
            
        cleaned_text = clean_html(html)
        truncated_text = cleaned_text[:5000]
        
        links = extract_hyperlinks(html, url)
        candidates = filter_candidate_links(links, sch_name)
        all_candidate_info.extend(candidates["info"])
        all_candidate_reg.extend(candidates["reg"])
        
        scraped_pages.append({
            "url": url,
            "type": url_type,
            "content": truncated_text
        })
        
        branching_keywords = [
            "research", "rs", "graduate", "indonesia", "tahap",
            "announcement", "guideline", "scholar", "2026", "2025",
            "deadline", "apply", "apply-now", "schedule", "timeline",
            "gks", "kgsp", "niied"
        ]
        branching_count = 0
        for sub_url in candidates["info"]:
            if branching_count >= 2:
                break
            if sub_url == url or sub_url in fetched_urls:
                continue
            url_lower = sub_url.lower()
            # Check for name-specific keywords and sub-page indicators
            if any(kw in url_lower for kw in branching_keywords):
                fetched_urls.add(sub_url)
                logger.info(f"Following branching sub-link ({branching_count + 1}): {sub_url}")
                sub_html = fetch_webpage_content(sub_url)
                if sub_html:
                    sub_text = clean_html(sub_html)[:5000]
                    sub_links = extract_hyperlinks(sub_html, sub_url)
                    sub_candidates = filter_candidate_links(sub_links, sch_name)
                    all_candidate_info.extend(sub_candidates["info"])
                    all_candidate_reg.extend(sub_candidates["reg"])
                    
                    scraped_pages.append({
                        "url": sub_url,
                        "type": "Branching Sub-link",
                        "content": sub_text
                    })
                    branching_count += 1
                
    # Construct raw context string
    context_str = ""
    for page in scraped_pages:
        context_str += f"\n--- PAGE URL: {page['url']} ({page['type']}) ---\n"
        context_str += page["content"]
        context_str += f"\n{'-'*60}\n"
        
    if not context_str:
        context_str = "No web context found."
    else:
        # Cap total context to avoid LLM timeout on very large inputs
        if len(context_str) > 12000:
            context_str = context_str[:12000] + "\n... [context truncated for API limits]"
        
    unique_candidate_info = list(set(all_candidate_info))[:15]
    unique_candidate_reg = list(set(all_candidate_reg))[:15]
    
    # 3. Invoke Cerebras Llama API
    try:
        verified_data = verify_scholarship_llama(
            scholarship_name=matched_row["scholarship_name"],
            historical_method=matched_row["historical_method"],
            historical_info_link=matched_row["historical_info_link"],
            historical_reg_link=matched_row["historical_reg_link"],
            estimated_timeline=matched_row["estimated_timeline"],
            scraped_web_text=context_str,
            candidate_info_links=unique_candidate_info,
            candidate_reg_links=unique_candidate_reg,
            model_name=model_name
        )
        
        print(f"Verified Status: {verified_data.get('status')}")
        print(f"Verified Start Date: {verified_data.get('application_start_date')}")
        print(f"Verified Deadline: {verified_data.get('application_deadline')}")
        print(f"Verified Info URL: {verified_data.get('official_source_url')}")
        print(f"Verified Reg URL: {verified_data.get('official_registration_url')}")
        print(f"Remarks: {verified_data.get('remarks')}")
        
        processed_results.append({
            "row_idx": matched_row["row_idx"],
            "verified_data": verified_data
        })
        
    except CerebrasQuotaExceededException as qe:
        logger.critical(f"Cerebras API Quota/Rate Limit Exceeded: {str(qe)}")
        quota_exceeded = True
    except Exception as e:
        logger.error(f"Verification failed for '{sch_name}': {str(e)}", exc_info=True)
        # Add error result so we still report it
        processed_results.append({
            "row_idx": matched_row["row_idx"],
            "verified_data": {
                "scholarship_name": sch_name,
                "status": "CLOSED",
                "application_start_date": None,
                "application_deadline": None,
                "official_source_url": matched_row["historical_info_link"],
                "official_registration_url": matched_row["historical_reg_link"],
                "url_verification_fallback_used": True,
                "confidence_score": 0.0,
                "processing_method_detected": matched_row["historical_method"],
                "remarks": f"System error: {str(e)}"
            }
        })
        
    # Write results so far and send email report
    if processed_results:
        logger.info(f"Initiating spreadsheet update for {len(processed_results)} rows...")
        try:
            conn.batch_write_results(processed_results)
            logger.info("Google Sheet successfully updated!")
        except Exception as sheet_err:
            logger.error(f"Failed to update sheet: {str(sheet_err)}")
            
        logger.info("Sending report email...")
        send_scout_report_email(processed_results, quota_exceeded)
    else:
        logger.warning("No results to save or email.")
        
    print(f"\n=========================================================")
    print("       RUN COMPLETED SUCCESSFULLY!")
    print(f"=========================================================\n")

def send_scout_report_email(processed_results: List[Dict[str, Any]], quota_exceeded: bool = False) -> bool:
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    
    host = os.getenv("SMTP_HOST")
    port = os.getenv("SMTP_PORT")
    user = os.getenv("SMTP_USER")
    passwd = os.getenv("SMTP_PASS")
    sender = os.getenv("SENDER_EMAIL")
    receiver = os.getenv("RECEIVER_EMAIL")
    model_name = os.getenv("OPENAI_MODEL_2", "zai-glm-4.7")
    
    if not all([host, port, user, passwd, sender, receiver]):
        logger.warning("SMTP configuration is incomplete. Cannot send report email.")
        return False
        
    try:
        port_int = int(port)
    except ValueError:
        logger.error(f"Invalid SMTP_PORT: '{port}'")
        return False
        
    # Build HTML table rows
    rows = []
    for r in processed_results:
        data = r["verified_data"]
        
        status = data.get("status", "CLOSED")
        status_color = "#2ecc71" if status == "OPEN" else ("#f39c12" if status == "NOT_YET_OPENED" else "#e74c3c")
        
        rows.append(f"""
        <tr style="border-bottom: 1px solid #dddddd;">
            <td style="padding: 12px 15px; font-weight: bold; color: #333333;">{data.get("scholarship_name")}</td>
            <td style="padding: 12px 15px; font-weight: bold; color: {status_color};">{status}</td>
            <td style="padding: 12px 15px; color: #555555;">{data.get("application_start_date") or "N/A"}</td>
            <td style="padding: 12px 15px; color: #555555;">{data.get("application_deadline") or "N/A"}</td>
            <td style="padding: 12px 15px; font-size: 12px;">
                <a href="{data.get("official_source_url")}" style="color: #3498db; text-decoration: none;">Info Link</a>
            </td>
            <td style="padding: 12px 15px; font-size: 12px;">
                <a href="{data.get("official_registration_url")}" style="color: #2ecc71; text-decoration: none; font-weight: bold;">Reg Link</a>
            </td>
            <td style="padding: 12px 15px; color: #7f8c8d; font-size: 13px;">{data.get("remarks")}</td>
        </tr>
        """)
        
    formatted_rows = "\n".join(rows)
    
    alert_banner = ""
    if quota_exceeded:
        alert_banner = f"""
        <div style="background-color: #fce4e4; border: 1px solid #f5c6cb; color: #721c24; padding: 15px; border-radius: 4px; margin-bottom: 20px; font-family: Arial, sans-serif;">
            <strong>⚠️ RUN INTERRUPTED:</strong> The Cerebras API daily quota limit was hit. The scouting process aborted early and has saved/reported the results gathered up to that point.
        </div>
        """
        
    html_content = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Scholarship Verification Run Report</title>
</head>
<body style="background-color: #f9f9f9; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; margin: 0; padding: 20px; color: #333333;">
    <div style="max-width: 900px; margin: 0 auto; background-color: #ffffff; padding: 30px; border-radius: 8px; box-shadow: 0 4px 10px rgba(0,0,0,0.05); border-top: 5px solid #3498db;">
        
        {alert_banner}
        
        <h2 style="font-size: 22px; font-weight: 600; margin-top: 0; color: #2c3e50;">🎓 Scholarship Verification Report</h2>
        <p style="font-size: 14px; color: #7f8c8d; margin-bottom: 25px;">
            This report contains the latest verification details compiled by the automated Scout pipeline running model <strong>{model_name}</strong>.
        </p>
        
        <table style="border-collapse: collapse; width: 100%; text-align: left; font-size: 14px;">
            <thead>
                <tr style="background-color: #f8f9fa; border-bottom: 2px solid #3498db; color: #2c3e50;">
                    <th style="padding: 12px 15px;">Scholarship Name</th>
                    <th style="padding: 12px 15px;">Status</th>
                    <th style="padding: 12px 15px;">Start Date</th>
                    <th style="padding: 12px 15px;">Deadline</th>
                    <th style="padding: 12px 15px;">Info Link</th>
                    <th style="padding: 12px 15px;">Reg. Link</th>
                    <th style="padding: 12px 15px;">Remarks</th>
                </tr>
            </thead>
            <tbody>
                {formatted_rows}
            </tbody>
        </table>
        
        <div style="margin-top: 30px; padding-top: 20px; border-top: 1px solid #eeeeee; font-size: 12px; color: #95a5a6; text-align: center;">
            Academic Scout Automated System • Local time: {time.strftime('%Y-%m-%d %H:%M:%S')}
        </div>
    </div>
</body>
</html>
"""

    try:
        msg = MIMEMultipart("alternative")
        subject = f"🎓 Scholarship Verification Report - {len(processed_results)} Processed"
        if quota_exceeded:
            subject = "⚠️ " + subject + " (API Limit Hit)"
        msg["Subject"] = subject
        msg["From"] = f"Academic Scout Agent <{sender}>"
        msg["To"] = receiver
        msg.attach(MIMEText(html_content, "html"))
        
        logger.info(f"Connecting to SMTP server {host}:{port_int} to send report email...")
        if port_int == 465:
            server = smtplib.SMTP_SSL(host, port_int, timeout=15)
        else:
            server = smtplib.SMTP(host, port_int, timeout=15)
            server.starttls()
            
        server.login(user, passwd)
        server.sendmail(sender, receiver, msg.as_string())
        server.quit()
        logger.info("Report email sent successfully!")
        return True
    except Exception as e:
        logger.error(f"Failed to send report email: {e}", exc_info=True)
        return False

if __name__ == "__main__":
    run_comparison()
