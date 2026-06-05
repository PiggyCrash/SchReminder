import logging
import requests
import urllib.parse
from bs4 import BeautifulSoup
from typing import Optional, List, Dict

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("Crawler")

def clean_bing_url(raw_url: str) -> str:
    """
    Decodes Bing's redirect link u-parameter (which is base64 encoded with an 'a1' prefix)
    """
    if not raw_url:
        return ""
    if "/ck/a?" not in raw_url:
        return raw_url
        
    try:
        parsed_url = urllib.parse.urlparse(raw_url)
        queries = urllib.parse.parse_qs(parsed_url.query)
        u_param = queries.get("u", [None])[0]
        if u_param:
            # Slicing off the 'a1' prefix and decoding base64
            base64_str = u_param[2:]
            # Pad base64 if necessary
            base64_str += "=" * ((4 - len(base64_str) % 4) % 4)
            # Use base64url standard or standard base64 decoding
            import base64
            # Handle standard or urlsafe base64
            try:
                decoded_bytes = base64.b64decode(base64_str.encode('utf-8'))
            except Exception:
                # Fallback to urlsafe b64
                decoded_bytes = base64.urlsafe_b64decode(base64_str.encode('utf-8'))
                
            decoded = decoded_bytes.decode('utf-8', errors='ignore')
            if decoded.startswith('http://') or decoded.startswith('https://'):
                return decoded
    except Exception as e:
        logger.debug(f"Failed to decode Bing URL: {raw_url}, error: {str(e)}")
        
    return raw_url

def perform_bing_fallback(query: str, max_results: int = 8) -> Optional[str]:
    """
    Scrapes Bing Search as a robust fallback.
    """
    logger.info(f"🌐 Falling back to Bing Search scraping for query: '{query}'")
    search_url = f"https://www.bing.com/search?q={urllib.parse.quote(query)}"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
    }
    
    try:
        response = requests.get(search_url, headers=headers, timeout=10)
        if not response.ok:
            logger.warning(f"Bing search HTTP error! status: {response.status_code}")
            return None
            
        soup = BeautifulSoup(response.text, 'html.parser')
        results = []
        
        # Each search result typically has class b_algo
        algo_elements = soup.find_all(class_='b_algo')
        for el in algo_elements[:max_results]:
            title_a = el.find('h2')
            if title_a:
                title_a = title_a.find('a')
                
            if not title_a:
                continue
                
            title = title_a.get_text(strip=True)
            raw_url = title_a.get('href', '')
            
            # Clean redirect Bing URLs
            href = clean_bing_url(raw_url)
            
            # Snippet text extraction
            snippet_p = el.find(class_='b_caption')
            if snippet_p:
                snippet_p = snippet_p.find('p')
            if not snippet_p:
                snippet_p = el.find(class_='b_snippet')
            if not snippet_p:
                snippet_p = el.find('p')
                
            snippet = snippet_p.get_text(strip=True) if snippet_p else ""
            
            if title and href:
                results.append({
                    "title": title,
                    "url": href,
                    "snippet": snippet
                })
                
        if not results:
            logger.warning("Bing returned 0 results.")
            return None
            
        formatted_results = []
        for idx, result in enumerate(results, 1):
            formatted_results.append(
                f"{idx}. Title: {result['title']}\n"
                f"   URL: {result['url']}\n"
                f"   Snippet: {result['snippet']}\n"
                f"{'-' * 60}"
            )
        logger.info(f"Successfully harvested {len(results)} search results from Bing.")
        return "\n".join(formatted_results)
    except Exception as e:
        logger.error(f"Bing search fallback failed: {str(e)}", exc_info=True)
        return None

def search_scholarship(scholarship_name: str, max_results: int = 8) -> Optional[str]:
    """
    Search for a scholarship on DuckDuckGo and retrieve top results.
    If it fails or is rate-limited (captcha), falls back to Bing Search.
    """
    query = f"{scholarship_name} scholarship application status deadline timeline"
    logger.info(f"Initiating search harvesting for: '{scholarship_name}'")
    
    url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
    }
    
    try:
        response = requests.get(url, headers=headers, timeout=10)
        
        # Check if rate-limited / captcha is requested
        if response.ok and ("ddg-captcha" in response.text or "robot" in response.text or "ddg-lms" in response.text):
            logger.warning("DuckDuckGo returned captcha/rate-limiting block page.")
            return perform_bing_fallback(query, max_results)
            
        if not response.ok:
            logger.warning(f"DuckDuckGo HTTP error! status: {response.status_code}")
            return perform_bing_fallback(query, max_results)
            
        soup = BeautifulSoup(response.text, 'html.parser')
        results = []
        
        # Extract results from class result
        result_divs = soup.find_all('div', class_='result')
        for div in result_divs[:max_results]:
            title_a = div.find('a', class_='result__url') or div.find('a', class_='result__title')
            if not title_a:
                continue
                
            title = title_a.get_text(strip=True)
            href = title_a.get('href', '')
            
            # Clean DDG redirects
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
                
        if not results:
            logger.warning("DuckDuckGo HTML returned 0 results. Checking Bing fallback...")
            return perform_bing_fallback(query, max_results)
            
        # Format the scraped web context
        formatted_results = []
        for idx, result in enumerate(results, 1):
            formatted_results.append(
                f"{idx}. Title: {result['title']}\n"
                f"   URL: {result['url']}\n"
                f"   Snippet: {result['snippet']}\n"
                f"{'-' * 60}"
            )
            
        logger.info(f"Successfully harvested {len(results)} search results from DuckDuckGo.")
        return "\n".join(formatted_results)
        
    except Exception as e:
        logger.warning(f"DuckDuckGo scraping raised exception: {str(e)}. Attempting Bing fallback...")
        return perform_bing_fallback(query, max_results)

if __name__ == "__main__":
    test_name = "Chevening Scholarship"
    context = search_scholarship(test_name)
    if context:
        print(f"--- Harvest Results for '{test_name}' ---\n")
        print(context[:1000])
    else:
        print("Crawler failed to harvest details.")
