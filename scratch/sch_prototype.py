import sys
import os
sys.path.insert(0, 'c:/Work/schreminder')

import time
import json
import re
import random
import datetime
import logging
import requests
import urllib.parse
from datetime import date, timedelta
from typing import Dict, Any, Optional, List, Tuple
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
TEST_SCHOLARSHIP_NAME = "DAAD STEM Discipline"

class CerebrasQuotaExceededException(Exception):
    pass


# Scholarship name prefixes (inside the opening parenthesis) that should be
# SKIPPED entirely -- these are internally-funded scholarships with no public
# application portal that can be scraped or verified by the scout engine.
SKIPPED_PREFIXES = {
    "uni-funded",   # University-funded internal grants
}


# ── A1: RESULT PERSISTENCE ───────────────────────────────────────────────────
def save_result_json(scholarship_name: str, model_used: str,
                     search_status: str, processed_results: list) -> None:
    """Saves the run result to scratch/result/ as a timestamped JSON file."""
    results_dir = os.path.join("c:/Work/schreminder/scratch/result")
    os.makedirs(results_dir, exist_ok=True)

    slug = re.sub(r'[^a-z0-9_]', '_', scholarship_name.lower().strip())[:40]
    ts   = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    filename = f"{ts}_{slug}.json"
    filepath = os.path.join(results_dir, filename)

    payload = {
        "run_ts":           datetime.datetime.now().isoformat(),
        "scholarship_name": scholarship_name,
        "model_used":       model_used,
        "search_status":    search_status,
        "results":          processed_results,
    }
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    logger.info(f"Result saved → {filepath}")


# ── B3a: UNI-TO-UNI NAME PARSER ───────────────────────────────────────────────
import re as _re

# Parenthesised prefixes that are category tags, NOT scholarship body names.
# Entries starting with these are treated as centralized scholarships.
_UNI_TO_UNI_SKIP_PREFIXES = {
    "uni-funded",   # e.g. (Uni-Funded) Leiden University Excellence Scholarships
}

def _find_balanced_close(s: str) -> int:
    """
    Return the index of the ')' that BALANCES the '(' at s[0].
    Returns -1 if the string doesn't start with '(' or is unbalanced.

    Examples
    --------
    '(MEXT Scholarship) foo'
        → 17  (first ')')

    '(Intl Grad Program (IGP) Special MEXT Scholarship) Hokkaido'
        → 50  (last ')', skipping the nested one after 'IGP')
    """
    if not s or s[0] != '(':
        return -1
    depth = 0
    for i, ch in enumerate(s):
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
            if depth == 0:
                return i
    return -1  # unbalanced

def parse_scholarship_name(name: str) -> dict:
    """
    Detects uni-to-uni naming pattern: (Scholarship Body) University Name

    Uses balanced-parenthesis tracking instead of a greedy/non-greedy regex so
    that nested parens in either part are handled correctly:

      '(MEXT Scholarship) Intl Grad Program (IGP) Special - Hokkaido'
          scholarship = 'MEXT Scholarship'
          university  = 'Intl Grad Program (IGP) Special - Hokkaido'

      '(Intl Grad Program (IGP) Special MEXT Scholarship) Hokkaido Univ'
          scholarship = 'Intl Grad Program (IGP) Special MEXT Scholarship'
          university  = 'Hokkaido Univ'

    Returns:
      { "type": "centralized", "display_name": name }        → normal scholarship
      { "type": "uni_to_uni",  "scholarship": "...",
        "university": "...",   "display_name": name }         → uni-specific entry
    """
    s = name.strip()
    if not s.startswith('('):
        return {"type": "centralized", "display_name": name}

    close_idx = _find_balanced_close(s)
    if close_idx == -1:
        return {"type": "centralized", "display_name": name}

    scholarship = s[1:close_idx].strip()          # text between outer ( and )
    rest        = s[close_idx + 1:].strip()       # text after the outer )

    if not rest or not scholarship:
        return {"type": "centralized", "display_name": name}

    if scholarship.lower() in _UNI_TO_UNI_SKIP_PREFIXES:
        return {"type": "centralized", "display_name": name}

    return {
        "type":         "uni_to_uni",
        "scholarship":  scholarship,
        "university":   rest,
        "display_name": name,
    }


