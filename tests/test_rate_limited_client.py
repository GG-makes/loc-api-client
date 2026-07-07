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
from src.newsagger.api_params import LegacyQueryBuilder, ChroniclingAmericaSearchParams
from newsagger.processor_new import LegacyResponseProcessor

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
        """Test getting all newspapers: client delegates to the builder and parses."""
        responses.add(
            responses.GET,
            'https://chroniclingamerica.loc.gov/newspapers.json',
            json={
                'newspapers': [
                    {'lccn': 'sn123', 'title': 'Test Paper 1'},
                    {'lccn': 'sn456', 'title': 'Test Paper 2'},
                ],
                'totalPages': 1,
            },
            status=200,
        )

        client = LocApiClient()
        builder = LegacyQueryBuilder(ChroniclingAmericaSearchParams())
        newspapers = list(client.get_all_newspapers(builder, LegacyResponseProcessor()))

        assert len(newspapers) == 2
        assert newspapers[0].lccn == 'sn123'
        assert newspapers[1].lccn == 'sn456'

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
    
    @pytest.mark.skip(
        reason="No client page-metadata method yet — blocked on item-detail enrichment "
               "(ADR 0003 / MIGRATION.md Phase 3). Previously called the retired "
               "get_newspaper_issues and asserted issue structure, not page metadata."
    )
    @responses.activate
    def test_get_page_metadata(self):
        """
        TODO (Phase 3): once a page item-detail fetch exists (?fo=json on a page URL),
        assert it parses resource.pdf / resource.image / pagination.current into a
        PageInfo via parse_page_details. Do NOT reintroduce get_newspaper_issues.
        """
        pass
            
    @responses.activate
    def test_get_count_no_results(self):
        """Test count retrieval with no results."""
        responses.add(
            responses.GET,
            'https://chroniclingamerica.loc.gov/search/pages/results/',
            json={'totalItems': 0},
            status=200
        )

        client = LocApiClient()
        builder = LegacyQueryBuilder(ChroniclingAmericaSearchParams(date1='1906', date2='1906'))
        assert client.get_count(builder) == 0
        
    @responses.activate
    def test_get_count_with_results(self):
        """Test count retrieval with results."""
        responses.add(
            responses.GET,
            'https://chroniclingamerica.loc.gov/search/pages/results/',
            json={'totalItems': 50},
            status=200
        )

        client = LocApiClient()
        builder = LegacyQueryBuilder(ChroniclingAmericaSearchParams(date1='1906', date2='1906'))
        assert client.get_count(builder) == 50
    
class TestGetAllBatchesAndCount:
    """get_all_batches/get_count delegate to the builder; build nothing themselves."""

    def setup_method(self):
        RateLimitedRequestManager._instance = None

    def test_get_all_batches_delegates_to_builder(self):
        client = LocApiClient()
        fake_builder = Mock()
        fake_builder.fetch_all_batches.return_value = iter([{'batch': 'a'}, {'batch': 'b'}])

        result = list(client.get_all_batches(fake_builder))

        fake_builder.fetch_all_batches.assert_called_once_with(client._make_request)
        assert result == [{'batch': 'a'}, {'batch': 'b'}]

    def test_get_count_reads_totalitems_for_legacy_shaped_response(self):
        client = LocApiClient()
        fake_builder = Mock()
        fake_builder.base_url = 'https://example.com/search/'
        fake_builder.build_count_only.return_value = {'format': 'json', 'rows': 1, 'page': 1}
        client._make_request = Mock(return_value={'totalItems': 42})

        assert client.get_count(fake_builder) == 42
        client._make_request.assert_called_once_with('https://example.com/search/', {'format': 'json', 'rows': 1, 'page': 1})

    def test_get_count_reads_pagination_total_for_locgov_shaped_response(self):
        client = LocApiClient()
        fake_builder = Mock()
        fake_builder.base_url = 'https://example.com/search/'
        fake_builder.build_count_only.return_value = {'fo': 'json', 'c': 1}
        client._make_request = Mock(return_value={'pagination': {'total': 99}})

        assert client.get_count(fake_builder) == 99


class TestSearchPagesEndpointSelection:
    """search_pages routes loc.gov-shaped params to base_url directly, legacy-shaped to the sub-path."""

    def setup_method(self):
        RateLimitedRequestManager._instance = None

    def test_legacy_shaped_params_use_search_pages_results_endpoint(self):
        client = LocApiClient()
        client._make_request = Mock(return_value={})
        client.search_pages(andtext='flood')
        args, _ = client._make_request.call_args
        assert args[0] == 'search/pages/results/'