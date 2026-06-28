"""
Library of Congress Chronicling America API Client

⚠️ DEPRECATED: This module is deprecated. Use rate_limited_client.LocApiClient instead.

The new rate_limited_client module provides:
- Centralized singleton rate limiting across all API clients
- Better handling for 429 errors and CAPTCHA detection  
- Thread-safe request management
- Improved statistics and monitoring

This file is kept for backward compatibility with existing test files.

MIGRATION NOTE: This file has been disconnected from the module. 
It remains so future users can rerun historical investigation scripts. 
"""

import time
import logging
import requests
import warnings
from typing import Dict, List, Optional, Generator
from urllib.parse import urljoin
from datetime import datetime
import json


class LocApiClient:
    """Client for interacting with the Library of Congress Chronicling America API."""
    
    def __init__(self, base_url: str = "https://chroniclingamerica.loc.gov/", 
                 request_delay: float = 3.0, max_retries: int = 3):
        warnings.warn(
            "api_client.LocApiClient is deprecated. Use rate_limited_client.LocApiClient instead "
            "for centralized singleton rate limiting across all components.",
            DeprecationWarning,
            stacklevel=2
        )
        
        self.base_url = base_url.rstrip('/') + '/'
        # LOC allows 20 requests/minute = 3 seconds between requests minimum
        self.request_delay = max(request_delay, 3.0)
        self.max_retries = max_retries
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Newsagger/0.1.0 (Educational Archive Tool)'
        })
        self.logger = logging.getLogger(__name__)
        
    def _make_request(self, endpoint: str, params: Optional[Dict] = None) -> Dict:
        """Make a rate-limited request to the API with retries and 429 handling."""
        url = urljoin(self.base_url, endpoint)
        
        for attempt in range(self.max_retries):
            try:
                time.sleep(self.request_delay)
                response = self.session.get(url, params=params, timeout=60)
                
                # Handle rate limiting (429) or CAPTCHA responses
                if response.status_code == 429:
                    self.logger.warning(f"Rate limited (429) - LOC API requires 1 hour wait")
                    self.logger.warning(f"You can interrupt with Ctrl+C and try again later")
                    self.logger.warning(f"Consider using --batch-size=20 to reduce API calls")
                    
                    # Instead of blocking for 1 hour, raise an exception that can be handled
                    raise requests.exceptions.RequestException(
                        "Rate limited by LOC API (429). Must wait ~1 hour before retry. "
                        "Try reducing batch size or running discovery later."
                    )
                
                # Check for CAPTCHA in HTML response
                if 'captcha' in response.text.lower() or 'recaptcha' in response.text.lower():
                    self.logger.warning(f"CAPTCHA detected - LOC API requires wait")
                    raise requests.exceptions.RequestException(
                        "CAPTCHA detected by LOC API. Must wait before retry. "
                        "Try running discovery later with reduced batch size."
                    )
                
                response.raise_for_status()
                return response.json()
                
            except requests.exceptions.RequestException as e:
                self.logger.warning(f"Request failed (attempt {attempt + 1}): {e}")
                if attempt == self.max_retries - 1:
                    raise
                # Exponential backoff for other errors
                time.sleep(self.request_delay * (2 ** attempt))
                
    def get_newspapers(self, page: int = 1, rows: int = 1000) -> Dict:
        """Get list of newspaper titles with up to 1000 per page."""
        # Limit to recommended max of 1000 items per page
        rows = min(rows, 1000)
        params = {
            'format': 'json',
            'page': page,
            'rows': rows
        }
        return self._make_request('newspapers.json', params)
    
    def get_all_newspapers(self) -> Generator[Dict, None, None]:
        """Generator to fetch all newspapers with pagination."""
        page = 1
        while True:
            data = self.get_newspapers(page=page)
            newspapers = data.get('newspapers', [])
            
            if not newspapers:
                break
                
            for newspaper in newspapers:
                yield newspaper
                
            page += 1
            
            # Check if we've reached the end
            if page > data.get('totalPages', 1):
                break
    
    def get_batches(self) -> Dict:
        """Get information about digitization batches."""
        return self._make_request('batches.json')
    
    def search_pages(self, andtext: str = "", date1: str = "1836", 
                    date2: str = None, sort: str = "date", 
                    page: int = 1, rows: int = 1000, dates_facet: str = None) -> Dict:
        """Search newspaper pages with faceting support to avoid deep paging."""
        # Limit to recommended max of 1000 items per page
        rows = min(rows, 1000)
        
        # Default date2 to current year if not specified
        if date2 is None:
            date2 = str(datetime.now().year)
        
        # Convert date format for LOC API (expects MM/DD/YYYY format for search)
        formatted_date1 = self._format_search_date(date1, is_end_date=False)
        formatted_date2 = self._format_search_date(date2, is_end_date=True)
            
        params = {
            'format': 'json',
            'sort': sort,
            'page': page,
            'rows': rows
        }
        
        # Add search text if provided
        if andtext:
            params['andtext'] = andtext
            
        # Add date range
        params['date1'] = formatted_date1
        params['date2'] = formatted_date2
        
        # Add date faceting if specified to limit result sets
        if dates_facet:
            params['dates'] = dates_facet
            
        response = self._make_request('search/pages/results/', params)
        
        # Normalize response format: API returns 'items' but our code expects 'results'
        if 'items' in response and 'results' not in response:
            response['results'] = response['items']
            
        return response
    
    def _format_search_date(self, date_str: str, is_end_date: bool = False) -> str:
        """Format date for LOC search API (expects MM/DD/YYYY)."""
        if len(date_str) == 4:
            # Year only - use January 1st for start, December 31st for end
            year = date_str
            if is_end_date:
                return f"12/31/{year}"
            else:
                return f"01/01/{year}"
        elif len(date_str) == 10 and date_str.count('-') == 2:
            # YYYY-MM-DD format - convert to MM/DD/YYYY
            parts = date_str.split('-')
            return f"{parts[1]}/{parts[2]}/{parts[0]}"
        else:
            # Assume it's already in correct format or close enough
            return date_str
    
    def get_page_metadata(self, lccn: str, date: str, edition: int, sequence: int) -> Dict:
        """Get metadata for a specific newspaper page."""
        endpoint = f'lccn/{lccn}/issues/{date}/ed-{edition}/seq-{sequence}.json'
        return self._make_request(endpoint)
    
    def get_newspaper_issues(self, lccn: str) -> Dict:
        """Get all issues for a specific newspaper."""
        endpoint = f'lccn/{lccn}.json'
        return self._make_request(endpoint)
    
    def get_newspaper_detail(self, lccn: str) -> Dict:
        """Get detailed information for a specific newspaper (alias for get_newspaper_issues)."""
        return self.get_newspaper_issues(lccn)
    
    def get_newspapers_with_details(self, max_newspapers: int = 50) -> Generator[Dict, None, None]:
        """
        Get newspapers with their detailed metadata.
        This is slower due to individual API calls but provides complete data.
        """
        newspapers_response = self.get_newspapers(rows=max_newspapers)
        newspapers = newspapers_response.get('newspapers', [])
        
        for newspaper in newspapers:
            try:
                # Get basic info
                basic_info = newspaper
                
                # Get detailed info
                detail_response = self.get_newspaper_detail(newspaper['lccn'])
                
                # Combine the data
                combined_data = {
                    **basic_info,
                    **detail_response
                }
                
                yield combined_data
                
            except Exception as e:
                self.logger.warning(f"Failed to get details for {newspaper.get('lccn', 'unknown')}: {e}")
                # Yield basic info if detail fetch fails
                yield newspaper
    
    def estimate_download_size(self, date_range: tuple, newspaper_lccn: Optional[str] = None) -> Dict:
        """Estimate the total size and time for a download operation using accurate sampling."""
        date1, date2 = date_range
        
        # Use dates facet for more accurate filtering
        search_params = {
            'date1': date1,
            'date2': date2,
            'dates_facet': f"{date1}/{date2}",
            'rows': 100  # Sample size for estimation
        }
        
        # Get a sample to estimate total results more accurately
        sample = self.search_pages(**search_params)
        sample_results = sample.get('items', [])
        
        if not sample_results:
            # No results found for this date range
            return {
                'total_pages': 0,
                'estimated_size_gb': 0,
                'estimated_time_hours': 0,
                'date_range': f"{date1}-{date2}",
                'newspaper_lccn': newspaper_lccn
            }
        
        # Check if we got fewer results than requested (indicates total < sample size)
        if len(sample_results) < 100:
            total_results = len(sample_results)
        else:
            # Try to get a better estimate by checking a few more pages
            # This is more API-efficient than the broken totalItems field
            total_results = len(sample_results)
            
            # Sample a few more pages to estimate total
            for page in [2, 3]:
                try:
                    next_sample = self.search_pages(page=page, **search_params)
                    next_results = next_sample.get('items', [])
                    if not next_results:
                        break
                    total_results += len(next_results)
                    if len(next_results) < 100:
                        break
                except:
                    break
            
            # If we got 300 results, estimate there might be more
            if total_results >= 300:
                # Conservative estimate: assume 500-2000 results for a single year
                total_results = min(total_results * 3, 2000)
        
        # Rough estimates based on typical newspaper page sizes
        avg_page_size_mb = 2.5  # Average size per page including images/text
        estimated_size_gb = (total_results * avg_page_size_mb) / 1024
        
        # Time estimation based on rate limiting
        estimated_time_hours = (total_results * self.request_delay) / 3600
        
        return {
            'total_pages': total_results,
            'estimated_size_gb': round(estimated_size_gb, 2),
            'estimated_time_hours': round(estimated_time_hours, 2),
            'date_range': f"{date1}-{date2}",
            'newspaper_lccn': newspaper_lccn
        }
    
    def get_search_facets(self, collection_url: str = None) -> Dict:
        """Get facets for a search query to enable faceted downloading."""
        endpoint = collection_url or 'search/'
        params = {
            'fo': 'json',
            'at': 'facets'
        }
        return self._make_request(endpoint, params)
    
    def search_with_faceted_dates(self, base_query: Dict, max_results_per_facet: int = 100000) -> Generator[Dict, None, None]:
        """
        Perform faceted search using date ranges to avoid deep paging limits.
        Yields results from each date facet to stay under 100,000 item limit.
        """
        # First get facets to understand date distribution
        facets_response = self.get_search_facets()
        
        # Look for date facets in the response
        date_facets = []
        for facet_key, facet_data in facets_response.get('facets', {}).items():
            if facet_data.get('type') == 'dates':
                date_facets = facet_data.get('filters', [])
                break
        
        if not date_facets:
            # Fallback to decade-based faceting if no facets available
            current_year = datetime.now().year
            start_year = 1836
            
            for decade_start in range(start_year, current_year, 10):
                decade_end = min(decade_start + 9, current_year)
                dates_facet = f"{decade_start}/{decade_end}"
                
                # Search with this date facet
                search_params = base_query.copy()
                search_params['dates_facet'] = dates_facet
                
                page = 1
                while True:
                    search_params['page'] = page
                    results = self.search_pages(**search_params)
                    
                    if not results.get('items'):
                        break
                        
                    yield results
                    
                    # Check if we have more pages and haven't hit limits
                    if len(results.get('items', [])) < search_params.get('rows', 1000):
                        break
                    
                    page += 1
                    # Prevent deep paging beyond reasonable limits
                    if page * search_params.get('rows', 1000) >= max_results_per_facet:
                        self.logger.warning(f"Reached max results limit for facet {dates_facet}")
                        break
        else:
            # Use actual facets from the API
            for facet in date_facets:
                if facet.get('count', 0) > max_results_per_facet:
                    self.logger.warning(f"Skipping facet {facet.get('title')} with {facet.get('count')} items (exceeds limit)")
                    continue
                
                # Extract date range from facet
                dates_range = facet.get('title', '').replace(' to ', '/')
                
                search_params = base_query.copy()
                search_params['dates_facet'] = dates_range
                
                page = 1
                while True:
                    search_params['page'] = page
                    results = self.search_pages(**search_params)
                    
                    if not results.get('items'):
                        break
                        
                    yield results
                    
                    if len(results.get('items', [])) < search_params.get('rows', 1000):
                        break
                    
                    page += 1