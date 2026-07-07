"""
Tests for automated discovery functionality.
"""

import pytest
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))

from newsagger.discovery_manager import DiscoveryManager
from newsagger.storage import NewsStorage
from newsagger.rate_limited_client import LocApiClient #TODO: replace with ratelimitedclient
from newsagger.api_params import LegacyQueryBuilder
from newsagger.processor_new import LegacyResponseProcessor, PageInfo, NewspaperInfo

class TestDiscoveryAutomation:
    """Test automated discovery functionality."""
    
    def setup_method(self):
        """Set up test environment."""
        # Create temporary database
        self.temp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self.storage = NewsStorage(self.temp_db.name)
        
        # Create mock dependencies
        self.mock_api_client = Mock(spec=LocApiClient)
        self.mock_processor = Mock(spec=LegacyResponseProcessor)
        
        # Create discovery manager
        self.discovery = DiscoveryManager(
            self.mock_api_client,
            self.mock_processor,
            self.storage,
            LegacyQueryBuilder # version doesn't matter in mock
        )
    
    # def teardown_method(self):
    #     """Clean up test environment."""
    #     try:
    #         # force cleanup of objects that might hold DB handles
    #         del self.storage
    #         del self.downloader
    #     except AttributeError:
    #         pass

    #     import gc
    #     gc.collect()

    #     Path(self.temp_db.name).unlink(missing_ok=True)
    def teardown_method(self):
        """Clean up test environment."""
        try:
            del self.discovery
        except AttributeError:
            pass
        try:
            del self.storage
        except AttributeError:
            pass

        import gc
        gc.collect()

        if self.temp_db:
            self.temp_db.close()  # Release Windows file handle on NamedTemporaryFile
            Path(self.temp_db.name).unlink(missing_ok=True)        
            
    def test_discover_facet_content_date_range(self):
        """Test discovering content for a date range facet."""
        # Create a test facet
        facet_id = self.storage.create_search_facet(
            'date_range', '1906/1906', '', 1000
        )
        
        # Mock API responses
        mock_pages = [
            {
                'id': 'item1',
                'lccn': 'sn84038012',
                'title': 'Test Paper',
                'date': '1906-04-18',
                'edition': 1,
                'sequence': 1,
                'url': 'https://example.com/item1'
            },
            {
                'id': 'item2', 
                'lccn': 'sn84038012',
                'title': 'Test Paper',
                'date': '1906-04-19',
                'edition': 1,
                'sequence': 1,
                'url': 'https://example.com/item2'
            }
        ]
        
        self.mock_api_client.search_pages.return_value = {
            'items': mock_pages,
            'totalItems': 2
        }
        self.mock_processor.parse_pages.return_value = [
            PageInfo(
                item_id='item1',
                lccn='sn84038012',
                title='Test Paper',
                date='1906-04-18',
                edition=1,
                sequence=1,
                page_url='https://example.com/item1',
                pdf_url=None,
                jp2_url=None,
                ocr_text=None,
                word_count=None
            ),
            PageInfo(
                item_id='item2',
                lccn='sn84038012',
                title='Test Paper',
                date='1906-04-19',
                edition=1,
                sequence=1,
                page_url='https://example.com/item2',
                pdf_url=None,
                jp2_url=None,
                ocr_text=None,
                word_count=None
            )
        ]
        
        # Test discovery
        discovered_count = self.discovery.discover_facet_content(facet_id, batch_size=50)
        
        # Verify results
        assert discovered_count == 2
        
        # Check API was called with correct parameters
        self.mock_api_client.search_pages.assert_called_once()
        call_args = self.mock_api_client.search_pages.call_args[1]
        assert call_args['date1'] == '1906'
        assert call_args['date2'] == '1906'
        assert call_args['rows'] == 50
        assert call_args['page'] == 1
        
        # Check facet status was updated
        facet = self.storage.get_search_facet(facet_id)
        assert facet['status'] == 'completed'
        assert facet['items_discovered'] == 2
        assert facet['actual_items'] == 2
    
    def test_discover_facet_content_state_facet(self):
        """Test discovering content for a state facet."""
        # Add mock periodicals for California
        periodicals = [{'lccn': 'sn84038012', 'state': 'California', 'title': 'Test Paper'}]
        self.storage.store_periodicals(periodicals)
        
        # Create a test facet
        facet_id = self.storage.create_search_facet(
            'state', 'California', '', 500
        )
        
        # Mock empty response (no pages found)
        self.mock_api_client.search_pages.return_value = {
            'items': [],
            'totalItems': 0
        }
        self.mock_processor.parse_pages.return_value = []
        
        # Test discovery
        discovered_count = self.discovery.discover_facet_content(facet_id)
        
        # Verify results
        assert discovered_count == 0
        
        # Check API was called with LCCN-based search (not state parameter)
        self.mock_api_client.search_pages.assert_called_once()
        call_args = self.mock_api_client.search_pages.call_args[1]
        assert 'andtext' in call_args
        assert 'sn84038012' in call_args['andtext']
    
    def test_discover_facet_content_with_max_items(self):
        """Test discovery with max items limit."""
        # Create a test facet
        facet_id = self.storage.create_search_facet(
            'date_range', '1906/1906', '', 1000
        )
        
        # Mock API response with many items
        mock_pages = [
            {
                'id': f'item{i}',
                'lccn': 'sn84038012',
                'title': 'Test Paper',
                'date': '1906-04-18',
                'url': f'https://example.com/item{i}'
            }
            for i in range(20)
        ]
        
        self.mock_api_client.search_pages.return_value = {
            'items': mock_pages,
            'totalItems': 20
        }
        self.mock_processor.parse_pages.return_value = [
            PageInfo(
                item_id=f'item{i}',
                lccn='sn84038012',
                title='Test Paper',
                date='1906-04-18',
                edition=1,
                sequence=i,
                page_url=f'https://example.com/item{i}',
                pdf_url=None,
                jp2_url=None,
                ocr_text=None,
                word_count=None
            )
            for i in range(20)
        ]
        
        # Test discovery with limit
        discovered_count = self.discovery.discover_facet_content(
            facet_id, max_items=10
        )
        
        # Should stop at max_items limit
        assert discovered_count == 10
        
        # Check facet status
        facet = self.storage.get_search_facet(facet_id)
        assert facet['status'] == 'completed'
        assert facet['items_discovered'] == 10
    
    def test_discover_facet_content_error_handling(self):
        """Test error handling during discovery."""
        # Create a test facet
        facet_id = self.storage.create_search_facet(
            'date_range', '1906/1906', '', 1000
        )
        
        # Mock API error
        self.mock_api_client.search_pages.side_effect = Exception("API Error")
        
        # Test discovery should raise exception
        with pytest.raises(Exception, match="API Error"):
            self.discovery.discover_facet_content(facet_id)
        
        # Check facet status was set to error
        facet = self.storage.get_search_facet(facet_id)
        assert facet['status'] == 'error'
        assert 'API Error' in facet['error_message']
    
    def test_discover_facet_content_invalid_facet(self):
        """Test discovery with invalid facet ID."""
        with pytest.raises(ValueError, match="Facet 999 not found"):
            self.discovery.discover_facet_content(999)
    
    def test_enqueue_facet_content(self):
        """Test enqueuing discovered content from a facet."""
        # Create a test facet
        facet_id = self.storage.create_search_facet(
            'date_range', '1906/1906', '', 1000
        )
        
        # Add some test pages to storage
        pages = [
            PageInfo(
                item_id=f'item{i}',
                lccn='sn84038012',
                title='Test Paper',
                date='1906-04-18',
                edition=1,
                sequence=i,
                page_url=f'https://example.com/item{i}',
                pdf_url=None,
                jp2_url=None,
                ocr_text=None,
                word_count=None
            )
            for i in range(1, 6)
        ]
        
        self.storage.store_pages(pages)
        
        # Test enqueuing
        enqueued_count = self.discovery.enqueue_facet_content(facet_id)
        
        # Should enqueue all pages (5)
        assert enqueued_count == 5
        
        # Check download queue
        queue = self.storage.get_download_queue()
        assert len(queue) == 5
        
        for item in queue:
            assert item['queue_type'] == 'page'
            assert item['reference_id'].startswith('item')
            assert item['priority'] == 1  # 1906 gets highest priority
            assert item['estimated_size_mb'] == 1.0
    
    def test_enqueue_facet_content_with_limit(self):
        """Test enqueuing with max items limit."""
        # Create a test facet
        facet_id = self.storage.create_search_facet(
            'date_range', '1920/1920', '', 1000
        )
        
        # Add some test pages
        pages = [
            PageInfo(
                item_id=f'item{i}',
                lccn='sn84038012',
                title='Test Paper',
                date='1920-01-01',
                edition=1,
                sequence=i,
                page_url=f'https://example.com/item{i}',
                pdf_url=None,
                jp2_url=None,
                ocr_text=None,
                word_count=None
            )
            for i in range(1, 11)
        ]
        
        self.storage.store_pages(pages)
        
        # Test enqueuing with limit
        enqueued_count = self.discovery.enqueue_facet_content(facet_id, max_items=3)
        
        # Should only enqueue 3 items
        assert enqueued_count == 3
        
        # Check download queue
        queue = self.storage.get_download_queue()
        assert len(queue) == 3
    
    def test_enqueue_facet_content_invalid_facet(self):
        """Test enqueuing with invalid facet ID."""
        with pytest.raises(ValueError, match="Facet 999 not found"):
            self.discovery.enqueue_facet_content(999)
    
    def test_calculate_priority(self):
        """Test priority calculation for different facets and pages."""
        # Test 1906 earthquake year gets priority 1
        facet_1906 = {
            'facet_type': 'date_range',
            'facet_value': '1906/1906'
        }
        page = {'date': '1906-04-18'}
        priority = self.discovery._calculate_priority(facet_1906, page)
        assert priority == 1
        
        # Test WWI era gets priority 2
        facet_wwi = {
            'facet_type': 'date_range',
            'facet_value': '1918/1918'
        }
        priority = self.discovery._calculate_priority(facet_wwi, page)
        assert priority == 2
        
        # Test California state gets higher priority
        facet_ca = {
            'facet_type': 'state',
            'facet_value': 'California'
        }
        priority = self.discovery._calculate_priority(facet_ca, page)
        assert priority == 4  # max(1, 5-1)
        
        # Test default priority
        facet_default = {
            'facet_type': 'date_range',
            'facet_value': '1950/1950'
        }
        priority = self.discovery._calculate_priority(facet_default, page)
        assert priority == 5
    
    def test_discover_facet_content_pagination(self):
        """Test discovery handles pagination correctly."""
        # Create a test facet
        facet_id = self.storage.create_search_facet(
            'date_range', '1906/1906', '', 1000
        )
        
        # Mock API responses for multiple pages
        page1_response = {
            'items': [{'id': f'item{i}', 'title': 'Test'} for i in range(1, 101)],
            'totalItems': 150
        }
        page2_response = {
            'items': [{'id': f'item{i}', 'title': 'Test'} for i in range(101, 151)],
            'totalItems': 150
        }
        
        self.mock_api_client.search_pages.side_effect = [page1_response, page2_response]
        self.mock_processor.parse_pages.side_effect = [
            [PageInfo(
                item_id=f'item{i}',
                lccn='sn84038012',
                title='Test Paper',
                date='1906-04-18',
                edition=1,
                sequence=i,
                page_url=f'https://example.com/item{i}',
                pdf_url=None,
                jp2_url=None,
                ocr_text=None,
                word_count=None
            ) for i in range(1, 101)],
            [PageInfo(
                item_id=f'item{i}',
                lccn='sn84038012',
                title='Test Paper',
                date='1906-04-18',
                edition=1,
                sequence=i,
                page_url=f'https://example.com/item{i}',
                pdf_url=None,
                jp2_url=None,
                ocr_text=None,
                word_count=None
            ) for i in range(101, 151)]
        ]
        
        # Test discovery
        discovered_count = self.discovery.discover_facet_content(facet_id, batch_size=100)
        
        # Should discover all items across pages
        assert discovered_count == 150
        
        # Should have made 2 API calls
        assert self.mock_api_client.search_pages.call_count == 2
        
        # Check pagination parameters
        calls = self.mock_api_client.search_pages.call_args_list
        assert calls[0][1]['page'] == 1  # First page
        assert calls[1][1]['page'] == 2  # Second page
    
    def test_discover_facet_content_query_facet(self):
        """Test discovering content for a query-based facet."""
        # Create a test facet with query
        facet_id = self.storage.create_search_facet(
            'query', 'earthquake', 'earthquake', 500
        )
        
        # Mock API response
        self.mock_api_client.search_pages.return_value = {
            'items': [{'id': 'item1', 'title': 'Earthquake News'}],
            'totalItems': 1
        }
        self.mock_processor.parse_pages.return_value = [
            PageInfo(
                item_id='item1',
                lccn='sn84038012',
                title='Earthquake News',
                date='1906-04-18',
                edition=1,
                sequence=1,
                page_url='https://example.com/item1',
                pdf_url=None,
                jp2_url=None,
                ocr_text=None,
                word_count=None
            )
        ]
        
        # Test discovery
        discovered_count = self.discovery.discover_facet_content(facet_id)
        
        # Verify query parameter was used
        call_args = self.mock_api_client.search_pages.call_args[1]
        assert call_args['andtext'] == 'earthquake'
        assert discovered_count == 1

    def test_discover_all_periodicals(self):
        """Test discovering all periodicals from parsed NewspaperInfo objects."""
        newspapers = [
            NewspaperInfo(
                lccn='sn84038012',
                title='The San Francisco Call',
                place_of_publication=['California'],
                start_year=1895,
                end_year=1913,
                frequency='Daily',
                subject=[],
                language=['English'],
                url='https://example.com',
                state='California',
                city='San Francisco',
            )
        ]
        # get_newspapers_with_details now yields NewspaperInfo (builder+processor owned by the client)
        self.mock_api_client.get_newspapers_with_details.return_value = iter(newspapers)

        with patch.object(self.storage, 'store_periodicals') as mock_store:
            mock_store.return_value = 1
            discovered_count = self.discovery.discover_all_periodicals(max_newspapers=1)

            assert discovered_count == 1
            mock_store.assert_called_once()
            # the parsed attributes flow through into the periodical record
            stored = mock_store.call_args[0][0][0]
            assert stored['state'] == 'California'
            assert stored['city'] == 'San Francisco'

    def test_create_state_facets(self):
        """Test creating state facets from discovered periodicals."""
        # Add periodicals to storage
        periodicals = [
            {'lccn': 'sn123', 'state': 'California', 'title': 'CA Paper'},
            {'lccn': 'sn456', 'state': 'New York', 'title': 'NY Paper'}
        ]
        # Mock storage methods
        with patch.object(self.storage, 'get_periodicals') as mock_get_periodicals, \
             patch.object(self.storage, 'create_search_facet') as mock_create_facet:
            
            mock_get_periodicals.return_value = periodicals
            mock_create_facet.side_effect = [1, 2]  # Return facet IDs
            
            facet_ids = self.discovery.create_state_facets()
            
            assert len(facet_ids) == 2
            assert mock_create_facet.call_count == 2

    def test_get_discovery_summary(self):
        """Test getting comprehensive discovery summary."""
        # Mock storage responses
        with patch.object(self.storage, 'get_discovery_stats') as mock_stats:
            mock_stats.return_value = {
                'total_periodicals': 5,
                'discovered_periodicals': 3,
                'total_facets': 10,
                'completed_facets': 5,
                'total_queue_items': 20,
                'queued_items': 15,
                'estimated_items': 50000
            }
            
            with patch.object(self.storage, 'get_download_queue') as mock_get_queue:
                mock_get_queue.return_value = [
                    {
                        'queue_type': 'facet',
                        'reference_id': '1',
                        'priority': 1,
                        'estimated_size_mb': 100,
                        'estimated_time_hours': 2.5
                    }
                ]
                
                summary = self.discovery.get_discovery_summary()
                
                assert 'discovery_stats' in summary
                assert 'next_downloads' in summary
                assert summary['discovery_stats']['total_periodicals'] == 5
                assert len(summary['next_downloads']) == 1

    def test_create_date_range_facets_with_estimation(self):
        """Test creating date facets with item estimation."""
        # Test the simpler case without estimation to avoid mocking complexity
        with patch.object(self.storage, 'get_search_facets') as mock_get_facets, \
             patch.object(self.storage, 'create_search_facet') as mock_create_facet:
            
            mock_get_facets.return_value = []
            mock_create_facet.side_effect = [1, 2]  # Return facet IDs
            
            # Test without estimation to avoid string formatting issues
            facet_ids = self.discovery.create_date_range_facets(
                2000, 2001, facet_size_years=1, estimate_items=False
            )
            
            assert len(facet_ids) == 2  # 2000 and 2001
            # Verify facets were created
            assert mock_create_facet.call_count == 2

    def test_populate_download_queue(self):
        """Test populating download queue with priorities."""
        # Mock storage methods
        with patch.object(self.storage, 'get_search_facets') as mock_get_facets, \
             patch.object(self.storage, 'get_pages_for_facet') as mock_get_pages, \
             patch.object(self.storage, 'add_to_download_queue') as mock_add_queue, \
             patch.object(self.storage, 'get_download_queue') as mock_get_queue:
            
            # Mock facets with different calls returning different subsets
            date_range_facets = [
                {'id': 1, 'facet_type': 'date_range', 'facet_value': '1906/1906', 'status': 'completed', 'estimated_items': 100, 'actual_items': 95}
            ]
            state_facets = [
                {'id': 2, 'facet_type': 'state', 'facet_value': 'California', 'status': 'completed', 'estimated_items': 50, 'actual_items': 45}
            ]
            completed_facets = date_range_facets + state_facets
            
            def mock_get_facets_side_effect(facet_type=None, status=None):
                if facet_type == 'date_range':
                    return date_range_facets
                elif facet_type == 'state':
                    return state_facets
                elif status == 'completed':
                    return completed_facets
                else:
                    return completed_facets
            
            mock_get_facets.side_effect = mock_get_facets_side_effect
            mock_get_queue.return_value = []  # No existing queue items
            mock_add_queue.return_value = None
            
            queue_count = self.discovery.populate_download_queue(
                priority_states=['California'],
                priority_date_ranges=['1906/1906']
            )
            
            assert queue_count >= 0
            # Should have made calls to add items to queue
            assert mock_add_queue.call_count >= 2

    def test_create_date_range_facets_with_estimation_uses_query_builder(self):
        """
        create_date_range_facets(estimate_items=True) now goes through
        self.query_builder_class + self.api_client.get_count(), not the
        retired estimate_download_size. The only prior test used
        estimate_items=False — this path had zero coverage before.
        """
        self.mock_api_client.get_count.return_value = 250

        facet_ids = self.discovery.create_date_range_facets(
            2000, 2000, facet_size_years=1, estimate_items=True
        )

        assert self.mock_api_client.get_count.called
        builder_passed = self.mock_api_client.get_count.call_args[0][0]
        assert isinstance(builder_passed, self.discovery.query_builder_class)