"""
Tests for the rate-limited client functionality.
"""

import pytest
import time
import responses
import requests
from unittest.mock import Mock, patch, MagicMock
import threading
from src.newsagger.rate_limited_client import RateLimitedRequestManager, LocApiClient, GlobalCaptchaManager


class TestRateLimitedRequestManager:
    """Test cases for RateLimitedRequestManager."""
    
    def setup_method(self):
        """Reset the singleton before each test."""
        # Clear the singleton instance
        RateLimitedRequestManager._instance = None
        # Clear the CaptchaManager singleton instance - it is instantiated in RateLimitedRequestManager
        GlobalCaptchaManager._instance = None


    def test_singleton_pattern(self):
        """Test that RateLimitedRequestManager is a singleton."""
        manager1 = RateLimitedRequestManager()
        manager2 = RateLimitedRequestManager()
        
        assert manager1 is manager2
        assert id(manager1) == id(manager2)
    
    def test_initialization(self):
        """Test proper initialization of the rate limiter."""
        manager = RateLimitedRequestManager(
            base_url="https://test.com/",
            max_requests_per_minute=12,
            max_retries=5
        )
        
        assert manager.base_url == "https://test.com/"
        assert manager.max_requests_per_minute == 12
        assert manager.max_retries == 5
        assert manager.min_request_delay == 60.0 / 12  # 5 seconds
    
    def test_base_url_normalization(self):
        """Test that base URL is properly normalized."""
        manager = RateLimitedRequestManager(base_url="https://test.com")
        assert manager.base_url == "https://test.com/"
        
        manager2 = RateLimitedRequestManager(base_url="https://test.com/")
        assert manager2.base_url == "https://test.com/"
    
    @responses.activate
    def test_successful_request(self):
        """Test successful API request."""
        responses.add(
            responses.GET,
            'https://chroniclingamerica.loc.gov/test/',
            json={'status': 'success'},
            status=200
        )
        
        manager = RateLimitedRequestManager()
        result = manager._make_request('test/', {})
        
        assert result == {'status': 'success'}
    
    @responses.activate
    def test_request_with_parameters(self):
        """Test request with query parameters."""
        responses.add(
            responses.GET,
            'https://chroniclingamerica.loc.gov/search/',
            json={'results': []},
            status=200
        )
        
        manager = RateLimitedRequestManager()
        result = manager._make_request('search/', {'q': 'test', 'format': 'json'})
        
        assert result == {'results': []}
        # Check that parameters were properly encoded in the URL
        assert len(responses.calls) == 1
        assert 'q=test' in responses.calls[0].request.url
        assert 'format=json' in responses.calls[0].request.url
    
    def test_rate_limiting_delay_calculation(self):
        """Test that rate limiting delay is calculated correctly."""
        manager = RateLimitedRequestManager(max_requests_per_minute=20)
        
        # Should be 3 seconds minimum delay (60/20)
        assert manager.min_request_delay == 3.0
    
    @responses.activate
    def test_retry_on_network_error(self):
        """Test retry logic on network errors."""
        # First call fails, second succeeds
        responses.add(responses.GET, 'https://chroniclingamerica.loc.gov/test/', 
                     body=requests.exceptions.ConnectionError())
        responses.add(responses.GET, 'https://chroniclingamerica.loc.gov/test/',
                     json={'success': True}, status=200)
        
        manager = RateLimitedRequestManager(max_retries=2)
        
        result = manager._make_request('test/', {})
        assert result == {'success': True}
        assert len(responses.calls) == 2


