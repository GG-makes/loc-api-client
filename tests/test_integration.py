"""
Integration tests for newsagger components.
"""

import pytest
import responses
import tempfile
import json
from pathlib import Path
from unittest.mock import patch

from newsagger.rate_limited_client import RateLimitedRequestManager, LocApiClient, GlobalCaptchaManager
from newsagger.processor import NewsDataProcessor
from newsagger.storage import NewsStorage
from newsagger.config import Config


class TestIntegration:
    """Integration tests for combined components."""
    
    @pytest.fixture
    def integration_config(self):
        """Create test configuration for integration tests."""
        with tempfile.TemporaryDirectory() as temp_dir:
            config = Config()
            config.database_path = str(Path(temp_dir) / 'test.db')
            config.download_dir = str(Path(temp_dir) / 'downloads')
            config.request_delay = 0.1  # Fast for testing
            config.log_level = 'WARNING'
            config.loc_base_url = 'https://chroniclingamerica.loc.gov/'  # Use real URL for mocking
            config.api_version = 'LEGACY'
            yield config
    
    @pytest.fixture
    def components(self, integration_config):
        """Create all components with test configuration."""

        RateLimitedRequestManager._instance = None  # reset singleton
        GlobalCaptchaManager._instance = None
        client = LocApiClient(**integration_config.get_api_config())
        client.base_url = 'https://chroniclingamerica.loc.gov'
        processor = NewsDataProcessor()
        storage = NewsStorage(integration_config.database_path)

        yield {
            'client': client,
            'processor': processor,
            'storage': storage,
            'config': integration_config
        }

        # Explicitly release storage to unlock SQLite file before TemporaryDirectory cleanup (Windows)
        del storage
        import gc
        gc.collect()
        
    @responses.activate
    def test_end_to_end_newspaper_workflow(self, components):
        """Test complete workflow: fetch newspapers -> process -> store."""
        client = components['client']
        processor = components['processor']
        storage = components['storage']
        
        # Mock API response
        newspaper_response = {
            'newspapers': [
                {
                    'lccn': 'sn84038012',
                    'title': 'The San Francisco Call',
                    'place_of_publication': ['San Francisco, Calif.'],
                    'start_year': '1895',
                    'end_year': '1913',
                    'frequency': 'Daily',
                    'subject': ['San Francisco (Calif.)--Newspapers'],
                    'language': ['English'],
                    'url': 'https://chroniclingamerica.loc.gov/lccn/sn84038012/'
                },
                {
                    'lccn': 'sn85066387',
                    'title': 'San Francisco Chronicle',
                    'place_of_publication': ['San Francisco, Calif.'],
                    'start_year': '1865',
                    'end_year': '1922',
                    'frequency': 'Daily',
                    'subject': ['San Francisco (Calif.)--Newspapers'],
                    'language': ['English'],
                    'url': 'https://chroniclingamerica.loc.gov/lccn/sn85066387/'
                }
            ],
            'totalPages': 1
        }
        
        responses.add(
            responses.GET,
            'https://chroniclingamerica.loc.gov/newspapers.json',
            json=newspaper_response,
            status=200
        )
        
        # Execute workflow
        # 1. Fetch from API
        api_response = client.get_newspapers()
        
        # 2. Process data
        newspapers = processor.process_newspapers_response(api_response)
        
        # 3. Store in database
        stored_count = storage.store_newspapers(newspapers)
        
        # Verify results
        assert stored_count == 2
        assert len(newspapers) == 2
        
        # Verify data in database
        retrieved = storage.get_newspapers()
        assert len(retrieved) == 2
        
        # Check specific newspaper
        sf_call = next((n for n in retrieved if n['lccn'] == 'sn84038012'), None)
        assert sf_call is not None
        assert sf_call['title'] == 'The San Francisco Call'
        assert sf_call['start_year'] == 1895
    
    @responses.activate
    def test_end_to_end_search_workflow(self, components):
        """Test complete workflow: search pages -> process -> store."""
        client = components['client']
        processor = components['processor']
        storage = components['storage']
        
        # Mock search response
        search_response = {
            'items': [
                {
                    'id': 'item123',
                    'lccn': 'sn84038012',
                    'title': 'The San Francisco Call',
                    'date': '1906-04-18',
                    'edition': 1,
                    'sequence': 1,
                    'url': 'https://chroniclingamerica.loc.gov/lccn/sn84038012/1906-04-18/ed-1/seq-1/',
                    'pdf_url': 'https://chroniclingamerica.loc.gov/lccn/sn84038012/1906-04-18/ed-1/seq-1.pdf',
                    'image_url': ['https://chroniclingamerica.loc.gov/lccn/sn84038012/1906-04-18/ed-1/seq-1.jp2']
                },
                {
                    'id': 'item124',
                    'lccn': 'sn84038012',
                    'title': 'The San Francisco Call',
                    'date': '1906-04-19',
                    'edition': 1,
                    'sequence': 1,
                    'url': 'https://chroniclingamerica.loc.gov/lccn/sn84038012/1906-04-19/ed-1/seq-1/',
                    'pdf_url': 'https://chroniclingamerica.loc.gov/lccn/sn84038012/1906-04-19/ed-1/seq-1.pdf',
                    'image_url': ['https://chroniclingamerica.loc.gov/lccn/sn84038012/1906-04-19/ed-1/seq-1.jp2']
                }
            ],
            'totalItems': 2
        }
        
        responses.add(
            responses.GET,
            'https://chroniclingamerica.loc.gov/search/pages/results/',
            json=search_response,
            status=200
        )
        
        # Execute workflow
        # 1. Search pages
        api_response = client.search_pages(andtext='earthquake', date1='1906', date2='1906')
        
        # 2. Process results
        pages = processor.process_search_response(api_response)
        
        # 3. Store pages
        stored_count = storage.store_pages(pages)
        
        # Verify results
        assert stored_count == 2
        assert len(pages) == 2
        
        # Verify data in database
        retrieved = storage.get_pages()
        assert len(retrieved) == 2
        
        # Check specific page
        earthquake_page = next((p for p in retrieved if p['date'] == '1906-04-18'), None)
        assert earthquake_page is not None
        assert earthquake_page['item_id'] == 'item123'
        assert earthquake_page['downloaded'] == 0  # Not downloaded yet
    
    @pytest.mark.skip(
    reason="test_download_session_workflow tests legacy discovery workflow including "
    "search_with_faceted_dates and session management which are pending migration "
    "to the new API. See MIGRATION.md Phase 2.")
    @responses.activate
    def test_download_session_workflow(self, components):
        """Test complete download session workflow."""
        client = components['client']
        processor = components['processor']
        storage = components['storage']
        
        # Mock search response for estimate - provide actual items
        estimate_response = {
            'items': [{'id': f'item_{i}', 'title': 'Test'} for i in range(100)],
            'totalItems': 100
        }
        responses.add(
            responses.GET,
            'https://chroniclingamerica.loc.gov/search/pages/results/',
            json=estimate_response,
            status=200
        )
        
        # Mock faceted search responses
        facet_response = {
            'facets': {
                'facets.3': {
                    'type': 'dates',
                    'filters': [
                        {
                            'title': '1906 to 1906',
                            'count': 50,
                            'on': 'https://test.com/search?dates=1906/1906'
                        }
                    ]
                }
            }
        }
        responses.add(
            responses.GET,
            'https://chroniclingamerica.loc.gov/search/',
            json=facet_response,
            status=200
        )
        
        # Mock search results for download
        download_response = {
            'items': [
                {
                    'id': 'download_item1',
                    'lccn': 'sn84038012',
                    'title': 'Test Paper',
                    'date': '1906-04-18',
                    'url': 'https://test.com/page1'
                }
            ]
        }
        responses.add(
            responses.GET,
            'https://chroniclingamerica.loc.gov/search/pages/results/',
            json=download_response,
            status=200
        )
        
        # Execute download session workflow
        # 1. Get estimate
        estimate = client.estimate_download_size(('1906', '1906'), 'sn84038012')
        assert estimate['total_pages'] >= 100  # The estimation algorithm may add a small buffer
        
        # 2. Create download session
        session_id = storage.create_download_session(
            'test_session',
            {'lccn': 'sn84038012', 'date1': '1906', 'date2': '1906'},
            estimate['total_pages']
        )
        
        # 3. Simulate download process
        base_query = {'andtext': 'lccn:sn84038012', 'date1': '1906', 'date2': '1906'}
        total_downloaded = 0
        
        # Process one batch of results
        for result_batch in client.search_with_faceted_dates(base_query):
            pages = processor.process_search_response(result_batch)
            stored = storage.store_pages(pages)
            total_downloaded += stored
            storage.update_session_progress(session_id, total_downloaded)
            break  # Just process one batch for test
        
        # 4. Complete session
        storage.complete_session(session_id)
        
        # Verify session was created and completed
        session_stats = storage.get_session_stats(session_id)
        assert session_stats['status'] == 'completed'
        assert session_stats['total_downloaded'] == 1
        assert session_stats['session_name'] == 'test_session'
    
    def test_filtering_workflow(self, components):
        """Test newspaper filtering workflow."""
        processor = components['processor']
        storage = components['storage']
        
        # Store test newspapers
        from newsagger.processor import NewspaperInfo
        newspapers = [
            NewspaperInfo(
                lccn='ca1', title='California Daily',
                place_of_publication=['San Francisco, California'],
                start_year=1900, end_year=1920, frequency='Daily',
                subject=[], language=['English'], url=''
            ),
            NewspaperInfo(
                lccn='ny1', title='New York Times',
                place_of_publication=['New York, New York'],
                start_year=1851, end_year=2023, frequency='Daily',
                subject=[], language=['English'], url=''
            ),
            NewspaperInfo(
                lccn='es1', title='El Diario',
                place_of_publication=['Los Angeles, California'],
                start_year=1950, end_year=1980, frequency='Daily',
                subject=[], language=['Spanish'], url=''
            )
        ]
        
        storage.store_newspapers(newspapers)
        
        # Test filtering by state
        ca_papers = storage.get_newspapers(state='California')
        assert len(ca_papers) == 2
        
        # Test filtering by language
        spanish_papers = storage.get_newspapers(language='Spanish')
        assert len(spanish_papers) == 1
        assert spanish_papers[0]['lccn'] == 'es1'
        
        # Test processor filtering
        all_papers = storage.get_newspapers()
        processed_papers = [NewspaperInfo.from_api_response({
            'lccn': p['lccn'],
            'title': p['title'],
            'place_of_publication': json.loads(p['place_of_publication']),  # Convert back from JSON
            'start_year': str(p['start_year']) if p['start_year'] else None,
            'end_year': str(p['end_year']) if p['end_year'] else None,
            'frequency': p['frequency'],
            'subject': json.loads(p['subject']),
            'language': json.loads(p['language']),
            'url': p['url']
        }) for p in all_papers]
        
        # Filter by criteria - newspapers that overlap with 1950+ (end_year >= 1950)
        recent_papers = processor.filter_newspapers_by_criteria(
            processed_papers, start_year=1950
        )
        assert len(recent_papers) == 2  # es1 and ny1 both end after 1950
        lccns = {p.lccn for p in recent_papers}
        assert lccns == {'es1', 'ny1'}
    
    def test_deduplication_across_sessions(self, components):
        """Test that deduplication works across multiple processing sessions."""
        processor = components['processor']
        storage = components['storage']
        
        # First batch of results
        batch1 = {
            'items': [
                {'id': 'item1', 'title': 'Page 1', 'lccn': 'test1'},
                {'id': 'item2', 'title': 'Page 2', 'lccn': 'test1'}
            ]
        }
        
        # Second batch with one duplicate
        batch2 = {
            'items': [
                {'id': 'item2', 'title': 'Page 2 Duplicate', 'lccn': 'test1'},  # Duplicate
                {'id': 'item3', 'title': 'Page 3', 'lccn': 'test1'}
            ]
        }
        
        # Process first batch
        pages1 = processor.process_search_response(batch1, deduplicate=True)
        stored1 = storage.store_pages(pages1)
        
        # Process second batch (should deduplicate)
        pages2 = processor.process_search_response(batch2, deduplicate=True)
        stored2 = storage.store_pages(pages2)
        
        assert stored1 == 2  # Both items from first batch
        assert stored2 == 1  # Only new item from second batch
        
        # Verify database has 3 unique items
        all_pages = storage.get_pages()
        assert len(all_pages) == 3
        item_ids = {p['item_id'] for p in all_pages}
        assert item_ids == {'item1', 'item2', 'item3'}
    
    def test_error_handling_workflow(self, components):
        """Test error handling in integrated workflow."""
        processor = components['processor']
        storage = components['storage']
        
        # Test processing invalid data
        invalid_response = {
            'newspapers': [
                {'lccn': 'valid1', 'title': 'Valid Paper'},
                {'invalid': 'data'},  # Missing required fields
                None,  # Completely invalid
                {'lccn': 'valid2', 'title': 'Another Valid Paper'}
            ]
        }
        
        # Should process valid entries and handle invalid ones gracefully
        newspapers = processor.process_newspapers_response(invalid_response)
        stored = storage.store_newspapers(newspapers)
        
        # Should have processed all entries (including invalid ones with empty fields)
        assert len(newspapers) == 3
        assert stored == 3
        
        # Verify all data in database (including invalid entries with empty fields)
        retrieved = storage.get_newspapers()
        assert len(retrieved) == 3
        lccns = {n['lccn'] for n in retrieved}
        assert lccns == {'valid1', 'valid2', ''}
    
    @responses.activate
    def test_rate_limiting_integration(self, components):
        """Test rate limiting behavior in integration scenario."""
        client = components['client']
        
        # Mock initial 429 response, then success
        responses.add(
            responses.GET,
            'https://chroniclingamerica.loc.gov/newspapers.json',
            status=429
        )
        responses.add(
            responses.GET,
            'https://chroniclingamerica.loc.gov/newspapers.json',
            json={'newspapers': []},
            status=200
        )
        
        # Should handle rate limiting and eventually succeed
        with patch('time.sleep'):  # Skip actual delays in test
            result = client.get_newspapers()
        
        assert result == {'newspapers': []}
        assert len(responses.calls) == 2  # First call gets 429, second succeeds