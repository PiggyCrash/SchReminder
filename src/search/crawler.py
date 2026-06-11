"""
Crawler — web search and page fetching for SchReminder Scout.

Implements:
  - Four-engine search (DuckDuckGo → Yahoo → Bing → SearXNG)
  - Three-round retry: immediate, 30s, 60s
  - Yahoo/Bing/SearXNG each retry internally on network errors with UA rotation
  - Classified search_status: SUCCESS / NETWORK_FAILURE / BLOCKED / NO_RESULTS
  - fetch_webpage_content() with 403 UA rotation, HTTPS auto-upgrade, captcha detection
  - extract_hyperlinks() and filter_candidate_links() for branching-link discovery
  - translate_text() for non-English pages (Bolashak etc.)
"""

import time
import logging
import random
import requests
import urllib.parse
from typing import Optional, List, Dict, Tuple

from bs4 import BeautifulSoup

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("Crawler")

# ── HTTPS upgrade helpers ──────────────────────────────────────────────────────
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
            logger.debug(f"HTTP->HTTPS upgrade: {url} -> {upgraded}")
            return upgraded
    return url

# ── Official / news domain classifiers ────────────────────────────────────────
# Official government / embassy / academic domains whose news/announcement
# sub-pages are still authoritative sources — never block these regardless of path.
OFFICIAL_DOMAINS = [
    ".go.id", ".go.jp", ".go.kr", ".go.th", ".go.au", ".gov", ".gov.au",
    ".ac.id", ".ac.jp", ".ac.kr", ".edu", ".edu.au",
    "emb-japan.go.jp", "mofa.go.kr", "niied.go.kr", "koica.go.kr",
    "scholarshipdb.net", "daad.de", "chevening.org", "britishcouncil.org"
]

# News/media domains that should never be used as official scholarship sources.
# Official government domains (e.g. kemenag.go.id/nasional/) are NOT blocked
# even if they publish news-style posts — those are official announcements.
NEWS_MEDIA_DOMAINS = [
    "kompas.com", "detik.com", "tribunnews.com", "liputan6.com", "okezone.com",
    "sindonews.com", "cnnindonesia.com", "tempo.co", "bisnis.com", "kumparan.com",
    "merdeka.com", "suara.com", "republika.co.id", "antara.co.id", "jpnn.com",
    "jawapos.com", "inews.id", "idntimes.com", "viva.co.id", "beritasatu.com",
    "thejakartapost.com", "medcom.id", "metrotvnews.com", "cnbcindonesia.com",
    "news.google.com", "yahoo.com/news", "bing.com/news"
]

def is_official_domain(url: str) -> bool:
    """Returns True if the URL belongs to a trusted government, embassy, or academic domain."""
    url_lower = url.lower()
    return any(domain in url_lower for domain in OFFICIAL_DOMAINS)

def is_news_domain(url: str) -> bool:
    """
    Returns True if the URL belongs to a known third-party news/media outlet.
    Official government/academic domains are never classified as news even if
    they publish news-style announcement posts.
    """
    if is_official_domain(url):
        return False  # Never block official domains
    url_lower = url.lower()
    return any(domain in url_lower for domain in NEWS_MEDIA_DOMAINS)

# ── URL cleaners ───────────────────────────────────────────────────────────────
def clean_bing_url(raw_url: str) -> str:
    """Decodes Bing's redirect link u-parameter (base64 encoded with 'a1' prefix)."""
    if not raw_url:
        return ""
    if "/ck/a?" not in raw_url:
        return raw_url
    try:
        import base64
        parsed_url = urllib.parse.urlparse(raw_url)
        queries = urllib.parse.parse_qs(parsed_url.query)
        u_param = queries.get("u", [None])[0]
        if u_param:
            base64_str = u_param[2:]
            base64_str += "=" * ((4 - len(base64_str) % 4) % 4)
            try:
                decoded_bytes = base64.b64decode(base64_str.encode("utf-8"))
            except Exception:
                decoded_bytes = base64.urlsafe_b64decode(base64_str.encode("utf-8"))
            decoded = decoded_bytes.decode("utf-8", errors="ignore")
            if decoded.startswith("http://") or decoded.startswith("https://"):
                return decoded
    except Exception as e:
        logger.debug(f"Failed to decode Bing URL: {raw_url}, error: {str(e)}")
    return raw_url

def clean_yahoo_url(redirect_url: str) -> str:
    """Decodes Yahoo's search redirect URL to get the actual destination URL."""
    if not redirect_url or "r.search.yahoo.com" not in redirect_url:
        return redirect_url
    try:
        parsed = urllib.parse.urlparse(redirect_url)
        path = parsed.path
        if "/RU=" in path:
            ru_part = path.split("/RU=")[1].split("/")[0]
            return urllib.parse.unquote(ru_part)
    except Exception:
        pass
    return redirect_url

