#!/usr/bin/env python3
"""
Debug search parameters to understand why results are 0.
"""

import sys
import json
import pprint
from pathlib import Path

# Add src to Python path
sys.path.insert(0, str(Path(__file__).parent / 'src'))

from api_client import LocApiClient

def debug_search():
    """Debug search parameters."""
    client = LocApiClient(request_delay=3.0)
    
    print("🐛 Debugging search parameters...\n")
    
    # Test different parameter combinations
    test_cases = [
        {
            'name': 'Original format with at=results',
            'params': {
                'format': 'json',
                'date1': '01/01/1906',
                'date2': '12/31/1906',
                'rows': 5,
                'at': 'results'
            }
        },
        {
            'name': 'With fo=json instead of format',
            'params': {
                'fo': 'json',
                'date1': '01/01/1906',
                'date2': '12/31/1906',
                'rows': 5
            }
        },
        {
            'name': 'Minimal parameters',
            'params': {
                'fo': 'json',
                'rows': 5
            }
        },
        {
            'name': 'Just format parameter',
            'params': {
                'format': 'json',
                'rows': 5
            }
        }
    ]
    
    for test_case in test_cases:
        print(f"🧪 Testing: {test_case['name']}")
        print(f"   Parameters: {test_case['params']}")
        
        try:
            result = client._make_request('search/pages/results/', test_case['params'])
            results_count = len(result.get('results', []))
            total_items = result.get('totalItems', 0)
            
            print(f"   ✅ Results: {results_count}/{total_items}")
            
            if results_count > 0:
                print(f"   📋 Sample keys: {list(result['results'][0].keys())}")
            else:
                print(f"   🔍 Response keys: {list(result.keys())}")
                # Show any error messages or additional info
                for key, value in result.items():
                    if key not in ['results', 'totalItems'] and not isinstance(value, list):
                        print(f"   {key}: {value}")
            
        except Exception as e:
            print(f"   ❌ Error: {e}")
        
        print()
    
    # Try the search endpoint without /results/
    print("🔍 Testing search endpoint without /results/...")
    try:
        result = client._make_request('search/pages/', {'format': 'json', 'rows': 5})
        print(f"✅ search/pages/ response keys: {list(result.keys())}")
        if 'results' in result:
            results_count = len(result.get('results', []))
            print(f"   Results count: {results_count}")
    except Exception as e:
        print(f"❌ Error with search/pages/: {e}")

if __name__ == '__main__':
    debug_search()