#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SEC M&A Scanner v2.0 - Item 1.01
Ulepszenia względem v1:
- HTML cleaning (BeautifulSoup) przed analizą AI
- feedparser zamiast xml.etree (odporna na uszkodzone RSS)
- HTTP Session z retry strategy
- Ticker z tytułu RSS regexem (primary) zamiast zgadywania
- Filtr MCap ($100M-$10B) + wolumen (100k/dzień, $500k/dzień)
- Wstępny filtr M&A keywords PRZED wywołaniem Groq
- Poprawiony wzór premii: (offer_price - current_price) / current_price
- Short interest w prompcie Groq (detekcja squeeze)
- Dokument 12 000 znaków zamiast 6 000
- Drugi RSS endpoint jako backup
- Temperature 0.2 (mniej halucynacji, zachowana jakość interpretacji)
- Regex deal value jako weryfikacja dla AI
- Osobny plik w Gist: processed_ma_v2.json
"""

import feedparser
import requests
import json
import os
import re
import time
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    import yfinance as yf
    YFINANCE_AVAILABLE = True
except ImportError:
    YFINANCE_AVAILABLE = False

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ============================================
# KONFIGURACJA
# ============================================

DISCORD_WEBHOOK_V2 = os.environ.get('DISCORD_WEBHOOK_V2', '')  # jeden kanał dla wszystkich alertów v2
GROQ_API_KEY             = os.environ.get('GROQ_API_KEY', '')
GIST_TOKEN               = os.environ.get('GIST_TOKEN', '')
GIST_ID                  = os.environ.get('GIST_ID_MA', '')

GIST_FILE_NAME = 'processed_ma_v2.json'  # osobny plik — nie nadpisuje v1

USER_AGENT = "SEC-MA-Scanner/2.0 (research@example.com)"

SEC_RSS_PRIMARY = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&count=100&output=atom"
SEC_RSS_BACKUP  = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&count=40&start=100&output=atom"

# ============================================
# FILTRY PŁYNNOŚCI
# ============================================

MIN_MARKET_CAP    = 100_000_000       # $100M
MAX_MARKET_CAP    = 10_000_000_000    # $10B
MIN_AVG_VOLUME    = 100_000           # 100k akcji/dzień
MIN_DOLLAR_VOLUME = 500_000           # $500k/dzień

# ============================================
# M&A KEYWORD PRE-FILTER
# ============================================

MA_KEYWORDS = [
    r'definitive\s+agreement',
    r'merger\s+agreement',
    r'acquisition\s+agreement',
    r'to\s+be\s+acquired',
    r'will\s+acquire',
    r'has\s+agreed\s+to\s+acquire',
    r'all.cash\s+(?:offer|transaction|deal|merger)',
    r'cash\s+consideration',
    r'tender\s+offer',
    r'going\s+private',
    r'take.private',
    r'leveraged\s+buyout',
    r'\blbo\b',
    r'acquired\s+by',
    r'acquisition\s+of\s+(?:all|the)',
    r'merger\s+with',
    r'agree(?:d|s)?\s+to\s+(?:acquire|merge|purchase)',
    r'purchase\s+(?:price|agreement)',
    r'offer\s+price',
]

# ============================================
# HTTP SESSION Z RETRY
# ============================================

_retry = Retry(
    total=3,
    backoff_factor=1,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET", "POST"],
)
HTTP_SESSION = requests.Session()
HTTP_SESSION.mount("https://", HTTPAdapter(max_retries=_retry))
HTTP_SESSION.headers.update({"User-Agent": USER_AGENT})

# ============================================
# GIST — ŚLEDZENIE PRZETWORZONYCH FILINGÓW
# ============================================

def load_processed_from_gist() -> set:
    if not GIST_TOKEN or not GIST_ID:
        return set()
    try:
        headers = {
            'Authorization': f'token {GIST_TOKEN}',
            'Accept': 'application/vnd.github.v3+json'
        }
        r = HTTP_SESSION.get(f'https://api.github.com/gists/{GIST_ID}', headers=headers, timeout=10)
        r.raise_for_status()
        data = r.json()
        if 'files' in data and GIST_FILE_NAME in data['files']:
            content = data['files'][GIST_FILE_NAME]['content']
            return set(json.loads(content).get('filings', []))
        return set()
    except Exception as e:
        logger.error(f"Error loading Gist: {e}")
        return set()


def save_processed_to_gist(processed: set):
    if not GIST_TOKEN or not GIST_ID:
        return
    try:
        headers = {
            'Authorization': f'token {GIST_TOKEN}',
            'Accept': 'application/vnd.github.v3+json'
        }
        payload = {
            'files': {
                GIST_FILE_NAME: {
                    'content': json.dumps({
                        'filings': list(processed),
                        'last_updated': datetime.now().isoformat(),
                        'total_count': len(processed)
                    }, indent=2)
                }
            }
        }
        r = HTTP_SESSION.patch(f'https://api.github.com/gists/{GIST_ID}', headers=headers, json=payload, timeout=10)
        r.raise_for_status()
        logger.info(f"✓ Saved {len(processed)} filings to Gist [{GIST_FILE_NAME}]")
    except Exception as e:
        logger.error(f"Error saving Gist: {e}")

# ============================================
# RSS — POBIERANIE FILINGÓW
# ============================================

def _parse_rss_feed(url: str) -> List[Dict]:
    """Parsuje RSS feed SEC używając feedparser (odporna na błędy XML)."""
    try:
        r = HTTP_SESSION.get(url, timeout=15)
        r.raise_for_status()
        feed = feedparser.parse(r.text)

        if getattr(feed, 'bozo', False):
            logger.warning(f"RSS feed ma drobne błędy XML, ale kontynuujemy ({feed.bozo_exception})")

        filings = []
        for entry in feed.entries:
            if '8-K' not in getattr(entry, 'title', ''):
                continue

            title = entry.title
            link  = getattr(entry, 'link', '')
            entry_id = getattr(entry, 'id', '') or ''

            # --- CIK ---
            cik = None
            for src in (link, entry_id):
                m = re.search(r'/data/(\d+)/', src)
                if m:
                    cik = m.group(1)
                    break
            if not cik:
                m = re.search(r'cik=(\d+)', link + entry_id, re.IGNORECASE)
                if m:
                    cik = m.group(1)

            # --- Accession number ---
            accession = None
            for src in (link, entry_id):
                m = re.search(r'(\d{10}-\d{2}-\d{6})', src)
                if m:
                    accession = m.group(1)
                    break
            if not accession:
                # format bez kresek (18 cyfr)
                for src in (link, entry_id):
                    m = re.search(r'/(\d{18})/', src)
                    if m:
                        d = m.group(1)
                        accession = f"{d[:10]}-{d[10:12]}-{d[12:]}"
                        break

            # --- Ticker z tytułu RSS: "8-K - COMPANY NAME (TICK)" ---
            ticker_from_title = None
            m = re.search(r'\(([A-Z]{1,5}(?:[.\-][A-Z]{1,3})?)\)\s*$', title)
            if m:
                ticker_from_title = m.group(1)

            filings.append({
                'title':            title,
                'link':             link,
                'cik':              cik,
                'accession':        accession,
                'ticker_hint':      ticker_from_title,
                'published':        getattr(entry, 'published', ''),
            })

        return filings

    except Exception as e:
        logger.error(f"Błąd parsowania RSS {url}: {e}")
        return []


def fetch_recent_8k() -> List[Dict]:
    """Pobiera 8-K z RSS — primary + backup (bez duplikatów)."""
    primary = _parse_rss_feed(SEC_RSS_PRIMARY)
    logger.info(f"✓ Primary RSS: {len(primary)} filingów")

    backup = _parse_rss_feed(SEC_RSS_BACKUP)
    logger.info(f"✓ Backup RSS: {len(backup)} filingów")

    # Scal bez duplikatów
    seen = {f['accession'] for f in primary if f['accession']}
    extra = [f for f in backup if f['accession'] and f['accession'] not in seen]
    combined = primary + extra
    logger.info(f"✓ Łącznie unikalnych: {len(combined)} filingów")
    return combined

# ============================================
# DOKUMENT — POBIERANIE I CLEANING
# ============================================

def fetch_document_content(accession: str, cik: str) -> str:
    if not accession or not cik:
        return ""
    try:
        acc_clean = accession.replace('-', '')
        url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_clean}/{accession}.txt"
        logger.info(f"   → Pobieranie: {url}")
        r = HTTP_SESSION.get(url, timeout=15)
        r.raise_for_status()
        return r.text
    except Exception as e:
        logger.warning(f"   ✗ Błąd pobierania dokumentu: {e}")
        return ""


def clean_html_document(raw: str) -> str:
    """Usuwa HTML tagi przez BeautifulSoup — czysty tekst do analizy AI."""
    try:
        soup = BeautifulSoup(raw, 'html.parser')
        for tag in soup(['script', 'style', 'meta', 'link', 'noscript']):
            tag.decompose()
        text = soup.get_text(separator='\n', strip=True)
        # Usuń nadmiar białych znaków
        lines = [l.strip() for l in text.split('\n') if l.strip()]
        text = '\n'.join(lines)
        text = re.sub(r'\n{3,}', '\n\n', text)
        ratio = len(text) / len(raw) * 100 if raw else 0
        logger.info(f"   ✓ HTML cleaning: {len(raw):,} → {len(text):,} znaków ({ratio:.0f}%)")
        return text
    except Exception as e:
        logger.warning(f"   ✗ HTML cleaning failed: {e} — używam surowego tekstu")
        return raw


def extract_item_101_section(clean_text: str, max_chars: int = 12000) -> str:
    """Wyciąga tylko sekcję Item 1.01 z dokumentu."""
    start_patterns = [
        r'Item\s*1\.01',
        r'Item\s*1\s*\.\s*01',
        r'ITEM\s*1\.01',
        r'Entry into a Material Definitive Agreement',
    ]
    end_patterns = [
        r'Item\s*[2-9]\.\d+',
        r'ITEM\s*[2-9]',
        r'SIGNATURES?',
        r'Pursuant\s+to\s+the\s+requirements',
        r'SIGNATURE\s+PAGE',
    ]

    start_pos = None
    for pat in start_patterns:
        m = re.search(pat, clean_text, re.IGNORECASE)
        if m:
            start_pos = m.start()
            break

    if start_pos is None:
        logger.warning("   ⚠ Nie znaleziono Item 1.01 — zwracam pierwsze 12 000 znaków")
        return clean_text[:max_chars]

    end_pos = len(clean_text)
    for pat in end_patterns:
        m = re.search(pat, clean_text[start_pos + 100:], re.IGNORECASE)
        if m:
            candidate = start_pos + 100 + m.start()
            if candidate < end_pos:
                end_pos = candidate

    section = clean_text[start_pos:end_pos].strip()
    if len(section) > max_chars:
        section = section[:max_chars]

    logger.info(f"   ✓ Wyciągnięto sekcję Item 1.01: {len(section):,} znaków")
    return section

# ============================================
# PRE-FILTRY
# ============================================

def has_ma_keywords(content: str) -> bool:
    """Szybka weryfikacja M&A keywords PRZED wywołaniem Groq."""
    for pattern in MA_KEYWORDS:
        if re.search(pattern, content, re.IGNORECASE):
            return True
    return False


def extract_ticker_from_document(content: str) -> Optional[str]:
    """Wyciąga ticker z treści dokumentu (fallback gdy nie ma w tytule RSS)."""
    patterns = [
        r'trading\s+symbol[:\s]+([A-Z]{1,5}(?:[.\-][A-Z]{1,3})?)',
        r'ticker\s+symbol[:\s]+([A-Z]{1,5}(?:[.\-][A-Z]{1,3})?)',
        r'nasdaq[:\s]+([A-Z]{1,5}(?:[.\-][A-Z]{1,3})?)',
        r'nyse[:\s]+([A-Z]{1,5}(?:[.\-][A-Z]{1,3})?)',
        r'nyse\s+(?:american|arca)[:\s]+([A-Z]{1,5})',
    ]
    for pat in patterns:
        m = re.search(pat, content, re.IGNORECASE)
        if m:
            return m.group(1).upper()
    return None


def get_ticker_from_sec_api(cik: str) -> Optional[str]:
    """Pobiera ticker ze SEC Company Tickers API po CIK (fallback)."""
    try:
        cik_clean = str(int(cik))
        r = HTTP_SESSION.get("https://www.sec.gov/files/company_tickers.json", timeout=10)
        r.raise_for_status()
        for _, company in r.json().items():
            if str(company.get('cik_str')) == cik_clean:
                ticker = company.get('ticker')
                if ticker:
                    return ticker.upper()
    except Exception as e:
        logger.warning(f"   ✗ SEC ticker lookup błąd: {e}")
    return None

# ============================================
# REGEX — EKSTRAKCJA WARTOŚCI DEALU
# ============================================

def _to_usd(amount_str: str, unit: Optional[str]) -> Optional[float]:
    """Konwertuje kwotę do USD."""
    try:
        amount = float(amount_str.replace(',', ''))
    except ValueError:
        return None
    if not unit:
        return amount
    u = unit.lower()
    if u in ('billion', 'billions', 'bn', 'b'):
        return amount * 1_000_000_000
    if u in ('million', 'millions', 'mm', 'm'):
        return amount * 1_000_000
    if u in ('thousand', 'thousands', 'k'):
        return amount * 1_000
    return amount


def extract_deal_value_regex(content: str) -> Optional[float]:
    """
    Wyciąga wartość dealu regexem jako WERYFIKACJA dla AI.
    Zwraca wartość w USD lub None.
    """
    number = r'\$\s*([\d,]+\.?\d*)\s*(billion|millions?|bn|mm?|b|k)?'
    patterns = [
        rf'(?:aggregate|total|purchase|transaction|deal|enterprise)\s+(?:consideration|price|value|amount)\s+of\s+approximately\s+{number}',
        rf'(?:aggregate|total|purchase|transaction|deal|enterprise)\s+(?:consideration|price|value|amount)\s+of\s+{number}',
        rf'consideration\s+of\s+approximately\s+{number}',
        rf'valued\s+at\s+approximately\s+{number}',
        rf'{number}\s+in\s+(?:cash|all.cash)',
        rf'offer\s+price\s+of\s+{number}\s+per\s+share',
    ]
    for pat in patterns:
        m = re.search(pat, content, re.IGNORECASE)
        if m:
            val = _to_usd(m.group(1), m.group(2))
            if val and val > 1_000_000:  # min $1M żeby odfiltrować noise
                logger.info(f"   ✓ Regex deal value: ${val:,.0f}")
                return val
    return None

# ============================================
# YAHOO FINANCE
# ============================================

def get_yahoo_data(ticker: str) -> Dict:
    if not YFINANCE_AVAILABLE or not ticker:
        return {}
    try:
        logger.info(f"   → Yahoo Finance: {ticker}")
        stock = yf.Ticker(ticker)
        info  = stock.info
        hist  = stock.history(period="1mo")

        current_price = info.get('currentPrice') or info.get('regularMarketPrice')
        market_cap    = info.get('marketCap')
        avg_volume    = info.get('averageVolume', 1)
        volume_today  = info.get('volume', 0)

        week_change = month_change = 0
        if not hist.empty and len(hist) >= 2:
            w = hist['Close'].iloc[-5] if len(hist) >= 5 else hist['Close'].iloc[0]
            week_change  = ((hist['Close'].iloc[-1] - w) / w * 100) if w else 0
            month_change = ((hist['Close'].iloc[-1] - hist['Close'].iloc[0]) / hist['Close'].iloc[0] * 100)

        mcap_fmt = "Unknown"
        if market_cap:
            mcap_fmt = f"${market_cap/1e9:.2f}B" if market_cap >= 1e9 else f"${market_cap/1e6:.1f}M"

        data = {
            'ticker':               ticker,
            'current_price':        current_price,
            'market_cap':           market_cap,
            'market_cap_formatted': mcap_fmt,
            'volume':               volume_today,
            'avg_volume':           avg_volume,
            'volume_spike':         (volume_today / avg_volume) if avg_volume else 1,
            'week_change_pct':      round(week_change, 2),
            'month_change_pct':     round(month_change, 2),
            'shares_outstanding':   info.get('sharesOutstanding'),
            'float_shares':         info.get('floatShares'),
            'short_ratio':          info.get('shortRatio'),
            'short_percent':        info.get('shortPercentOfFloat'),
            'pe_ratio':             info.get('trailingPE'),
            'beta':                 info.get('beta'),
            'fifty_two_week_high':  info.get('fiftyTwoWeekHigh'),
            'fifty_two_week_low':   info.get('fiftyTwoWeekLow'),
            'institutional_pct':    info.get('heldPercentInstitutions'),
            'analyst_target':       info.get('targetMeanPrice'),
            'recommendation':       info.get('recommendationKey'),
        }
        logger.info(f"   ✓ Yahoo: ${current_price} | MCap: {mcap_fmt}")
        return data

    except Exception as e:
        logger.warning(f"   ✗ Yahoo Finance błąd dla {ticker}: {e}")
        return {'ticker': ticker, 'error': str(e)}


def check_liquidity(yahoo_data: Dict) -> tuple:
    """
    Sprawdza czy spółka spełnia filtry płynności.
    Zwraca (passes: bool, reason: str).
    """
    mcap = yahoo_data.get('market_cap')
    if not mcap:
        return False, "Brak danych MCap"
    if mcap < MIN_MARKET_CAP:
        return False, f"MCap zbyt niska: {yahoo_data['market_cap_formatted']} (min $100M)"
    if mcap > MAX_MARKET_CAP:
        return False, f"MCap zbyt wysoka: {yahoo_data['market_cap_formatted']} (max $10B)"

    avg_vol = yahoo_data.get('avg_volume', 0)
    if avg_vol < MIN_AVG_VOLUME:
        return False, f"Wolumen zbyt niski: {avg_vol:,.0f}/dzień (min 100k)"

    price = yahoo_data.get('current_price', 0) or 0
    dollar_vol = avg_vol * price
    if dollar_vol < MIN_DOLLAR_VOLUME:
        return False, f"Dollar volume zbyt niski: ${dollar_vol:,.0f}/dzień (min $500k)"

    return True, "OK"

# ============================================
# GROQ AI ANALYSIS
# ============================================

def analyze_with_groq(section: str, company: str, yahoo_data: Dict, deal_value_regex: Optional[float]) -> Optional[Dict]:
    if not GROQ_API_KEY:
        logger.warning("Brak GROQ_API_KEY")
        return None

    # --- kontekst Yahoo Finance ---
    yf_ctx = "\nMARKET DATA: Not available\n"
    if yahoo_data and not yahoo_data.get('error'):
        ticker = yahoo_data.get('ticker', 'N/A')
        price  = yahoo_data.get('current_price', 'N/A')
        mcap   = yahoo_data.get('market_cap_formatted', 'N/A')
        shares = yahoo_data.get('shares_outstanding')
        short_pct   = yahoo_data.get('short_percent', 0) or 0
        short_ratio = yahoo_data.get('short_ratio', 'N/A')
        vol_spike   = yahoo_data.get('volume_spike', 1)
        w_chg       = yahoo_data.get('week_change_pct', 0)

        shares_fmt = f"{shares:,.0f}" if shares else "N/A"
        yf_ctx = f"""
