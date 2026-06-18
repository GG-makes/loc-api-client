"""
Centralized Rate-Limited API Client

A singleton client that ensures all API requests respect LOC rate limits
by queuing requests through a single, centralized rate limiter.
"""

import time
import logging
import requests
import threading
from typing import Dict, List, Optional, Generator
from urllib.parse import urljoin
from datetime import datetime, timedelta
import json
from queue import Queue, Empty
import atexit
import random
import warnings

from .config import Config
from .utils import retry_on_network_failure


class GlobalCaptchaManager:
    """
    Singleton class to manage global CAPTCHA state across all discovery operations.
    
    When any facet hits CAPTCHA, ALL discovery operations are blocked until
    sufficient cooling-off time has passed.
    """
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self):
        if hasattr(self, '_initialized'):
            return
            
        self.last_captcha_time = None
        self.base_cooling_off_hours = 1.0
        self.cooling_off_multiplier = 1.0
        self.consecutive_captchas = 0
        self.logger = logging.getLogger(__name__)
        self._initialized = True
    
    def record_captcha(self, context: str = "unknown"):
        """Record that a CAPTCHA was encountered, triggering global cooling-off."""
        current_time = time.time()
        
        # Check if this is a consecutive CAPTCHA (within 2 hours of last one)
        if self.last_captcha_time and (current_time - self.last_captcha_time) < 7200:
            self.consecutive_captchas += 1
            # Escalate cooling-off period for consecutive CAPTCHAs
            self.cooling_off_multiplier = min(4.0, 1.5 ** self.consecutive_captchas)
        else:
            # Reset consecutive count if enough time has passed
            self.consecutive_captchas = 1
            self.cooling_off_multiplier = 1.0
        
        self.last_captcha_time = current_time
        cooling_off_hours = self.base_cooling_off_hours * self.cooling_off_multiplier
        
        self.logger.warning(
            f"GLOBAL CAPTCHA triggered from {context}. "
            f"Consecutive: {self.consecutive_captchas}, "
            f"Cooling-off: {cooling_off_hours:.1f} hours, "
            f"Resume after: {time.ctime(current_time + cooling_off_hours * 3600)}"
        )
    
    def can_make_requests(self) -> tuple[bool, str]:
        """
        Check if any requests can be made currently.
        
        Returns:
            (can_proceed, reason) - True if requests allowed, False with reason if blocked
        """
        if self.last_captcha_time is None:
            return True, "No previous CAPTCHA"
        
        current_time = time.time()
        cooling_off_hours = self.base_cooling_off_hours * self.cooling_off_multiplier
        required_wait_time = self.last_captcha_time + (cooling_off_hours * 3600)
        
        if current_time >= required_wait_time:
            return True, "Cooling-off period completed"
        else:
            remaining_minutes = (required_wait_time - current_time) / 60
            return False, f"Global cooling-off active: {remaining_minutes:.1f} minutes remaining"
    
    def get_status(self) -> Dict:
        """Get current global CAPTCHA status."""
        can_proceed, reason = self.can_make_requests()
        
        return {
            'blocked': not can_proceed,
            'reason': reason,
            'last_captcha_time': self.last_captcha_time,
            'consecutive_captchas': self.consecutive_captchas,
            'cooling_off_hours': self.base_cooling_off_hours * self.cooling_off_multiplier if self.last_captcha_time else 0,
            'cooling_off_multiplier': self.cooling_off_multiplier
        }
    
    def reset_state(self):
        """Reset CAPTCHA state (for testing or manual override)."""
        self.logger.info("Manually resetting global CAPTCHA state")
        self.last_captcha_time = None
        self.consecutive_captchas = 0
        self.cooling_off_multiplier = 1.0


class CaptchaHandlingException(Exception):
    """Custom exception for CAPTCHA scenarios that need special handling."""
    def __init__(self, message, retry_strategy=None, suggested_params=None):
        super().__init__(message)
        self.retry_strategy = retry_strategy
        self.suggested_params = suggested_params


