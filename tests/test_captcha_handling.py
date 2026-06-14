"""
Tests for CAPTCHA handling during batch discovery operations.
"""

import pytest
import io
import logging
import time
from pathlib import Path
from unittest.mock import Mock, patch

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))

from newsagger.batch_discovery import BatchDiscoveryProcessor
from newsagger.storage import NewsStorage
from newsagger.rate_limited_client import LocApiClient, CaptchaHandlingException, GlobalCaptchaManager
from newsagger.processor import NewsDataProcessor


class TestCaptchaHandling:
    """Test CAPTCHA handling in discovery operations."""

    def setup_method(self):
        """Set up test environment."""
        self.mock_api_client = Mock(spec=LocApiClient)
        self.mock_processor = Mock(spec=NewsDataProcessor)

        # Mock storage with all methods BatchDiscoveryProcessor calls
        self.storage = Mock(spec=NewsStorage)
        self.storage.get_batch_discovery_session = Mock(return_value=None)  # No existing session
        self.storage.create_batch_discovery_session = Mock()
        self.storage.update_batch_discovery_session = Mock()
        self.storage.complete_batch_discovery_session = Mock()
        self.storage.count_issue_pages = Mock(return_value=0)  # No pre-existing pages
        self.storage.store_pages = Mock(return_value=0)
        self.storage.store_pages_and_enqueue = Mock(return_value=(0, 0))

        # Target class under test
        self.batch_processor = BatchDiscoveryProcessor(
            self.mock_api_client,
            self.mock_processor,
            self.storage
        )

        # Reset global CAPTCHA state for clean tests
        GlobalCaptchaManager().reset_state()

    def teardown_method(self):
        GlobalCaptchaManager().reset_state()  # ensure clean state for subsequent tests
        import gc
        gc.collect()

    def _make_mock_batches_and_details(self):
        """Shared fixture data for batch/issue structure."""
        mock_batches = [
            {
                'name': 'test_batch_1',
                'url': 'https://chroniclingamerica.loc.gov/batches/test_batch_1/',
                'page_count': 100
            }
        ]
        mock_batch_details = {
            'issues': [
                {'url': 'https://chroniclingamerica.loc.gov/lccn/sn83045201/1925-02-20/ed-1.json'},
                {'url': 'https://chroniclingamerica.loc.gov/lccn/sn83045201/1925-02-27/ed-1.json'},
            ]
        }
        mock_issue_details = {
            'pages': [
                {'url': 'https://example.com/page1.json'},
                {'url': 'https://example.com/page2.json'},
            ]
        }
        return mock_batches, mock_batch_details, mock_issue_details

    def test_batch_discovery_handles_captcha_properly(self):
        """
        Test that batch discovery waits for cooling-off period when CAPTCHA is triggered,
        instead of just logging errors and continuing.
        """
        mock_batches, mock_batch_details, mock_issue_details = self._make_mock_batches_and_details()
        self.mock_api_client.get_all_batches.return_value = mock_batches

        captcha_exception = CaptchaHandlingException(
            "CAPTCHA detected - global cooling-off period required",
            retry_strategy="global_cooling_off",
            suggested_params={'reason': 'Global cooling-off active: 60.0 minutes remaining'}
        )

        def fake_make_request(endpoint):
            if "batches" in endpoint:
                return mock_batch_details
            if "sn83045201/1925-02-20" in endpoint:
                raise captcha_exception
            return mock_issue_details

        self.mock_api_client._make_request.side_effect = fake_make_request
        self.mock_processor.process_page_from_issue.return_value = Mock()

        with patch('newsagger.batch_discovery.GlobalCaptchaManager') as mock_gcm_class:
            mock_gcm = Mock()
            mock_gcm_class.return_value = mock_gcm
            mock_gcm.can_make_requests.side_effect = [
                (False, "Global cooling-off active: 60.0 minutes remaining"),
                (True, "Cooling-off period completed"),
            ]

            with patch('time.sleep'):
                result = self.batch_processor.discover_content_via_batches(
                    max_batches=1,
                    auto_enqueue=False
                )

            assert mock_gcm.can_make_requests.call_count >= 2
            assert result['method'] == 'batch_discovery'

    def test_batch_discovery_without_captcha_handling_fails(self):
        """
        Test that CAPTCHA errors are logged properly and handled (not silently swallowed).
        """
        mock_batches, mock_batch_details, _ = self._make_mock_batches_and_details()
        self.mock_api_client.get_all_batches.return_value = mock_batches

        captcha_exception = CaptchaHandlingException(
            "CAPTCHA detected - global cooling-off period required",
            retry_strategy="global_cooling_off",
            suggested_params={'reason': 'Global cooling-off active: 60.0 minutes remaining'}
        )

        self.mock_api_client._make_request.side_effect = [
            mock_batch_details,
            captcha_exception,
            captcha_exception,
        ]
        self.mock_processor.process_page_from_issue.return_value = Mock()

        # Capture WARNING+ log output from batch_discovery logger
        log_capture = io.StringIO()
        handler = logging.StreamHandler(log_capture)
        handler.setLevel(logging.WARNING)
        logger = logging.getLogger('newsagger.batch_discovery')
        logger.addHandler(handler)
        logger.setLevel(logging.WARNING)

        try:
            with patch('newsagger.batch_discovery.GlobalCaptchaManager') as mock_gcm_class:
                mock_gcm = Mock()
                mock_gcm_class.return_value = mock_gcm
                mock_gcm.can_make_requests.side_effect = [
                    (False, "Global cooling-off active: 60.0 minutes remaining"),
                    (True, "Cooling-off period completed"),
                ]

                with patch('time.sleep'):
                    result = self.batch_processor.discover_content_via_batches(
                        max_batches=1,
                        auto_enqueue=False
                    )

                assert mock_gcm.can_make_requests.called, \
                    "GlobalCaptchaManager.can_make_requests should have been called"
        finally:
            logger.removeHandler(handler)

        log_contents = log_capture.getvalue()

        assert 'CAPTCHA detected while processing issue' in log_contents, (
            f"Expected CAPTCHA warning in logs. Got: {log_contents}"
        )
        assert 'Global CAPTCHA protection triggered' in log_contents, (
            f"Expected global CAPTCHA log. Got: {log_contents}"
        )
        assert result['discovered_pages'] == 0
        assert result['processed_batches'] == 0

    # --- These tests don't touch BatchDiscoveryProcessor, so they're unchanged ---

    def test_captcha_cooling_off_simulation(self):
        """Test the cooling-off period simulation logic."""
        global_captcha = GlobalCaptchaManager()

        can_proceed, reason = global_captcha.can_make_requests()
        assert can_proceed is True

        global_captcha.record_captcha("test_endpoint")

        can_proceed, reason = global_captcha.can_make_requests()
        assert can_proceed is False
        assert "Global cooling-off active" in reason

        global_captcha.reset_state()

        can_proceed, reason = global_captcha.can_make_requests()
        assert can_proceed is True

    def test_captcha_exception_properties(self):
        """Test that CaptchaHandlingException has the right properties."""
        exception = CaptchaHandlingException(
            "Test CAPTCHA message",
            retry_strategy="global_cooling_off",
            suggested_params={'reason': 'Test reason'}
        )

        assert str(exception) == "Test CAPTCHA message"
        assert exception.retry_strategy == "global_cooling_off"
        assert exception.suggested_params == {'reason': 'Test reason'}