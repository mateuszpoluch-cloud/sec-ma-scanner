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
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '')
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
        for line in lines[:100]:
            # Look for trading symbol in header
            if 'TRADING SYMBOL' in line.upper() or 'TICKER SYMBOL' in line.upper():
                parts = line.split(':')
                if len(parts) > 1:
                    ticker = parts[1].strip().split()[0]
                    return ticker.upper()
            # Look for common patterns
            if 'NASDAQ:' in line.upper() or 'NYSE:' in line.upper():
                parts = line.split(':')
                if len(parts) > 1:
                    ticker = parts[1].strip().split()[0]
                    return ticker.upper()
        return None
    except:
        return None

def get_yahoo_finance_data(ticker: Optional[str], company_name: str) -> Dict:
    """Fetch real-time data from Yahoo Finance"""
    
    if not YFINANCE_AVAILABLE:
        print("   → Yahoo Finance not available (install: pip install yfinance)")
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
                filing_info = {
                    'title': title.text,
                    'updated': updated.text if updated is not None else '',
                    'link': link.get('href') if link is not None else ''
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
            for part in parts:
                # Accession format: XXXXXXXXXX-XX-XXXXXX (10-2-6 digits with dashes)
                if len(part) >= 18 and part.count('-') >= 2:
                    # Remove -index.htm suffix if present
                    accession = part.replace('-index.htm', '').replace('-index.html', '')
                    # Validate it looks like accession (has dashes in right places)
                    if accession.count('-') == 2:
                        return accession
        
        return None
    except:
        return None

def fetch_document_content(accession: str, link: str = "") -> str:
    """Fetch full 8-K document content"""
    try:
        # Extract CIK from link if provided (RSS format)
        cik = None
        if link and '/data/' in link:
            # Extract CIK from: /Archives/edgar/data/874501/000162828025047943/...
            parts = link.split('/data/')
            if len(parts) > 1:
                cik = parts[1].split('/')[0]
        
        # Build document URL
        acc_no_dashes = accession.replace('-', '')
        
        if cik:
            # Use CIK from link
            url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_no_dashes}/{accession}.txt"
        else:
            # Fallback: try to extract CIK from accession (first 10 digits without dashes)
            # This might not always work but better than nothing
            url = f"https://www.sec.gov/Archives/edgar/data/{acc_no_dashes[:10]}/{acc_no_dashes}/{accession}.txt"
        
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
# GEMINI AI ANALYSIS
# ============================================

