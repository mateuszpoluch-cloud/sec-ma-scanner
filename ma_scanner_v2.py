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
from email.utils import parsedate_to_datetime
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

MIN_MARKET_CAP    = 100_000_000       # $100M (min — odfiltruj mikro-caps)
MIN_AVG_VOLUME    = 100_000           # 100k akcji/dzień
MIN_DOLLAR_VOLUME = 500_000           # $500k/dzień
# MAX_MARKET_CAP usunięty — duży acquirer (>$10B) też może przejmować małe spółki

# ============================================
# M&A KEYWORD PRE-FILTER
# ============================================

# Mocne sygnały — jeden wystarczy, żeby przepuścić filing do Groq
MA_KEYWORDS_STRONG = [
    r'merger\s+agreement',
    r'acquisition\s+agreement',
    r'to\s+be\s+acquired',
    r'will\s+acquire',
    r'has\s+agreed\s+to\s+(?:acquire|merge)',
    r'all.cash\s+(?:offer|transaction|deal|merger)',
    r'tender\s+offer',
    r'going\s+private',
    r'take.private',
    r'leveraged\s+buyout',
    r'\blbo\b',
    r'acquired\s+by\b',
    r'acquisition\s+of\s+(?:all|the)\s+(?:outstanding|issued)',
    r'merger\s+with\b',
    r'agree(?:d|s)?\s+to\s+(?:acquire|merge)\b',
    r'definitive\s+(?:merger|acquisition|business\s+combination)\s+agreement',
]

# Słabe sygnały — przepuszczają TYLKO gdy brak wzorców wykluczających
MA_KEYWORDS_WEAK = [
    r'definitive\s+agreement',
    r'cash\s+consideration',
    r'purchase\s+(?:price|agreement)',
    r'offer\s+price',
    r'business\s+combination',
]

# Wzorce wykluczające — jeśli dokument zawiera którykolwiek z nich, słabe keywords są ignorowane
EXCLUSION_PATTERNS = [
    r'revolving\s+(?:credit|loan)',
    r'credit\s+(?:facility|agreement|line)',
    r'term\s+loan',
    r'line\s+of\s+credit',
    r'credit\s+and\s+security\s+agreement',
    r'senior\s+secured\s+(?:credit|revolving|term)',
    r'indenture',
    r'promissory\s+note',
    r'employment\s+agreement',
    r'executive\s+(?:employment|compensation|severance)',
    r'change\s+in\s+control\s+agreement',
    r'registration\s+rights\s+agreement',
    r'at.the.market\s+(?:offering|program)',
    r'equity\s+distribution\s+agreement',
    r'underwriting\s+agreement',
    r'loan\s+(?:agreement|amendment)',
    r'security\s+agreement',
]

# ============================================
# HTTP SESSION Z RETRY
# ============================================

_retry = Retry(
    total=3,
    backoff_factor=1,
    status_forcelist=[500, 502, 503, 504],  # NIE 429 — Groq zwraca Retry-After: 1200s co blokuje cały job
    allowed_methods=["GET", "POST"],
    respect_retry_after_header=False,  # ignoruj Retry-After header
)
HTTP_SESSION = requests.Session()
HTTP_SESSION.mount("https://", HTTPAdapter(max_retries=_retry))
HTTP_SESSION.headers.update({"User-Agent": USER_AGENT})