MARKET DATA:
Ticker: {ticker} | Price: ${price} | MCap: {mcap}
Shares Outstanding: {shares_fmt}
Volume: {vol_spike:.1f}x avg | 1W Change: {w_chg:+.1f}%
Short % of Float: {short_pct*100:.1f}% | Short Ratio: {short_ratio}
Beta: {yahoo_data.get('beta', 'N/A')} | Institutional: {(yahoo_data.get('institutional_pct') or 0)*100:.0f}%
"""

    # --- dane z regex (zakotwiczenie dla AI) ---
    regex_anchor = ""
    if deal_value_regex:
        regex_anchor = f"\nREGEX PRE-EXTRACTION (verified):\n- Deal value found in document: ${deal_value_regex:,.0f}\n"
    else:
        regex_anchor = "\nREGEX PRE-EXTRACTION: No deal value found by regex — extract from document text.\n"

    # --- short squeeze note ---
    short_note = ""
    if yahoo_data and (yahoo_data.get('short_percent', 0) or 0) > 0.15:
        short_note = f"\n⚠️ HIGH SHORT INTEREST ({(yahoo_data.get('short_percent',0))*100:.1f}% of float). If this is an acquisition → MASSIVE short squeeze likely → increase impact_score by 1-2 points.\n"

    prompt = f"""You are an expert M&A analyst. Analyze this SEC 8-K Item 1.01 filing.

