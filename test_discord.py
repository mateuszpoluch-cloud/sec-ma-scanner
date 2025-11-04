#!/usr/bin/env python3
"""
Discord Webhook Tester
Testuje połączenie z webhookami Discord
"""

import requests
import os
import sys
from datetime import datetime

# Load webhooks from environment
DISCORD_WEBHOOK_MEGA = os.environ.get('DISCORD_WEBHOOK_MEGA', '').strip()
DISCORD_WEBHOOK_MAJOR = os.environ.get('DISCORD_WEBHOOK_MAJOR', '').strip()
DISCORD_WEBHOOK_STANDARD = os.environ.get('DISCORD_WEBHOOK_STANDARD', '').strip()

def test_webhook(webhook_url: str, name: str) -> bool:
    """Test single webhook"""
    print(f"\n{'='*60}")
    print(f"Testing: {name}")
    print(f"{'='*60}")
    
    if not webhook_url:
        print(f"❌ FAILED: Webhook URL is empty")
        return False
    
    # Debug info
    print(f"URL Length: {len(webhook_url)}")
    print(f"URL Start: {webhook_url[:50]}")
    print(f"URL End: {webhook_url[-20:]}")
    print(f"URL Repr: {repr(webhook_url)}")
    
    # Check for common issues
    if '\n' in webhook_url or '\r' in webhook_url:
        print(f"⚠️  WARNING: Webhook contains newline characters!")
    if ' ' in webhook_url:
        print(f"⚠️  WARNING: Webhook contains spaces!")
    
    # Test embed message
    embed = {
        "title": f"✅ {name} Webhook Test",
        "description": f"Test message sent at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "color": 5763719,  # Green
        "fields": [
            {
                "name": "Status",
                "value": "Webhook is working correctly!",
                "inline": False
            },
            {
                "name": "Test Info",
                "value": f"• URL Length: {len(webhook_url)}\n• Timestamp: {datetime.now().isoformat()}",
                "inline": False
            }
        ],
        "footer": {
            "text": "Discord Webhook Test | M&A Scanner"
        }
    }
    
    payload = {"embeds": [embed]}
    
    try:
        print(f"\n📤 Sending test message...")
        response = requests.post(webhook_url, json=payload, timeout=10)
        
        print(f"Response Status: {response.status_code}")
        print(f"Response Headers: {dict(response.headers)}")
        
        if response.status_code == 204:
            print(f"✅ SUCCESS: Webhook is working!")
            return True
        else:
            print(f"❌ FAILED: Status {response.status_code}")
            print(f"Response Body: {response.text}")
            return False
            
    except requests.exceptions.RequestException as e:
        print(f"❌ FAILED: {e}")
        return False

def main():
    """Run all webhook tests"""
    print("\n" + "="*60)
    print("DISCORD WEBHOOK VERIFICATION")
    print("="*60)
    
    results = {}
    
    # Test all webhooks
    webhooks = [
        (DISCORD_WEBHOOK_MEGA, "MEGA"),
        (DISCORD_WEBHOOK_MAJOR, "MAJOR"),
        (DISCORD_WEBHOOK_STANDARD, "STANDARD")
    ]
    
    for webhook_url, name in webhooks:
        results[name] = test_webhook(webhook_url, name)
    
    # Summary
    print("\n" + "="*60)
    print("TEST SUMMARY")
    print("="*60)
    
    for name, success in results.items():
        status = "✅ PASS" if success else "❌ FAIL"
        print(f"{status} - {name} webhook")
    
    print("="*60 + "\n")
    
    # Exit with error if any test failed
    if not all(results.values()):
        sys.exit(1)
    else:
        print("🎉 All webhooks are working correctly!")
        sys.exit(0)

if __name__ == "__main__":
    main()
