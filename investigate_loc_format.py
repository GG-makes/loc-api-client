#!/usr/bin/env python3
"""
Investigate the actual LOC API data format in detail.
"""

import sys
import json
import pprint
from pathlib import Path

# Add src to Python path
sys.path.insert(0, str(Path(__file__).parent / 'src'))

from api_client import LocApiClient

def investigate_format():
    """Investigate actual LOC API data formats."""
    client = LocApiClient(request_delay=3.0)
    
    print("🔍 Investigating Library of Congress API data formats...\n")
    
    # 1. Get detailed newspaper info
    print("📰 Testing individual newspaper detail API...")
    try:
        # Get a specific newspaper's detailed info
        newspaper_detail = client.get_newspaper_issues('sn86072192')  # From our previous result
        print("✅ Retrieved detailed newspaper info:")
        pprint.pprint(newspaper_detail, indent=2, width=100)
        
    except Exception as e:
        print(f"❌ Error getting newspaper detail: {e}")
    
    print("\n" + "="*80 + "\n")
    
    # 2. Try a broader search for pages
    print("📄 Testing broader page search...")
    try:
        # Try a broader search to get some results
        search_response = client.search_pages(
            andtext="",  # No text filter
            date1="1906", 
            date2="1906",
            rows=5
        )
        
        print(f"✅ Search returned {len(search_response.get('results', []))} results")
        
        if search_response.get('results'):
            print("\n📋 Sample page result:")
            pprint.pprint(search_response['results'][0], indent=2, width=100)
        else:
            print("🔍 No results found. Let's try different parameters...")
            
            # Try with different dates
            search_response2 = client.search_pages(
                date1="1900", 
                date2="1910",
                rows=5
            )
            print(f"✅ Broader search (1900-1910) returned {len(search_response2.get('results', []))} results")
            
            if search_response2.get('results'):
                print("\n📋 Sample page result from broader search:")
                pprint.pprint(search_response2['results'][0], indent=2, width=100)
        
    except Exception as e:
        print(f"❌ Error searching pages: {e}")
    
    print("\n" + "="*80 + "\n")
    
    # 3. Check what endpoints are actually available
    print("🔗 Testing different API endpoints...")
    
    endpoints_to_try = [
        ('newspapers.json?rows=1', 'Newspapers list'),
        ('batches.json', 'Batches info'),
        ('search/pages/results/?rows=1', 'Page search'),
    ]
    
    for endpoint, description in endpoints_to_try:
        try:
            print(f"\n📡 Testing {description} ({endpoint})...")
            result = client._make_request(endpoint)
            print(f"✅ Response keys: {list(result.keys())}")
            
            # Show structure without full data
            for key, value in result.items():
                if isinstance(value, list) and len(value) > 0:
                    print(f"   {key}: List with {len(value)} items")
                    if len(value) > 0:
                        print(f"      Sample item keys: {list(value[0].keys()) if isinstance(value[0], dict) else type(value[0])}")
                else:
                    print(f"   {key}: {type(value)} = {value}")
                    
        except Exception as e:
            print(f"❌ Error with {description}: {e}")

if __name__ == '__main__':
    investigate_format()