COMPANY: {company}
{yf_ctx}
{regex_anchor}
{short_note}

ANALYSIS RULES:
1. DEAL TYPE: Only mark as "acquisition" if target company is being fully bought out. Mark as "partnership" or "contract" for commercial agreements.
2. PREMIUM CALCULATION (CORRECT METHOD):
   - For full company acquisitions: Premium % = (Offer Price Per Share - Current Stock Price) / Current Stock Price × 100
   - Offer Price Per Share = Total Deal Value / Shares Outstanding
   - NEVER compare Total Deal Value directly to Market Cap as % premium — that is mathematically wrong
   - If it's a partial acquisition (division/asset), set premium_pct to null
3. DEAL VALUE: Use EXACT numbers from document. If regex found ${f"{deal_value_regex:,.0f}" if deal_value_regex else "N/A"} — confirm or explain discrepancy.
4. If information is NOT in the document → write null. DO NOT invent numbers.
5. SHORT SQUEEZE: If short_pct > 15% and deal_type = "acquisition" → note squeeze risk in reasoning.

IMPACT SCORE (1-10):
9-10 MEGA:   Full acquisition, all-cash, premium >40%, or short squeeze setup
7-8 MAJOR:   Full acquisition mixed/stock, premium 20-40%, or strategic acquisition with clear synergies
5-6 STANDARD: Partial acquisition, major partnership with financial terms, large contract
1-4 LOW:     Commercial agreement, minor partnership, MOU without binding terms → DO NOT send alert

