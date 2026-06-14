"""
Test continuous download processing functionality.
"""

import pytest
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime
import tempfile
from pathlib import Path

from newsagger.downloader import DownloadProcessor
from newsagger.storage import NewsStorage
from newsagger.rate_limited_client import LocApiClient


class TestContinuousDownloads:
    """Test continuous download processing."""
    
    def test_continuous_processing_calls_correct_storage_methods(self):
        """Test that continuous processing uses correct storage method names."""
        # Setup mocks
        mock_storage = Mock(spec=NewsStorage)
        mock_client = Mock(spec=LocApiClient)
        
        # Mock queue items
        mock_queue_items = [
            {
                'id': 1,
                'queue_type': 'page',
                'reference_id': 'test_page_1',
                'estimated_size_mb': 1.0,
                'priority': 1,
                'status': 'queued'
            },
            {
                'id': 2,
                'queue_type': 'page', 
                'reference_id': 'test_page_2',
                'estimated_size_mb': 1.0,
                'priority': 1,
                'status': 'queued'
            }
        ]
        
        # Setup storage mock responses
        def queue_side_effect(*args, **kwargs):
            if not hasattr(queue_side_effect, "called"):
                queue_side_effect.called = True
                return mock_queue_items
            return []

        mock_storage.get_download_queue.side_effect = queue_side_effect
        
        # Mock the queue status update method (correct name is update_queue_item)
        mock_storage.update_queue_item = Mock()
        
        with tempfile.TemporaryDirectory() as temp_dir:
            downloader = DownloadProcessor(mock_storage, mock_client, temp_dir, ['pdf'])
            
            # Mock the _process_queue_item method to avoid actual downloads
            with patch.object(downloader, '_process_queue_item') as mock_process:
                mock_process.return_value = {
                    'success': True,
                    'file_path': '/fake/path.pdf',
                    'size_mb': 1.0
                }
                
                # Mock _process_batch_updates to avoid database operations
                with patch.object(downloader, '_process_batch_updates'):
                    # This should fail with the attribute error if method name is wrong
                    result = downloader.process_queue(
                        max_items=10,
                        continuous=True,
                        max_idle_minutes=0.01  # Very short timeout for test - now even tigher for Windows
                    )
        
        # Verify the method was called (this will fail if method name is wrong)
        assert mock_storage.update_queue_item.called
        assert result['batches_processed'] >= 1
    
    def test_continuous_processing_handles_missing_method_gracefully(self):
        """Test that we get a clear error when storage method is missing."""

        mock_storage = Mock(spec=NewsStorage)
        mock_client = Mock(spec=LocApiClient)

        mock_storage.get_download_queue.return_value = [
            {
                'id': 1,
                'queue_type': 'page',
                'reference_id': 'test_page_1',
                'estimated_size_mb': 1.0,
                'priority': 1,
                'status': 'queued'
            }
        ]
        del mock_storage.update_queue_item  # Force AttributeError when called

        with tempfile.TemporaryDirectory() as temp_dir:
            downloader = DownloadProcessor(mock_storage, mock_client, temp_dir, ['pdf'])

            # Should not raise - AttributeError should be caught and logged
            try:
                result = downloader.process_queue(max_items=1)
            except AttributeError:
                pytest.fail("AttributeError from missing update_queue_item was not handled gracefully")
