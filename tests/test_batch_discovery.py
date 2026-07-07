"""
Batch discovery processing tests (non-CAPTCHA).

Regression coverage for process_issue_from_batch: it must extract pages from a
real loc.gov issue-detail response via the processor. The live path called the
removed processor.process_page_from_issue and read the legacy
issue_details['pages'] key, so on loc.gov it silently discovered zero pages.
The existing CAPTCHA tests miss this because they mock process_page_from_issue
onto a bare Mock and feed the legacy 'pages' shape.
"""

import pytest
from unittest.mock import Mock
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))

from newsagger.batch_discovery import BatchDiscoveryProcessor
from newsagger.processor_new import LocGovResponseProcessor


class TestBatchDiscoveryProcessing:
    """process_issue_from_batch against a real processor and loc.gov shapes."""

    def _locgov_issue_details(self):
        """A loc.gov issue-detail response: pages live under resources[0]['files']."""
        return {
            'item': {
                'date_issued': '1906-04-18',
                'number_lccn': ['sn84038012'],
                'newspaper_title': 'The San Francisco Call',
                'number_edition': ['1'],
            },
            'resources': [{
                'files': [
                    [
                        {'mimetype': 'image/jp2', 'url': 'https://tile.loc.gov/sn84038012/1906-04-18/ed-1/seq-1.jp2'},
                        {'mimetype': 'application/pdf', 'url': 'https://tile.loc.gov/sn84038012/1906-04-18/ed-1/seq-1.pdf'},
                        {'mimetype': 'text/plain', 'fulltext_service': 'https://www.loc.gov/resource/sn84038012/1906-04-18/ed-1/?sp=1&fo=json'},
                    ],
                    [
                        {'mimetype': 'image/jp2', 'url': 'https://tile.loc.gov/sn84038012/1906-04-18/ed-1/seq-2.jp2'},
                        {'mimetype': 'application/pdf', 'url': 'https://tile.loc.gov/sn84038012/1906-04-18/ed-1/seq-2.pdf'},
                    ],
                ],
            }],
        }

    def test_process_issue_from_batch_extracts_pages_from_locgov_issue(self):
        """
        Regression: with a real LocGovResponseProcessor and a loc.gov issue
        shape, the two pages must be parsed and handed to storage.

        Fails against the pre-fix code, which reads issue_details['pages'] (empty
        on loc.gov) and calls the removed processor.process_page_from_issue, so
        batch_pages is empty and storage is never called -> silent (0, 0).
        """
        mock_api_client = Mock()
        mock_api_client.base_url = "https://www.loc.gov/"
        mock_api_client._make_request.return_value = self._locgov_issue_details()

        mock_storage = Mock()
        mock_storage.count_issue_pages.return_value = 0
        mock_storage.store_pages_and_enqueue.return_value = (2, 2)

        discovery = BatchDiscoveryProcessor(mock_api_client, LocGovResponseProcessor(), mock_storage)

        # loc.gov issue URL uses /resource/, not /lccn/, so the fast dup-check is skipped.
        issue_data = {'url': 'https://www.loc.gov/resource/sn84038012/1906-04-18/ed-1/'}
        discovered, enqueued = discovery.process_issue_from_batch(
            issue_data, session_name='s', batch_index=0, issue_idx=0, auto_enqueue=True
        )

        # The processor parsed both pages and passed them to storage.
        mock_storage.store_pages_and_enqueue.assert_called_once()
        stored_pages = mock_storage.store_pages_and_enqueue.call_args[0][0]
        assert len(stored_pages) == 2, "expected 2 PageInfo parsed from resources[0]['files']"
        assert discovered == 2