#!/usr/bin/env python3
"""
SEC M&A Scanner - Item 1.01
Skanuje wszystkie 8-K z Item 1.01 (M&A, partnerships, major contracts)
AI-driven impact scoring z Gemini
"""

import requests
import json
import os
import time
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional
import xml.etree.ElementTree as ET

# Yahoo Finance integration
try:
    import yfinance as yf
    YFINANCE_AVAILABLE = True
except ImportError:
    YFINANCE_AVAILABLE = False
    print("⚠️  WARNING: yfinance not installed - Yahoo Finance integration disabled")

# ============================================
# CONFIGURATION
# ============================================

DISCORD_WEBHOOK_MEGA = os.environ.get('DISCORD_WEBHOOK_MEGA', '')
DISCORD_WEBHOOK_MAJOR = os.environ.get('DISCORD_WEBHOOK_MAJOR', '')
DISCORD_WEBHOOK_STANDARD = os.environ.get('DISCORD_WEBHOOK_STANDARD', '')
GROQ_API_KEY = os.environ.get('GROQ_API_KEY', '')  # Groq instead of Gemini
GIST_TOKEN = os.environ.get('GIST_TOKEN', '')
GIST_ID = os.environ.get('GIST_ID_MA', '')

USER_AGENT = "SEC-MA-Scanner/1.0 (research@example.com)"
SEC_RSS_URL = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&count=100&output=atom"

# ============================================
# GIST TRACKING
# ============================================

def load_processed_from_gist() -> set:
    """Load processed filings from Gist"""
    if not GIST_TOKEN or not GIST_ID:
        return set()
    
    try:
        headers = {
            'Authorization': f'token {GIST_TOKEN}',
            'Accept': 'application/vnd.github.v3+json'
        }
        
        response = requests.get(f'https://api.github.com/gists/{GIST_ID}', headers=headers, timeout=10)
        response.raise_for_status()
        
        gist_data = response.json()
        if 'files' in gist_data and 'processed_ma.json' in gist_data['files']:
            content = gist_data['files']['processed_ma.json']['content']
            data = json.loads(content)
            return set(data.get('filings', []))
        return set()
    except Exception as e:
        print(f"Error loading from Gist: {e}")
        return set()

def save_processed_to_gist(processed: set):
    """Save processed filings to Gist"""
    if not GIST_TOKEN or not GIST_ID:
        return
    
    try:
        headers = {
            'Authorization': f'token {GIST_TOKEN}',
            'Accept': 'application/vnd.github.v3+json'
        }
        
        data = {
            'filings': list(processed),
            'last_updated': datetime.now().isoformat(),
            'total_count': len(processed)
        }
        
        payload = {
            'files': {
                'processed_ma.json': {
                    'content': json.dumps(data, indent=2)
                }
            }
        }
        
        response = requests.patch(f'https://api.github.com/gists/{GIST_ID}', 
                                 headers=headers, json=payload, timeout=10)
        response.raise_for_status()
        print(f"✓ Saved {len(processed)} filings to Gist")
    except Exception as e:
        print(f"Error saving to Gist: {e}")

# ============================================
# YAHOO FINANCE INTEGRATION
# ============================================