# ── Page fetching ──────────────────────────────────────────────────────────────
def fetch_webpage_content(url: str, retries: int = 2, retry_delay: int = 180) -> Optional[str]:
    """
    Fetches URL HTML content.
    - HTTP 403: rotates through realistic browser User-Agent strings.
    - HTTP 429 or captcha: retries with 3-min sleep (genuine rate limits).
    - SSL/connection errors: skips immediately without sleep.
    - http:// on official TLDs: auto-upgraded to https:// before first request.
    """
    if not url or not (url.startswith("http://") or url.startswith("https://")):
        return None
    # Proactively upgrade http:// to https:// for official academic/govt domains
    url = _upgrade_to_https(url)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }
    for attempt in range(1, retries + 1):
        try:
            logger.info(f"Fetching URL (Attempt {attempt}/{retries}): {url}")
            response = requests.get(url, headers=headers, timeout=15)

            # 403 = WAF/bot-detection block. Rotate through realistic browser UA strings.
            if response.status_code == 403:
                stealth_ua_pool = [
                    (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                        "https://www.google.com/"
                    ),
                    (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 "
                        "(KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
                        "https://www.google.com/"
                    ),
                    (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) "
                        "Gecko/20100101 Firefox/125.0",
                        "https://www.google.co.id/"
                    ),
                    (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
                        "https://www.bing.com/"
                    ),
                ]
                logger.warning(f"403 on {url} - rotating User-Agent to bypass bot filter...")
                for ua_str, referer in stealth_ua_pool:
                    try:
                        stealth_headers = {
                            "User-Agent": ua_str,
                            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                            "Accept-Language": "en-US,en;q=0.9,id;q=0.8",
                            "Accept-Encoding": "gzip, deflate, br",
                            "Referer": referer,
                            "Sec-Fetch-Dest": "document",
                            "Sec-Fetch-Mode": "navigate",
                            "Sec-Fetch-Site": "cross-site",
                            "Cache-Control": "no-cache",
                            "Connection": "keep-alive",
                            "Upgrade-Insecure-Requests": "1",
                        }
                        r2 = requests.get(url, headers=stealth_headers, timeout=15)
                        if r2.ok:
                            logger.info(f"UA rotation succeeded for {url} (status {r2.status_code})")
                            return r2.text
                        logger.warning(f"UA rotation attempt failed: status {r2.status_code}")
                    except Exception as ua_err:
                        logger.warning(f"UA rotation attempt error: {ua_err}")
                logger.warning(f"403 Forbidden on {url} - all UA rotation attempts failed. Skipping.")
                return None

            is_blocked = False
            if response.status_code == 429:
                is_blocked = True
            else:
                # Strip scripts/styles before checking — JS variable names like 'silentcaptcha'
                # appear on many normal sites and must not be treated as captcha challenges.
                _soup = BeautifulSoup(response.text, "html.parser")
                for _tag in _soup(["script", "style"]):
                    _tag.decompose()
                visible_text = _soup.get_text().lower()
                if any(phrase in visible_text for phrase in [
                    "ddg-captcha", "i am not a robot", "prove you are human",
                    "too many requests", "ddg-lms", "verify you are human"
                ]):
                    is_blocked = True

            if is_blocked:
                logger.warning(f"Rate limit or Captcha block detected on {url}. Status: {response.status_code}")
                if attempt < retries:
                    logger.info(f"Sleeping for {retry_delay} seconds before retry...")
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
            is_network_error = any(kw in err_type or kw in err_str for kw in [
                "SSL", "Connection", "ConnectionReset", "timeout", "Timeout",
                "HANDSHAKE", "forcibly closed", "10054", "10061", "RemoteDisconnected"
            ])
            if is_network_error:
                # If the URL is http:// and it timed out/refused, try https:// once
                if url.startswith("http://") and "ConnectTimeout" in err_type:
                    https_url = "https://" + url[7:]
                    logger.info(f"ConnectTimeout on http:// - retrying with https://: {https_url}")
                    return fetch_webpage_content(https_url, retries=1, retry_delay=0)
                logger.warning(f"Network error on {url} - skipping without retry sleep.")
                return None
            if attempt < retries:
                logger.info(f"Sleeping for {retry_delay} seconds before retry...")
                time.sleep(retry_delay)
            else:
                return None
    return None

def clean_html(html_content: str) -> str:
    """Strips script/style/nav/footer tags and returns clean text."""
    soup = BeautifulSoup(html_content, "html.parser")
    for element in soup(["script", "style", "nav", "header", "footer", "iframe", "aside"]):
        element.decompose()
    text = soup.get_text(separator=" ")
    cleaned_lines = [line.strip() for line in text.splitlines() if line.strip()]
    return "\n".join(cleaned_lines)

