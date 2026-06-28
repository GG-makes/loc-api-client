"""
Test batch utilities functionality.

Tests for BatchMapper and BatchSessionTracker classes that handle
batch-to-LCCN mapping and progress tracking.
"""

import pytest
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime
import tempfile
import sqlite3
from pathlib import Path
import sys
import gc
import time
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))

from newsagger.batch_utils import BatchMapper, BatchSessionTracker
from newsagger.storage import NewsStorage
from newsagger.rate_limited_client import LocApiClient


class TestBatchMapper:
    """Test BatchMapper functionality."""
    
    def test_batch_mapper_initialization(self):
        """Test BatchMapper initialization."""
        mock_storage = Mock(spec=NewsStorage)
        mock_client = Mock(spec=LocApiClient)
        
        mapper = BatchMapper(mock_storage, mock_client)
        
        assert mapper.storage == mock_storage
        assert mapper.api_client == mock_client
        assert mapper._batch_cache == {}
        assert mapper._lccn_to_batch_cache == {}
    
    def test_batch_mapper_default_client(self):
        """Test BatchMapper with default API client."""
        mock_storage = Mock(spec=NewsStorage)
        
        with patch('newsagger.batch_utils.LocApiClient') as mock_client_class:
            mock_client = Mock()
            mock_client_class.return_value = mock_client
            
            mapper = BatchMapper(mock_storage)
            
            assert mapper.storage == mock_storage
            assert mapper.api_client == mock_client
            mock_client_class.assert_called_once()
    
    def test_get_batch_metadata_cached(self):
        """Test getting batch metadata from cache."""
        mock_storage = Mock(spec=NewsStorage)
        mock_client = Mock(spec=LocApiClient)
        
        mapper = BatchMapper(mock_storage, mock_client)
        
        # Pre-populate cache
        cached_metadata = {'name': 'test_batch', 'page_count': 100}
        mapper._batch_cache['test_batch'] = cached_metadata
        
        result = mapper.get_batch_metadata('test_batch')
        
        assert result == cached_metadata
        # API should not be called
        mock_client._make_request.assert_not_called()
    
    def test_get_batch_metadata_api_call(self):
        """Test getting batch metadata from API."""
        mock_storage = Mock(spec=NewsStorage)
        mock_client = Mock(spec=LocApiClient)
        
        api_response = {
            'name': 'test_batch',
            'page_count': 500,
            'issues': [
                {'url': 'https://chroniclingamerica.loc.gov/lccn/sn12345678/1900-01-01/ed-1.json'},
                {'url': 'https://chroniclingamerica.loc.gov/lccn/sn87654321/1900-01-02/ed-1.json'}
            ],
            'url': 'https://chroniclingamerica.loc.gov/batches/test_batch.json',
            'created': '2023-01-01',
            'ingested': '2023-01-02'
        }
        mock_client._make_request.return_value = api_response
        
        mapper = BatchMapper(mock_storage, mock_client)
        result = mapper.get_batch_metadata('test_batch')
        
        # Verify API was called
        mock_client._make_request.assert_called_once_with('batches/test_batch.json')
        
        # Verify result structure
        assert result['name'] == 'test_batch'
        assert result['page_count'] == 500
        assert result['issue_count'] == 2
        assert len(result['lccns']) == 2
        assert 'sn12345678' in result['lccns']
        assert 'sn87654321' in result['lccns']
        
        # Verify caching
        assert mapper._batch_cache['test_batch'] == result
    
    def test_get_batch_metadata_api_error(self):
        """Test handling API errors."""
        mock_storage = Mock(spec=NewsStorage)
        mock_client = Mock(spec=LocApiClient)
        mock_client._make_request.side_effect = Exception("API Error")
        
        mapper = BatchMapper(mock_storage, mock_client)
        result = mapper.get_batch_metadata('test_batch')
        
        assert 'error' in result
        assert result['name'] == 'test_batch'
        assert 'API Error' in result['error']
    
    def test_extract_lccns_from_batch(self):
        """Test LCCN extraction from batch data."""
        mock_storage = Mock(spec=NewsStorage)
        mock_client = Mock(spec=LocApiClient)
        
        batch_data = {
            'issues': [
                {'url': 'https://chroniclingamerica.loc.gov/lccn/sn12345678/1900-01-01/ed-1.json'},
                {'url': 'https://chroniclingamerica.loc.gov/lccn/sn87654321/1900-01-02/ed-1.json'},
                {'url': 'https://chroniclingamerica.loc.gov/lccn/sn11111111/1900-01-03/ed-1.json'},
                {'url': 'https://example.com/invalid/url'},  # Should be ignored
                {'url': 'https://chroniclingamerica.loc.gov/lccn/invalid/1900-01-04/ed-1.json'}  # Invalid LCCN
            ]
        }
        
        mapper = BatchMapper(mock_storage, mock_client)
        lccns = mapper._extract_lccns_from_batch(batch_data)
        
        # Should extract valid LCCNs only
        assert len(lccns) == 3
        assert 'sn12345678' in lccns
        assert 'sn87654321' in lccns
        assert 'sn11111111' in lccns
        assert 'invalid' not in lccns
    
    def test_get_lccn_to_batch_mapping(self):
        """Test creating LCCN to batch mapping."""
        mock_storage = Mock(spec=NewsStorage)
        mock_client = Mock(spec=LocApiClient)
        
        mapper = BatchMapper(mock_storage, mock_client)
        
        # Mock get_batch_metadata to return different LCCNs per batch
        def mock_get_metadata(batch_name):
            if batch_name == 'batch1':
                return {'lccns': {'sn12345678', 'sn87654321'}}
            elif batch_name == 'batch2':
                return {'lccns': {'sn11111111', 'sn22222222'}}
            return {'lccns': set()}
        
        mapper.get_batch_metadata = Mock(side_effect=mock_get_metadata)
        
        result = mapper.get_lccn_to_batch_mapping(['batch1', 'batch2'])
        
        expected = {
            'sn12345678': 'batch1',
            'sn87654321': 'batch1',
            'sn11111111': 'batch2',
            'sn22222222': 'batch2'
        }
        
        assert result == expected
        assert mapper._lccn_to_batch_cache == expected
    
    @patch('newsagger.batch_utils.sqlite3.connect')
    def test_get_batch_discovery_status(self, mock_connect):
        """Test getting batch discovery status."""
        mock_storage = Mock(spec=NewsStorage)
        mock_storage.db_path = '/test/db.db'
        mock_client = Mock(spec=LocApiClient)
        mock_client.base_url = r'https://chroniclingamerica.loc.gov'
        
        # Mock database
        mock_conn = MagicMock()
        mock_cursor = Mock()
        mock_connect.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        mock_conn.__enter__.return_value = mock_conn
        mock_conn.__exit__.return_value = None

        # Replace MockRow with a simple dict subclass that also supports attribute access
        class MockRow(dict):
            def __getattr__(self, key):
                return self[key]
        
        mock_cursor.fetchall.return_value = [
            MockRow(item_id='page1', page_url='/lccn/sn12345678/1900-01-01/ed-1/seq-1/', downloaded=True, created_at='2023-01-01'),
            MockRow(item_id='page2', page_url='/lccn/sn12345678/1900-01-01/ed-1/seq-2/', downloaded=False, created_at='2023-01-01')
        ]
        
        mapper = BatchMapper(mock_storage, mock_client)
        
        # Mock batch metadata
        batch_metadata = {
            'name': 'test_batch',
            'page_count': 100,
            'issue_count': 10,
            'issues': [
                {'url': 'https://chroniclingamerica.loc.gov/lccn/sn12345678/1900-01-01/ed-1.json'}
            ],
            'lccns': {'sn12345678'}
        }
        mapper.get_batch_metadata = Mock(return_value=batch_metadata)
        
        result = mapper.get_batch_discovery_status('test_batch')
        
        assert result['batch_name'] == 'test_batch'
        assert result['expected_issues'] == 10
        assert result['expected_pages'] == 100
        assert result['discovered_issues'] == 1
        assert result['discovered_pages'] == 2
        assert result['discovery_issue_pct'] == 10.0  # 1/10 * 100
        assert result['discovery_page_pct'] == 2.0    # 2/100 * 100
        assert result['is_discovery_complete'] is False
        assert result['lccns'] == {'sn12345678'}
    
    @patch('newsagger.batch_utils.Path')
    def test_get_batch_download_status(self, mock_path_class):
        """Test getting batch download status with filesystem check."""
        mock_storage = Mock(spec=NewsStorage)
        mock_client = Mock(spec=LocApiClient)
        
        mapper = BatchMapper(mock_storage, mock_client)
        
        # Mock discovery status
        discovery_status = {
            'batch_name': 'test_batch',
            'expected_pages': 100,
            'discovered_pages': 50,
            'pages_data': [
                {'downloaded': True},
                {'downloaded': False},
                {'downloaded': True}
            ],
            'lccns': {'sn12345678', 'sn87654321'}
        }
        mapper.get_batch_discovery_status = Mock(return_value=discovery_status)
        
        # Mock filesystem
        mock_downloads_path = Mock()
        mock_path_class.return_value = mock_downloads_path
        mock_downloads_path.exists.return_value = True
        
        # Mock LCCN directories and files
        mock_lccn_dir1 = Mock()
        mock_lccn_dir1.exists.return_value = True
        mock_file1 = Mock()
        mock_file1.is_file.return_value = True
        mock_file1.stat.return_value.st_size = 1024 * 1024  # 1MB
        mock_lccn_dir1.rglob.return_value = [mock_file1, mock_file1]  # 2 files
        
        mock_lccn_dir2 = Mock()
        mock_lccn_dir2.exists.return_value = True
        mock_file2 = Mock()
        mock_file2.is_file.return_value = True
        mock_file2.stat.return_value.st_size = 2 * 1024 * 1024  # 2MB
        mock_lccn_dir2.rglob.return_value = [mock_file2]  # 1 file
        
        # Configure Path division operator properly
        def mock_truediv(lccn):
            return {
                'sn12345678': mock_lccn_dir1,
                'sn87654321': mock_lccn_dir2
            }[lccn]
        
        mock_downloads_path.__truediv__ = Mock(side_effect=mock_truediv)
        
        result = mapper.get_batch_download_status('test_batch', '/test/downloads')
        
        # Verify download statistics
        assert result['downloaded_pages'] == 2  # From pages_data
        assert result['download_pct_of_discovered'] == 4.0  # 2/50 * 100
        assert result['download_pct_of_expected'] == 2.0    # 2/100 * 100
        assert result['filesystem_files'] == 3  # 2 + 1 files
        assert result['filesystem_size_mb'] == 4.0  # 1+1+2 MB