DOCUMENT (Item 1.01 section):
{section}

LANGUAGE: Write strategic_rationale, all items in key_points, risks, sympathy_plays arrays, and reasoning in Polish (język polski). Keep all JSON keys and fixed enum values (verdict, deal_type, deal_structure, short_squeeze_risk) in English.

Respond ONLY with valid JSON:
{{
  "impact_score": <1-10>,
  "deal_type": "acquisition|merger|partnership|contract|asset_purchase|other",
  "is_full_acquisition": <true if entire company being acquired, else false>,
  "target_company": "<name or null>",
  "acquirer": "<name or null>",
  "deal_value": "<e.g. $2.1B or undisclosed>",
  "deal_value_usd": <number in USD or null>,
  "deal_value_source": "regex_confirmed|document_only|undisclosed",
  "offer_price_per_share": <number or null>,
  "current_price": <from Yahoo Finance or null>,
  "premium_pct": <correctly calculated % or null>,
  "premium_calculation": "<step-by-step or null>",
  "upside_to_offer": "<e.g. +34% or null>",
  "deal_structure": "all-cash|all-stock|mixed|undisclosed",
  "short_squeeze_risk": "high|medium|low|none",
  "verdict": "MEGA|MAJOR|STANDARD|LOW",
  "short_term_move": "<e.g. +25-35% or null>",
  "confidence": <1-10>,
  "strategic_rationale": "<why this deal makes sense>",
  "key_points": ["<point1>", "<point2>", "<point3>"],
  "risks": ["<risk1>", "<risk2>"],
  "sympathy_plays": ["<TICK: reason>"],
  "leak_detected": <true if unusual pre-filing volume/price action>,
  "reasoning": "<full justification>"
}}"""

    try:
        r = HTTP_SESSION.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": "llama-3.3-70b-versatile",
                "messages": [
                    {"role": "system", "content": "You are an expert M&A analyst. Respond ONLY with valid JSON, no extra text. Never hallucinate numbers not found in the document."},
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0.2,
                "max_tokens": 1200
            },
            timeout=45
        )
        r.raise_for_status()
        text = r.json()['choices'][0]['message']['content'].strip()

        # Wyciągnij JSON z odpowiedzi
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if m:
            result = json.loads(m.group(0))
            logger.info(f"   ✓ Groq: impact={result.get('impact_score')}/10 | {result.get('deal_type')} | premium={result.get('premium_pct')}%")
            return result

        logger.warning("   ✗ Groq nie zwrócił poprawnego JSON")
        return None

    except Exception as e:
        logger.error(f"   ✗ Groq błąd: {e}")
        return None

# ============================================
# DISCORD
# ============================================

def _poland_time(utc_str: str) -> str:
    try:
        dt = datetime.strptime(utc_str.split('.')[0].replace('Z', ''), '%Y-%m-%dT%H:%M:%S')
        dt = dt.replace(tzinfo=timezone.utc)
        offset = 2 if 3 < dt.month < 10 else 1
        tz_name = "CEST" if offset == 2 else "CET"
        pl = dt.astimezone(timezone(timedelta(hours=offset)))
        return pl.strftime(f'%Y-%m-%d %H:%M {tz_name}')
    except Exception:
        return utc_str


def send_discord_alert(filing: Dict, analysis: Dict, yahoo_data: Dict, priority: str):
    color_map = {
        "MEGA":     (15158332, "🔴🔴🔴"),
        "MAJOR":    (16753920, "🟠"),
        "STANDARD": (16776960, "🟡"),
    }
    color, emoji = color_map.get(priority, (0, "⚪"))
    webhook_url = DISCORD_WEBHOOK_V2
    if not webhook_url:
        logger.warning("Brak DISCORD_WEBHOOK_V2")
        return

    target   = analysis.get('target_company') or filing.get('company', 'Unknown')
    acquirer = analysis.get('acquirer', 'Unknown')
    ticker   = yahoo_data.get('ticker', '')

    title = f"{emoji} [v2] {priority} M&A — {ticker or target}"

    desc = f"**{target}** → **{acquirer}**\n\n"

    if analysis.get('deal_value'):
        desc += f"💰 **Wartość transakcji:** {analysis['deal_value']}"
        if analysis.get('deal_value_source') == 'regex_confirmed':
            desc += " ✅ (potwierdzone regexem)"
        desc += "\n"

    if analysis.get('premium_pct') is not None:
        desc += f"🔥 **Premia:** {analysis['premium_pct']}%"
        if analysis.get('premium_calculation'):
            desc += f" *(obliczenie: {analysis['premium_calculation'][:80]})*"
        desc += "\n"

    if analysis.get('offer_price_per_share') and yahoo_data.get('current_price'):
        desc += f"🎯 **Oferta:** ${analysis['offer_price_per_share']}/akcję vs ${yahoo_data['current_price']} aktualny kurs\n"

    if analysis.get('upside_to_offer'):
        desc += f"📈 **Potencjał do oferty:** {analysis['upside_to_offer']}\n"

    squeeze = analysis.get('short_squeeze_risk', 'none')
    SQUEEZE_PL = {'high': 'WYSOKI', 'medium': 'ŚREDNI', 'low': 'NISKI', 'none': 'BRAK'}
    if squeeze in ('high', 'medium'):
        short_pct = (yahoo_data.get('short_percent', 0) or 0) * 100
        desc += f"⚡ **Ryzyko short squeeze:** {SQUEEZE_PL.get(squeeze, squeeze.upper())} ({short_pct:.1f}% krótkich pozycji)\n"

    desc += f"\n**OCENA AI: {analysis.get('impact_score', 0)}/10** | **Pewność: {analysis.get('confidence', 0)}/10**\n"
    desc += f"**Werdykt:** {analysis.get('verdict', 'N/A')} | **Szacowany ruch:** {analysis.get('short_term_move', 'N/A')}\n"

    STRUCTURE_PL = {
        'all-cash':    'Tylko gotówka',
        'all-stock':   'Wymiana akcji',
        'mixed':       'Mieszana (gotówka + akcje)',
        'undisclosed': 'Nie ujawniono',
    }
    structure = analysis.get('deal_structure', 'N/A')
    desc += f"**Forma płatności:** {STRUCTURE_PL.get(structure, structure)}\n"

    fields = []

    if yahoo_data and not yahoo_data.get('error'):
        yf_text = ""
        if yahoo_data.get('market_cap_formatted'):
            yf_text += f"**Wycena:** {yahoo_data['market_cap_formatted']}\n"
        if yahoo_data.get('current_price'):
            yf_text += f"**Kurs:** ${yahoo_data['current_price']}\n"
        vs = yahoo_data.get('volume_spike', 1)
        if vs > 1.5:
            yf_text += f"🔊 **Wolumen:** {vs:.1f}x śr.\n"
        if yahoo_data.get('week_change_pct') is not None:
            chg = yahoo_data['week_change_pct']
            yf_text += f"{'📈' if chg > 0 else '📉'} **Tydzień:** {chg:+.2f}%\n"
        if fields or yf_text:
            fields.append({"name": "📊 Dane rynkowe", "value": yf_text or "—", "inline": True})

    if analysis.get('strategic_rationale'):
        fields.append({"name": "🧠 Uzasadnienie strategiczne", "value": analysis['strategic_rationale'][:300], "inline": False})

    if analysis.get('key_points'):
        fields.append({"name": "📌 Kluczowe punkty", "value": "\n".join(f"• {p}" for p in analysis['key_points'][:3]), "inline": False})

    if analysis.get('risks'):
        fields.append({"name": "⚠️ Ryzyka", "value": "\n".join(f"• {r}" for r in analysis['risks'][:2]), "inline": True})

    if analysis.get('sympathy_plays'):
        fields.append({"name": "🔗 Powiązane spółki", "value": "\n".join(f"• {s}" for s in analysis['sympathy_plays'][:3]), "inline": True})

    if analysis.get('leak_detected'):
        fields.append({"name": "🕵️ Wykryto przeciek", "value": analysis.get('reasoning', '')[:200], "inline": False})

    fields.append({"name": "🔗 Zgłoszenie SEC", "value": f"[Otwórz w EDGAR]({filing.get('link', '#')})", "inline": True})
    fields.append({"name": "⏰ Czas (PL)", "value": _poland_time(datetime.utcnow().isoformat()), "inline": True})

    payload = {"embeds": [{"title": title, "description": desc, "color": color, "fields": fields}]}

    try:
        r = HTTP_SESSION.post(webhook_url, json=payload, timeout=10)
        r.raise_for_status()
        logger.info(f"✓ Discord [{priority}]: {ticker or target}")
    except Exception as e:
        logger.error(f"✗ Discord błąd: {e}")

# ============================================
# MAIN
# ============================================

def scan_ma_deals():
    logger.info("=" * 60)
    logger.info(f"SEC M&A Scanner v2.0 | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)

    processed = load_processed_from_gist()
    logger.info(f"Już przetworzone: {len(processed)} filingów")

    filings = fetch_recent_8k()
    if not filings:
        logger.warning("Brak filingów z RSS")
        return

    new_alerts = 0
    skipped_processed = 0
    skipped_no_keywords = 0
    skipped_liquidity = 0
    skipped_low_impact = 0

    for filing in filings:
        accession = filing.get('accession')
        cik       = filing.get('cik')

        if not accession:
            continue

        if accession in processed:
            skipped_processed += 1
            continue

        logger.info(f"\n🔍 {filing.get('title', '')[:70]}")
        logger.info(f"   Accession: {accession} | CIK: {cik}")

        # --- Pobierz dokument ---
        raw_content = fetch_document_content(accession, cik)
        if not raw_content:
            processed.add(accession)
            continue

        # --- Szybki test: czy jest Item 1.01? ---
        if not re.search(r'item\s*1\.01', raw_content, re.IGNORECASE):
            logger.info("   ↳ Brak Item 1.01 — pomijam")
            processed.add(accession)
            continue

        # --- HTML cleaning ---
        clean_text = clean_html_document(raw_content)

        # --- Wyciągnij sekcję Item 1.01 ---
        section = extract_item_101_section(clean_text, max_chars=12000)

        # --- PRE-FILTER: M&A keywords przed wywołaniem Groq ---
        if not has_ma_keywords(section):
            logger.info("   ↳ Brak M&A keywords (prawdopodobnie zwykła umowa handlowa) — pomijam")
            skipped_no_keywords += 1
            processed.add(accession)
            continue

        logger.info("   ✓ M&A keywords znalezione — kontynuuję analizę")

        # --- Regex: wartość dealu jako weryfikacja dla AI ---
        deal_value_regex = extract_deal_value_regex(section)

        # --- Ticker lookup (priorytet: tytuł RSS > dokument > SEC API) ---
        ticker = filing.get('ticker_hint')
        if not ticker:
            ticker = extract_ticker_from_document(clean_text)
        if not ticker and cik:
            ticker = get_ticker_from_sec_api(cik)
            time.sleep(0.3)

        # --- Yahoo Finance ---
        yahoo_data = {}
        if ticker:
            yahoo_data = get_yahoo_data(ticker)
            time.sleep(0.5)

            # --- Filtr płynności ---
            passes, reason = check_liquidity(yahoo_data)
            if not passes:
                logger.info(f"   ↳ Filtr płynności: {reason} — pomijam")
                skipped_liquidity += 1
                processed.add(accession)
                continue
        else:
            logger.info("   ⚠ Brak tickera — analiza AI bez danych rynkowych")

        # --- Uzupełnij company name ---
        company = "Unknown Company"
        for line in clean_text.split('\n')[:50]:
            if 'COMPANY CONFORMED NAME' in line.upper():
                company = line.split(':')[-1].strip()
                break

        filing['company'] = company

        # --- Groq AI analysis ---
        analysis = analyze_with_groq(section, company, yahoo_data, deal_value_regex)

        if not analysis:
            logger.warning("   ↳ Groq analiza nieudana — pomijam")
            processed.add(accession)
            continue

        # --- Routing na Discord ---
        impact = analysis.get('impact_score', 0)

        if impact >= 9:
            send_discord_alert(filing, analysis, yahoo_data, "MEGA")
            new_alerts += 1
        elif impact >= 7:
            send_discord_alert(filing, analysis, yahoo_data, "MAJOR")
            new_alerts += 1
        elif impact >= 5:
            send_discord_alert(filing, analysis, yahoo_data, "STANDARD")
            new_alerts += 1
        else:
            logger.info(f"   ↳ Low impact ({impact}/10) — pomijam alert")
            skipped_low_impact += 1

        processed.add(accession)
        time.sleep(1)

    save_processed_to_gist(processed)

    logger.info("\n" + "=" * 60)
    logger.info(f"✓ Scan v2 complete: {new_alerts} alertów wysłanych")
    logger.info(f"  Już przetworzone:  {skipped_processed}")
    logger.info(f"  Brak M&A keywords: {skipped_no_keywords}")
    logger.info(f"  Filtr płynności:   {skipped_liquidity}")
    logger.info(f"  Low impact (<5):   {skipped_low_impact}")
    logger.info("=" * 60)


if __name__ == "__main__":
    scan_ma_deals()