def extract_hyperlinks(html_content: str, base_url: str) -> List[Dict[str, str]]:
    """Extracts all absolute hyperlinks from an HTML page."""
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
    """
    Classifies links from a page into info candidates and registration candidates.
    News/media domains are filtered out of info candidates.
    """
    keywords = [w.lower() for w in scholarship_name.split() if len(w) > 3]
    keywords_info = keywords + [
        "research", "rs", "student", "graduate", "announcement", "scholarship",
        "guideline", "guide", "tahap", "stem"
    ]
    keywords_reg = ["apply", "register", "pendaftaran", "daftar", "login", "portal", "form", "online", "submit"]

    candidate_info = []
    candidate_reg = []

    for link in links:
        url_lower = link["url"].lower()
        text_lower = link["text"].lower()

        if any(kw in url_lower or kw in text_lower for kw in keywords_reg):
            candidate_reg.append(link["url"])

        # Never include third-party news/media sites as official info sources.
        if not is_news_domain(link["url"]):
            if any(kw in url_lower or kw in text_lower for kw in keywords_info):
                candidate_info.append(link["url"])

    return {
        "info": list(set(candidate_info))[:10],
        "reg":  list(set(candidate_reg))[:10]
    }

# ── Translation helper ─────────────────────────────────────────────────────────
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

# ── Search engine helpers ──────────────────────────────────────────────────────
def _try_duckduckgo(query: str, max_results: int = 5) -> Optional[List[Dict[str, str]]]:
    """
    Attempts a single DuckDuckGo HTML search.
    Returns results list or None on failure/captcha.
    """
    url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    response = requests.get(url, headers=headers, timeout=15)  # may raise — let caller catch

    if not response.ok:
        logger.warning(f"DuckDuckGo HTTP {response.status_code}")
        return None
    if any(kw in response.text for kw in ["ddg-captcha", "ddg-lms"]):
        logger.warning("DuckDuckGo returned captcha/rate-limit page.")
        return None

    soup = BeautifulSoup(response.text, "html.parser")
    results = []
    for div in soup.find_all("div", class_="result")[:max_results]:
        title_a = div.find("a", class_="result__url") or div.find("a", class_="result__title")
        if not title_a:
            continue
        title = title_a.get_text(strip=True)
        href  = title_a.get("href", "")
        if "uddg=" in href:
            try:
                parsed_href = urllib.parse.urlparse(href)
                qs = urllib.parse.parse_qs(parsed_href.query)
                if "uddg" in qs:
                    href = qs["uddg"][0]
            except Exception:
                pass
        elif href.startswith("//"):
            href = "https:" + href
        snippet_div = div.find(class_="result__snippet")
        snippet = snippet_div.get_text(strip=True) if snippet_div else ""
        if title and href:
            results.append({"title": title, "url": href, "snippet": snippet})

    if not results:
        logger.warning("DuckDuckGo returned 0 parseable results.")
        return None
    logger.info(f"DuckDuckGo: harvested {len(results)} results.")
    return results


