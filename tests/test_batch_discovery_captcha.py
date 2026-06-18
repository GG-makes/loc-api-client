"""
Test for batch discovery CAPTCHA handling.

This test verifies that when CAPTCHA is detected during batch discovery,
the system properly pauses, waits for the cooling-off period, and then
resumes processing the current batch without skipping any items.
"""

import pytest
import time
from unittest.mock import Mock, patch, MagicMock
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))

from newsagger.batch_discovery import BatchDiscoveryProcessor
from newsagger.rate_limited_client import CaptchaHandlingException


class TestBatchDiscoveryCaptchaHandling:
    """Test CAPTCHA handling during batch discovery."""
    
    def test_batch_discovery_captcha_resume(self):
        """Test that batch discovery resumes and completes the current batch after CAPTCHA."""
        # Setup mocks
        mock_api_client = Mock()
        mock_api_client.base_url = "https://example.com"
        mock_processor = Mock()
        mock_storage = Mock()
        
        # Create discovery processor
        discovery = BatchDiscoveryProcessor(mock_api_client, mock_processor, mock_storage)
        
        # Mock batch data
        batch_data = {
            'name': 'test_batch_001',
            'url': 'https://example.com/batch/test_batch_001'
        }
        
        # Create 5 mock issues for the batch
        mock_issues = []
        for i in range(1, 6):
            mock_issues.append({
                'url': f'https://example.com/lccn/sn12345/1900-01-{i:02d}/ed-1.json',
                'date': f'1900-01-{i:02d}'
            })
        
        # Mock API to return batch issues
        mock_api_client.get_batch_issues.return_value = mock_issues
        
        # Mock the batch details request
        mock_api_client._make_request.return_value = {
            'issues': mock_issues,
            'name': 'test_batch_001',
            'page_count': 10
        }
        
        # Track which pages were discovered
        discovered_pages = []
        captcha_triggered = False
        
        def mock_make_request(endpoint):
            """Mock _make_request for issue details with CAPTCHA on second issue."""
            nonlocal captcha_triggered
            
            # Handle batch details request
            if 'batch' in endpoint:
                return {
                    'issues': mock_issues,
                    'name': 'test_batch_001',
                    'page_count': 10
                }
            
            # Extract issue number from endpoint
            if '1900-01-' in endpoint:
                # Extract day number from date pattern 1900-01-DD
                date_match = endpoint.split('1900-01-')[1]
                issue_num = int(date_match[:2])
                
                # Trigger CAPTCHA on the second issue, but only once
                if issue_num == 2 and not captcha_triggered:
                    captcha_triggered = True
                    raise CaptchaHandlingException(
                        "CAPTCHA detected - global cooling-off period required",
                        retry_strategy='global_cooling_off',
                        suggested_params={
                            'reason': 'Global cooling-off active: 60.0 minutes remaining'
                        }
                    )
                
                # Return mock pages for the issue
                return {
                    'pages': [
                        {'url': f'{endpoint}/seq-1', 'sequence': 1},
                        {'url': f'{endpoint}/seq-2', 'sequence': 2}
                    ]
                }
            
            return {}
        
        # Override the existing mock
        mock_api_client._make_request.side_effect = mock_make_request
        mock_api_client.base_url = "example.com"
        
        # Mock processor to track pages
        def mock_process_page(page_data, issue_details):
            page_id = page_data['url'].split('/')[-1]
            discovered_pages.append(page_id)
            return {
                'item_id': page_id,
                'lccn': 'sn12345',
                'title': 'Test Paper',
                'date': '1900-01-01',
                'edition': 1,
                'sequence': page_data.get('sequence', 1),
                'page_url': page_data['url']
            }
        
        mock_processor.process_page_from_issue.side_effect = mock_process_page
        
        # Mock storage methods
        mock_storage.store_pages.side_effect = lambda pages: len(pages)
        mock_storage.get_batch_discovery_session.return_value = None
        mock_storage.create_batch_discovery_session.return_value = None
        mock_storage.update_batch_discovery_session.return_value = None
        mock_storage.count_issue_pages.return_value = 0  # No existing pages
        
        # Mock api client
        mock_api_client = Mock()
        mock_api_client.base_url = 'example.com'

        # Override the cooling-off wait time to 1 second
        with patch('newsagger.batch_discovery.time.sleep') as mock_sleep, \
             patch('newsagger.batch_discovery.GlobalCaptchaManager') as mock_captcha_manager:
            
            # Setup CAPTCHA manager mock
            captcha_instance = Mock()
            mock_captcha_manager.return_value = captcha_instance
            
            # First call returns False (still cooling off), second returns True
            captcha_instance.can_make_requests.side_effect = [
                (False, "Cooling off for 1 minute"),
                (True, "Can proceed")
            ]
            
            # Process the batch
            batch_discovered, batch_enqueued = discovery._process_single_batch(
                batch_data, 0, "test_session", 0, 0, False, mock_api_client
            )
            
            # Verify CAPTCHA handling
            assert mock_sleep.call_count >= 1  # Should have slept during cooling-off
            assert mock_sleep.call_args[0][0] == 300  # 5 minute check interval
            
            # Verify all 5 issues were processed (10 pages total)
            assert batch_discovered == 10  # 5 issues * 2 pages each
            assert len(discovered_pages) == 10
            
            # Verify the second issue was retried after CAPTCHA
            # We should see pages from all 5 issues
            expected_pages = []
            for i in range(1, 6):
                expected_pages.extend([f'seq-1', f'seq-2'])
            
            # All pages should be discovered
            assert len(discovered_pages) == len(expected_pages)
            
            # Verify storage was updated during CAPTCHA handling
            update_calls = mock_storage.update_batch_discovery_session.call_args_list
            captcha_blocked_call = None
            active_call = None
            
            for call in update_calls:
                if call[1].get('status') == 'captcha_blocked':
                    captcha_blocked_call = call
                elif call[1].get('status') == 'active':
                    active_call = call
            
            # Should have marked session as captcha_blocked
            assert captcha_blocked_call is not None
            assert captcha_blocked_call[1]['current_batch_index'] == 0
            assert captcha_blocked_call[1]['current_issue_index'] == 2  # Second issue
            
            # Should have marked session as active again after cooling off
            assert active_call is not None
    
    def test_batch_discovery_tracks_progress_correctly(self):
        """Test that batch discovery correctly tracks which pages were discovered."""
        # Setup mocks
        mock_api_client = Mock()
        mock_api_client.base_url = "https://example.com"
        mock_processor = Mock()
        mock_storage = Mock()
        
        discovery = BatchDiscoveryProcessor(mock_api_client, mock_processor, mock_storage)
        
        # Create a batch with 3 issues
        batch_data = {'name': 'test_batch', 'url': 'https://example.com/batch'}
        mock_issues = [
            {'url': f'https://example.com/issue{i}', 'date': f'1900-01-{i:02d}'}
            for i in range(1, 4)
        ]
        
        mock_api_client.get_batch_issues.return_value = mock_issues
        
        # Track discovery order
        discovery_order = []
        
        def mock_make_request(endpoint):
            # Handle batch details request
            if 'batch' in endpoint:
                return {
                    'issues': mock_issues,
                    'name': 'test_batch',
                    'page_count': 3
                }
            
            # Handle issue requests
            if 'issue' in endpoint:
                issue_num = int(endpoint[-1])
                discovery_order.append(f'issue{issue_num}')
                return {
                    'pages': [{'url': f'{endpoint}/page1', 'sequence': 1}]
                }
            
            return {}
        
        mock_api_client._make_request.side_effect = mock_make_request
        
        def mock_process_page(page_data, issue_details):
            return {'item_id': page_data['url'].split('/')[-1]}
        
        mock_processor.process_page_from_issue.side_effect = mock_process_page
        mock_storage.store_pages.side_effect = lambda pages: len(pages)
        mock_storage.get_batch_discovery_session.return_value = None
        mock_storage.count_issue_pages.return_value = 0  # No existing pages
        mock_api_client = Mock()
        mock_api_client.base_url = 'example.com'
        # Process batch
        discovery._process_single_batch(batch_data, 0, "test", 0, 0, False, mock_api_client)
        
        # Verify all issues were discovered in order
        assert discovery_order == ['issue1', 'issue2', 'issue3']