class TestBatchSessionTracker:
    """Test BatchSessionTracker functionality."""
    
    def test_session_tracker_initialization(self):
        """Test BatchSessionTracker initialization."""
        mock_storage = Mock(spec=NewsStorage)
        tracker = BatchSessionTracker(mock_storage)
        
        assert tracker.storage == mock_storage
    
    @patch('newsagger.batch_utils.sqlite3.connect')
    def test_get_active_sessions(self, mock_connect):
        """Test getting active batch discovery sessions."""
        mock_storage = Mock(spec=NewsStorage)
        mock_storage.db_path = '/test/db.db'
        
        # Mock database
        mock_conn = MagicMock()
        mock_cursor = Mock()
        mock_connect.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        mock_conn.__enter__.return_value = mock_conn
        mock_conn.__exit__.return_value = None

        # Replace MockRow with a simple dict subclass that also supports attribute access
        class MockRow(dict):
            def __getattr__(self, key):
                return self[key]
                
        mock_cursor.fetchall.return_value = [
            MockRow(session_name='session1', status='active', current_batch_name='batch1'),
            MockRow(session_name='session2', status='captcha_blocked', current_batch_name='batch2')
        ]
        
        tracker = BatchSessionTracker(mock_storage)
        result = tracker.get_active_sessions()
        
        assert len(result) == 2
        mock_cursor.execute.assert_called_once()
        # Verify the query looks for active and captcha_blocked sessions
        call_args = mock_cursor.execute.call_args[0][0]
        assert 'active' in call_args
        assert 'captcha_blocked' in call_args
    
    @patch('newsagger.batch_utils.sqlite3.connect')
    def test_get_session_progress(self, mock_connect):
        """Test getting detailed session progress."""
        mock_storage = Mock(spec=NewsStorage)
        mock_storage.db_path = '/test/db.db'
        
        # Mock database
        mock_conn = MagicMock()
        mock_cursor = Mock()
        mock_connect.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        mock_conn.close.return_value = None
        mock_conn.__enter__.return_value = mock_conn
        mock_conn.__exit__.return_value = None

        # Replace MockRow with a simple dict subclass that also supports attribute access
        class MockRow(dict):
            def __getattr__(self, key):
                return self[key]
        
        session_row = MockRow(
            session_name='test_session',
            started_at='2023-01-01T10:00:00',
            updated_at='2023-01-01T11:00:00',
            total_pages_discovered=1000
        )
        
        mock_cursor.fetchone.return_value = session_row
        
        tracker = BatchSessionTracker(mock_storage)
        result = tracker.get_session_progress('test_session')
        
        assert result is not None
        assert result['session_name'] == 'test_session'
        assert 'duration_str' in result
        assert 'duration_seconds' in result
        assert 'pages_per_hour' in result
        assert 'pages_per_minute' in result
        
        # Verify it calculated rates (1000 pages in 1 hour = 1000 pages/hour)
        assert result['pages_per_hour'] == 1000
    
    @patch('newsagger.batch_utils.sqlite3.connect')
    def test_get_session_progress_not_found(self, mock_connect):
        """Test getting progress for non-existent session."""
        mock_storage = Mock(spec=NewsStorage)
        mock_storage.db_path = '/test/db.db'
        
        # Mock database
        mock_conn = MagicMock()
        mock_cursor = Mock()
        mock_connect.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        mock_conn.close.return_value = None
        mock_conn.__enter__.return_value = mock_conn
        mock_conn.__exit__.return_value = None

        mock_cursor.fetchone.return_value = None
        
        tracker = BatchSessionTracker(mock_storage)
        result = tracker.get_session_progress('nonexistent_session')
        
        assert result is None