def _try_bing(query: str, max_results: int = 5) -> Optional[List[Dict[str, str]]]:
    """
    Bing HTML search — third engine fallback.
    Bing is on completely different infrastructure from DDG/Yahoo.
    Retries up to 3 times on network errors with UA rotation.
    """
    logger.info(f"Trying Bing search for query: '{query}'")
    search_url = f"https://www.bing.com/search?q={urllib.parse.quote(query)}&setlang=en"

    ua_pool = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    ]

    for attempt in range(1, 4):
        headers = {
            "User-Agent": ua_pool[(attempt - 1) % len(ua_pool)],
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        try:
            response = requests.get(search_url, headers=headers, timeout=20, allow_redirects=True)
            if response.status_code == 429:
                logger.warning(f"Bing rate-limited (attempt {attempt}/3).")
                return None  # rate-limit: don't retry
            if response.status_code >= 500:
                logger.warning(f"Bing HTTP {response.status_code} (attempt {attempt}/3) - retrying in 5s...")
                if attempt < 3:
                    time.sleep(5)
                    continue
                return None
            if not response.ok:
                logger.warning(f"Bing HTTP {response.status_code}")
                return None

            soup = BeautifulSoup(response.text, "html.parser")
            results = []
            # Bing results are in <li class="b_algo"> blocks
            for li in soup.find_all("li", class_="b_algo")[:max_results]:
                h2 = li.find("h2")
                if not h2:
                    continue
                a = h2.find("a", href=True)
                if not a:
                    continue
                title = a.get_text(strip=True)
                href  = a.get("href", "")
                # Bing sometimes wraps in /ck/a redirect — decode it
                href = clean_bing_url(href) if "/ck/a?" in href else href
                snippet_p = li.find("p") or li.find("div", class_="b_caption")
                snippet = snippet_p.get_text(strip=True)[:300] if snippet_p else ""
                if title and href and href.startswith("http"):
                    results.append({"title": title, "url": href, "snippet": snippet})

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
    SearXNG is open-source and aggregates Google/Bing/DDG itself,
    so it works even when individual engines block direct access.
    Uses JSON API endpoint for reliable parsing.
    Tries multiple public instances in order.
    """
    logger.info(f"Trying SearXNG search for query: '{query}'")

    # Multiple public SearXNG instances as fallbacks
    searx_instances = [
        "https://searx.be",
        "https://search.sapti.me",
        "https://searxng.site",
    ]

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json",
    }

    for instance in searx_instances:
        try:
            search_url = f"{instance}/search"
            params = {
                "q": query,
                "format": "json",
                "engines": "google,bing,duckduckgo",
                "language": "en-US",
            }
            response = requests.get(search_url, headers=headers, params=params, timeout=20)
            if not response.ok:
                logger.warning(f"SearXNG instance {instance} returned HTTP {response.status_code} - trying next...")
                continue

            data = response.json()
            raw_results = data.get("results", [])
            results = []
            for r in raw_results[:max_results]:
                url   = r.get("url", "")
                title = r.get("title", "")
                snippet = r.get("content", "")[:300]
                if url and title and url.startswith("http"):
                    results.append({"title": title, "url": url, "snippet": snippet})

            if not results:
                logger.warning(f"SearXNG instance {instance} returned 0 usable results - trying next...")
                continue

            logger.info(f"SearXNG ({instance}): harvested {len(results)} results.")
            return results

        except (requests.exceptions.SSLError,
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as net_err:
            logger.warning(f"SearXNG {instance} network error: {type(net_err).__name__} - trying next instance...")
            continue
        except Exception as e:
            logger.warning(f"SearXNG {instance} unexpected error: {e} - trying next instance...")
            continue

    logger.error("All SearXNG instances failed.")
    return None

def _try_yahoo(query: str, max_results: int = 5) -> Optional[List[Dict[str, str]]]:
    """
    Yahoo search fallback — real HTML results parseable without JavaScript.
    Retries up to 3 times on 5xx server errors AND on network/connection errors.
    """
    logger.info(f"Falling back to Yahoo Search scraping for query: '{query}'")
    search_url = f"https://search.yahoo.com/search?p={urllib.parse.quote(query)}"

    ua_pool = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    ]

    for attempt in range(1, 4):
        headers = {
            "User-Agent": ua_pool[(attempt - 1) % len(ua_pool)],
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        try:
            response = requests.get(search_url, headers=headers, timeout=20, allow_redirects=True)
            if response.status_code >= 500:
                logger.warning(f"Yahoo HTTP {response.status_code} (attempt {attempt}/3) - retrying in 5s...")
                if attempt < 3:
                    time.sleep(5)
                    continue
                else:
                    logger.error("Yahoo returned 5xx after 3 attempts.")
                    return None
            if not response.ok:
                logger.warning(f"Yahoo search returned HTTP {response.status_code}")
                return None

            soup = BeautifulSoup(response.text, "html.parser")
            results = []
            algo_elements = soup.find_all(class_="algo")
            logger.info(f"Yahoo returned {len(algo_elements)} result elements")
            for el in algo_elements[:max_results]:
                anchors = el.find_all("a", href=True)
                if not anchors:
                    continue
                a = anchors[0]
                title = a.get_text(strip=True)
                raw_href = a.get("href", "")
                href = clean_yahoo_url(raw_href)

                snippet_div = el.find("div", class_="compText") or el.find("p")
                snippet = snippet_div.get_text(strip=True)[:300] if snippet_div else ""

                if title and href and href.startswith("http"):
                    results.append({"title": title, "url": href, "snippet": snippet})

            if not results:
                logger.warning("Yahoo returned 0 parseable results.")
                return None
            logger.info(f"Yahoo: harvested {len(results)} results successfully.")
            return results

        except (requests.exceptions.SSLError,
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as net_err:
            # Network errors are transient — retry with next UA instead of bailing immediately
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

def search_scholarship_with_retry(
    query: str, max_results: int = 5
) -> Tuple[Optional[List[Dict[str, str]]], str]:
    """
    Four-engine, three-round search with aggressive retry.

    Each round tries engines in order: DDG -> Yahoo -> Bing -> SearXNG.
    The first engine to return >= 1 result wins immediately.
    Yahoo/Bing/SearXNG each have their own internal retry on network errors.

    Round schedule:
      Round 1: all four engines (immediate)
      Round 2: all four engines (after 30s sleep — fast recovery for brief blips)
      Round 3: all four engines (after 60s sleep — last-chance)

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