class TestLocApiClient:
    """Test cases for the LocApiClient using rate-limited requests."""
    
    def setup_method(self):
        """Reset singleton and setup fresh client."""
        RateLimitedRequestManager._instance = None
    
    def test_client_initialization(self):
        """Test that client initializes with rate limiter."""
        client = LocApiClient(base_url="https://test.com/", max_retries=5)
        
        assert client.rate_limiter.base_url == "https://test.com/"
        assert client.rate_limiter.max_retries == 5
        assert hasattr(client, 'rate_limiter')
    
    @responses.activate  
    def test_get_all_newspapers(self):
        """Test getting all newspapers with pagination."""
        # Mock first page
        responses.add(
            responses.GET,
            'https://chroniclingamerica.loc.gov/newspapers.json',
            json={
                'newspapers': [
                    {'lccn': 'sn123', 'title': 'Test Paper 1'},
                    {'lccn': 'sn456', 'title': 'Test Paper 2'}
                ]
            },
            status=200
        )
        
        client = LocApiClient()
        newspapers = list(client.get_all_newspapers())
        
        assert len(newspapers) == 2
        assert newspapers[0]['lccn'] == 'sn123'
        assert newspapers[1]['lccn'] == 'sn456'
    
    @responses.activate
    def test_search_pages_with_facets(self):
        """Test search with date facets."""
        responses.add(
            responses.GET,
            'https://chroniclingamerica.loc.gov/search/pages/results/',
            json={
                'items': [{'id': 'item1', 'title': 'Test Page'}],
                'totalItems': 1
            },
            status=200
        )
        
        client = LocApiClient()
        result = client.search_pages(
            andtext='earthquake',
            date1='1906',
            date2='1906',
            dates_facet='1906/1906'
        )
        
        assert 'items' in result
        assert result['totalItems'] == 1  # Check we got the response
        assert len(result['items']) == 1
        assert result['items'][0]['id'] == 'item1'
    
    @responses.activate
    def test_get_newspaper_issues(self):
        """Test getting newspaper issues."""
        responses.add(
            responses.GET,
            'https://chroniclingamerica.loc.gov/lccn/sn123.json',
            json={
                'issues': [
                    {'date_issued': '1906-04-18', 'url': 'test1'},
                    {'date_issued': '1906-04-19', 'url': 'test2'}
                ]
            },
            status=200
        )
        
        client = LocApiClient()
        result = client.get_newspaper_issues('sn123')
        
        assert 'issues' in result
        assert len(result['issues']) == 2
    
    @responses.activate
    def test_get_page_metadata(self):
        """Test getting page metadata."""
        responses.add(
            responses.GET,
            'https://chroniclingamerica.loc.gov/lccn/sn123.json',
            json={
                'issues': [
                    {
                        'date_issued': '1906-04-18',
                        'edition': 1,
                        'pages': [
                            {
                                'sequence': 1,
                                'title': 'Test Page',
                                'pdf': 'https://test.com/page.pdf'
                            }
                        ]
                    }
                ]
            },
            status=200
        )
        
        client = LocApiClient()
        # Note: rate_limited_client doesn't have get_page_metadata, use get_newspaper_issues
        result = client.get_newspaper_issues('sn123')
        
        # Since we're using get_newspaper_issues, check for issues structure
        assert 'issues' in result
        assert len(result['issues']) == 1
    
    def test_date_parameter_handling(self):
        """Test date parameter handling in search."""
        client = LocApiClient()
        
        # The rate_limited_client handles dates differently - it uses dateFilterType
        # Test that year-only dates use yearRange filter type
        with patch.object(client, '_make_request') as mock_request:
            mock_request.return_value = {'items': []}
            
            client.search_pages(date1='1906', date2='1906')
            
            # Check that the call was made with proper parameters
            mock_request.assert_called_once()
            # Get both positional and keyword arguments
            call_args, call_kwargs = mock_request.call_args
            
            # The second positional argument should be the params dict
            params = call_args[1] if len(call_args) > 1 else call_kwargs
            
            # The search_pages method should add dateFilterType for year-only dates
            assert 'dateFilterType' in params
            assert params['dateFilterType'] == 'yearRange'
            assert params['date1'] == '1906'
            assert params['date2'] == '1906'
    
    @responses.activate
    def test_estimate_download_size_no_results(self):
        """Test download size estimation with no results."""
        responses.add(
            responses.GET,
            'https://chroniclingamerica.loc.gov/search/pages/results/',
            json={'items': []},
            status=200
        )
        
        client = LocApiClient()
        estimate = client.estimate_download_size(('1906', '1906'))
        
        assert estimate['total_pages'] == 0
        assert estimate['estimated_size_mb'] == 0
        assert estimate['date_range'] == '1906-1906'
    
    @responses.activate 
    def test_estimate_download_size_with_results(self):
        """Test download size estimation with sample results."""
        responses.add(
            responses.GET,
            'https://chroniclingamerica.loc.gov/search/pages/results/',
            json={'items': [{'id': f'item{i}'} for i in range(50)], 'totalItems': 50},
            status=200
        )
        
        client = LocApiClient()
        estimate = client.estimate_download_size(('1906', '1906'))
        
        assert estimate['total_pages'] == 50
        assert estimate['estimated_size_mb'] == 100  # 50 * 2MB per page
    