def extract_ticker_from_document(content: str) -> Optional[str]:
    """Try to extract ticker from 8-K document"""
    try:
        lines = content.split('\n')
        
        # Search in first 200 lines (more coverage)
        for line in lines[:200]:
            line_upper = line.upper()
            
            # Pattern 1: TRADING SYMBOL
            if 'TRADING SYMBOL' in line_upper or 'TICKER SYMBOL' in line_upper:
                parts = line.split(':')
                if len(parts) > 1:
                    ticker = parts[1].strip().split()[0]
                    # Clean ticker
                    ticker = ticker.replace(',', '').replace('.', '').replace('(', '').replace(')', '')
                    if ticker and len(ticker) <= 5:
                        return ticker.upper()
            
            # Pattern 2: NASDAQ: or NYSE:
            if 'NASDAQ:' in line_upper or 'NYSE:' in line_upper:
                parts = line.split(':')
                if len(parts) > 1:
                    ticker = parts[1].strip().split()[0]
                    ticker = ticker.replace(',', '').replace('.', '').replace('(', '').replace(')', '')
                    if ticker and len(ticker) <= 5:
                        return ticker.upper()
            
            # Pattern 3: (NASDAQ: XXXX) or (NYSE: XXXX)
            if '(NASDAQ:' in line_upper or '(NYSE:' in line_upper or '(NYSE:' in line_upper:
                start = line_upper.find('(NASDAQ:') if '(NASDAQ:' in line_upper else line_upper.find('(NYSE:') if '(NYSE:' in line_upper else line_upper.find('(NYSE:')
                if start >= 0:
                    segment = line[start:start+20]
                    ticker = segment.split(':')[1].split(')')[0].strip()
                    ticker = ticker.replace(',', '').replace('.', '')
                    if ticker and len(ticker) <= 5:
                        return ticker.upper()
            
            # Pattern 4: Common name: <COMPANY NAME> (<TICKER>)
            if 'COMPANY' in line_upper and '(' in line and ')' in line:
                # Look for pattern: COMPANY NAME (XXXX)
                parts = line.split('(')
                if len(parts) > 1:
                    potential_ticker = parts[-1].split(')')[0].strip()
                    # Check if it looks like a ticker (2-5 uppercase letters)
                    if potential_ticker and 2 <= len(potential_ticker) <= 5 and potential_ticker.replace('.', '').isalpha():
                        return potential_ticker.upper()
        
        # If nothing found, try CIK-based lookup from header
        # Look for CONFORMED SUBMISSION TYPE and get company CIK
        for line in lines[:50]:
            if 'CENTRAL INDEX KEY' in line.upper():
                # We have CIK but not ticker - Yahoo Finance search by company name might work
                pass
        
        return None
    except Exception as e:
        print(f"   ⚠️  Ticker extraction error: {str(e)[:30]}")
        return None