class TestBatchUtilsIntegration:
    """Integration tests for batch utilities."""
        
    def test_batch_mapper_full_workflow(self):
        """Test full BatchMapper workflow with mocked components."""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp_db:
            db_path = tmp_db.name

        try:
            # Create storage
            storage = NewsStorage(db_path)

            # Mock API client
            mock_client = Mock(spec=LocApiClient)
            mock_client._make_request.return_value = {
                'name': 'test_batch',
                'page_count': 100,
                'issues': [
                    {'url': 'https://chroniclingamerica.loc.gov/lccn/sn12345678/1900-01-01/ed-1.json'}
                ]
            }

            mapper = BatchMapper(storage, mock_client)

            # Test getting metadata
            metadata = mapper.get_batch_metadata('test_batch')
            assert metadata['name'] == 'test_batch'
            assert 'sn12345678' in metadata['lccns']

            # Test LCCN mapping
            mapping = mapper.get_lccn_to_batch_mapping(['test_batch'])
            assert mapping['sn12345678'] == 'test_batch'

            del storage
            gc.collect()
        finally:
            for _ in range(10):
                try:
                    Path(db_path).unlink(missing_ok=True)
                    break
                except PermissionError:
                    time.sleep(0.1)

                    
if __name__ == '__main__':
    pytest.main([__file__, '-v'])