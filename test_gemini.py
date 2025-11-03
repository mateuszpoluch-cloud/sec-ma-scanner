#!/usr/bin/env python3
"""
Gemini API Test - Check if API key works and which models are available
"""

import os
import requests
import json

print("\n" + "="*70)
print("GEMINI API TEST")
print("="*70 + "\n")

# Get API key from environment or user input
api_key = os.environ.get('GEMINI_API_KEY')

if not api_key:
    print("⚠️  GEMINI_API_KEY not found in environment")
    print("This test will run in GitHub Actions with your secret.")
    print()
    print("To test locally, run:")
    print("  export GEMINI_API_KEY='your-key-here'")
    print("  python test_gemini.py")
    print()
    exit(0)

print(f"✓ API Key found: {api_key[:10]}...")
print()

# Test different models
models_to_test = [
    "gemini-1.5-flash",
    "gemini-1.5-flash-latest",
    "gemini-pro",
    "gemini-1.5-pro"
]

test_prompt = "Respond with just: OK"

print("="*70)
print("TESTING MODELS")
print("="*70 + "\n")

working_models = []

for model_name in models_to_test:
    print(f"Testing: {model_name}")
    
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
        
        payload = {
            "contents": [{
                "parts": [{"text": test_prompt}]
            }],
            "generationConfig": {
                "temperature": 0.1,
                "maxOutputTokens": 100
            }
        }
        
        response = requests.post(url, json=payload, timeout=15)
        
        if response.status_code == 200:
            result = response.json()
            if 'candidates' in result:
                text = result['candidates'][0]['content']['parts'][0]['text']
                print(f"  ✓ SUCCESS - Response: {text.strip()}")
                working_models.append(model_name)
            else:
                print(f"  ⚠️  Unexpected response format")
        else:
            print(f"  ✗ FAILED - HTTP {response.status_code}")
            if response.status_code == 404:
                print(f"     Model not found or not available in your region")
            elif response.status_code == 403:
                print(f"     API key invalid or no access to this model")
            else:
                try:
                    error = response.json()
                    print(f"     Error: {error.get('error', {}).get('message', 'Unknown')}")
                except:
                    print(f"     Error: {response.text[:100]}")
    
    except requests.exceptions.Timeout:
        print(f"  ✗ TIMEOUT")
    except Exception as e:
        print(f"  ✗ ERROR: {str(e)[:80]}")
    
    print()

print("="*70)
print("SUMMARY")
print("="*70)
print(f"Working models: {len(working_models)}/{len(models_to_test)}")
print()

if working_models:
    print("✓ GEMINI API IS WORKING!")
    print()
    print("Available models:")
    for model in working_models:
        print(f"  • {model}")
    print()
    print("Recommended for bot: " + working_models[0])
else:
    print("✗ NO WORKING MODELS FOUND!")
    print()
    print("Possible issues:")
    print("  1. API key is invalid")
    print("  2. API key doesn't have access to Gemini models")
    print("  3. Region restriction (Gemini not available in your location)")
    print("  4. Account quota exceeded")
    print()
    print("Solutions:")
    print("  • Check API key at: https://aistudio.google.com/app/apikey")
    print("  • Verify key starts with: AIza...")
    print("  • Check regional availability")
    print("  • Try creating a new API key")

print()
print("="*70 + "\n")
