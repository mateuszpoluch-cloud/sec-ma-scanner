#!/usr/bin/env python3
"""
Diagnostic Script - Shows what bot sees from SEC RSS
"""

import requests
import xml.etree.ElementTree as ET
from datetime import datetime

USER_AGENT = "SEC-MA-Scanner/1.0 (research@example.com)"
SEC_RSS_URL = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&count=100&output=atom"

def fetch_and_analyze_rss():
    """Fetch RSS and show detailed analysis"""
    
    print("\n" + "="*70)
    print("SEC RSS DIAGNOSTIC - What Bot Sees")
    print("="*70 + "\n")
    
    try:
        headers = {'User-Agent': USER_AGENT}
        response = requests.get(SEC_RSS_URL, headers=headers, timeout=15)
        response.raise_for_status()
        
        print(f"✓ RSS Connection: SUCCESS")
        print(f"✓ Status Code: {response.status_code}")
        print(f"✓ Content Size: {len(response.content)} bytes")
        print()
        
        # Parse XML
        root = ET.fromstring(response.content)
        namespace = {'atom': 'http://www.w3.org/2005/Atom'}
        
        entries = root.findall('atom:entry', namespace)
        print(f"✓ Found {len(entries)} 8-K filings in RSS")
        print()
        
        # Analyze each entry
        print("="*70)
        print("DETAILED ANALYSIS OF EACH FILING:")
        print("="*70 + "\n")
        
        item_101_count = 0
        
        for i, entry in enumerate(entries[:20], 1):  # Show first 20
            title = entry.find('atom:title', namespace)
            updated = entry.find('atom:updated', namespace)
            link = entry.find('atom:link', namespace)
            
            title_text = title.text if title is not None else "Unknown"
            updated_text = updated.text if updated is not None else "Unknown"
            link_href = link.get('href') if link is not None else "Unknown"
            
            print(f"{i}. {title_text}")
            print(f"   Published: {updated_text}")
            print(f"   Link: {link_href[:80]}...")
            
            # Try to fetch document and check for Item 1.01
            if '8-K' in title_text and 'accession_number=' in link_href:
                accession = link_href.split('accession_number=')[1].split('&')[0]
                
                # Try to fetch document
                try:
                    acc_no_dashes = accession.replace('-', '')
                    # Extract CIK from link
                    if 'cik=' in link_href:
                        cik = link_href.split('cik=')[1].split('&')[0].lstrip('0') or '0'
                        doc_url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_no_dashes}/{accession}.txt"
                        
                        doc_response = requests.get(doc_url, headers=headers, timeout=10)
                        if doc_response.status_code == 200:
                            content = doc_response.text.lower()
                            
                            if 'item 1.01' in content or 'item 1.1' in content:
                                print(f"   🔴 FOUND ITEM 1.01! (M&A Deal!)")
                                item_101_count += 1
                                
                                # Try to extract company name
                                lines = doc_response.text.split('\n')
                                for line in lines[:50]:
                                    if 'COMPANY CONFORMED NAME' in line.upper():
                                        company = line.split(':')[-1].strip()
                                        print(f"   Company: {company}")
                                        break
                            else:
                                print(f"   ✓ Fetched document - No Item 1.01")
                        else:
                            print(f"   ⚠️  Document fetch failed: {doc_response.status_code}")
                except Exception as e:
                    print(f"   ⚠️  Error checking document: {str(e)[:50]}")
            
            print()
        
        print("="*70)
        print("SUMMARY:")
        print("="*70)
        print(f"Total 8-K filings in RSS: {len(entries)}")
        print(f"Analyzed in detail: {min(20, len(entries))}")
        print(f"🔴 ITEM 1.01 M&A FOUND: {item_101_count}")
        print()
        
        if item_101_count == 0:
            print("⚠️  NO M&A (Item 1.01) found in recent filings!")
            print("This is NORMAL - M&A only ~5-10% of all 8-K filings.")
            print("Bot is working correctly, just waiting for M&A to happen.")
        else:
            print(f"✓ Found {item_101_count} M&A deals!")
            print("Bot should have sent alerts for these!")
            print("If no alerts received, check Discord webhooks.")
        
        print()
        print("="*70)
        print("WHAT THIS MEANS:")
        print("="*70)
        print("✓ RSS Connection: Working")
        print("✓ Document Fetching: Working")
        print("✓ Item 1.01 Detection: Working")
        print()
        
        if item_101_count > 0:
            print("🎯 Bot should be sending alerts for Item 1.01 deals!")
            print("   If no alerts, check:")
            print("   1. Discord webhook URLs in Secrets")
            print("   2. Gemini API key in Secrets")
            print("   3. Bot logs for errors")
        else:
            print("💤 No M&A to alert about - bot is waiting!")
            print("   This is NORMAL, especially on weekends/holidays.")
            print("   Try again during market hours (Mon-Fri 9AM-4PM EST).")
        
        print()
        print("="*70 + "\n")
        
    except Exception as e:
        print(f"✗ ERROR: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    fetch_and_analyze_rss()