# ── B2: TRANSLATION HELPER ──────────────────────────────────────────────────
def translate_text(text: str, source_lang: str = "auto", target_lang: str = "en") -> str:
    """Translates a text snippet via MyMemory API (free, no key needed, ~500 chars/call)."""
    try:
        resp = requests.get(
            "https://api.mymemory.translated.net/get",
            params={"q": text, "langpair": f"{source_lang}|{target_lang}"},
            timeout=10
        )
        if resp.ok:
            return resp.json().get("responseData", {}).get("translatedText", text)
    except Exception as e:
        logger.warning(f"Translation failed ({e}). Using original text.")
    return text


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
    Fallback search using Yahoo.
    Retries up to 3 times on 5xx AND on network/connection errors with UA rotation.
    """
    logger.info(f"🌐 Falling back to Yahoo Search scraping for query: '{query}'")
    search_url = f"https://search.yahoo.com/search?p={urllib.parse.quote(query)}"

    ua_pool = [
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15',
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0',
    ]

    for attempt in range(1, 4):
        headers = {
            'User-Agent': ua_pool[(attempt - 1) % len(ua_pool)],
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
        }
        try:
            response = requests.get(search_url, headers=headers, timeout=20, allow_redirects=True)
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
                    results.append({"title": title, "url": href, "snippet": snippet})
            if not results:
                logger.warning("Yahoo returned 0 parseable results.")
                return None
            logger.info(f"Yahoo: harvested {len(results)} results successfully.")
            return results
        except (requests.exceptions.SSLError,
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as net_err:
            # Network errors are transient — retry with next UA
            logger.warning(f"Yahoo network error (attempt {attempt}/3): {type(net_err).__name__}: {net_err}")
            if attempt < 3:
                sleep_s = attempt * 5  # 5s, 10s
                logger.info(f"Retrying Yahoo in {sleep_s}s with next UA...")
                time.sleep(sleep_s)
                continue
            logger.error("Yahoo network error persisted after 3 attempts.")
            return None
        except Exception as e:
            logger.error(f"Yahoo fallback unexpected error: {str(e)}")
            return None
    return None

# ── A3: Internal DDG + Yahoo helpers (called by the main retry loop) ─────────
def _try_duckduckgo(query: str, max_results: int = 5) -> Optional[List[Dict[str, str]]]:
    """Attempts a single DuckDuckGo search. Returns results list or None on failure.
    Raises network exceptions so the caller can classify them."""
    url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
    }
    response = requests.get(url, headers=headers, timeout=15)  # may raise — let caller catch

    if not response.ok:
        logger.warning(f"DuckDuckGo HTTP {response.status_code}")
        return None  # blocked / bad status
    if any(kw in response.text for kw in ["ddg-captcha", "ddg-lms"]):
        logger.warning("DuckDuckGo returned captcha/rate-limit page.")
        return None

    soup = BeautifulSoup(response.text, 'html.parser')
    results = []
    for div in soup.find_all('div', class_='result')[:max_results]:
        title_a = div.find('a', class_='result__url') or div.find('a', class_='result__title')
        if not title_a:
            continue
        title = title_a.get_text(strip=True)
        href  = title_a.get('href', '')
        if 'uddg=' in href:
            try:
                parsed_href = urllib.parse.urlparse(href)
                qs = urllib.parse.parse_qs(parsed_href.query)
                if 'uddg' in qs:
                    href = qs['uddg'][0]
            except Exception:
                pass
        elif href.startswith('//'):
            href = 'https:' + href
        snippet_div = div.find(class_='result__snippet')
        snippet = snippet_div.get_text(strip=True) if snippet_div else ""
        if title and href:
            results.append({"title": title, "url": href, "snippet": snippet})

    if not results:
        logger.warning("DuckDuckGo returned 0 parseable results.")
        return None
    logger.info(f"DuckDuckGo: harvested {len(results)} results.")
    return results


def _try_yahoo(query: str, max_results: int = 5) -> Optional[List[Dict[str, str]]]:
    """Yahoo search fallback — delegates to perform_bing_fallback_raw."""
    return perform_bing_fallback_raw(query, max_results)


def _try_bing(query: str, max_results: int = 5) -> Optional[List[Dict[str, str]]]:
    """
    Bing HTML search — third engine fallback.
    On completely different infrastructure from DDG/Yahoo.
    Retries up to 3 times on network errors with UA rotation.
    """
    logger.info(f"Trying Bing search for query: '{query}'")
    search_url = f"https://www.bing.com/search?q={urllib.parse.quote(query)}&setlang=en"

    ua_pool = [
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0',
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0',
    ]

    for attempt in range(1, 4):
        headers = {
            'User-Agent': ua_pool[(attempt - 1) % len(ua_pool)],
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
        }
        try:
            response = requests.get(search_url, headers=headers, timeout=20, allow_redirects=True)
            if response.status_code == 429:
                logger.warning(f"Bing rate-limited (attempt {attempt}/3).")
                return None
            if response.status_code >= 500:
                logger.warning(f"Bing HTTP {response.status_code} (attempt {attempt}/3) - retrying in 5s...")
                if attempt < 3:
                    time.sleep(5)
                    continue
                return None
            if not response.ok:
                logger.warning(f"Bing HTTP {response.status_code}")
                return None

            soup = BeautifulSoup(response.text, 'html.parser')
            results = []
            for li in soup.find_all('li', class_='b_algo')[:max_results]:
                h2 = li.find('h2')
                if not h2:
                    continue
                a = h2.find('a', href=True)
                if not a:
                    continue
                title = a.get_text(strip=True)
                href  = a.get('href', '')
                snippet_p = li.find('p') or li.find('div', class_='b_caption')
                snippet = snippet_p.get_text(strip=True)[:300] if snippet_p else ''
                if title and href and href.startswith('http'):
                    results.append({'title': title, 'url': href, 'snippet': snippet})

            if not results:
                logger.warning("Bing returned 0 parseable results.")
                return None
            logger.info(f"Bing: harvested {len(results)} results successfully.")
            return results

        except (requests.exceptions.SSLError,
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as net_err:
            logger.warning(f"Bing network error (attempt {attempt}/3): {type(net_err).__name__}: {net_err}")
            if attempt < 3:
                sleep_s = attempt * 5
                logger.info(f"Retrying Bing in {sleep_s}s with next UA...")
                time.sleep(sleep_s)
                continue
            logger.error("Bing network error persisted after 3 attempts.")
            return None
        except Exception as e:
            logger.error(f"Bing unexpected error: {str(e)}")
            return None
    return None


def _try_searx(query: str, max_results: int = 5) -> Optional[List[Dict[str, str]]]:
    """
    SearXNG public instance — fourth engine, last-resort fallback.
    Aggregates Google/Bing/DDG via its own servers, so it works even when
    individual engines block direct access from this machine.
    Tries multiple public instances in order.
    """
    logger.info(f"Trying SearXNG search for query: '{query}'")

    searx_instances = [
        'https://searx.be',
        'https://search.sapti.me',
        'https://searxng.site',
    ]
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Accept': 'application/json',
    }

    for instance in searx_instances:
        try:
            params = {
                'q': query,
                'format': 'json',
                'engines': 'google,bing,duckduckgo',
                'language': 'en-US',
            }
            response = requests.get(f"{instance}/search", headers=headers, params=params, timeout=20)
            if not response.ok:
                logger.warning(f"SearXNG {instance} HTTP {response.status_code} - trying next...")
                continue
            data = response.json()
            results = []
            for r in data.get('results', [])[:max_results]:
                url     = r.get('url', '')
                title   = r.get('title', '')
                snippet = r.get('content', '')[:300]
                if url and title and url.startswith('http'):
                    results.append({'title': title, 'url': url, 'snippet': snippet})
            if not results:
                logger.warning(f"SearXNG {instance} returned 0 usable results - trying next...")
                continue
            logger.info(f"SearXNG ({instance}): harvested {len(results)} results.")
            return results
        except (requests.exceptions.SSLError,
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as net_err:
            logger.warning(f"SearXNG {instance} network error: {type(net_err).__name__} - trying next...")
            continue
        except Exception as e:
            logger.warning(f"SearXNG {instance} error: {e} - trying next...")
            continue

    logger.error("All SearXNG instances failed.")
    return None


def search_scholarship_with_retry(query: str, max_results: int = 5) -> Tuple[Optional[List[Dict[str, str]]], str]:
    """
    A3: Four-engine, three-round search with aggressive retry.

    Each round tries: DDG -> Yahoo -> Bing -> SearXNG.
    The first engine to return >= 1 result wins immediately.

    Round schedule:
      Round 1: all four engines (immediate)
      Round 2: all four engines (after 30s sleep)
      Round 3: all four engines (after 60s sleep)

    Returns (results, search_status) where search_status is one of:
      'SUCCESS'          - >= 1 result retrieved from any engine
      'NETWORK_FAILURE'  - connection/SSL error across all engines all rounds
      'BLOCKED'          - captcha/rate-limit across all engines all rounds
      'NO_RESULTS'       - all engines responded but returned 0 parseable items
    """
    last_error_type = "NETWORK_FAILURE"
    sleep_schedule  = [30, 60]  # sleep before round 2, round 3

    for round_num in range(1, 4):  # Round 1, Round 2, Round 3
        logger.info(f"Search round {round_num}/3 for: '{query}'")

        # ---- Engine 1: DuckDuckGo ----
        try:
            result = _try_duckduckgo(query, max_results)
            if result:
                return (result, "SUCCESS")
            last_error_type = "BLOCKED"
        except (requests.exceptions.SSLError,
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as e:
            logger.warning(f"DDG network error (round {round_num}): {type(e).__name__}: {e}")
            last_error_type = "NETWORK_FAILURE"
        except Exception as e:
            logger.warning(f"DDG unexpected error (round {round_num}): {e}")
            last_error_type = "NETWORK_FAILURE"

        # ---- Engine 2: Yahoo ----
        result = _try_yahoo(query, max_results)
        if result:
            return (result, "SUCCESS")

        # ---- Engine 3: Bing ----
        result = _try_bing(query, max_results)
        if result:
            return (result, "SUCCESS")

        # ---- Engine 4: SearXNG ----
        result = _try_searx(query, max_results)
        if result:
            return (result, "SUCCESS")

        # ---- All four engines failed this round ----
        if round_num < 3:
            sleep_s = sleep_schedule[round_num - 1] + random.uniform(-5, 5)
            logger.info(
                f"All 4 search engines failed (round {round_num}/3). "
                f"Sleeping {sleep_s:.0f}s then retrying (round {round_num + 1}/3)..."
            )
            time.sleep(sleep_s)

    logger.error(f"All search attempts failed after 3 rounds x 4 engines. Final status: {last_error_type}")
    return (None, last_error_type)

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

# Official TLDs that universally serve HTTPS — proactively upgrade http:// links
# to avoid ConnectTimeout on port 80 (common when a page has an old http:// href).
_HTTPS_ONLY_TLDS = (
    ".ac.jp", ".go.jp", ".go.kr", ".go.id", ".go.th", ".go.au",
    ".ac.kr", ".ac.id", ".edu.au", ".gov", ".edu",
)

def _upgrade_to_https(url: str) -> str:
    """Rewrite http:// to https:// for domains known to require HTTPS."""
    if url.startswith("http://"):
        domain = urllib.parse.urlparse(url).netloc.lower()
        if any(domain.endswith(tld) for tld in _HTTPS_ONLY_TLDS):
            upgraded = "https://" + url[7:]
            logger.debug(f"HTTP→HTTPS upgrade: {url} → {upgraded}")
            return upgraded
    return url