def get_yahoo_finance_data(ticker: Optional[str], company_name: str) -> Dict:
    """Fetch real-time data from Yahoo Finance"""
    
    if not YFINANCE_AVAILABLE:
        print("   → Yahoo Finance not available (install: pip install yfinance)")
        return {}
    
    # If no ticker, try to find it using company name
    if not ticker and company_name and company_name != "Unknown Company":
        print(f"   → No ticker in document, searching by company name...")
        try:
            # Try to search for ticker using yfinance
            # Remove common suffixes
            search_name = company_name.replace(' INC', '').replace(' CORP', '').replace(' LTD', '').replace(' CO', '').replace(',', '').strip()
            
            # yfinance doesn't have direct search, but we can try common patterns
            # Try exact company name as ticker (sometimes works)
            test_tickers = []
            
            # Try company initials
            words = search_name.split()
            if len(words) >= 2:
                initials = ''.join([w[0] for w in words if w])
                if 2 <= len(initials) <= 5:
                    test_tickers.append(initials)
            
            # Try first word (sometimes company name is the ticker)
            if len(words) > 0 and 2 <= len(words[0]) <= 5:
                test_tickers.append(words[0])
            
            # Try to fetch info for each potential ticker
            for test_ticker in test_tickers:
                try:
                    stock = yf.Ticker(test_ticker.upper())
                    info = stock.info
                    # Check if this ticker actually has data
                    if info.get('regularMarketPrice') or info.get('currentPrice'):
                        ticker = test_ticker.upper()
                        print(f"   ✓ Found ticker by search: {ticker}")
                        break
                except:
                    continue
            
            if not ticker:
                print(f"   ✗ Could not find ticker for {company_name}")
                return {}
                
        except Exception as e:
            print(f"   ✗ Ticker search failed: {str(e)[:50]}")
            return {}
    
    if not ticker:
        print(f"   → No ticker found for {company_name}")
        return {}
    
    try:
        print(f"   → Fetching Yahoo Finance data for {ticker}...")
        
        stock = yf.Ticker(ticker)
        info = stock.info
        hist = stock.history(period="1mo")
        
        # Get current data
        current_price = info.get('currentPrice') or info.get('regularMarketPrice')
        market_cap = info.get('marketCap')
        
        # Calculate recent performance
        if not hist.empty:
            week_ago_price = hist['Close'].iloc[-5] if len(hist) >= 5 else hist['Close'].iloc[0]
            month_ago_price = hist['Close'].iloc[0]
            current_close = hist['Close'].iloc[-1]
            
            week_change = ((current_close - week_ago_price) / week_ago_price * 100) if week_ago_price else 0
            month_change = ((current_close - month_ago_price) / month_ago_price * 100) if month_ago_price else 0
        else:
            week_change = 0
            month_change = 0
        
        yahoo_data = {
            'ticker': ticker,
            'current_price': current_price,
            'market_cap': market_cap,
            'market_cap_formatted': f"${market_cap/1e9:.2f}B" if market_cap and market_cap > 1e9 else f"${market_cap/1e6:.1f}M" if market_cap else "Unknown",
            'volume': info.get('volume'),
            'avg_volume': info.get('averageVolume'),
            'volume_spike': (info.get('volume', 0) / info.get('averageVolume', 1)) if info.get('averageVolume') else 1,
            'week_change_pct': round(week_change, 2),
            'month_change_pct': round(month_change, 2),
            'fifty_two_week_high': info.get('fiftyTwoWeekHigh'),
            'fifty_two_week_low': info.get('fiftyTwoWeekLow'),
            'pe_ratio': info.get('trailingPE'),
            'forward_pe': info.get('forwardPE'),
            'price_to_book': info.get('priceToBook'),
            'enterprise_value': info.get('enterpriseValue'),
            'shares_outstanding': info.get('sharesOutstanding'),
            'float_shares': info.get('floatShares'),
            'short_ratio': info.get('shortRatio'),
            'short_percent': info.get('shortPercentOfFloat'),
            'institutional_ownership': info.get('heldPercentInstitutions'),
            'insider_ownership': info.get('heldPercentInsiders'),
            'beta': info.get('beta'),
            'analyst_target': info.get('targetMeanPrice'),
            'analyst_count': info.get('numberOfAnalystOpinions'),
            'recommendation': info.get('recommendationKey'),
        }
        
        print(f"   ✓ Yahoo Finance: Price ${current_price}, MCap {yahoo_data['market_cap_formatted']}")
        return yahoo_data
        
    except Exception as e:
        print(f"   ✗ Error fetching Yahoo Finance for {ticker}: {e}")
        return {'ticker': ticker, 'error': str(e)}

# ============================================
# SEC RSS SCRAPING
# ============================================

def fetch_recent_8k() -> List[Dict]:
    """Fetch recent 8-K filings from SEC RSS feed"""
    try:
        headers = {'User-Agent': USER_AGENT}
        response = requests.get(SEC_RSS_URL, headers=headers, timeout=15)
        response.raise_for_status()
        
        root = ET.fromstring(response.content)
        namespace = {'atom': 'http://www.w3.org/2005/Atom'}
        
        filings = []
        for entry in root.findall('atom:entry', namespace):
            title = entry.find('atom:title', namespace)
            updated = entry.find('atom:updated', namespace)
            link = entry.find('atom:link', namespace)
            
            if title is not None and '8-K' in title.text:
                # Extract CIK and accession from title
                # Format: "8-K - COMPANY NAME (CIK)"
                cik = None
                accession = None
                
                # Try to extract CIK from title
                if '(' in title.text and ')' in title.text:
                    cik_part = title.text.split('(')[-1].split(')')[0]
                    if cik_part.isdigit():
                        cik = cik_part
                
                # Get original link
                original_link = link.get('href') if link is not None else ''
                
                # Extract accession from link
                if '/Archives/edgar/data/' in original_link:
                    parts = original_link.split('/')
                    for i, part in enumerate(parts):
                        if part == 'data' and i + 2 < len(parts):
                            if not cik:
                                cik = parts[i + 1]
                            # Accession is in the next part (without dashes)
                            acc_no_dashes = parts[i + 2]
                            # Convert to standard format: XXXXXXXXXX-XX-XXXXXX
                            if len(acc_no_dashes) == 18 and acc_no_dashes.isdigit():
                                accession = f"{acc_no_dashes[:10]}-{acc_no_dashes[10:12]}-{acc_no_dashes[12:]}"
                            break
                
                # Build proper SEC viewer URL
                if cik and accession:
                    proper_link = f"https://www.sec.gov/cgi-bin/viewer?action=view&cik={cik}&accession_number={accession}&xbrl_type=v"
                else:
                    proper_link = original_link
                
                filing_info = {
                    'title': title.text,
                    'updated': updated.text if updated is not None else '',
                    'link': proper_link,
                    'original_link': original_link,
                    'cik': cik,
                    'accession': accession
                }
                filings.append(filing_info)
        
        print(f"✓ Found {len(filings)} 8-K filings in RSS")
        return filings
        
    except Exception as e:
        print(f"Error fetching RSS: {e}")
        return []