def analyze_with_gemini(document: str, company_info: Dict, yahoo_data: Dict) -> Optional[Dict]:
    """Analyze M&A deal with Gemini AI + Yahoo Finance data"""
    if not GEMINI_API_KEY:
        print("Warning: No GEMINI_API_KEY set")
        return None
    
    try:
        # Build Yahoo Finance context
        yf_context = ""
        if yahoo_data and not yahoo_data.get('error'):
            yf_context = f"""
REAL-TIME MARKET DATA (Yahoo Finance):
Ticker: {yahoo_data.get('ticker', 'N/A')}
Current Price: ${yahoo_data.get('current_price', 'N/A')}
Market Cap: {yahoo_data.get('market_cap_formatted', 'Unknown')}
Volume Today: {yahoo_data.get('volume', 'N/A'):,} (Avg: {yahoo_data.get('avg_volume', 'N/A'):,})
Volume Spike: {yahoo_data.get('volume_spike', 1):.1f}x normal
1-Week Change: {yahoo_data.get('week_change_pct', 0):+.2f}%
1-Month Change: {yahoo_data.get('month_change_pct', 0):+.2f}%
52-Week Range: ${yahoo_data.get('fifty_two_week_low', 'N/A')} - ${yahoo_data.get('fifty_two_week_high', 'N/A')}
P/E Ratio: {yahoo_data.get('pe_ratio', 'N/A')}
Shares Outstanding: {f"{yahoo_data.get('shares_outstanding'):,}" if yahoo_data.get('shares_outstanding') else 'N/A'}
Float: {f"{yahoo_data.get('float_shares'):,}" if yahoo_data.get('float_shares') else 'N/A'}
Short Interest: {yahoo_data.get('short_percent', 'N/A')}
Institutional Ownership: {yahoo_data.get('institutional_ownership', 'N/A')}
Analyst Target: ${yahoo_data.get('analyst_target', 'N/A')}
Recommendation: {yahoo_data.get('recommendation', 'N/A').upper() if yahoo_data.get('recommendation') else 'N/A'}
"""
        else:
            yf_context = "\nREAL-TIME MARKET DATA: Not available\n"

        prompt = f"""Jesteś ekspertem analizy M&A i partnerships. Przeanalizuj ten dokument 8-K Item 1.01.

FIRMA: {company_info['company']}

{yf_context}

DOKUMENT 8-K:
{document[:15000]}

INSTRUKCJE OBLICZANIA PREMIUM:

KROK 1: Znajdź wartość transakcji w dokumencie
- Szukaj: "aggregate consideration", "purchase price", "transaction value"
- Format może być: $X million, $X billion, $X per share

KROK 2: Użyj Yahoo Finance market cap jako bazy
- Current Market Cap: {yahoo_data.get('market_cap_formatted', 'Unknown')}
- Current Price: ${yahoo_data.get('current_price', 'N/A')}
- Shares Outstanding: {yahoo_data.get('shares_outstanding', 'N/A')}

KROK 3: Oblicz premium
Premium % = (Deal Value - Current Market Cap) / Current Market Cap × 100

PRZYKŁAD:
Current Market Cap: $250M
Deal Value: $1.2B
Premium = ($1,200M - $250M) / $250M × 100 = 380%

KROK 4: Oblicz offer price per share (jeśli możliwe)
Offer Price = Deal Value / Shares Outstanding
Upside = (Offer Price - Current Price) / Current Price × 100

OCEŃ IMPACT NA CENĘ AKCJI (1-10):

KRYTERIA WYSOKIEGO IMPACT (9-10):
✅ Premium >50% (im wyższy tym lepiej!)
✅ Strategic acquirer (duży kupuje małego = najlepsze!)
✅ All-cash deal (pewność finansowania)
✅ Synergies >20% (silny strategic fit)
✅ Sector consolidation trend (3+ M&A w sektorze ostatnio)
✅ High volume spike (>3x = institutions loading)
✅ High institutional ownership (>70% = smooth approval)

KRYTERIA ŚREDNIEGO (5-8):
• Premium 20-50%
• Mixed payment (cash + stock)
• Moderate synergies
• Standard transaction

KRYTERIA NISKIEGO (1-4):
• Premium <20% (słaby deal!)
• All-stock deal (ryzyko dla akcji acquirera)
• No clear synergies
• High regulatory risk
• Already huge runup (>30% w tydzień = leak, mało upsidu)

DODATKOWE UWAGI:
- Jeśli volume spike >5x = insider leak prawdopodobny
- Jeśli week change >30% = runup przed announcement
- Jeśli float mały (<5M shares) = untradable for retail ⚠️
- Jeśli short interest >20% = potential short squeeze 🚀
- Jeśli institutional ownership >80% = high approval probability

ODPOWIEDZ W JSON:
{{
  "impact_score": X,
  "deal_type": "acquisition/partnership/contract",
  "target_company": "nazwa",
  "acquirer": "nazwa",
  "deal_value": "wartość lub 'undisclosed'",
  "deal_value_numeric": liczba_w_usd_lub_null,
  "premium_pct": Y lub null,
  "premium_calculation": "szczegółowe obliczenie krok po kroku",
  "offer_price_per_share": Z lub null,
  "upside_to_offer": "+X%" lub null,
  "verdict": "MEGA/HIGH/MEDIUM/LOW",
  "short_term_move": "+/-X%",
  "confidence": X,
  "deal_structure": "all-cash/stock/mixed + %",
  "strategic_rationale": "dlaczego ten deal ma sens",
  "key_points": ["punkt1", "punkt2", "punkt3"],
  "risks": ["risk1", "risk2", "risk3"],
  "sympathy_plays": ["ticker1: reason", "ticker2: reason"],
  "sector_context": "czy to część większego trendu?",
  "liquidity_warning": "jeśli float mały lub volume niska",
  "leak_detected": true/false,
  "leak_reasoning": "dlaczego sądzisz że był leak",
  "approval_probability": "X%",
  "reasoning": "pełne uzasadnienie impact score"
}}

WAŻNE:
- Używaj DOKŁADNYCH liczb z dokumentu
- Jeśli Yahoo Finance ma current market cap - użyj tego do premium calc!
- Jeśli brak market cap w Yahoo - wyciągnij z dokumentu lub oszacuj
- Bądź conservative z impact scores - 10/10 tylko dla wyjątkowych dealów!
- Zwróć TYLKO JSON, bez dodatkowego tekstu."""

        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
        
        payload = {
            "contents": [{
                "parts": [{"text": prompt}]
            }],
            "generationConfig": {
                "temperature": 0.3,
                "maxOutputTokens": 1024
            }
        }
        
        response = requests.post(url, json=payload, timeout=45)
        response.raise_for_status()
        
        result = response.json()
        
        if 'candidates' in result and len(result['candidates']) > 0:
            text = result['candidates'][0]['content']['parts'][0]['text']
            
            # Extract JSON from response
            text = text.strip()
            if '```json' in text:
                text = text.split('```json')[1].split('```')[0]
            elif '```' in text:
                text = text.split('```')[1].split('```')[0]
            
            analysis = json.loads(text.strip())
            print(f"✓ Gemini analysis: Impact {analysis.get('impact_score', 0)}/10 (Premium: {analysis.get('premium_pct', 'N/A')}%)")
            return analysis
        
        return None
        
    except Exception as e:
        print(f"Error with Gemini: {e}")
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
    
    description += f"\n**GEMINI IMPACT: {analysis.get('impact_score', 0)}/10**\n"
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
    
    if analysis.get('reasoning'):
        fields.append({
            "name": "💡 Full Reasoning",
            "value": analysis['reasoning'][:500],
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
    
    # Sector Context
    if analysis.get('sector_context'):
        fields.append({
            "name": "📈 Sector Context",
            "value": analysis['sector_context'][:300],
            "inline": False
        })
    
    poland_time = convert_to_poland_time(filing.get('updated', ''))
    
    embed = {
        "title": title,
        "description": description,
        "color": color,
        "fields": fields,
        "footer": {
            "text": f"⏰ {poland_time} | 🤖 M&A Scanner v1.0 + Yahoo Finance | Powered by Gemini"
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
        # Extract accession number
        accession = extract_accession_from_link(filing.get('link', ''))
        
        if not accession:
            continue
        
        filing_id = accession
        
        # Skip if already processed
        if filing_id in processed:
            continue
        
        print(f"\n🔍 Analyzing: {filing.get('title', 'Unknown')[:60]}...")
        
        # Fetch document (pass link to extract CIK)
        content = fetch_document_content(accession, filing.get('link', ''))
        
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
        
        # AI Analysis with Yahoo Finance data
        analysis = analyze_with_gemini(content, company_info, yahoo_data)
        
        if not analysis:
            print("   ↳ Gemini analysis failed")
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