class RateLimitedRequestManager:
    """
    Singleton class that manages all API requests with centralized rate limiting.
    
    Ensures that no more than 12 requests per minute are made to respect
    LOC's 20 requests/minute limit with a conservative safety buffer.
    Includes advanced CAPTCHA handling with alternative retry strategies.
    """
    
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls, *args, **kwargs):
        """Singleton pattern implementation."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self, base_url: str = "https://chroniclingamerica.loc.gov/", 
                 max_requests_per_minute: int = 12, max_retries: int = 3):
        # Only initialize once (singleton pattern)
        if hasattr(self, '_initialized'):
            return
            
        self.base_url = base_url.rstrip('/') + '/'
        self.max_requests_per_minute = max_requests_per_minute
        self.max_retries = max_retries
        
        # Calculate minimum delay between requests (with safety buffer)
        self.min_request_delay = 60.0 / max_requests_per_minute
        
        # Session for connection pooling with rotating user agents
        self.session = requests.Session()
        self.user_agents = [
            'Newsagger/0.1.0 (Educational Archive Tool - Rate Limited)',
            'Newsagger/0.1.0 (Digital Humanities Research Tool)',
            'Newsagger/0.1.0 (Historical Archive Client)',
            'Newsagger/0.1.0 (Academic Research Tool)',
            'Newsagger/0.1.0 (Library Science Tool)'
        ]
        self.current_user_agent_index = 0
        self.session.headers.update({
            'User-Agent': self.user_agents[0]
        })
        
        # Rate limiting state
        self.last_request_time = 0.0
        self.request_count_window = []  # Track requests in current minute
        self.rate_limit_lock = threading.Lock()
        
        # Request queue for threading support
        self.request_queue = Queue()
        self.is_processing = False
        
        # CAPTCHA handling state
        self.captcha_count = 0
        self.last_captcha_time = 0
        self.adaptive_delay_multiplier = 1.0
        self.consecutive_captchas = 0
        self.session_start_time = time.time()
        self.immediate_captcha_detected = False
        
        # Global CAPTCHA manager
        self.global_captcha_manager = GlobalCaptchaManager()
        
        self.logger = logging.getLogger(__name__)
        self.logger.info(f"Initialized rate-limited client: max {max_requests_per_minute} req/min")
        
        # Ensure cleanup on exit
        atexit.register(self._cleanup)
        self._initialized = True
    
    def _cleanup(self):
        """Clean up resources on exit."""
        if hasattr(self, 'session'):
            self.session.close()
    
    def _wait_for_rate_limit(self):
        """
        Ensure we don't exceed rate limits by waiting if necessary.
        Uses both time-based and count-based limiting.
        """
        with self.rate_limit_lock:
            current_time = time.time()
            
            # Remove requests older than 1 minute from our tracking window
            cutoff_time = current_time - 60
            self.request_count_window = [
                req_time for req_time in self.request_count_window 
                if req_time > cutoff_time
            ]
            
            # Check if we would exceed the per-minute limit
            if len(self.request_count_window) >= self.max_requests_per_minute:
                # Wait until the oldest request in the window is > 1 minute old
                oldest_request = min(self.request_count_window)
                wait_time = 60 - (current_time - oldest_request) + 0.1  # Small buffer
                if wait_time > 0:
                    self.logger.debug(f"Rate limiting: waiting {wait_time:.1f}s to respect {self.max_requests_per_minute}/min limit")
                    time.sleep(wait_time)
                    current_time = time.time()
            
            # Also ensure minimum delay between consecutive requests with random jitter
            time_since_last = current_time - self.last_request_time
            if time_since_last < self.min_request_delay:
                # Add random jitter (±20%) to make requests less predictable
                jitter_factor = random.uniform(0.8, 1.2)
                wait_time = (self.min_request_delay - time_since_last) * jitter_factor
                self.logger.debug(f"Minimum delay with jitter: waiting {wait_time:.1f}s")
                time.sleep(wait_time)
                current_time = time.time()
            else:
                # Even if we don't need to wait, add small random delay to vary patterns
                small_jitter = random.uniform(0.1, 0.8)
                self.logger.debug(f"Adding pattern variation: {small_jitter:.1f}s")
                time.sleep(small_jitter)
                current_time = time.time()
            
            # Record this request
            self.last_request_time = current_time
            self.request_count_window.append(current_time)
    
    def _determine_captcha_strategy(self, attempt: int, params: Dict) -> Dict:
        """
        Determine the best strategy for handling CAPTCHA based on attempt count and history.
        
        Returns a strategy dictionary with action, wait_time, and other relevant parameters.
        """
        current_time = time.time()
        time_since_last_captcha = current_time - self.last_captcha_time if self.last_captcha_time else float('inf')
        time_since_session_start = current_time - self.session_start_time
        
        # Detect immediate CAPTCHA (within first 2 minutes of session)
        if time_since_session_start < 120 and attempt == 0:
            self.immediate_captcha_detected = True
            self.logger.warning("Immediate CAPTCHA detected - API likely still in protection mode from previous session")
            return {
                'action': 'suggest_alternatives',
                'strategy': 'persistent_captcha_state',
                'message': (
                    'CAPTCHA detected immediately on session start. '
                    'API appears to still be in protection mode. '
                    'Recommend waiting 2+ hours before resuming discovery operations.'
                ),
                'suggested_params': {
                    'cooling_off_hours': 2,
                    'persistent_state': True,
                    'retry_with_splitting': True
                }
            }
        
        # Strategy 1: First CAPTCHA - Try reducing batch size and longer delay
        if attempt == 0:
            # Adaptive wait time based on consecutive CAPTCHAs
            base_wait = 600  # 10 minutes base wait
            adaptive_wait = base_wait * (1.5 ** max(0, self.consecutive_captchas - 1))
            wait_time = min(adaptive_wait, 3600)  # Cap at 1 hour
            
            def modify_params(original_params):
                modified = original_params.copy() if original_params else {}
                # Reduce batch size if present
                if 'rows' in modified:
                    original_rows = int(modified['rows'])
                    modified['rows'] = max(10, original_rows // 2)  # Halve batch size, min 10
                # Add random jitter to avoid patterns
                if 'page' in modified:
                    time.sleep(random.uniform(2, 8))  # Random delay between 2-8 seconds
                return modified
            
            return {
                'action': 'retry',
                'strategy': 'reduce_batch_size',
                'wait_time': wait_time,
                'modify_params': modify_params,
                'message': f'Reducing batch size and waiting {wait_time/60:.1f} minutes'
            }
        
        # Strategy 2: Second CAPTCHA - Try even smaller batch and longer delay
        elif attempt == 1:
            wait_time = 1200 * (1.2 ** self.consecutive_captchas)  # 20 minutes, scaling up
            wait_time = min(wait_time, 7200)  # Cap at 2 hours
            
            def modify_params(original_params):
                modified = original_params.copy() if original_params else {}
                # Further reduce batch size
                if 'rows' in modified:
                    modified['rows'] = max(5, int(modified['rows']) // 3)  # Even smaller batches
                # Add more specific date constraints to reduce load
                if 'date1' in modified and 'date2' in modified:
                    # If searching a year range, split it
                    try:
                        date1 = int(modified['date1'])
                        date2 = int(modified['date2'])
                        if date2 - date1 > 0:
                            # Limit to first half of year range
                            mid_year = date1 + (date2 - date1) // 2
                            modified['date2'] = str(mid_year)
                    except (ValueError, TypeError):
                        pass  # Keep original dates if parsing fails
                return modified
            
            return {
                'action': 'retry',
                'strategy': 'micro_batches_split_dates',
                'wait_time': wait_time,
                'modify_params': modify_params,
                'message': f'Using micro-batches with date splitting, waiting {wait_time/60:.1f} minutes'
            }
        
        # Strategy 3: Third CAPTCHA - Suggest facet splitting or cooling-off
        else:
            # If we've hit multiple CAPTCHAs recently, suggest a different approach
            if self.consecutive_captchas >= 3 or time_since_last_captcha < 1800:  # Less than 30 minutes
                return {
                    'action': 'suggest_alternatives',
                    'strategy': 'facet_splitting_required',
                    'message': (
                        f'Multiple CAPTCHAs detected (consecutive: {self.consecutive_captchas}). '
                        f'Consider splitting this facet into smaller date ranges or using manual cool-off period.'
                    ),
                    'suggested_params': {
                        'split_facet': True,
                        'cooling_off_hours': 2,
                        'reduce_batch_size': 5
                    }
                }
            else:
                # Final retry with very conservative settings
                wait_time = 3600  # 1 hour wait
                
                def modify_params(original_params):
                    modified = original_params.copy() if original_params else {}
                    # Ultra-conservative settings
                    modified['rows'] = '1'  # Single item per request
                    return modified
                
                return {
                    'action': 'retry',
                    'strategy': 'ultra_conservative',
                    'wait_time': wait_time,
                    'modify_params': modify_params,
                    'message': f'Final attempt with ultra-conservative settings: 1 item per request, {wait_time/60:.1f} minute wait'
                }
    
    def _detect_captcha_advanced(self, response) -> bool:
        """
        Enhanced CAPTCHA detection using multiple indicators.
        
        Returns True if CAPTCHA is detected, False otherwise.
        """
        # Check status code patterns that indicate CAPTCHA
        if response.status_code in [403, 406, 503]:
            # These status codes sometimes indicate CAPTCHA challenges
            if any(pattern in response.text.lower() for pattern in ['captcha', 'challenge', 'verify']):
                self.logger.debug(f"CAPTCHA suspected from status code {response.status_code} with challenge content")
                return True
        
        # Enhanced text pattern detection
        captcha_patterns = [
            'captcha', 'recaptcha', 'g-recaptcha',
            'challenge', 'verify you are human', 'verify that you are human',
            'prove you are human', 'robot verification',
            'security check', 'access denied', 'blocked request',
            'hcaptcha', 'cloudflare', 'ray id',  # Cloudflare protection
            'checking your browser', 'ddos protection',
            'please wait while we verify', 'verifying you are human'
        ]
        
        response_text_lower = response.text.lower()
        detected_patterns = [pattern for pattern in captcha_patterns if pattern in response_text_lower]
        
        if detected_patterns:
            self.logger.debug(f"CAPTCHA patterns detected: {detected_patterns}")
            return True
        
        # Check for specific HTML elements that indicate CAPTCHA
        html_indicators = [
            '<div class="g-recaptcha"',
            '<script src="https://www.google.com/recaptcha/',
            '<div id="captcha"',
            '<div class="captcha"',
            'data-sitekey=',  # reCAPTCHA site key
            'cf-browser-verification',  # Cloudflare browser check
            'cf-challenge-form',  # Cloudflare challenge form
            'hcaptcha-container'  # hCaptcha container
        ]
        
        detected_html = [indicator for indicator in html_indicators if indicator in response.text]
        
        if detected_html:
            self.logger.debug(f"CAPTCHA HTML indicators detected: {detected_html}")
            return True
        
        # Check for JavaScript patterns that load CAPTCHA
        js_patterns = [
            'grecaptcha.render',
            'grecaptcha.execute',
            'turnstile.render',  # Cloudflare Turnstile
            'hcaptcha.render',
            'challenge-form'
        ]
        
        detected_js = [pattern for pattern in js_patterns if pattern in response.text]
        
        if detected_js:
            self.logger.debug(f"CAPTCHA JavaScript patterns detected: {detected_js}")
            return True
        
        # Check response headers for CAPTCHA indicators
        headers = response.headers
        captcha_headers = [
            'cf-ray',  # Cloudflare Ray ID often appears with challenges
            'cf-cache-status',
            'x-captcha-required',
            'x-rate-limit-exceeded'
        ]
        
        detected_headers = [header for header in captcha_headers if header.lower() in [h.lower() for h in headers.keys()]]
        
        if detected_headers:
            self.logger.debug(f"CAPTCHA-related headers detected: {detected_headers}")
            # Only return True for definitive CAPTCHA headers
            if any(header.lower() in ['x-captcha-required'] for header in detected_headers):
                return True
        
        # Check for content length patterns (CAPTCHA pages are often much smaller)
        content_length = len(response.text)
        if content_length < 5000 and any(pattern in response_text_lower for pattern in ['challenge', 'verify', 'access']):
            self.logger.debug(f"Suspicious small content length ({content_length}) with challenge keywords")
            return True
        
        return False
    
    def reset_captcha_counters(self):
        """Reset CAPTCHA counters after successful requests."""
        if self.consecutive_captchas > 0:
            self.logger.info(f"Resetting CAPTCHA counters after successful request (was {self.consecutive_captchas} consecutive)")
            self.consecutive_captchas = 0
            self.adaptive_delay_multiplier = max(1.0, self.adaptive_delay_multiplier * 0.9)  # Gradually reduce delay
    
    def _make_request(self, endpoint: str, params: Optional[Dict] = None) -> Dict:
        """
        Make a rate-limited request to the API with retries and 429 handling.
        All requests go through this central bottleneck.
        """
        # Check global CAPTCHA state before making any requests
        can_proceed, reason = self.global_captcha_manager.can_make_requests()
        if not can_proceed:
            raise CaptchaHandlingException(
                f"Request blocked by global CAPTCHA protection: {reason}",
                retry_strategy="global_cooling_off",
                suggested_params={'reason': reason}
            )
        
        url = urljoin(self.base_url, endpoint)
        
        for attempt in range(self.max_retries):
            try:
                # Wait for rate limit before making request
                self._wait_for_rate_limit()
                
                # Rotate user agent occasionally to vary request patterns
                if random.random() < 0.3:  # 30% chance to rotate
                    self.current_user_agent_index = (self.current_user_agent_index + 1) % len(self.user_agents)
                    self.session.headers.update({
                        'User-Agent': self.user_agents[self.current_user_agent_index]
                    })
                
                self.logger.debug(f"Making request to {endpoint} (attempt {attempt + 1})")
                response = self.session.get(url, params=params, timeout=60)
                
                # Handle rate limiting (429) or CAPTCHA responses
                if response.status_code == 429:
                    # Calculate exponential backoff, but start with LOC's recommended 1 hour
                    backoff_time = 3600 * (2 ** attempt)  # 1h, 2h, 4h
                    self.logger.error(f"Rate limited (429) on attempt {attempt + 1}")
                    self.logger.error(f"LOC API requires {backoff_time/3600:.1f} hour wait")
                    
                    if attempt < self.max_retries - 1:
                        self.logger.warning(f"Waiting {backoff_time/60:.1f} minutes before retry...")
                        time.sleep(backoff_time)
                        continue
                    else:
                        raise requests.exceptions.RequestException(
                            f"Rate limited by LOC API (429) after {self.max_retries} attempts. "
                            f"Server requires {backoff_time/3600:.1f} hour wait."
                        )
                
                # Enhanced CAPTCHA detection
                if self._detect_captcha_advanced(response):
                    self.captcha_count += 1
                    self.consecutive_captchas += 1
                    self.last_captcha_time = time.time()
                    
                    # Record global CAPTCHA state
                    context = f"endpoint={endpoint}, attempt={attempt + 1}"
                    self.global_captcha_manager.record_captcha(context)
                    
                    self.logger.warning(f"CAPTCHA detected on attempt {attempt + 1} (total: {self.captcha_count}, consecutive: {self.consecutive_captchas})")
                    
                    # For any CAPTCHA, immediately fail with global cooling-off requirement
                    raise CaptchaHandlingException(
                        f"CAPTCHA detected - global cooling-off period required",
                        retry_strategy="global_cooling_off",
                        suggested_params=self.global_captcha_manager.get_status()
                    )
                
                response.raise_for_status()
                self.logger.debug(f"Request successful: {endpoint}")
                
                # Reset CAPTCHA counters on successful request
                self.reset_captcha_counters()
                
                return response.json()
                
            except requests.exceptions.RequestException as e:
                self.logger.warning(f"Request failed (attempt {attempt + 1}): {e}")
                if attempt == self.max_retries - 1:
                    raise
                # Exponential backoff for other errors (network issues, etc.)
                wait_time = 5 * (attempt + 1) ** 2  # 5s, 20s, 45s
                self.logger.info(f"Retrying in {wait_time}s...")
                time.sleep(wait_time)
    
    def get_request_stats(self) -> Dict:
        """Get comprehensive statistics about request rate limiting."""
        with self.rate_limit_lock:
            current_time = time.time()
            # Count requests in last minute
            recent_requests = [
                req_time for req_time in self.request_count_window
                if req_time > current_time - 60
            ]
            
            # Calculate next allowed request time
            next_request_time = None
            if self.last_request_time:
                min_next_time = self.last_request_time + self.min_request_delay
                if min_next_time > current_time:
                    next_request_time = min_next_time
            
            # Check if we're at rate limit capacity
            at_rate_limit = len(recent_requests) >= self.max_requests_per_minute
            
            # Get CAPTCHA status
            captcha_manager = GlobalCaptchaManager()
            captcha_status = captcha_manager.get_status()
            
            return {
                'requests_last_minute': len(recent_requests),
                'max_requests_per_minute': self.max_requests_per_minute,
                'min_delay_seconds': self.min_request_delay,
                'last_request_time': self.last_request_time,
                'next_request_time': next_request_time,
                'time_since_last_request': current_time - self.last_request_time if self.last_request_time else None,
                'at_rate_limit': at_rate_limit,
                'current_delay_active': next_request_time is not None,
                'captcha_blocked': captcha_status['blocked'],
                'captcha_reason': captcha_status['reason'],
                'captcha_cooling_off_hours': captcha_status['cooling_off_hours'],
                'captcha_multiplier': captcha_status['cooling_off_multiplier'],
                'consecutive_captchas': captcha_status['consecutive_captchas']
            }


class LocApiClient:
    """
    Enhanced LOC API Client that uses the centralized rate limiter.
    
    This provides the same interface as the original but routes all requests
    through the centralized rate-limited request manager.
    """
    
    def __init__(self, base_url: str = "https://chroniclingamerica.loc.gov/", 
                 request_delay: float = 3.0, max_retries: int = 3):
        # Get the singleton rate limiter
        self.base_url = base_url
        self.rate_limiter = RateLimitedRequestManager(
            base_url=base_url,
            max_requests_per_minute=12,  # Conservative limit to avoid CAPTCHA protection
            max_retries=max_retries
        )
        self.logger = logging.getLogger(__name__)
        


    def _make_request(self, endpoint: str, params: Optional[Dict] = None) -> Dict:
        """Route all requests through the centralized rate limiter."""
        return self.rate_limiter._make_request(endpoint, params)
    
    def get_newspapers(self, page: int = 1, rows: int = 1000) -> Dict:
        """Get list of newspaper titles with up to 1000 per page."""
        # Limit to recommended max of 1000 items per page
        # TODO: MIGRATION. Needs to be hooked up to parse_newspapers
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
    
    def get_newspapers_with_details(self, max_newspapers: int = None) -> Generator[Dict, None, None]:
        """Get newspapers with additional details."""
        count = 0
        for newspaper in self.get_all_newspapers():
            if max_newspapers and count >= max_newspapers:
                break
            yield newspaper
            count += 1
    
    def get_newspaper_issues(self, lccn: str) -> Dict:
        """Get issues for a specific newspaper by LCCN."""
        # TODO: Migration. constructs lccn/{lccn}.json style URLs
        endpoint = f'lccn/{lccn}.json'
        return self._make_request(endpoint)
    
    def search_pages(self, **params) -> Dict:
        """
        Search newspaper pages with various parameters.
        
        Common parameters:
        - andtext: Search text
        - date1, date2: Date range (YYYY-MM-DD or YYYY)
        - page: Page number
        - rows: Results per page
        - sort: Sort order
        """
        # Add default format
        #TODO: Uses old parameters that have been renamed.
        search_params = {'format': 'json'}
        search_params.update(params)
        
        # Handle date parameters and add proper dateFilterType
        if 'date1' in search_params and 'date2' in search_params:
            date1 = search_params['date1']
            date2 = search_params['date2']
            
            # If both dates are 4-digit years, use yearRange filter type
            if len(date1) == 4 and len(date2) == 4:
                # For year-only searches, use dateFilterType=yearRange and keep dates as years
                search_params['dateFilterType'] = 'yearRange'
                # Keep dates as YYYY format for yearRange
            elif len(date1) == 4:
                # Convert year to MM/DD/YYYY format for range searches
                search_params['date1'] = f'01/01/{date1}'
                search_params['dateFilterType'] = 'range'
            elif len(date2) == 4:
                # Convert year to MM/DD/YYYY format for range searches  
                search_params['date2'] = f'12/31/{date2}'
                search_params['dateFilterType'] = 'range'
            else:
                # Both are specific dates, use range filter
                search_params['dateFilterType'] = 'range'
        
        return self._make_request('search/pages/results/', search_params)
    
    
    def estimate_download_size(self, date_range: tuple, lccn: Optional[str] = None) -> Dict:
        """Estimate the number of pages available for a date range."""
        #TODO: Migration. pagination.total in the new response gives exact filtered
        # result count, so will be more accurate.
        start_year, end_year = date_range
        
        # Use the search API to get total count with proper date filtering
        params = {
            'date1': str(start_year),
            'date2': str(end_year),
            'dateFilterType': 'yearRange',  # Use yearRange for year-based searches
            'rows': 1,  # Minimal results, we just want the total
            'page': 1
        }
        
        try:
            response = self.search_pages(**params)
            total_pages = response.get('totalItems', 0)
            
            return {
                'total_pages': total_pages,
                'estimated_size_mb': total_pages * 2,  # Rough estimate: 2MB per page
                'date_range': f"{start_year}-{end_year}"
            }
        except Exception as e:
            self.logger.warning(f"Failed to estimate size for {start_year}-{end_year}: {e}")
            return {
                'total_pages': 0,
                'estimated_size_mb': 0,
                'date_range': f"{start_year}-{end_year}",
                'error': str(e)
            }
    
    def get_batches(self, page: int = 1, rows: int = 100) -> Dict:
        """
        Get digitization batches (server-friendly alternative to search API).
        
        Batches represent groups of digitized newspaper pages and are designed
        for bulk access without triggering CAPTCHA protection.
        """
        #TODO: MIGRATION. Has a new endpoint and response structure.

        params = {
            'format': 'json',
            'page': page,
            'rows': min(rows, 1000)  # Respect API limits
        }
        return self._make_request('batches.json', params)
    
    def get_all_batches(self) -> Generator[Dict, None, None]:
        """Generator to fetch all batches with pagination."""
        #TODO: MIGRATION. Has a new endpoint and response structure.

        page = 1
        while True:
            try:
                data = self.get_batches(page=page)
                batches = data.get('batches', [])
                
                if not batches:
                    break
                    
                for batch in batches:
                    yield batch
                    
                page += 1
                
                # Check if we've reached the end
                if page > data.get('totalPages', 1):
                    break
                    
            except Exception as e:
                self.logger.error(f"Error fetching batches page {page}: {e}")
                break
    
    def get_request_stats(self) -> Dict:
        """Get rate limiting statistics."""
        return self.rate_limiter.get_request_stats()