# ============================================
# DOCUMENT ANALYSIS
# ============================================

def extract_accession_from_link(link: str) -> Optional[str]:
    """Extract accession number from SEC link"""
    try:
        # Format 1: Viewer link with accession_number parameter
        # https://www.sec.gov/cgi-bin/viewer?action=view&cik=XXX&accession_number=XXX
        if 'accession_number=' in link:
            return link.split('accession_number=')[1].split('&')[0]
        
        # Format 2: Direct archive link (RSS format)
        # https://www.sec.gov/Archives/edgar/data/874501/000162828025047943/0001628280-25-047943-index.htm
        if '/Archives/edgar/data/' in link:
            # Extract from path: /data/{CIK}/{ACCESSION}/{ACCESSION}-index.htm
            parts = link.split('/')
            for i, part in enumerate(parts):
                if part == 'data' and i + 2 < len(parts):
                    # Accession is in the next part (without dashes)
                    acc_no_dashes = parts[i + 2]
                    # Convert to standard format: XXXXXXXXXX-XX-XXXXXX
                    if len(acc_no_dashes) == 18 and acc_no_dashes.isdigit():
                        return f"{acc_no_dashes[:10]}-{acc_no_dashes[10:12]}-{acc_no_dashes[12:]}"
                    # Or it might already have dashes
                    elif '-' in acc_no_dashes and acc_no_dashes.replace('-', '').isdigit():
                        return acc_no_dashes.split('-index')[0]
        
        return None
    except:
        return None

def extract_cik_from_link(link: str) -> Optional[str]:
    """Extract CIK from SEC link"""
    try:
        # Format 1: Viewer link with cik parameter
        if 'cik=' in link:
            return link.split('cik=')[1].split('&')[0]
        
        # Format 2: Archive link
        if '/Archives/edgar/data/' in link:
            parts = link.split('/data/')
            if len(parts) > 1:
                return parts[1].split('/')[0]
        
        return None
    except:
        return None

def fetch_document_content(accession: str, link: str = "", cik: str = None) -> str:
    """Fetch full 8-K document content"""
    try:
        # Extract CIK if not provided
        if not cik and link:
            cik = extract_cik_from_link(link)
        
        if not cik:
            print(f"   ✗ Cannot fetch document: CIK not found")
            return ""
        
        # Build document URL
        acc_no_dashes = accession.replace('-', '')
        url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_no_dashes}/{accession}.txt"
        
        print(f"   → Fetching document: {url}")
        
        headers = {'User-Agent': USER_AGENT}
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        
        return response.text
    except Exception as e:
        print(f"   ✗ Error fetching document {accession}: {e}")
        return ""

def has_item_101(content: str) -> bool:
    """Check if document contains Item 1.01"""
    content_lower = content.lower()
    return 'item 1.01' in content_lower or 'item 1.1' in content_lower

def extract_company_info(content: str) -> Dict:
    """Extract company name and ticker from document"""
    try:
        lines = content.split('\n')
        company_name = "Unknown Company"
        
        for line in lines[:50]:
            if 'COMPANY CONFORMED NAME' in line.upper():
                company_name = line.split(':')[-1].strip()
                break
        
        return {'company': company_name}
    except:
        return {'company': 'Unknown Company'}

# ============================================
# GROQ AI ANALYSIS (OPTIMIZED)
# ============================================