def fetch_webpage_content(url: str, retries: int = 2, retry_delay: int = 180) -> Optional[str]:
    """
    Fetches URL HTML content.
    - HTTP 429/403 or captcha blocks: retries with 3-min sleep (genuine rate limits).
    - SSL/connection errors: skips immediately without sleep (server refused us).
    - http:// on official TLDs: auto-upgraded to https:// to avoid port-80 timeout.
    """
    if not url or not (url.startswith("http://") or url.startswith("https://")):
        return None
    # Proactively upgrade http:// to https:// for official academic/govt domains
    url = _upgrade_to_https(url)
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
    }
    for attempt in range(1, retries + 1):
        try:
            logger.info(f"Fetching URL (Attempt {attempt}/{retries}): {url}")
            response = requests.get(url, headers=headers, timeout=15)
            
            # 403 = WAF/bot-detection block. Before giving up, rotate through realistic
            # browser User-Agent strings — government sites often block only the default UA.
            if response.status_code == 403:
                stealth_ua_pool = [
                    # Chrome on Windows (most common)
                    (
                        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                        '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
                        'https://www.google.com/'
                    ),
                    # Safari on macOS
                    (
                        'Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 '
                        '(KHTML, like Gecko) Version/17.4.1 Safari/605.1.15',
                        'https://www.google.com/'
                    ),
                    # Firefox on Windows
                    (
                        'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) '
                        'Gecko/20100101 Firefox/125.0',
                        'https://www.google.co.id/'
                    ),
                    # Edge on Windows
                    (
                        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                        '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0',
                        'https://www.bing.com/'
                    ),
                ]
                logger.warning(f"403 on {url} — rotating User-Agent to bypass bot filter...")
                for ua_str, referer in stealth_ua_pool:
                    try:
                        stealth_headers = {
                            'User-Agent': ua_str,
                            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
                            'Accept-Language': 'en-US,en;q=0.9,id;q=0.8',
                            'Accept-Encoding': 'gzip, deflate, br',
                            'Referer': referer,
                            'Sec-Fetch-Dest': 'document',
                            'Sec-Fetch-Mode': 'navigate',
                            'Sec-Fetch-Site': 'cross-site',
                            'Cache-Control': 'no-cache',
                            'Connection': 'keep-alive',
                            'Upgrade-Insecure-Requests': '1',
                        }
                        r2 = requests.get(url, headers=stealth_headers, timeout=15)
                        if r2.ok:
                            logger.info(f"UA rotation succeeded for {url} (status {r2.status_code})")
                            return r2.text
                        logger.warning(f"UA rotation attempt failed: status {r2.status_code}")
                    except Exception as ua_err:
                        logger.warning(f"UA rotation attempt error: {ua_err}")
                logger.warning(f"403 Forbidden on {url} — all UA rotation attempts failed. Skipping.")
                return None

            is_blocked = False
            if response.status_code == 429:
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
                # If the URL is http:// and it timed out/refused, try https:// once
                # before giving up — many servers no longer listen on port 80.
                if url.startswith("http://") and "ConnectTimeout" in err_type:
                    https_url = "https://" + url[7:]
                    logger.info(f"ConnectTimeout on http:// — retrying with https://: {https_url}")
                    return fetch_webpage_content(https_url, retries=1, retry_delay=0)
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

# Official government / embassy / academic domains whose news/announcement
# sub-pages are still authoritative sources — never block these regardless of path.
OFFICIAL_DOMAINS = [
    ".go.id", ".go.jp", ".go.kr", ".go.th", ".go.au", ".gov", ".gov.au",
    ".ac.id", ".ac.jp", ".ac.kr", ".edu", ".edu.au",
    "emb-japan.go.jp", "mofa.go.kr", "niied.go.kr", "koica.go.kr",
    "scholarshipdb.net", "daad.de", "chevening.org", "britishcouncil.org"
]

def is_official_domain(url: str) -> bool:
    """Returns True if the URL belongs to a trusted government, embassy, or academic domain."""
    url_lower = url.lower()
    return any(domain in url_lower for domain in OFFICIAL_DOMAINS)

# News/media domains that should never be used as official scholarship sources.
# Links from these domains are filtered OUT of candidate_info to prevent the LLM
# from citing a news article instead of the real official site.
# Note: official government domains (e.g. kemenag.go.id/nasional/) are NOT
# blocked even if they publish news-style posts — those are official announcements.
NEWS_MEDIA_DOMAINS = [
    "kompas.com", "detik.com", "tribunnews.com", "liputan6.com", "okezone.com",
    "sindonews.com", "cnnindonesia.com", "tempo.co", "bisnis.com", "kumparan.com",
    "merdeka.com", "suara.com", "republika.co.id", "antara.co.id", "jpnn.com",
    "jawapos.com", "inews.id", "idntimes.com", "viva.co.id", "beritasatu.com",
    "thejakartapost.com", "medcom.id", "metrotvnews.com", "cnbcindonesia.com",
    "news.google.com", "yahoo.com/news", "bing.com/news"
]

def is_news_domain(url: str) -> bool:
    """Returns True if the URL belongs to a known third-party news/media outlet.
    Official government/academic domains are never classified as news even if
    they publish news-style announcement posts."""
    if is_official_domain(url):
        return False  # Never block official domains
    url_lower = url.lower()
    return any(domain in url_lower for domain in NEWS_MEDIA_DOMAINS)

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
            
        # Never include third-party news/media sites as official info sources.
        # Official government / academic domains ARE allowed even if news-like.
        if not is_news_domain(l["url"]):
            if any(kw in url_lower or kw in text_lower for kw in keywords_info):
                candidate_info.append(l["url"])
            
    return {
        "info": list(set(candidate_info))[:10],
        "reg": list(set(candidate_reg))[:10]
    }