# Osobna sesja dla Groq — bez retry, krótki timeout, nie czeka na rate limit
_groq_retry = Retry(total=1, backoff_factor=0, status_forcelist=[500, 502, 503, 504])
GROQ_SESSION = requests.Session()
GROQ_SESSION.mount("https://", HTTPAdapter(max_retries=_groq_retry))

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

            # Czas publikacji → konwertuj RFC 2822 na ISO UTC żeby _poland_time mogło go obsłużyć
            pub_raw = getattr(entry, 'published', '')
            pub_iso = ''
            if pub_raw:
                try:
                    pub_iso = parsedate_to_datetime(pub_raw).astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')
                except Exception:
                    pub_iso = ''

            filings.append({
                'title':            title,
                'link':             link,
                'cik':              cik,
                'accession':        accession,
                'ticker_hint':      ticker_from_title,
                'published':        pub_raw,
                'published_iso':    pub_iso,
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
    """
    Szybka weryfikacja M&A keywords PRZED wywołaniem Groq.
    Logika:
      - mocny keyword → od razu przepuść (bez względu na exclusions)
      - słaby keyword + brak exclusion → przepuść
      - słaby keyword + exclusion (kredyt, zatrudnienie, itp.) → odrzuć
    """
    # Mocne sygnały — wystarczy jeden
    for pattern in MA_KEYWORDS_STRONG:
        if re.search(pattern, content, re.IGNORECASE):
            return True

    # Sprawdź czy dokument jest wykluczonego typu (kredyt, zatrudnienie, itp.)
    is_excluded = any(
        re.search(p, content, re.IGNORECASE) for p in EXCLUSION_PATTERNS
    )
    if is_excluded:
        return False  # Słabe keywords w takim dokumencie = false positive

    # Słabe sygnały — OK tylko bez wykluczeń
    for pattern in MA_KEYWORDS_WEAK:
        if re.search(pattern, content, re.IGNORECASE):
            return True

    return False


def get_sec_filing_metadata(cik: str, accession: str) -> Dict:
    """
    Pobiera z SEC Submissions API w jednym zapytaniu:
    - ticker spółki
    - items konkretnego filingu (sprawdzenie Item 1.01 bez pobierania dokumentu)
    Zastępuje oddzielne get_ticker_from_sec_api + sprawdzenie dokumentu.
    """
    try:
        cik_padded = str(int(cik)).zfill(10)
        url = f"https://data.sec.gov/submissions/CIK{cik_padded}.json"
        r = HTTP_SESSION.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()

        # Ticker
        tickers = data.get('tickers', [])
        ticker = tickers[0].upper() if tickers else None

        # Items dla konkretnego accession
        filings_recent = data.get('filings', {}).get('recent', {})
        acc_list   = filings_recent.get('accessionNumber', [])
        items_list = filings_recent.get('items', [])
        has_item_101 = None
        items_str = ''
        if accession in acc_list:
            idx = acc_list.index(accession)
            items_str = str(items_list[idx]) if idx < len(items_list) else ''
            has_item_101 = ('1.01' in items_str) if items_str else None

        logger.info(f"   ✓ SEC API: ticker={ticker} | items='{items_str}'")
        return {'ticker': ticker, 'has_item_101': has_item_101}

    except Exception as e:
        logger.warning(f"   ✗ SEC metadata błąd: {e}")
        return {'ticker': None, 'has_item_101': None}


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
    """
    Pobiera ticker z SEC EDGAR Submissions API (bezpośredni endpoint per CIK).
    Dużo szybsze niż pobieranie całego company_tickers.json (~500KB).
    """
    try:
        cik_padded = str(int(cik)).zfill(10)
        url = f"https://data.sec.gov/submissions/CIK{cik_padded}.json"
        r = HTTP_SESSION.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        tickers = data.get('tickers', [])
        if tickers:
            logger.info(f"   ✓ SEC Submissions API: {tickers}")
            return tickers[0].upper()
        # Fallback: spróbuj z pola exchanges
        exchanges = data.get('exchanges', [])
        if exchanges:
            logger.info(f"   ✓ SEC Submissions API (name fallback): {data.get('name', '')}")
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

        current_price = (info.get('currentPrice') or info.get('regularMarketPrice')
                         or info.get('previousClose') or info.get('regularMarketPreviousClose'))
        market_cap    = info.get('marketCap')
        if not market_cap:
            shares     = info.get('sharesOutstanding')
            prev_close = info.get('previousClose') or info.get('regularMarketPreviousClose')
            if shares and prev_close:
                market_cap = shares * prev_close
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
    # Brak górnego limitu MCap — duży acquirer (>$10B) też może przejmować małe spółki

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
6. FILER ROLE: Determine if the company filing this 8-K is the TARGET (being acquired) or the ACQUIRER (doing the buying).
   - filer_role = "target" if the filing company IS being acquired
   - filer_role = "acquirer" if the filing company IS doing the acquisition
   - filer_role = "unknown" if unclear from the document
7. TICKER SYMBOLS (critical for trading):
   - target_ticker: NYSE/NASDAQ ticker of the TARGET company — this stock will jump to offer price
   - acquirer_ticker: NYSE/NASDAQ ticker of the ACQUIRER company
   - Use only the raw symbol without exchange prefix (e.g. "BIOM" not "NASDAQ:BIOM")
   - Set null if company is private, foreign, or ticker unknown
8. CREDIT & FINANCIAL AGREEMENTS (critical — must score LOW):
   - Revolving credit facility, term loan, credit line, credit agreement → ALWAYS score 1-3 (LOW)
   - Indenture, promissory note, security agreement, loan amendment → ALWAYS score 1-3 (LOW)
   - Employment agreement, executive compensation, severance → ALWAYS score 1-3 (LOW)
   - At-the-market equity offering, underwriting agreement, registration rights → ALWAYS score 1-3 (LOW)
   - These are NEVER M&A transactions regardless of deal size mentioned.
9. FREE-FORM TEXT — RULES (critical):
   - alert_headline: ONE sentence max. Deal type + strategic significance only. NO numbers/prices/percentages. Polish.
     Example: "Brink's przejmuje NCR Atleos w transakcji gotówkowej — NATL staje się spółką prywatną"
   - analyst_verdict: Your EXPERT PREDICTION and REASONING as a senior M&A analyst. This is the most important field.
     → 5-8 sentences in Polish. NO dollar amounts, NO percentages, NO share prices (those are shown separately).
     → START with an explicit trading recommendation on the FIRST LINE:
        "KUPUJ TARGET [ticker]" / "SPEKULATYWNIE KUPUJ TARGET [ticker]" / "OBSERWUJ — ryzyko niedomknięcia" / "UNIKAJ"
        followed by one-line justification of the recommendation.
     → Then REASON from your knowledge of:
        • Historical M&A patterns: how similar deals in this sector/size typically played out
        • Regulatory environment: what antitrust history says about this type of combination
        • Deal structure signals: what all-cash vs stock deals historically mean for closure probability
        • Market dynamics: what institutional ownership, short interest, sector trends suggest
        • What the market is likely MISSING or OVERPRICING in this situation
        • Your probability assessment: is this deal likely to close, get bumped, or fall apart — and WHY
     → Be specific, opinionated, and confident. Do not hedge everything. Give a real take.
     → Example quality: "KUPUJ TARGET NATL — transakcja gotówkowa z silnym nabywcą, historycznie bardzo wysoka
        pewność zamknięcia. Transakcje all-cash w sektorze usług finansowych rzadko upadają po ogłoszeniu, bo
        nabywca traci reputację przy wycofaniu. Podobne przejęcia w segmencie logistyki gotówkowej przeszły przez
        regulatorów z minimalnymi wymaganiami dywersytury. Rynek prawdopodobnie przecenia ryzyko antymonopolowe,
        co tworzy atrakcyjny spread dla strategii arbitrażu fuzyjnego."

IMPACT SCORE (1-10):
9-10 MEGA:   Full acquisition, all-cash, premium >40%, or short squeeze setup
7-8 MAJOR:   Full acquisition mixed/stock, premium 20-40%, or strategic acquisition with clear synergies
5-6 STANDARD: Partial acquisition of a company, definitive merger agreement (company vs company)
1-4 LOW:     Credit facility, loan, employment contract, commercial agreement, MOU, partnership → DO NOT send alert

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
  "filer_role": "target|acquirer|unknown",
  "target_ticker": "<ticker or null>",
  "acquirer_ticker": "<ticker or null>",
  "alert_headline": "<1 sentence in Polish, NO numbers/prices/percentages — deal type + strategic significance>",
  "analyst_verdict": "<5-8 sentences in Polish, NO dollar amounts/percentages/prices — expert prediction, historical patterns, regulatory outlook, probability assessment, what market is missing>",
  "reasoning": "<full justification>"
}}"""

    try:
        r = GROQ_SESSION.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": "llama-3.3-70b-versatile",
                "messages": [
                    {"role": "system", "content": "You are an expert M&A analyst. Respond ONLY with valid JSON, no extra text. Never hallucinate numbers not found in the document."},
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0.3,
                "max_tokens": 1800
            },
            timeout=30  # krótszy timeout — Groq odpowiada w <10s, 30s to dużo marginesu
        )
        if r.status_code == 429:
            logger.warning("   ✗ Groq rate limit (429) — pomijam ten filing")
            return None
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

def _truncate_sentence(text: str, max_len: int = 1020) -> str:
    """Ucina tekst na granicy ostatniego pełnego zdania w limicie max_len znaków."""
    if len(text) <= max_len:
        return text
    chunk = text[:max_len]
    # Szukaj ostatniego końca zdania (. ! ?) żeby nie ciąć w środku
    last = max(chunk.rfind('. '), chunk.rfind('! '), chunk.rfind('? '), chunk.rfind('.\n'))
    if last > max_len // 2:
        return chunk[:last + 1]
    return chunk.rstrip() + '…'

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


def send_discord_alert(filing: Dict, analysis: Dict, target_yahoo: Dict, acquirer_yahoo: Dict, priority: str):
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

    target_name  = analysis.get('target_company') or filing.get('company', 'Unknown')
    acquirer_name = analysis.get('acquirer', 'Unknown')
    target_ticker  = target_yahoo.get('ticker', '')
    acquirer_ticker = acquirer_yahoo.get('ticker', '')

    # Tytuł i TradingView link → zawsze dla TARGETU (ten skacze)
    display_ticker = target_ticker or analysis.get('target_ticker') or acquirer_ticker
    title = f"{emoji} [v2] {priority} M&A — {display_ticker or target_name}"
    tv_url = f"https://www.tradingview.com/symbols/{display_ticker}/" if display_ticker else None

    # --- Nagłówek: AI headline (jakościowy, bez liczb) ---
    headline = analysis.get('alert_headline', '')
    desc = f"*{headline}*\n\n" if headline else ""

    # --- Spółki + TradingView ---
    desc += f"🎯 **Cel:** {target_name}"
    if target_ticker:
        desc += f" `({target_ticker})`"
    desc += f"　　🏢 **Nabywca:** {acquirer_name}"
    if acquirer_ticker:
        desc += f" `({acquirer_ticker})`"
    desc += "\n"
    if tv_url:
        desc += f"📊 **[{display_ticker} — wykres TradingView]({tv_url})**\n"
    desc += "\n"

    # --- Wartość transakcji — Groq string → fallback regex (zawsze zweryfikowany) ---
    deal_val_str = (analysis.get('deal_value') or '').strip()
    deal_val_regex = filing.get('_deal_value_regex')
    if deal_val_str and deal_val_str.lower() not in ('null', 'undisclosed', 'none', ''):
        desc += f"💰 **Wartość:** {deal_val_str}"
        if analysis.get('deal_value_source') == 'regex_confirmed':
            desc += " ✅"
        desc += "\n"
    elif deal_val_regex:
        desc += f"💰 **Wartość:** ${deal_val_regex:,.0f} ✅ *(regex)*\n"

    # Oferta per akcję (z dokumentu przez Groq) + kurs (Yahoo)
    target_price = target_yahoo.get('current_price')
    offer_price  = analysis.get('offer_price_per_share')
    if offer_price and target_price:
        desc += f"🎯 **Oferta:** ${offer_price}/akcję  |  **Kurs:** ${target_price}\n"

    # Premia: Groq liczy z dokumentu (wymaga pre-announcement price)
    try:
        prem = float(analysis['premium_pct'])
        desc += f"🔥 **Premia:** +{prem:.1f}%\n"
    except (TypeError, ValueError, KeyError):
        pass

    # Upside: Python liczy z Yahoo (kurs aktualny → cena oferty) — brak halucynacji
    py_upside = analysis.get('_py_upside_pct')
    if py_upside is not None:
        icon = "📈" if py_upside >= 0 else "📉"
        desc += f"{icon} **Potencjał do oferty:** {py_upside:+.1f}% *(kurs aktualny → cena oferty przy zamknięciu)*\n"

    # Short squeeze
    squeeze = analysis.get('short_squeeze_risk', 'none')
    SQUEEZE_PL = {'high': 'WYSOKI', 'medium': 'ŚREDNI', 'low': 'NISKI', 'none': 'BRAK'}
    if squeeze in ('high', 'medium'):
        short_pct = (target_yahoo.get('short_percent', 0) or 0) * 100
        desc += f"⚡ **Short squeeze:** {SQUEEZE_PL.get(squeeze, squeeze.upper())} ({short_pct:.1f}% flotu)\n"

    # --- Sygnał tradingowy — wynika z mechaniki dealu, nie z AI ---
    filer_role    = analysis.get('filer_role', 'unknown')
    is_full_acq   = analysis.get('is_full_acquisition', False)
    deal_struct   = analysis.get('deal_structure', '')
    desc += "\n"
    if filer_role == 'target' and is_full_acq:
        if deal_struct == 'all-cash':
            desc += "🟢 **SYGNAŁ: KUPUJ TARGET** — gotówkowy buyout, kurs zmierza do stałej ceny oferty\n"
        elif deal_struct == 'all-stock':
            desc += "🟡 **SYGNAŁ: SPEKULATYWNIE KUPUJ TARGET** — fuzja przez akcje, wartość zależy od kursu acquirera\n"
        elif deal_struct == 'mixed':
            desc += "🟡 **SYGNAŁ: SPEKULATYWNIE KUPUJ TARGET** — transakcja mieszana (gotówka + akcje)\n"
        else:
            desc += "🟡 **SYGNAŁ: OBSERWUJ TARGET** — struktura transakcji do potwierdzenia\n"
    elif filer_role == 'acquirer':
        target_is_public = bool(analysis.get('target_ticker') or (target_yahoo and target_yahoo.get('current_price')))
        if target_is_public:
            desc += "🔵 **SYGNAŁ: OBSERWUJ ACQUIRERA** — nabywca zazwyczaj traci krótkoterminowo; kupuj TARGET (skacze do oferty)\n"
        else:
            desc += "🔵 **SYGNAŁ: OBSERWUJ ACQUIRERA** — target prywatny, brak spreadu arbitrażowego; oceniaj wpływ przejęcia na CXDO\n".replace('CXDO', display_ticker or 'acquirera')
    elif is_full_acq:
        desc += "🟢 **SYGNAŁ: KUPUJ TARGET** — przejęcie całej spółki\n"
    else:
        desc += "⚪ **SYGNAŁ: OBSERWUJ** — częściowe przejęcie lub nieznana rola filer\n"

    # Ocena AI
    STRUCTURE_PL = {
        'all-cash':    'Gotówka',
        'all-stock':   'Akcje',
        'mixed':       'Gotówka + akcje',
        'undisclosed': 'Nie ujawniono',
    }
    structure = STRUCTURE_PL.get(analysis.get('deal_structure', ''), analysis.get('deal_structure', 'N/A'))
    desc += f"**AI: {analysis.get('impact_score', 0)}/10** · **Pewność: {analysis.get('confidence', 0)}/10** · {structure}\n"

    fields = []

    # Dane targetu (cel przejęcia — ten skacze)
    if target_yahoo and not target_yahoo.get('error'):
        yf_text = ""
        if target_yahoo.get('market_cap_formatted'):
            yf_text += f"**Wycena:** {target_yahoo['market_cap_formatted']}\n"
        if target_yahoo.get('current_price'):
            yf_text += f"**Kurs:** ${target_yahoo['current_price']}\n"

        vs = target_yahoo.get('volume_spike', 1)
        if vs >= 10:
            vol_label = "ekstremalny — instytucje w pozycji"
        elif vs >= 5:
            vol_label = "bardzo wysoki — rynek się pozycjonuje"
        elif vs >= 2:
            vol_label = "podwyższony"
        else:
            vol_label = None
        if vs > 1.5:
            yf_text += f"🔊 **Wolumen:** {vs:.1f}x śr." + (f" *({vol_label})*" if vol_label else "") + "\n"

        if target_yahoo.get('week_change_pct') is not None:
            chg = target_yahoo['week_change_pct']
            chg_label = " *(kurs już ruszył po ogłoszeniu)*" if abs(chg) > 5 else ""
            yf_text += f"{'📈' if chg > 0 else '📉'} **Tydzień:** {chg:+.2f}%{chg_label}\n"

        if target_yahoo.get('short_percent'):
            sp = target_yahoo['short_percent'] * 100
            if sp >= 15:
                short_label = "wysoki — squeeze przy zamknięciu"
            elif sp >= 7:
                short_label = "umiarkowany — shorty muszą odkupić"
            else:
                short_label = "niski"
            yf_text += f"🩳 **Short:** {sp:.1f}% flotu *({short_label})*\n"

        inst = (target_yahoo.get('institutional_pct') or 0) * 100
        if inst > 0:
            inst_label = "akceptacja prawie pewna" if inst >= 80 else "znaczący udział" if inst >= 50 else "niski udział"
            yf_text += f"🏦 **Instytucje:** {inst:.0f}% *({inst_label})*\n"

        if yf_text:
            fields.append({"name": "📊 Dane targetu (CEL)", "value": yf_text, "inline": True})

    elif analysis.get('target_company'):
        # Target prywatny lub brak tickera — pokaż przynajmniej nazwę
        priv_text = f"**Spółka:** {analysis['target_company']}\n🔒 Spółka prywatna — brak notowań\n"
        if analysis.get('deal_value') and analysis['deal_value'].lower() not in ('null', 'undisclosed', 'none', ''):
            priv_text += f"💰 Wartość przejęcia: {analysis['deal_value']}\n"
        fields.append({"name": "📊 Dane targetu (CEL)", "value": priv_text, "inline": True})

    # Dane acquirera (nabywca — kontekst)
    if acquirer_yahoo and not acquirer_yahoo.get('error') and acquirer_yahoo.get('current_price'):
        acq_text = ""
        if acquirer_yahoo.get('market_cap_formatted'):
            acq_text += f"**Wycena:** {acquirer_yahoo['market_cap_formatted']}\n"
        if acquirer_yahoo.get('current_price'):
            acq_text += f"**Kurs:** ${acquirer_yahoo['current_price']}\n"
        if acquirer_yahoo.get('week_change_pct') is not None:
            chg = acquirer_yahoo['week_change_pct']
            chg_label = " *(rynek akceptuje deal)*" if chg >= 0 else " *(rynek kwestionuje cenę)*"
            acq_text += f"{'📈' if chg > 0 else '📉'} **Tydzień:** {chg:+.2f}%{chg_label}\n"
        t_mcap = target_yahoo.get('market_cap') if target_yahoo else None
        a_mcap = acquirer_yahoo.get('market_cap')
        if a_mcap and t_mcap:
            ratio = a_mcap / t_mcap
            acq_text += f"📐 **Nabywca/target:** {ratio:.1f}x wyceny\n"
        if acq_text:
            fields.append({"name": "🏢 Dane nabywcy", "value": acq_text, "inline": True})

    # analyst_verdict: ekspercka interpretacja i predykcja Groqa
    # Guard: blokuj tylko gdy Groq wstawi kwoty ($X) lub procenty z cyframi (X.X%) — lata i kwartały OK
    analyst_verdict = analysis.get('analyst_verdict', '').strip()
    if analyst_verdict and re.search(r'\$[\d,]+|\b\d+[.,]\d+\s*%', analyst_verdict):
        logger.warning("   ⚠ analyst_verdict zawiera kwoty/procenty — pomijam (halucynacja finansowa)")
        analyst_verdict = ''
    if analyst_verdict:
        fields.append({"name": "🧠 Interpretacja analityczna", "value": _truncate_sentence(analyst_verdict, 1020), "inline": False})

    if analysis.get('key_points'):
        kp_text = "\n".join(f"• {k}" for k in analysis['key_points'][:4])
        fields.append({"name": "🔑 Kluczowe punkty", "value": _truncate_sentence(kp_text, 1020), "inline": False})

    if analysis.get('risks'):
        fields.append({"name": "⚠️ Ryzyka", "value": "\n".join(f"• {r}" for r in analysis['risks'][:2]), "inline": True})

    if analysis.get('sympathy_plays'):
        fields.append({"name": "🔗 Powiązane spółki", "value": "\n".join(f"• {s}" for s in analysis['sympathy_plays'][:3]), "inline": True})

    if analysis.get('leak_detected'):
        fields.append({"name": "🕵️ Wykryto przeciek", "value": analysis.get('reasoning', '')[:200], "inline": False})

    # Czas publikacji 8-K (z RSS) — nie czas wysłania alertu
    pub_iso = filing.get('published_iso', '')
    pub_display = _poland_time(pub_iso) if pub_iso else _poland_time(datetime.utcnow().isoformat())

    fields.append({
        "name": "📋 Formularz",
        "value": f"8-K · Item 1.01\n[Otwórz w EDGAR]({filing.get('link', '#')})",
        "inline": True
    })
    fields.append({
        "name": "⏰ Opublikowano (8-K)",
        "value": pub_display,
        "inline": True
    })

    # Debug: pokaż które sekcje trafiły do alertu
    field_names = [f['name'] for f in fields]
    logger.info(f"   📨 Alert fields ({len(fields)}): {' | '.join(field_names)}")
    if not any('target' in n.lower() or 'CEL' in n for n in field_names):
        logger.warning(f"   ⚠ Brak sekcji target — target_yahoo={bool(target_yahoo)}, error={target_yahoo.get('error','none') if target_yahoo else 'empty'}")

    embed = {"title": title, "description": desc, "color": color, "fields": fields}
    if tv_url:
        embed["url"] = tv_url  # tytuł alertu staje się klikalnym linkiem do TradingView
    payload = {"embeds": [embed]}

    try:
        r = HTTP_SESSION.post(webhook_url, json=payload, timeout=10)
        r.raise_for_status()
        logger.info(f"✓ Discord [{priority}]: {display_ticker or target_name}")
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
    filings_since_save = 0
    GIST_SAVE_INTERVAL = 15  # zapisuj Gist co 15 przetworzonych filingów

    consecutive_processed = 0
    EARLY_EXIT_THRESHOLD = 15  # jeśli 15 z rzędu już przetworzone → reszta też stara → stop

    for filing in filings:
        accession = filing.get('accession')
        cik       = filing.get('cik')

        if not accession:
            continue

        if accession in processed:
            skipped_processed += 1
            consecutive_processed += 1
            if consecutive_processed >= EARLY_EXIT_THRESHOLD:
                logger.info(f"   ⏩ {EARLY_EXIT_THRESHOLD} filingów z rzędu już przetworzone — przerywam pętlę")
                break
            continue

        consecutive_processed = 0  # reset gdy znajdziemy nowy filing

        logger.info(f"\n🔍 {filing.get('title', '')[:70]}")
        logger.info(f"   Accession: {accession} | CIK: {cik}")

        # --- SEC Submissions API: ticker + items w jednym zapytaniu (przed pobraniem dokumentu) ---
        ticker = filing.get('ticker_hint')
        has_101 = None
        if cik:
            meta = get_sec_filing_metadata(cik, accession)
            time.sleep(0.3)
            if not ticker:
                ticker = meta.get('ticker')
            has_101 = meta.get('has_item_101')

        # Jeśli API potwierdza brak Item 1.01 — pomijamy bez pobierania dokumentu
        if has_101 is False:
            logger.info("   ↳ SEC API: brak Item 1.01 — pomijam (bez pobierania dokumentu)")
            processed.add(accession)
            filings_since_save += 1
            if filings_since_save >= GIST_SAVE_INTERVAL:
                save_processed_to_gist(processed)
                filings_since_save = 0
            continue

        # --- Pobierz pełny dokument (tylko gdy API potwierdził Item 1.01 lub nie wiadomo) ---
        raw_content = fetch_document_content(accession, cik)
        if not raw_content:
            processed.add(accession)
            continue

        # --- Fallback check w dokumencie (gdy API nie zwróciło items) ---
        if has_101 is None and not re.search(r'item\s*1\.01', raw_content, re.IGNORECASE):
            logger.info("   ↳ Brak Item 1.01 — pomijam")
            processed.add(accession)
            continue

        # --- HTML cleaning ---
        clean_text = clean_html_document(raw_content)

        # --- Wyciągnij sekcję Item 1.01 ---
        section = extract_item_101_section(clean_text, max_chars=12000)

        # --- Regex: wartość dealu jako weryfikacja dla AI ---
        deal_value_regex = extract_deal_value_regex(section)
        filing['_deal_value_regex'] = deal_value_regex  # fallback gdy Groq zwróci null

        # Fallback ticker z dokumentu gdy SEC API i tytuł RSS nie dały rezultatu
        if not ticker:
            ticker = extract_ticker_from_document(clean_text)

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

        # --- Resolve target vs acquirer ---
        # Groq mówi nam kto jest filer: target czy acquirer
        # Chcemy zawsze mieć dane TARGETU (ten skacze) + ACQUIRERA (kontekst)
        filer_role = analysis.get('filer_role', 'unknown')
        target_yahoo  = {}
        acquirer_yahoo = {}

        if filer_role == 'acquirer':
            # Filer to nabywca — mamy jego dane w yahoo_data
            # Szukamy danych TARGETU (ten będzie skakał)
            acquirer_yahoo = yahoo_data
            tgt_ticker = analysis.get('target_ticker')
            if tgt_ticker and tgt_ticker != ticker:
                logger.info(f"   → Pobieram dane targetu: {tgt_ticker} (filer={ticker} to acquirer)")
                tgt_data = get_yahoo_data(tgt_ticker)
                time.sleep(0.3)
                if tgt_data and not tgt_data.get('error') and tgt_data.get('market_cap'):
                    target_yahoo = tgt_data
                    logger.info(f"   ✓ Target {tgt_ticker}: ${tgt_data.get('current_price')} MCap:{tgt_data.get('market_cap_formatted')}")
                else:
                    logger.warning(f"   ⚠ Brak danych Yahoo dla targetu {tgt_ticker}")
            else:
                logger.info(f"   ⚠ Brak tickera targetu w analizie Groq (filer=acquirer)")

        elif filer_role == 'target':
            # Filer to cel przejęcia — yahoo_data już jest danymi targetu
            target_yahoo = yahoo_data
            acq_ticker = analysis.get('acquirer_ticker')
            if acq_ticker and acq_ticker != ticker:
                logger.info(f"   → Pobieram dane acquirera: {acq_ticker}")
                acq_data = get_yahoo_data(acq_ticker)
                time.sleep(0.3)
                if acq_data and not acq_data.get('error'):
                    acquirer_yahoo = acq_data

        else:
            # Nie wiadomo kto jest kim — traktujemy filer jako target
            target_yahoo = yahoo_data

        # --- Python-side upside calculation (nie ufamy Groqowi z matematyką) ---
        # Upside = dystans od aktualnego kursu do ceny oferty → czyste dane Yahoo + Groq
        offer_price  = analysis.get('offer_price_per_share')
        target_price = target_yahoo.get('current_price')
        if offer_price and target_price and float(target_price) > 0:
            py_upside = round((float(offer_price) - float(target_price)) / float(target_price) * 100, 1)
            analysis['_py_upside_pct'] = py_upside
            logger.info(f"   ✓ Python upside: {py_upside:+.1f}% (oferta ${offer_price} vs kurs ${target_price})")
        else:
            analysis['_py_upside_pct'] = None

        # Walidacja: jeśli Groq podał premium ale nie ma offer_price → nie ufamy premium
        if not offer_price and analysis.get('premium_pct') is not None:
            logger.warning("   ⚠ Groq podał premium_pct bez offer_price_per_share — ignoruję premium")
            analysis['premium_pct'] = None

        # --- Routing na Discord ---
        impact = analysis.get('impact_score', 0)

        if impact >= 9:
            send_discord_alert(filing, analysis, target_yahoo, acquirer_yahoo, "MEGA")
            new_alerts += 1
        elif impact >= 7:
            send_discord_alert(filing, analysis, target_yahoo, acquirer_yahoo, "MAJOR")
            new_alerts += 1
        elif impact >= 5:
            send_discord_alert(filing, analysis, target_yahoo, acquirer_yahoo, "STANDARD")
            new_alerts += 1
        else:
            logger.info(f"   ↳ Low impact ({impact}/10) — pomijam alert")
            skipped_low_impact += 1

        processed.add(accession)
        filings_since_save += 1
        if filings_since_save >= GIST_SAVE_INTERVAL:
            save_processed_to_gist(processed)
            filings_since_save = 0
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