def extract_relevant_sections(document: str, max_chars: int = 8000) -> str:
    """Extract most relevant sections from 8-K for analysis"""
    try:
        # Find Item 1.01 section
        doc_lower = document.lower()
        item_101_start = doc_lower.find('item 1.01')
        
        if item_101_start == -1:
            item_101_start = doc_lower.find('item 1.1')
        
        if item_101_start >= 0:
            # Extract from Item 1.01 onwards
            relevant_text = document[item_101_start:]
            
            # Find end of Item 1.01 (usually next item or signature)
            end_markers = ['item 2.', 'item 3.', 'item 5.', 'item 7.', 'item 8.', 'item 9.', 'signature']
            end_pos = len(relevant_text)
            
            for marker in end_markers:
                pos = relevant_text.lower().find(marker, 100)  # Skip first 100 chars to avoid false positives
                if pos > 0 and pos < end_pos:
                    end_pos = pos
            
            relevant_text = relevant_text[:end_pos]
            
            # Limit to max_chars
            if len(relevant_text) > max_chars:
                relevant_text = relevant_text[:max_chars]
            
            return relevant_text
        else:
            # Fallback: return first max_chars
            return document[:max_chars]
    except:
        return document[:max_chars]

def analyze_with_groq(document: str, company_info: Dict, yahoo_data: Dict) -> Optional[Dict]:
    """Analyze M&A deal with Groq AI (Llama 3.1) + Yahoo Finance data - OPTIMIZED"""
    if not GROQ_API_KEY:
        print("Warning: No GROQ_API_KEY set")
        return None
    
    try:
        # Extract only relevant sections (Item 1.01)
        relevant_doc = extract_relevant_sections(document, max_chars=6000)
        
        # Build compact Yahoo Finance context
        yf_context = ""
        if yahoo_data and not yahoo_data.get('error'):
            yf_context = f"""
MARKET DATA:
Ticker: {yahoo_data.get('ticker', 'N/A')} | Price: ${yahoo_data.get('current_price', 'N/A')} | MCap: {yahoo_data.get('market_cap_formatted', 'Unknown')}
Volume: {yahoo_data.get('volume_spike', 1):.1f}x avg | 1W: {yahoo_data.get('week_change_pct', 0):+.1f}% | 1M: {yahoo_data.get('month_change_pct', 0):+.1f}%
Shares: {f"{yahoo_data.get('shares_outstanding'):,.0f}" if yahoo_data.get('shares_outstanding') else 'N/A'}
"""
        else:
            yf_context = "\nMARKET DATA: Not available\n"

        # Compact prompt
        prompt = f"""Analyze this 8-K Item 1.01 M&A deal.

COMPANY: {company_info['company']}
{yf_context}

DOCUMENT (Item 1.01 section):
{relevant_doc}

ANALYSIS INSTRUCTIONS:

1. FIND DEAL VALUE in document (search: "consideration", "purchase price", "transaction value")
2. CALCULATE PREMIUM using Yahoo Finance market cap: {yahoo_data.get('market_cap_formatted', 'Unknown')}
   Premium % = (Deal Value - Current MCap) / Current MCap × 100
3. CALCULATE OFFER PRICE if possible:
   Offer Price = Deal Value / Shares Outstanding ({yahoo_data.get('shares_outstanding', 'N/A')})
   Upside = (Offer Price - Current Price ${yahoo_data.get('current_price', 'N/A')}) / Current Price × 100

IMPACT SCORE (1-10):
HIGH (9-10): Premium >50%, strategic buyer, all-cash, high synergies, volume spike >3x
MEDIUM (5-8): Premium 20-50%, mixed payment, moderate synergies
LOW (1-4): Premium <20%, all-stock, no synergies, high risk

RESPOND IN JSON:
{{
  "impact_score": X,
  "deal_type": "acquisition/partnership/contract",
  "target_company": "name",
  "acquirer": "name",
  "deal_value": "value or undisclosed",
  "deal_value_numeric": number_in_usd_or_null,
  "premium_pct": Y or null,
  "premium_calculation": "step by step calc",
  "offer_price_per_share": Z or null,
  "upside_to_offer": "+X%" or null,
  "verdict": "MEGA/HIGH/MEDIUM/LOW",
  "short_term_move": "+/-X%",
  "confidence": X,
  "deal_structure": "all-cash/stock/mixed",
  "strategic_rationale": "why this deal makes sense",
  "key_points": ["point1", "point2", "point3"],
  "risks": ["risk1", "risk2"],
  "sympathy_plays": ["ticker1: reason", "ticker2: reason"],
  "sector_context": "part of bigger trend?",
  "liquidity_warning": "if low float/volume",
  "leak_detected": true/false,
  "leak_reasoning": "why leak suspected",
  "approval_probability": "X%",
  "reasoning": "full justification"
}}

IMPORTANT: Use EXACT numbers from document. Be conservative with scores. Return ONLY JSON."""

        # Groq API call
        url = "https://api.groq.com/openai/v1/chat/completions"
        
        headers = {
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "model": "llama-3.1-70b-versatile",
            "messages": [
                {
                    "role": "system",
                    "content": "You are an expert M&A analyst. Respond ONLY with valid JSON, no additional text."
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            "temperature": 0.3,
            "max_tokens": 1024
        }
        
        response = requests.post(url, headers=headers, json=payload, timeout=45)
        response.raise_for_status()
        
        result = response.json()
        
        if 'choices' in result and len(result['choices']) > 0:
            text = result['choices'][0]['message']['content']
            
            # Extract JSON from response
            text = text.strip()
            if '```json' in text:
                text = text.split('```json')[1].split('```')[0]
            elif '```' in text:
                text = text.split('```')[1].split('```')[0]
            
            analysis = json.loads(text.strip())
            print(f"✓ Groq AI analysis: Impact {analysis.get('impact_score', 0)}/10 (Premium: {analysis.get('premium_pct', 'N/A')}%)")
            return analysis
        
        return None
    
    except requests.exceptions.HTTPError as e:
        print(f"✗ Groq API error: {e}")
        if hasattr(e.response, 'text'):
            print(f"   Response: {e.response.text[:200]}")
        return None
    except Exception as e:
        print(f"✗ Unexpected error in Groq analysis: {str(e)[:100]}")
        return None

# ============================================
# DISCORD ALERTS
# ============================================

def convert_to_poland_time(utc_time_str: str) -> str:
    """Convert UTC to Poland time (CET/CEST)"""
    try:
        dt_obj = datetime.strptime(utc_time_str.split('.')[0].replace('Z', ''), '%Y-%m-%dT%H:%M:%S')
        dt_obj = dt_obj.replace(tzinfo=timezone.utc)
        
        month = dt_obj.month
        day = dt_obj.day
        
        if 3 < month < 10:
            offset_hours = 2
            tz_name = "CEST"
        elif month == 3 or month == 10:
            if (month == 3 and day >= 25) or (month == 10 and day < 25):
                offset_hours = 2
                tz_name = "CEST"
            else:
                offset_hours = 1
                tz_name = "CET"
        else:
            offset_hours = 1
            tz_name = "CET"
        
        poland_tz = timezone(timedelta(hours=offset_hours))
        poland_time = dt_obj.astimezone(poland_tz)
        return poland_time.strftime(f'%Y-%m-%d o %H:%M:%S {tz_name}')
    except:
        return utc_time_str

def send_discord_alert(filing: Dict, analysis: Dict, yahoo_data: Dict, priority: str):
    """Send formatted alert to Discord"""
    
    # Determine webhook
    if priority == "MEGA":
        webhook_url = DISCORD_WEBHOOK_MEGA
        color = 15158332  # Red
        emoji = "🔴🔴🔴"
    elif priority == "MAJOR":
        webhook_url = DISCORD_WEBHOOK_MAJOR
        color = 16753920  # Orange
        emoji = "🟠"
    else:
        webhook_url = DISCORD_WEBHOOK_STANDARD
        color = 16776960  # Yellow
        emoji = "🟡"
    
    if not webhook_url:
        print(f"Warning: No webhook for priority {priority}")
        return
    
    company = filing.get('company', 'Unknown')
    deal_type = analysis.get('deal_type', 'deal')
    target = analysis.get('target_company', company)
    acquirer = analysis.get('acquirer', 'Unknown')
    
    title = f"{emoji} {priority} M&A - Item 1.01"
    
    # Main description with deal info
    description = f"**{target} → {acquirer}**\n\n"
    
    if analysis.get('deal_value'):
        description += f"💰 **Deal Value:** {analysis['deal_value']}\n"
    
    # Premium with Yahoo Finance context
    if analysis.get('premium_pct'):
        description += f"🔥 **Premium:** {analysis['premium_pct']}%"
        if yahoo_data and yahoo_data.get('current_price'):
            description += f" (from ${yahoo_data['current_price']})"
        description += "\n"
    
    if analysis.get('offer_price_per_share'):
        description += f"🎯 **Offer Price:** ${analysis['offer_price_per_share']}/share\n"
    
    if analysis.get('upside_to_offer'):
        description += f"📈 **Upside to Offer:** {analysis['upside_to_offer']}\n"
    
    description += f"\n**AI IMPACT: {analysis.get('impact_score', 0)}/10**\n"
    description += f"**VERDICT:** {analysis.get('verdict', 'N/A')}\n"
    description += f"**SHORT-TERM:** {analysis.get('short_term_move', 'N/A')}\n"
    description += f"**CONFIDENCE:** {analysis.get('confidence', 0)}/10\n"
    
    if analysis.get('deal_structure'):
        description += f"**STRUCTURE:** {analysis['deal_structure']}\n"
    
    fields = []
    
    # Yahoo Finance Market Data
    if yahoo_data and not yahoo_data.get('error'):
        yf_text = ""
        if yahoo_data.get('market_cap_formatted'):
            yf_text += f"**Market Cap:** {yahoo_data['market_cap_formatted']}\n"
        if yahoo_data.get('current_price'):
            yf_text += f"**Current Price:** ${yahoo_data['current_price']}\n"
        if yahoo_data.get('volume_spike') and yahoo_data['volume_spike'] > 2:
            yf_text += f"🔊 **Volume Spike:** {yahoo_data['volume_spike']:.1f}x normal\n"
        if yahoo_data.get('week_change_pct'):
            change_emoji = "📈" if yahoo_data['week_change_pct'] > 0 else "📉"
            yf_text += f"{change_emoji} **1W Change:** {yahoo_data['week_change_pct']:+.2f}%\n"
        if yahoo_data.get('institutional_ownership'):
            yf_text += f"🏦 **Institutional:** {yahoo_data['institutional_ownership']:.1%}\n"
        
        if yf_text:
            fields.append({
                "name": "📊 Market Data (Yahoo Finance)",
                "value": yf_text.strip(),
                "inline": False
            })
    
    # Premium Calculation (if available)
    if analysis.get('premium_calculation'):
        fields.append({
            "name": "🧮 Premium Calculation",
            "value": analysis['premium_calculation'][:500],
            "inline": False
        })
    
    # Strategic Rationale
    if analysis.get('strategic_rationale'):
        fields.append({
            "name": "💡 Strategic Rationale",
            "value": analysis['strategic_rationale'][:500],
            "inline": False
        })
    
    # Key Points
    if analysis.get('key_points'):
        points = '\n'.join([f"• {p}" for p in analysis['key_points'][:3]])
        fields.append({
            "name": "🎯 Key Points",
            "value": points,
            "inline": False
        })
    
    # Risks
    if analysis.get('risks'):
        risks = '\n'.join([f"• {r}" for r in analysis['risks'][:3]])
        fields.append({
            "name": "⚠️ Risks",
            "value": risks,
            "inline": False
        })
    
    # Warnings (leak detection, liquidity)
    warnings = []
    if analysis.get('leak_detected'):
        warnings.append(f"🚨 **Leak Detected:** {analysis.get('leak_reasoning', 'Unusual price action')}")
    if analysis.get('liquidity_warning'):
        warnings.append(f"⚠️ **Liquidity:** {analysis['liquidity_warning']}")
    if warnings:
        fields.append({
            "name": "⚠️ Trading Warnings",
            "value": "\n".join(warnings),
            "inline": False
        })
    
    # Sympathy Plays
    if analysis.get('sympathy_plays'):
        sympathy = '\n'.join([f"• {sp}" for sp in analysis['sympathy_plays'][:4]])
        fields.append({
            "name": "🔔 Sympathy Plays",
            "value": sympathy,
            "inline": False
        })
    
    poland_time = convert_to_poland_time(filing.get('updated', ''))
    
    embed = {
        "title": title,
        "description": description,
        "color": color,
        "fields": fields,
        "footer": {
            "text": f"⏰ {poland_time} | 🤖 M&A Scanner v1.1 | Powered by Groq AI"
        }
    }
    
    if filing.get('link'):
        embed["url"] = filing['link']
    
    # Add TradingView link if ticker available
    if yahoo_data and yahoo_data.get('ticker'):
        ticker = yahoo_data['ticker']
        embed["author"] = {
            "name": f"View {ticker} Chart",
            "url": f"https://www.tradingview.com/chart/?symbol={ticker}"
        }
    
    payload = {"embeds": [embed]}
    
    try:
        response = requests.post(webhook_url, json=payload, timeout=10)
        response.raise_for_status()
        print(f"✓ Sent {priority} alert for {company}")
    except Exception as e:
        print(f"Error sending Discord alert: {e}")

# ============================================
# MAIN SCANNER
# ============================================

def scan_ma_deals():
    """Main scanning function"""
    print("\n" + "="*60)
    print(f"SEC M&A Scanner - Item 1.01 | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("="*60)
    
    # Load processed filings
    processed = load_processed_from_gist()
    print(f"Already processed: {len(processed)} filings")
    
    # Fetch recent 8-K
    filings = fetch_recent_8k()
    
    if not filings:
        print("No 8-K filings found")
        return
    
    new_alerts = 0
    
    for filing in filings:
        # Get accession and CIK from filing info
        accession = filing.get('accession')
        cik = filing.get('cik')
        
        if not accession:
            # Fallback: try to extract from link
            accession = extract_accession_from_link(filing.get('link', ''))
        
        if not accession:
            print(f"   ✗ Could not extract accession from: {filing.get('title', 'Unknown')[:60]}")
            continue
        
        filing_id = accession
        
        # Skip if already processed
        if filing_id in processed:
            continue
        
        print(f"\n🔍 Analyzing: {filing.get('title', 'Unknown')[:60]}...")
        print(f"   CIK: {cik}, Accession: {accession}")
        
        # Fetch document
        content = fetch_document_content(accession, filing.get('link', ''), cik)
        
        if not content:
            processed.add(filing_id)
            continue
        
        # Check for Item 1.01
        if not has_item_101(content):
            print("   ↳ No Item 1.01 - skipping")
            processed.add(filing_id)
            continue
        
        print("   ✓ Found Item 1.01!")
        
        # Extract company info
        company_info = extract_company_info(content)
        filing['company'] = company_info['company']
        
        # Extract ticker from document
        ticker = extract_ticker_from_document(content)
        
        # Fetch Yahoo Finance data
        yahoo_data = {}
        if ticker:
            yahoo_data = get_yahoo_finance_data(ticker, filing['company'])
            time.sleep(1)  # Rate limiting for Yahoo Finance
        else:
            print("   ↳ No ticker found - skipping Yahoo Finance")
        
        # AI Analysis with Groq + Yahoo Finance data
        analysis = analyze_with_groq(content, company_info, yahoo_data)
        
        if not analysis:
            print("   ↳ Groq analysis failed")
            processed.add(filing_id)
            continue
        
        # Route based on impact score
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
            print(f"   ↳ Low impact ({impact}/10) - skipping alert")
        
        processed.add(filing_id)
        
        # Rate limiting
        time.sleep(2)
    
    # Save processed filings
    save_processed_to_gist(processed)
    
    print("\n" + "="*60)
    print(f"✓ Scan complete: {new_alerts} new alerts sent")
    print("="*60 + "\n")

# ============================================
# ENTRY POINT
# ============================================

if __name__ == "__main__":
    scan_ma_deals()