def verify_scholarship_llama(
    scholarship_name: str,
    scraped_web_text: str,
    candidate_info_links: List[str],
    candidate_reg_links: List[str],
    model_name: Optional[str] = None,
    uni_context_note: str = "",          # B3b: injected for uni-to-uni entries
) -> Dict[str, Any]:
    """
    Calls the OpenAI-compatible endpoint (Cerebras/Llama) using requests.
    NOTE: Only the scholarship name and independently scraped web content are
    passed here. No spreadsheet historical data is provided — the LLM must
    discover all links, dates, and status from the web context alone.
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
5. "official_source_url": The primary official info URL (government/embassy/university/foundation page). Must NOT be a third-party news site.
6. "official_registration_url": The verified submission/registration portal URL (must be different from official_source_url), or null.
7. "supplementary_source_url": If the PRIMARY source has no current cycle dates but you found an official government/embassy/ministry announcement page (same official domain, e.g. /nasional/, /news/, /berita/) with more up-to-date or extended deadline info — output that announcement URL here. Only use this for truly official domain posts, NOT for third-party media. Output null if not applicable.
8. "url_verification_fallback_used": true (boolean) if no web content was successfully scraped and the output relies on LLM training knowledge alone; false (boolean) if scraped page content was used.
9. "confidence_score": Float between 0.0 to 1.0 reflecting source reliability based on the text context
10. "processing_method_detected": Detect if registration requires 'Online', 'Offline/Mail-in', 'Hybrid', or 'Register First, Upload Later' (string)
11. "remarks": A concise summary. If dates come from two different sources (primary page vs. announcement post), explain the discrepancy. Always cite which page URL the dates were extracted from.

CRITICAL RULES FOR LINKS — READ CAREFULLY AND FOLLOW STRICTLY:
- "official_source_url" (Info Link) MUST be a URL from the OFFICIAL scholarship body — a government ministry, embassy, university, or foundation. NEVER cite a news article, blog, or third-party media site (e.g. kompas.com, detik.com, tribunnews.com, liputan6.com, etc.) as the official_source_url. This is an absolute rule.
- "official_registration_url" (Registration Link) MUST be a DIFFERENT URL that directly allows the user to register, log in, or submit an online application (e.g., a portal login page, Google Form, or direct submission URL).
- ABSOLUTELY FORBIDDEN: Do NOT output the same URL for both "official_source_url" and "official_registration_url" unless the registration form is literally embedded directly on the info page itself. This is a hard rule with zero exceptions.
- If you cannot find a distinct registration link, output null for "official_registration_url". Do NOT copy the info URL and do NOT invent a URL.
- If you cannot find a verified official source URL, output null for "official_source_url". Do NOT use a news article URL as a fallback.
- Always prefer specific sub-page URLs (e.g., /apply, /register, /form, /pendaftaran, /burse-2026, /news/2026-application) over generic homepage URLs.
- For date extraction: Parse application dates precisely from the page text. Look for explicit open/close/deadline date ranges labelled as 'important date', 'deadline', 'application period', 'batas waktu', or similar. Output in YYYY-MM-DD format.

SYSTEM LOGIC & ANALYSIS STRATEGY:
1. PHASE 1: Scan all PAGE URL sections in the scraped web context. Identify which pages belong to official scholarship bodies (government, embassy, university, foundation) vs. news/blog sites.
2. PHASE 2: Find the most current application window dates from the scraped text. Look for explicit date ranges in sections labelled 'Important Date', 'Application Period', 'Deadline', or similar. Dates found on official pages take priority over dates found on news/blog pages.
3. PHASE 3: Select distinct, specific URLs for info and registration from the CANDIDATE LINKS lists — never duplicates.
4. URL INTEGRITY RULE (STRICT): Only output URLs that were literally present in:
   a) The scraped page text (shown in the PAGE URL sections below)
   b) The CANDIDATE INFO LINKS or CANDIDATE REGISTRATION LINKS lists
   NEVER construct, infer, guess, or modify URL paths (e.g. do not change /app/beranda to /register, or add /apply to a domain). If no valid URL is found, output null.
5. STATUS RULE (STRICT): Compare the application deadline you find against TODAY'S DATE (provided in the user prompt).
   - If today's date is BEFORE the application start date → status = 'NOT_YET_OPENED'
   - If today's date is WITHIN start and end date → status = 'OPEN'
   - If today's date is AFTER the application deadline → status = 'CLOSED'
   - If no explicit deadline is found in the scraped text → ASSUME status = 'CLOSED' (conservative). Do NOT output 'OPEN' without an explicit future deadline date. This is a hard rule.
   - NEVER output 'OPEN' if today's date is after the deadline. This is a hard rule.
6. DATE PRIORITY RULE: If the scraped page text contains explicit, precise dates (e.g. '2026-11-01 to 2027-01-31'), use those as ground truth. Only use context clues or estimates if NO explicit dates appear anywhere in the scraped content.

FIELD 12 — date_precision (REQUIRED):
12. "date_precision": Strictly one of: 'exact' | 'monthly' | 'quarterly' | 'unknown'
    - 'exact'     : Specific YYYY-MM-DD dates found in the source
    - 'monthly'   : Only month names or ranges stated (e.g. "December - January")
    - 'quarterly' : Quarter or semester mentioned (e.g. "Q1 2026", "Semester 1")
    - 'unknown'   : No date information found at all

DATE INFERENCE FOR MONTH-RANGE SOURCES:
If dates are stated as month name ranges only (e.g. "December - January" or "Jun - Jul"):
  - application_start_date = first day of start month  -> YYYY-MM-01
  - application_deadline   = last day of end month     -> use calendar (Jan=31, Apr=30, Feb=28, etc.)
  - Use the nearest upcoming cycle year. Example: if today is June 2026 and the source
    says "Dec - Jan", use Dec 2026 - Jan 2027.
  - Set date_precision = 'monthly'
For quarters: infer first/last day of the quarter. Set date_precision = 'quarterly'.

SOURCE AUTHORITY HIERARCHY — apply in strict descending priority:
1. Official embassy/consulate page for the applicant's home country
   (e.g. id.emb-japan.go.jp for Indonesian applicants to MEXT)
2. Issuing government ministry or national agency
   (e.g. niied.go.kr, mext.go.jp, hea.ie, bolashak.gov.kz)
3. Official scholarship foundation website
   (e.g. gksscholarship.com, chevening.org, cmkfoundation-globalscholarship.org)
4. Official university or institution admission page
   (for uni-to-uni scholarships: this becomes PRIORITY #1 — see UNI-TO-UNI note if present)
5. Study-abroad portals (e.g. studyinjapan.go.jp, studyinkorea.go.kr)
   -> USE ONLY if no higher-priority source exists in the scraped content
6. News articles, aggregator blogs, third-party media -> FORBIDDEN as official_source_url
When sources contradict each other, ALWAYS use the higher-priority source's dates.
When a lower-priority source is the only one available, note it in 'remarks'.
"""

    candidate_info_str = "\n".join([f"- {url}" for url in candidate_info_links]) if candidate_info_links else "None found."
    candidate_reg_str = "\n".join([f"- {url}" for url in candidate_reg_links]) if candidate_reg_links else "None found."

    user_prompt = f"""
{uni_context_note}
TODAY'S DATE: {time.strftime('%Y-%m-%d')} (use this to determine if the scholarship is currently OPEN, CLOSED, or NOT_YET_OPENED)

SCHOLARSHIP NAME TO VERIFY: {scholarship_name}

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

    # Retry loop: handles both Timeout and 429 queue_exceeded with exponential backoff.
    # Up to 4 total attempts. Wait schedule: 10s -> 20s -> 30s between consecutive attempts.
    # "queue_exceeded" 429 = transient server congestion, safe to retry.
    # Any other 429 (hard rate/quota limit) is raised immediately without retry.
    _LLM_RETRY_WAITS = (10, 20, 30)  # seconds to wait before attempt 2, 3, 4
    response = None
    for llm_attempt in range(1, 5):  # up to 4 attempts total
        try:
            response = requests.post(url, json=payload, headers=headers, timeout=90)
        except requests.exceptions.Timeout:
            if llm_attempt < 4:
                wait_s = _LLM_RETRY_WAITS[llm_attempt - 1]
                logger.warning(
                    f"Cerebras API timed out (attempt {llm_attempt}/4) — "
                    f"retrying in {wait_s}s..."
                )
                time.sleep(wait_s)
                continue
            else:
                raise  # all 4 attempts timed out

        # Got a response — check if it's a retryable queue_exceeded (transient congestion)
        if response.status_code == 429 and "queue_exceeded" in response.text:
            if llm_attempt < 4:
                wait_s = _LLM_RETRY_WAITS[llm_attempt - 1]
                logger.warning(
                    f"Cerebras server queue exceeded (attempt {llm_attempt}/4) — "
                    f"server is temporarily congested. Retrying in {wait_s}s..."
                )
                time.sleep(wait_s)
                continue
            else:
                raise CerebrasQuotaExceededException(
                    f"Cerebras API limit/quota hit: {response.status_code} - {response.text}"
                )

        break  # non-timeout, non-queue_exceeded response — proceed

    latency = time.time() - start_time
    
    # Catch API rate-limiting or quota limit hits.
    # IMPORTANT: Only inspect response.text for error keywords when the HTTP status
    # is already non-OK. A 200 OK response contains the model's JSON output (which
    # may legitimately contain words like "quota" from scholarship content, e.g.
    # "special-quota.php") and must NEVER be inspected for API error keywords.
    if response.status_code == 429:
        raise CerebrasQuotaExceededException(f"Cerebras API limit/quota hit: {response.status_code} - {response.text}")
    if not response.ok:
        err_text = response.text
        if "RESOURCE_EXHAUSTED" in err_text or "quota" in err_text.lower() or "limit exceeded" in err_text.lower():
            raise CerebrasQuotaExceededException(f"Cerebras API limit/quota hit: {response.status_code} - {err_text}")
        
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
    conn.connect(read_only=True)  # Prototype: never touch the sheet structure
    
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

        # Only read scholarship_name — no historical links or metadata used by the engine
        name = get_cell_text("scholarship_name")
        if name.strip().lower() == TEST_SCHOLARSHIP_NAME.strip().lower():
            matched_row = {
                "row_idx": idx,
                "scholarship_name": name,
            }
            break
            
    if not matched_row:
        logger.error(f"Could not find scholarship '{TEST_SCHOLARSHIP_NAME}' in sheet!")
        return
        
    logger.info(f"Matched scholarship in sheet: row={matched_row['row_idx']}, name='{matched_row['scholarship_name']}'")
    
    processed_results = []
    model_name = os.getenv("OPENAI_MODEL_2", "zai-glm-4.7")

    quota_exceeded = False
    sch_name = TEST_SCHOLARSHIP_NAME

    # ── A4: Read Col C (Status) and Col D (Verified) for bypass check ─────────
    # Re-read cells for the matched row so we can check bypass conditions.
    # We need to iterate again since get_cell_text is defined inside the loop above.
    bypass_cells = []
    for idx2, r2 in enumerate(data_rows, start=2):
        if idx2 == matched_row["row_idx"]:
            bypass_cells = r2.get("values", [])
            break

    def _get_bypass_text(field_key: str) -> str:
        col_idx = conn.col_map.get(field_key)
        if col_idx and col_idx <= len(bypass_cells):
            return bypass_cells[col_idx - 1].get("formattedValue", "").strip()
        return ""

    def _get_bypass_link(field_key: str) -> Optional[str]:
        col_idx = conn.col_map.get(field_key)
        if col_idx and col_idx <= len(bypass_cells):
            cell = bypass_cells[col_idx - 1]
            return cell.get("hyperlink") or cell.get("formattedValue") or None
        return None

    col_c_val = _get_bypass_text("active_status")   # Col C
    col_d_val = _get_bypass_text("verified")         # Col D

    if col_c_val.upper() == "T" and col_d_val.upper() == "F":
        logger.info(
            f"[BYPASS] '{sch_name}': Status=T, Verified=F → "
            f"emailing sheet data directly (no search/LLM)."
        )
        bypass_data = {
            "scholarship_name":           sch_name,
            "status":                     "VERIFIED (MANUAL)",
            "application_start_date":     None,
            "application_deadline":       _get_bypass_text("estimated_timeline"),    # Col G verbatim
            "official_source_url":        _get_bypass_link("historical_info_link"),  # Col I
            "official_registration_url":  _get_bypass_link("historical_reg_link"),   # Col J
            "processing_method_detected": _get_bypass_text("historical_method"),     # Col H
            "supplementary_source_url":   None,
            "url_verification_fallback_used": False,
            "confidence_score":           1.0,
            "remarks":                    _get_bypass_text("note"),                  # Col B verbatim
        }
        processed_results.append({
            "row_idx":       matched_row["row_idx"],
            "search_status": "BYPASS",
            "verified_data": bypass_data,
        })
        save_result_json(sch_name, model_name, "BYPASS", processed_results)
        send_scout_report_email(processed_results, quota_exceeded)
        return
    # ── End A4 bypass ─────────────────────────────────────────────────────────

    # ── SKIP: Internally-funded scholarships (e.g. (Uni-Funded) ...) ─────────
    # These have no public portal to scrape. Notify and exit cleanly.
    _sn_stripped = sch_name.strip()
    if _sn_stripped.startswith("("):
        _skip_m = re.match(r'^\(([^)]+)\)', _sn_stripped)
        if _skip_m and _skip_m.group(1).strip().lower() in SKIPPED_PREFIXES:
            logger.info(
                f"[SKIP] '{sch_name}': prefix '({_skip_m.group(1)})' is in SKIPPED_PREFIXES. "
                f"No public portal to scrape. Exiting without LLM call."
            )
            print(f"\n[SKIP] '{sch_name}' is a (Uni-Funded) scholarship -- skipping.")
            return

    # ── B2: Config-driven query + URL queue ────────────────────────────────────
    from scholarship_config import get_scholarship_config
    sch_cfg     = get_scholarship_config(sch_name)
    name_parsed = parse_scholarship_name(sch_name)   # B3a — use name_parsed to avoid collision with URL parsing

    if sch_cfg.get("preferred_query"):
        search_query = sch_cfg["preferred_query"]
        logger.info(f"[CONFIG] Using preferred query: {search_query}")
    elif name_parsed["type"] == "uni_to_uni":
        # Uni-to-uni programs often publish their page using the ADMISSION year
        # (e.g. "admission in October 2027") rather than the application deadline year.
        # Including both current year (deadline year) AND current+1 (admission year)
        # helps search engines surface the live program page instead of stale news
        # announcements about the previous application cycle.
        # "university recommendation" is the official MEXT track name for uni-to-uni
        # scholarships — university pages frequently use this phrase, making it a
        # strong signal to surface official program pages over generic news articles.
        _cur_year = datetime.datetime.now().year
        _adm_year = _cur_year + 1  # admission cohort is typically one year ahead
        search_query = (
            f"{name_parsed['university']} {name_parsed['scholarship']} "
            f"university recommendation application {_cur_year} OR {_adm_year} deadline"
        )
        logger.info(f"[UNI-TO-UNI] Auto query (deadline={_cur_year}, admission={_adm_year}): {search_query}")
    else:
        # Default: scholarship name + year only — no country hardcoded.
        search_query = f"{sch_name} important date deadline {time.strftime('%Y')}"
    logger.info(f"Search query: '{search_query}'")

    # Per-run domain allowlist: don't mutate the global OFFICIAL_DOMAINS
    run_official_domains = set(OFFICIAL_DOMAINS)
    if sch_cfg.get("preferred_domains"):
        run_official_domains.update(sch_cfg["preferred_domains"])

    # ── B4: LOCKED MODE — skip search engine, scrape only pre-configured URLs ──
    # When locked_urls is set in config, the entire search pipeline is bypassed.
    # URLs are scraped directly; {year} is substituted to the current calendar year.
    is_locked = bool(sch_cfg.get("locked_urls"))
    locked_source_note = ""

    if is_locked:
        cur_year = datetime.datetime.now().year
        locked_urls = [u.format(year=cur_year) for u in sch_cfg["locked_urls"]]
        urls_to_scrape = [(u, "Locked URL") for u in locked_urls]
        search_status = "LOCKED"
        locked_source_note = (
            "[LOCKED SOURCE] Search engine skipped. Scraped only: "
            + ", ".join(locked_urls)
        )
        logger.info(f"[LOCKED] Skipping search engine. Locked URLs: {locked_urls}")
    else:
        # A3: search now returns (results, search_status)
        urls_to_scrape = []  # initialize here — may be overridden by fallback or queue-builder below
        search_results, search_status = search_scholarship_with_retry(search_query)

        # A3: abort early with differentiated remark if search completely failed
        # Exception: if preferred_urls are configured, fall through to scrape them
        # even when the search engine is down (preferred_urls act as mini-locked mode).
        if not search_results:
            fallback_preferred = sch_cfg.get("preferred_urls", [])
            if fallback_preferred:
                # Search failed but we have known-good URLs — scrape those instead
                logger.warning(
                    f"Search failed ({search_status}) but {len(fallback_preferred)} preferred_url(s) "
                    f"configured — falling back to scraping preferred URLs only."
                )
                urls_to_scrape = [(u, "Config Preferred URL (search-failed fallback)") for u in fallback_preferred]
                # Override status so email doesn't show NET ERR for scholarships that got results
                search_status = "FALLBACK"
                # Skip the normal queue-building below
            else:
                remark_map = {
                    "NETWORK_FAILURE": (
                        "[NETWORK FAILURE] Both DuckDuckGo and Yahoo were unreachable "
                        "(SSL/connection error). No web context retrieved. "
                        "Retry manually in a few minutes."
                    ),
                    "BLOCKED": (
                        "[SEARCH BLOCKED] Search engines returned captcha/rate-limit. "
                        "Wait ~5 minutes and re-run."
                    ),
                    "NO_RESULTS": (
                        "[NO RESULTS] Search engines responded but returned 0 parseable "
                        "result links. The scholarship name may need a config override in "
                        "scholarship_config.py."
                    ),
                }
                remark = remark_map.get(search_status, "[UNKNOWN SEARCH FAILURE]")
                logger.warning(f"Search failed ({search_status}). Sending failure report.")
                processed_results.append({
                    "row_idx":       matched_row["row_idx"],
                    "search_status": search_status,
                    "verified_data": {
                        "scholarship_name":           sch_name,
                        "status":                     "UNKNOWN",
                        "application_start_date":     None,
                        "application_deadline":       None,
                        "official_source_url":        None,
                        "official_registration_url":  None,
                        "supplementary_source_url":   None,
                        "url_verification_fallback_used": True,
                        "confidence_score":           0.0,
                        "processing_method_detected": "Unknown",
                        "remarks":                    remark,
                    }
                })
                save_result_json(sch_name, model_name, search_status, processed_results)
                send_scout_report_email(processed_results, quota_exceeded)
                return

        if not urls_to_scrape:  # only set by the fallback block above
            search_results = search_results or []


        # Build scrape queue: preferred URLs first, then search results
        # (only runs when search succeeded — fallback path above already set urls_to_scrape)
        if not urls_to_scrape:
            preferred_entries = [
                (u, "Config Preferred URL") for u in sch_cfg.get("preferred_urls", [])
            ]
            search_entries = [
                (r["url"], "Search Result") for r in search_results[:5]
            ]
            # Deduplicate: don't re-scrape preferred URLs if search also returned them
            preferred_set  = {u for u, _ in preferred_entries}
            search_entries = [e for e in search_entries if e[0] not in preferred_set]

            # Official-first ordering among search entries (keep existing logic)
            official_entries = [(u, t) for u, t in search_entries if not is_news_domain(u)]
            news_entries     = [(u, t) for u, t in search_entries if is_news_domain(u)]

            urls_to_scrape = preferred_entries + official_entries + news_entries

    # 2. Deep scraping & link extraction
    # Only from search results / preferred URLs — historical spreadsheet links are NOT scraped.
    scraped_pages = []
    all_candidate_info = []
    all_candidate_reg = []
    fetched_urls = set()
        
    BINARY_EXTENSIONS = (".pdf", ".xlsx", ".xls", ".docx", ".doc", ".ppt", ".pptx", ".zip", ".rar")
    
    # Branching keywords — broad enough to catch news/announcement sub-pages like
    # /news/2026-application, /burse-2026, /program/scholarship, etc.
    branching_keywords = [
        "research", "rs", "graduate", "indonesia", "tahap",
        "announcement", "guideline", "scholar", "2026", "2025",
        "deadline", "apply", "apply-now", "schedule", "timeline",
        "gks", "kgsp", "niied",
        "news", "application", "open", "program", "burse",
        "scholarship", "grant", "award", "selection", "intake",
        "period", "cycle", "applic", "eligib", "require"
    ]
    # Global branching counter — shared across ALL top-level pages so we get
    # up to MAX_BRANCHES total sub-page fetches, not 2 per top-level URL.
    branching_count = 0
    MAX_BRANCHES = 4
    
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
        _char_limit = sch_cfg.get("scrape_char_limit", 5000)
        truncated_text = cleaned_text[:_char_limit]

        # B2: Translation sub-step for non-English pages
        if sch_cfg.get("needs_translation") and cleaned_text:
            lang_hint   = sch_cfg.get("translation_lang", "auto")
            ascii_ratio = sum(
                1 for c in cleaned_text if c.isascii() and c.isalpha()
            ) / max(len(cleaned_text), 1)
            if ascii_ratio < 0.05:
                logger.info(
                    f"Non-English content (ASCII ratio {ascii_ratio:.2f}). "
                    f"Translating excerpt (lang_hint={lang_hint})..."
                )
                translated   = translate_text(cleaned_text[:500], source_lang=lang_hint)
                cleaned_text = f"[TRANSLATED EXCERPT]:\n{translated}\n\n[ORIGINAL]:\n{cleaned_text}"
                truncated_text = cleaned_text[:_char_limit]
        
        links = extract_hyperlinks(html, url)
        candidates = filter_candidate_links(links, sch_name)
        all_candidate_info.extend(candidates["info"])
        all_candidate_reg.extend(candidates["reg"])
        
        scraped_pages.append({
            "url": url,
            "type": url_type,
            "content": truncated_text
        })
        
        # Determine the domain of the current page for same-domain branching
        current_netloc = urllib.parse.urlparse(url).netloc.lower()

        # Sort candidate sub-links so the most scholarship-relevant URLs are
        # branched into first, before the budget runs out on generic pages.
        # Priority 0: path matches a standard branching keyword (e.g. /apply, /deadline)
        # Priority 1: URL contains a scholarship-name word (e.g. "special" from "Special MEXT")
        # Priority 2: everything else (official but generic, e.g. global.hokudai.ac.jp/)
        _name_words = [w.lower() for w in sch_name.split() if len(w) > 3]
        def _branch_priority(u: str) -> int:
            p = urllib.parse.urlparse(u).path.lower()
            if any(kw in p for kw in branching_keywords):
                return 0
            if any(w in u.lower() for w in _name_words):
                return 1
            return 2

        for sub_url in sorted(candidates["info"], key=_branch_priority):
            if branching_count >= MAX_BRANCHES:
                break
            if sub_url == url or sub_url in fetched_urls:
                continue

            sub_parsed = urllib.parse.urlparse(sub_url)
            sub_netloc = sub_parsed.netloc.lower()
            sub_path = sub_parsed.path.lower()

            # Skip binary files — PDFs/docs waste a branch slot and yield no text
            if any(sub_path.endswith(ext) for ext in BINARY_EXTENSIONS):
                logger.debug(f"Skipping binary sub-link: {sub_url}")
                continue

            # Hard-skip known useless URL patterns and noise domains
            USELESS_PATH_PATTERNS = (
                "/search/label/", "/search/tag/", "/tag/", "/category/",
                "/contact", "/about", "/privacy", "/terms", "/faq",
                "/p/contact", "/p/about", "/sitemap",
            )
            USELESS_DOMAINS = (
                "www.google.com", "google.com", "translate.google.com",
                "twitter.com", "x.com", "instagram.com", "facebook.com",
                "linkedin.com", "youtube.com", "t.me", "wa.me",
            )
            if sub_netloc in USELESS_DOMAINS:
                continue
            if any(pat in sub_path for pat in USELESS_PATH_PATTERNS):
                continue

            # DOMAIN RESTRICTION: Only branch into sub-pages of the CURRENT page's
            # domain, or into known official domains. This prevents aggregator sites
            # like fullscholarships.net from burning the branch budget on their own
            # "related scholarship" articles from completely different sites.
            is_same_domain = (sub_netloc == current_netloc)
            is_official = is_official_domain(sub_url)
            if not is_same_domain and not is_official:
                continue

            # Match keywords against the URL PATH only (not domain) to avoid
            # substring false-positives from the domain name itself.
            # EXCEPTION: official-domain URLs that already passed filter_candidate_links
            # are trusted regardless of path keywords. This covers pages whose paths
            # use non-standard terms (e.g. "special-quota.php", "admissions.php")
            # that don't appear in the generic branching keyword list.
            passes_path_keywords = any(kw in sub_path for kw in branching_keywords)
            if passes_path_keywords or is_official:
                fetched_urls.add(sub_url)
                logger.info(f"Following branching sub-link ({branching_count + 1}/{MAX_BRANCHES}): {sub_url}")
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

    # Build the set of ALL URLs actually visited/found via scraping — used to validate LLM output.
    # No historical spreadsheet links are included here.
    all_known_urls = set(fetched_urls)
    all_known_urls.update(all_candidate_info)
    all_known_urls.update(all_candidate_reg)
    
    # 3. Invoke Cerebras LLM API
    # B3b: Uni-to-uni LLM context injection
    if name_parsed["type"] == "uni_to_uni":
        uni_context_note = f"""
