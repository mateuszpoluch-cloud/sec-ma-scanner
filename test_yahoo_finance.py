#!/usr/bin/env python3
"""
Yahoo Finance Test Script
Sprawdza czy integracja działa poprawnie
"""

import os
import sys

# Dodaj ścieżkę do głównego skryptu
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Import funkcji z głównego bota
try:
    import yfinance as yf
    YFINANCE_AVAILABLE = True
    print("✓ yfinance library installed")
except ImportError:
    YFINANCE_AVAILABLE = False
    print("✗ yfinance NOT installed")
    print("Run: pip install yfinance")
    sys.exit(1)

def test_yahoo_finance():
    """Test Yahoo Finance integration"""
    
    print("\n" + "="*60)
    print("YAHOO FINANCE TEST")
    print("="*60 + "\n")
    
    # Test companies (różne wielkości)
    test_tickers = [
        ('AAPL', 'Apple (mega cap)'),
        ('NVDA', 'NVIDIA (large cap)'),
        ('PLTR', 'Palantir (mid cap)'),
        ('IONQ', 'IonQ (small cap)'),
    ]
    
    for ticker, name in test_tickers:
        print(f"\n{'='*60}")
        print(f"Testing: {ticker} - {name}")
        print('='*60)
        
        try:
            stock = yf.Ticker(ticker)
            info = stock.info
            hist = stock.history(period="1mo")
            
            # Get current data
            current_price = info.get('currentPrice') or info.get('regularMarketPrice')
            market_cap = info.get('marketCap')
            
            # Calculate recent performance
            if not hist.empty:
                week_ago_price = hist['Close'].iloc[-5] if len(hist) >= 5 else hist['Close'].iloc[0]
                current_close = hist['Close'].iloc[-1]
                week_change = ((current_close - week_ago_price) / week_ago_price * 100) if week_ago_price else 0
            else:
                week_change = 0
            
            # Format market cap
            if market_cap:
                if market_cap > 1e9:
                    mcap_formatted = f"${market_cap/1e9:.2f}B"
                else:
                    mcap_formatted = f"${market_cap/1e6:.1f}M"
            else:
                mcap_formatted = "Unknown"
            
            # Display results
            print(f"\n✓ Yahoo Finance Response:")
            print(f"  Ticker: {ticker}")
            print(f"  Current Price: ${current_price}")
            print(f"  Market Cap: {mcap_formatted}")
            print(f"  Volume: {info.get('volume', 'N/A'):,}")
            print(f"  Avg Volume: {info.get('averageVolume', 'N/A'):,}")
            
            volume_spike = (info.get('volume', 0) / info.get('averageVolume', 1)) if info.get('averageVolume') else 1
            print(f"  Volume Spike: {volume_spike:.1f}x")
            print(f"  1W Change: {week_change:+.2f}%")
            print(f"  52W High: ${info.get('fiftyTwoWeekHigh', 'N/A')}")
            print(f"  52W Low: ${info.get('fiftyTwoWeekLow', 'N/A')}")
            print(f"  P/E Ratio: {info.get('trailingPE', 'N/A')}")
            print(f"  Shares Outstanding: {info.get('sharesOutstanding', 'N/A'):,}" if info.get('sharesOutstanding') else "  Shares Outstanding: N/A")
            print(f"  Float: {info.get('floatShares', 'N/A'):,}" if info.get('floatShares') else "  Float: N/A")
            print(f"  Short %: {info.get('shortPercentOfFloat', 'N/A')}")
            print(f"  Institutional Own: {info.get('heldPercentInstitutions', 'N/A')}")
            print(f"  Analyst Target: ${info.get('targetMeanPrice', 'N/A')}")
            print(f"  Recommendation: {info.get('recommendationKey', 'N/A').upper() if info.get('recommendationKey') else 'N/A'}")
            
            # Check if data is complete
            missing = []
            if not current_price:
                missing.append("current_price")
            if not market_cap:
                missing.append("market_cap")
            if not info.get('volume'):
                missing.append("volume")
            
            if missing:
                print(f"\n⚠️  Missing data: {', '.join(missing)}")
            else:
                print(f"\n✓ All key data present!")
            
        except Exception as e:
            print(f"\n✗ Error: {e}")
            continue
    
    print("\n" + "="*60)
    print("TEST COMPLETE")
    print("="*60)
    
    print("\n📊 SUMMARY:")
    print("If you see prices and market caps above, Yahoo Finance works! ✓")
    print("If you see errors or 'N/A' everywhere, there's a problem. ✗")
    
    print("\n💡 WHAT THIS MEANS FOR THE BOT:")
    print("When bot finds Item 1.01 M&A:")
    print("1. Extracts ticker from 8-K document")
    print("2. Calls Yahoo Finance (like above)")
    print("3. Gets real-time data")
    print("4. Passes to Gemini AI for analysis")
    print("5. Sends enriched alert to Discord")
    
    print("\nIf Yahoo Finance works in this test → it will work in the bot! ✓")
    print("\n" + "="*60 + "\n")

if __name__ == "__main__":
    test_yahoo_finance()
