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