IMPORTANT CONTEXT — UNI-TO-UNI SCHOLARSHIP:
This is a UNI-TO-UNI entry. '{name_parsed["scholarship"]}' is being checked specifically
for '{name_parsed["university"]}'. This university manages its own application window \u2014
it may differ from the scholarship body's central portal dates.

RULES FOR THIS ENTRY:
1. For official_source_url and dates: PRIORITISE the university's own page.
2. If the central scholarship body's dates are also found: include them in
   'remarks' (e.g. "Central body deadline: YYYY-MM-DD. University page: YYYY-MM-DD").
3. The university page date is what the user will act on \u2014 use it as the primary result.
"""
    else:
        uni_context_note = ""

    # date_source_domain: hard constraint injected when config specifies the ONLY valid date source
    date_domain = sch_cfg.get("date_source_domain", "")
    if date_domain:
        uni_context_note += f"""
\u26a0\ufe0f  HARD DATE SOURCE CONSTRAINT \u2014 READ AND FOLLOW STRICTLY:
The dates for this scholarship MUST ONLY come from pages on the domain: {date_domain}
  - If you find dates on pages from other domains (e.g. studyinjapan.go.jp, guides,
    or any other country's embassy page), you MUST ignore those dates entirely.
  - The official_source_url MUST be a URL on {date_domain}.
  - If NO date information was found on {date_domain} pages in the scraped content, then:
      * Set application_start_date = null
      * Set application_deadline = null
      * Set status = 'CLOSED' (conservative)
      * Set url_verification_fallback_used = true
      * In remarks, state exactly: "REQUIRED SOURCE ({date_domain}) was not accessible or had no dates."
  - DO NOT substitute dates from any other domain, even if that domain has dates.
  - This constraint is ABSOLUTE. There are no exceptions.
"""

    # B4: Locked mode LLM note — inform LLM that only pre-configured pages were scraped
    # and that these pages ARE authoritative (prevents it rating them as "third-party blogs")
    if is_locked:
        uni_context_note += """
⚠️  LOCKED SOURCE MODE — READ AND FOLLOW STRICTLY:
The pages in the scraped context below are the ONLY sources available for this scholarship.
No search engine was used. These pre-configured URLs are the designated authoritative sources:
  - Treat them as official scholarship pages, NOT as third-party blogs or unofficial sites.
  - Extract all date, status, and link information exclusively from these pages.
  - Do NOT downgrade or dismiss these pages based on their domain name or writing style.
  - These are the definitive sources the operator has verified for this scholarship.
"""

    # Config-injected context hint — used when the page content is truncated or buried
    # (e.g. CMK: date section is past the 5,000-char limit due to privacy modal HTML above it).
    # Appended to the prompt so the LLM has the known schedule even if scraping misses it.
    if sch_cfg.get("context_hint"):
        uni_context_note += f"\n\nADDITIONAL CONTEXT (operator-verified):\n{sch_cfg['context_hint']}\n"

    try:
        verified_data = verify_scholarship_llama(
            scholarship_name=matched_row["scholarship_name"],
            scraped_web_text=context_str,
            candidate_info_links=unique_candidate_info,
            candidate_reg_links=unique_candidate_reg,
            model_name=model_name,
            uni_context_note=uni_context_note,   # B3b
        )
        
        # Sanitize links: replace empty strings or literal "None" strings with Python None
        def sanitize_link(val):
            if not val or str(val).strip().lower() in ("none", "", "null", "-", "n/a"):
                return None
            return val
        
        verified_data["official_source_url"] = sanitize_link(verified_data.get("official_source_url"))
        verified_data["official_registration_url"] = sanitize_link(verified_data.get("official_registration_url"))
        verified_data["supplementary_source_url"] = sanitize_link(verified_data.get("supplementary_source_url"))

        # Validate supplementary_source_url: only keep if from a trusted official domain
        supp_url = verified_data.get("supplementary_source_url")
        if supp_url and is_news_domain(supp_url):
            logger.warning(f"Supplementary URL '{supp_url}' is a news/media site — discarding.")
            verified_data["supplementary_source_url"] = None

        # ── URL HALLUCINATION GUARD ──────────────────────────────────────────────
        # Validate that the reg URL the LLM returned was actually seen during scraping.
        # If not, reject it — no historical fallback exists, so set to None.
        reg_url = verified_data.get("official_registration_url")
        if reg_url and reg_url not in all_known_urls:
            reg_domain = urllib.parse.urlparse(reg_url).netloc
            domain_seen = any(
                urllib.parse.urlparse(u).netloc == reg_domain
                for u in all_known_urls if u
            )
            if not domain_seen:
                logger.warning(
                    f"Reg URL '{reg_url}' domain was never scraped — "
                    f"likely hallucinated. Setting to null."
                )
            else:
                logger.warning(
                    f"Reg URL '{reg_url}' has a known domain but an unseen path — "
                    f"setting to null to avoid hallucinated paths."
                )
            verified_data["official_registration_url"] = None

        # ── INFO URL HALLUCINATION GUARD ─────────────────────────────────────────
        # Validate info URL domain was actually seen in scraping.
        # Allow sub-pages of scraped domains (e.g. /news/2026-application is fine
        # if the domain itself was visited). Reject entirely unknown domains.
        info_url = verified_data.get("official_source_url")
        if info_url and info_url not in all_known_urls:
            info_domain = urllib.parse.urlparse(info_url).netloc
            domain_seen = any(
                urllib.parse.urlparse(u).netloc == info_domain
                for u in all_known_urls if u
            )
            if not domain_seen:
                logger.warning(
                    f"Info URL '{info_url}' domain was never scraped — "
                    f"likely hallucinated. Setting to null."
                )
                verified_data["official_source_url"] = None
            else:
                logger.info(
                    f"Info URL '{info_url}' path not in exact list but domain is known — "
                    f"keeping as valid sub-page of a scraped domain."
                )

        # ── A2: START DATE ESTIMATION ────────────────────────────────────────────
        # If LLM found a deadline but no start date, estimate start = deadline − 90 days.
        _start = verified_data.get("application_start_date")
        _end   = verified_data.get("application_deadline")
        if _start is None and _end is not None:
            try:
                _end_dt   = date.fromisoformat(_end)
                _start_dt = _end_dt - timedelta(days=90)
                verified_data["application_start_date"] = _start_dt.isoformat()
                verified_data["remarks"] = (
                    (verified_data.get("remarks") or "") +
                    " [Start date estimated: only deadline found - start = deadline - 90 days.]"
                ).strip()
                logger.info(
                    f"Start date estimated: {_start_dt.isoformat()} "
                    f"(deadline - 90 days from {_end})"
                )
            except ValueError:
                pass  # unparseable end_date — leave start as None

        # ── STATUS SAFETY NET ───────────────────────────────────────────────────
        # If LLM returned OPEN but couldn't find a deadline, we can't confirm it.
        # Default conservatively to CLOSED so the user doesn't miss a deadline.
        if (verified_data.get("status") == "OPEN"
                and verified_data.get("application_deadline") is None):
            logger.warning(
                "Status is OPEN but no deadline was found — cannot confirm. "
                "Forcing status to CLOSED (conservative fallback)."
            )
            verified_data["status"] = "CLOSED"
            verified_data["remarks"] = (
                (verified_data.get("remarks") or "") +
                " [Status overridden to CLOSED: no explicit deadline date found to confirm OPEN.]"
            ).strip()

        # B4: Prepend locked source note to remarks so it's always visible in the email
        if is_locked and locked_source_note:
            verified_data["remarks"] = (
                locked_source_note + " | " + (verified_data.get("remarks") or "")
            ).strip(" |").strip()

        print(f"Verified Status:           {verified_data.get('status')}")
        print(f"Verified Start Date:       {verified_data.get('application_start_date')}")
        print(f"Verified Deadline:         {verified_data.get('application_deadline')}")
        print(f"Processing Method:         {verified_data.get('processing_method_detected')}")
        print(f"Confidence Score:          {verified_data.get('confidence_score')}")
        print(f"Verified Info URL:         {verified_data.get('official_source_url')}")
        print(f"Supplementary URL:         {verified_data.get('supplementary_source_url')}")
        print(f"Verified Reg URL:          {verified_data.get('official_registration_url')}")
        print(f"Fallback Used:             {verified_data.get('url_verification_fallback_used')}")
        print(f"Remarks:                   {verified_data.get('remarks')}")
        
        processed_results.append({
            "row_idx":       matched_row["row_idx"],
            "search_status": search_status,   # "SUCCESS" or "LOCKED"
            "verified_data": verified_data,
        })
        
    except CerebrasQuotaExceededException as qe:
        logger.critical(f"Cerebras API Quota/Rate Limit Exceeded: {str(qe)}")
        quota_exceeded = True
    except Exception as e:
        logger.error(f"Verification failed for '{sch_name}': {str(e)}", exc_info=True)
        processed_results.append({
            "row_idx": matched_row["row_idx"],
            "verified_data": {
                "scholarship_name": sch_name,
                "status": "CLOSED",
                "application_start_date": None,
                "application_deadline": None,
                "official_source_url": None,
                "official_registration_url": None,
                "url_verification_fallback_used": True,
                "confidence_score": 0.0,
                "processing_method_detected": "Unknown",
                "remarks": f"System error: {str(e)}"
            }
        })
        
    # Prototype mode: DO NOT write to the sheet.
    # Only send the email report so you can review results without touching spreadsheet data.
    if processed_results:
        logger.info("Prototype mode: skipping sheet write. Sending email report only...")
        save_result_json(sch_name, model_name, "SUCCESS", processed_results)  # A1
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

        # A3/A4: determine display status and colour based on search_status first
        search_status_val = r.get("search_status", "SUCCESS")
        status            = data.get("status", "CLOSED")

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
            status_label = f"{_label} ⚙️"  # gear icon signals fallback mode
        else:
            status_color = "#2ecc71" if status == "OPEN" else (
                "#f39c12" if status == "NOT_YET_OPENED" else "#e74c3c"
            )
            status_label = "NOT YET OPEN" if status == "NOT_YET_OPENED" else status
        
        info_url = data.get("official_source_url")
        supp_url = data.get("supplementary_source_url")
        reg_url  = data.get("official_registration_url")
        
        # Build info link cell — primary + optional supplementary announcement link
        if info_url:
            info_cell = f'<a href="{info_url}" style="color: #3498db; text-decoration: none;">Info Link</a>'
        else:
            info_cell = '<span style="color: #bdc3c7;">—</span>'
        if supp_url:
            info_cell += f' &nbsp;<a href="{supp_url}" style="color: #8e44ad; text-decoration: none; font-size: 11px;">[Announcement ↗]</a>'
        
        reg_cell = (
            f'<a href="{reg_url}" style="color: #2ecc71; text-decoration: none; font-weight: bold;">Reg Link</a>'
            if reg_url else '<span style="color: #bdc3c7;">—</span>'
        )
        
        # B3c: UNI-TO-UNI badge on scholarship name
        _parsed_name = parse_scholarship_name(data.get("scholarship_name", ""))
        if _parsed_name["type"] == "uni_to_uni":
            name_display = (
                f'{data.get("scholarship_name")} '
                f'<span style="background:#8e44ad;color:white;font-size:10px;'
                f'padding:1px 5px;border-radius:3px;vertical-align:middle;">UNI-TO-UNI</span>'
            )
        else:
            name_display = data.get("scholarship_name", "")

        # C1: date_precision — prefix estimated dates with ~
        date_precision = data.get("date_precision", "exact")
        start_display = data.get("application_start_date") or "N/A"
        end_display   = data.get("application_deadline")    or "N/A"
        if date_precision in ("monthly", "quarterly") and start_display != "N/A":
            start_display = f"~{start_display}"
        if date_precision in ("monthly", "quarterly") and end_display != "N/A":
            end_display = f"~{end_display}"

        method = data.get("processing_method_detected") or "—"
        rows.append(f"""
        <tr style="border-bottom: 1px solid #dddddd;">
            <td style="padding: 12px 15px; font-weight: bold; color: #333333;">{name_display}</td>
            <td style="padding: 12px 15px; font-weight: bold; color: {status_color}; text-decoration: none; white-space: nowrap;">{status_label}</td>
            <td style="padding: 12px 15px; color: #555555;">{start_display}</td>
            <td style="padding: 12px 15px; color: #555555;">{end_display}</td>
            <td style="padding: 12px 15px; font-size: 12px;">{info_cell}</td>
            <td style="padding: 12px 15px; font-size: 12px;">{reg_cell}</td>
            <td style="padding: 12px 15px; color: #555555; font-size: 12px;">{method}</td>
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
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Scholarship Verification Run Report</title>
</head>
<body style="background-color: #f9f9f9; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; margin: 0; padding: 20px; color: #333333;">
    <div style="max-width: 900px; margin: 0 auto; background-color: #ffffff; padding: 30px; border-radius: 8px; box-shadow: 0 4px 10px rgba(0,0,0,0.05); border-top: 5px solid #3498db;">
        
        {alert_banner}
        
        <h2 style="font-size: 22px; font-weight: 600; margin-top: 0; color: #2c3e50;">🎓 Scholarship Verification Report</h2>
        <p style="font-size: 14px; color: #7f8c8d; margin-bottom: 25px;">
            This report contains the latest verification details compiled by the automated Scout pipeline running model <strong>{model_name}</strong>.
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
