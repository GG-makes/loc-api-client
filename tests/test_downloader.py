"""
Tests for the download processor module.
"""

import pytest
import tempfile
import shutil
import unittest.mock
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
import json
import requests

from src.newsagger.downloader import DownloadProcessor
from src.newsagger.storage import NewsStorage
from src.newsagger.rate_limited_client import LocApiClient


class TestDownloadProcessor:
    
    @pytest.fixture
    def temp_dir(self):
        """Create a temporary directory for downloads."""
        temp_dir = tempfile.mkdtemp()
        yield temp_dir
        shutil.rmtree(temp_dir)
    
    @pytest.fixture
    def mock_storage(self):
        """Mock storage instance."""
        storage = Mock(spec=NewsStorage)
        
        # Mock storage methods
        storage.get_download_queue.return_value = [
            {
                'id': 1,
                'queue_type': 'page',
                'reference_id': 'test_page_1',
                'priority': 1,
                'estimated_size_mb': 2.5,
                'estimated_time_hours': 0.1,
                'status': 'queued'
            },
            {
                'id': 2,
                'queue_type': 'facet',
                'reference_id': '123',
                'priority': 2,
                'estimated_size_mb': 50.0,
                'estimated_time_hours': 1.0,
                'status': 'queued'
            }
        ]
        
        storage.get_pages.return_value = [
            {
                'item_id': 'test_page_1',
                'lccn': 'sn12345678',
                'title': 'Test Newspaper',
                'date': '1906-04-18',
                'edition': 1,
                'sequence': 1,
                'page_url': 'https://chroniclingamerica.loc.gov/lccn/sn12345678/1906-04-18/ed-1/seq-1/',
                'pdf_url': 'https://chroniclingamerica.loc.gov/lccn/sn12345678/1906-04-18/ed-1/seq-1.pdf',
                'jp2_url': 'https://chroniclingamerica.loc.gov/lccn/sn12345678/1906-04-18/ed-1/seq-1.jp2',
                'ocr_text': 'Sample OCR text content',
                'downloaded': False
            }
        ]
        
        storage.get_pages_for_facet.return_value = [
            {
                'item_id': 'facet_page_1',
                'lccn': 'sn87654321',
                'title': 'Another Test Newspaper',
                'date': '1906-04-19',
                'edition': 1,
                'sequence': 1,
                'page_url': 'https://chroniclingamerica.loc.gov/lccn/sn87654321/1906-04-19/ed-1/seq-1/',
                'pdf_url': 'https://chroniclingamerica.loc.gov/lccn/sn87654321/1906-04-19/ed-1/seq-1.pdf',
                'jp2_url': None,
                'ocr_text': None,
                'downloaded': False
            }
        ]
        
        storage.get_search_facet.return_value = {
            'id': 123,
            'facet_type': 'date_range',
            'facet_value': '1906/1906',
            'query': '',
            'items_discovered': 5
        }
        
        storage.get_download_queue_stats.return_value = {
            'total_items': 2,
            'queued': 2,
            'active': 0,
            'completed': 0,
            'failed': 0,
            'total_size_mb': 52.5
        }
        
        storage.get_storage_stats.return_value = {
            'total_pages': 100,
            'downloaded_pages': 10,
            'total_newspapers': 5
        }
        
        return storage
    
    @pytest.fixture
    def mock_api_client(self):
        """Mock API client instance."""
        client = Mock(spec=LocApiClient)
        client.request_delay = 1.0
        return client
    
    @pytest.fixture
    def downloader(self, mock_storage, mock_api_client, temp_dir):
        """Create DownloadProcessor instance with mocked dependencies."""
        return DownloadProcessor(mock_storage, mock_api_client, temp_dir)
    
    def test_init(self, mock_storage, mock_api_client, temp_dir):
        """Test DownloadProcessor initialization."""
        downloader = DownloadProcessor(mock_storage, mock_api_client, temp_dir)
        
        assert downloader.storage == mock_storage
        assert downloader.api_client == mock_api_client
        assert downloader.download_dir == Path(temp_dir)
        assert downloader.download_dir.exists()
        assert hasattr(downloader, 'session')
    
    def test_process_queue_dry_run(self, downloader, mock_storage):
        """Test dry run processing of download queue."""
        result = downloader.process_queue(dry_run=True)
        
        assert 'would_download' in result
        assert 'estimated_size_mb' in result
        assert result['dry_run'] is True
        assert result['would_download'] == 2
        assert result['estimated_size_mb'] == 52.5
    
    def test_process_queue_max_items_limit(self, downloader, mock_storage):
        """Test processing queue with max items limit."""
        result = downloader.process_queue(max_items=1, dry_run=True)
        
        assert result['would_download'] == 1
    
    def test_process_queue_max_size_limit(self, downloader, mock_storage):
        """Test processing queue with max size limit."""
        result = downloader.process_queue(max_size_mb=30.0, dry_run=True)
        
        # Should only include the first item (2.5 MB)
        assert result['would_download'] == 1
        assert result['estimated_size_mb'] == 2.5
    
    def test_process_queue_empty(self, downloader, mock_storage):
        """Test processing empty queue."""
        mock_storage.get_download_queue.return_value = []
        
        result = downloader.process_queue()
        
        assert result['downloaded'] == 0
        assert result['errors'] == 0
        assert result['skipped'] == 0
    
    @patch('requests.Session.get')
    def test_download_file_success(self, mock_get, downloader, temp_dir):
        """Test successful file download."""
        # Mock successful HTTP response
        test_content = b'test content chunk'
        mock_response = Mock()
        mock_response.raise_for_status.return_value = None
        mock_response.headers = {'content-length': str(len(test_content))}
        mock_response.iter_content.return_value = [test_content]
        mock_get.return_value = mock_response
        
        file_path = Path(temp_dir) / 'test_file.pdf'
        result = downloader._download_file('https://example.com/test.pdf', file_path)
        
        assert result['success'] is True
        assert result['file_path'] == str(file_path)
        assert result['size_mb'] > 0
        assert file_path.exists()
    
    @patch('requests.Session.get')
    def test_download_file_already_exists(self, mock_get, downloader, temp_dir):
        """Test download when file already exists."""
        file_path = Path(temp_dir) / 'existing_file.pdf'
        file_path.write_text('existing content')
        
        result = downloader._download_file('https://example.com/test.pdf', file_path)
        
        assert result['success'] is True
        assert result['skipped'] is True
        assert result['size_mb'] > 0
        mock_get.assert_not_called()
    
    @patch('requests.Session.get')
    def test_download_file_network_error(self, mock_get, downloader, temp_dir):
        """Test download with network error."""
        mock_get.side_effect = requests.exceptions.RequestException("Network error")
        
        file_path = Path(temp_dir) / 'test_file.pdf'
        result = downloader._download_file('https://example.com/test.pdf', file_path)
        
        assert result['success'] is False
        assert 'Network error' in result['error']
        assert not file_path.exists()
    
    @patch('requests.Session')
    def test_download_page_success(self, mock_session_class, downloader, mock_storage, temp_dir):
        test_content = b'test pdf content' * 1000

        mock_response = Mock()
        mock_response.raise_for_status.return_value = None
        mock_response.headers = {'content-length': str(len(test_content))}
        mock_response.iter_content.return_value = [test_content]

        mock_session = Mock()
        mock_session.get.return_value = mock_response
        mock_session_class.return_value = mock_session

        mock_storage.get_page_by_item_id.return_value = {
            'item_id': 'test_page_1',
            'lccn': 'sn12345678',
            'title': 'Test Paper',
            'date': '1925-01-01',
            'edition': 1,
            'sequence': 1,
            'page_url': 'https://example.com/page1',
            'pdf_url': 'https://example.com/page1.pdf',
            'jp2_url': None,
            'ocr_text': 'Sample OCR text content',
            'downloaded': False
        }

        result = downloader._download_page('test_page_1')

        assert result['success'] is True
        assert result['size_mb'] > 0
        assert 'files' in result

        # Directory matches date in mock data: 1925/01
        expected_dir = Path(temp_dir) / 'sn12345678' / '1925' / '01'
        assert expected_dir.exists()

        pdf_file = expected_dir / 'test_page_1.pdf'
        assert pdf_file.exists()

        ocr_file = expected_dir / 'test_page_1_ocr.txt'
        assert ocr_file.exists()
        assert ocr_file.read_text(encoding='utf-8') == 'Sample OCR text content'

        metadata_file = expected_dir / 'test_page_1_metadata.json'
        assert metadata_file.exists()
        metadata = json.loads(metadata_file.read_text(encoding='utf-8'))
        assert metadata['item_id'] == 'test_page_1'
        assert metadata['lccn'] == 'sn12345678'

        mock_storage.mark_page_downloaded.assert_called_once_with('test_page_1')

        
    def test_download_page_already_downloaded(self, downloader, mock_storage):
        """Test download of already downloaded page."""
        # Mock page as already downloaded
        mock_storage.get_pages.return_value = [
            {
                'item_id': 'test_page_1',
                'lccn': 'sn12345678',
                'title': 'Test Newspaper',
                'date': '1906-04-18',
                'edition': 1,
                'sequence': 1,
                'downloaded': True  # Already downloaded
            }
        ]
        
        result = downloader._download_page('test_page_1')
        
        assert result['success'] is True
        assert result['skipped'] is True
        mock_storage.mark_page_downloaded.assert_not_called()
        
    def test_download_page_not_found(self, downloader, mock_storage):
        """Test download of non-existent page."""
        mock_storage.get_page_by_item_id.return_value = None

        result = downloader._download_page('nonexistent_page')

        assert result['success'] is False
        assert 'not found' in result['error']    

    @patch.object(DownloadProcessor, '_download_page')
    def test_download_facet_content(self, mock_download_page, downloader, mock_storage):
        """Test downloading all content from a facet."""
        # Mock successful page downloads
        mock_download_page.return_value = {
            'success': True,
            'size_mb': 2.0,
            'files': ['test.pdf']
        }
        
        result = downloader._download_facet_content('123')
        
        assert result['success'] is True
        assert result['downloaded_pages'] == 1
        assert result['size_mb'] == 2.0
        assert result['total_pages'] == 1
        
        # Verify page download was called
        mock_download_page.assert_called_once_with('facet_page_1')
    
    @patch.object(DownloadProcessor, '_download_page')
    def test_download_facet_content_with_errors(self, mock_download_page, downloader, mock_storage):
        """Test downloading facet content with some page errors."""
        # Mock one successful and one failed download
        mock_download_page.side_effect = [
            {'success': True, 'size_mb': 2.0},
            {'success': False, 'error': 'Download failed'}
        ]
        
        # Add another page to facet
        mock_storage.get_pages_for_facet.return_value = [
            {'item_id': 'facet_page_1', 'downloaded': False},
            {'item_id': 'facet_page_2', 'downloaded': False}
        ]
        
        result = downloader._download_facet_content('123')
        
        assert result['success'] is True  # Should succeed if at least one page downloads
        assert result['downloaded_pages'] == 1
        assert result['errors'] == 1
        assert result['total_pages'] == 2
    
    @patch.object(DownloadProcessor, '_download_page')
    def test_download_periodical(self, mock_download_page, downloader, mock_storage):
        """Test downloading all content from a periodical."""
        mock_download_page.return_value = {
            'success': True,
            'size_mb': 1.5,
            'files': ['test.pdf']
        }
        
        # Mock periodical pages
        mock_storage.get_pages.return_value = [
            {'item_id': 'page_1', 'downloaded': False},
            {'item_id': 'page_2', 'downloaded': True}  # Already downloaded
        ]
        
        result = downloader._download_periodical('sn12345678')
        
        assert result['success'] is True
        assert result['downloaded_pages'] == 1  # Only one was not downloaded
        assert result['total_pages'] == 1
        
        # Should only download the undownloaded page
        mock_download_page.assert_called_once_with('page_1')
    
    def test_resume_failed_downloads(self, downloader, mock_storage):
        """Test resuming failed downloads."""
        mock_storage.get_download_queue.return_value = [
            {'id': 1, 'status': 'failed'},
            {'id': 2, 'status': 'failed'}
        ]
        
        result = downloader.resume_failed_downloads()
        
        assert result['resumed'] == 2
        
        # Verify that failed items were reset to queued
        expected_calls = [
            unittest.mock.call(1, status='queued', error_message=None),
            unittest.mock.call(2, status='queued', error_message=None)
        ]
        mock_storage.update_queue_item.assert_has_calls(expected_calls, any_order=True)
    
    def test_resume_failed_downloads_none_failed(self, downloader, mock_storage):
        """Test resuming when no downloads are failed."""
        mock_storage.get_download_queue.return_value = []
        
        result = downloader.resume_failed_downloads()
        
        assert result['resumed'] == 0
        mock_storage.update_queue_item.assert_not_called()
    
    def test_get_download_stats(self, downloader, mock_storage, temp_dir):
        """Test getting comprehensive download statistics."""
        # Create some test files to calculate disk usage
        test_file = Path(temp_dir) / 'test_file.txt'
        test_file.write_text('test content with some meaningful size to ensure non-zero disk usage' * 1000)  # Make it larger
        
        stats = downloader.get_download_stats()
        
        assert 'queue_stats' in stats
        assert 'storage_stats' in stats
        assert 'disk_usage_mb' in stats
        assert 'download_directory' in stats
        assert 'files_on_disk' in stats
        
        assert stats['download_directory'] == str(Path(temp_dir))
        assert stats['files_on_disk'] >= 1  # At least our test file
        assert stats['disk_usage_mb'] > 0
    
    def test_cleanup_incomplete_downloads(self, downloader, temp_dir):
        """Test cleanup of incomplete/corrupted downloads."""
        # Create test files
        zero_byte_file = Path(temp_dir) / 'zero_byte.pdf'
        zero_byte_file.touch()  # Creates empty file
        
        small_pdf = Path(temp_dir) / 'small.pdf'
        small_pdf.write_bytes(b'tiny')  # Very small PDF (suspicious)
        
        normal_file = Path(temp_dir) / 'normal.txt'
        normal_file.write_text('This is a normal file with content')
        
        result = downloader.cleanup_incomplete_downloads()
        
        assert result['cleaned_files'] >= 1  # Should clean at least the zero-byte file
        assert not zero_byte_file.exists()  # Zero-byte file should be removed
        assert not small_pdf.exists()  # Small PDF should be removed
        assert normal_file.exists()  # Normal file should remain
    
    @patch.object(DownloadProcessor, '_process_queue_item')
    def test_process_queue_with_updates(self, mock_process_item, downloader, mock_storage):
        mock_process_item.return_value = {'success': True, 'size_mb': 2.5}

        with patch.object(downloader, '_process_batch_updates') as mock_batch_updates:
            result = downloader.process_queue(max_items=1)

        assert result['downloaded'] == 1
        assert result['errors'] == 0
        assert result['total_size_mb'] == 2.5

        mock_storage.update_queue_item.assert_any_call(1, status='active')
        
        mock_batch_updates.assert_called_once()
        update = mock_batch_updates.call_args[0][0][0]
        assert update['id'] == 1
        assert update['status'] == 'completed'
        assert update['progress_percent'] == 100.0

    @patch.object(DownloadProcessor, '_process_queue_item')
    def test_process_queue_with_errors(self, mock_process_item, downloader, mock_storage):
        mock_process_item.return_value = {'success': False, 'error': 'Download failed'}

        with patch.object(downloader, '_process_batch_updates') as mock_batch_updates:
            result = downloader.process_queue(max_items=1)

        assert result['downloaded'] == 0
        assert result['errors'] == 1

        mock_batch_updates.assert_called_once()
        update = mock_batch_updates.call_args[0][0][0]
        assert update['id'] == 1
        assert update['status'] == 'failed'
        assert update['error_message'] == 'Download failed'
    
    def test_process_queue_item_page(self, downloader):
        """Test processing a page queue item."""
        with patch.object(downloader, '_download_page') as mock_download:
            mock_download.return_value = {'success': True, 'size_mb': 2.0}
            
            result = downloader._process_queue_item({
                'queue_type': 'page',
                'reference_id': 'test_page_1'
            })
            
            assert result['success'] is True
            mock_download.assert_called_once_with('test_page_1')
    
    def test_process_queue_item_facet(self, downloader):
        """Test processing a facet queue item."""
        with patch.object(downloader, '_download_facet_content') as mock_download:
            mock_download.return_value = {'success': True, 'downloaded_pages': 5}
            
            result = downloader._process_queue_item({
                'queue_type': 'facet',
                'reference_id': '123'
            })
            
            assert result['success'] is True
            mock_download.assert_called_once_with('123')
    
    def test_process_queue_item_periodical(self, downloader):
        """Test processing a periodical queue item."""
        with patch.object(downloader, '_download_periodical') as mock_download:
            mock_download.return_value = {'success': True, 'downloaded_pages': 10}
            
            result = downloader._process_queue_item({
                'queue_type': 'periodical',
                'reference_id': 'sn12345678'
            })
            
            assert result['success'] is True
            mock_download.assert_called_once_with('sn12345678')
    
    def test_process_queue_item_unknown_type(self, downloader):
        """Test processing unknown queue item type."""
        result = downloader._process_queue_item({
            'queue_type': 'unknown_type',
            'reference_id': 'test_id'
        })
        
        assert result['success'] is False
        assert 'Unknown queue type' in result['error']


if __name__ == '__main__':
    pytest.main([__file__])