"""
Batch Discovery Module

Handles content discovery via digitization batches from the Library of Congress API.
This provides an alternative to search-based discovery that is less likely to trigger
CAPTCHA protection.
"""

import logging
import time
from typing import Dict, Tuple
from tqdm import tqdm

from .config import Config
from .rate_limited_client import CaptchaHandlingException, GlobalCaptchaManager
from .api_params import ChroniclingAmericaSearchParams

class BatchDiscoveryProcessor:
    """Handles batch-based content discovery operations."""
    
    def __init__(self, api_client, processor, storage, query_builder_class=None):
        """
        Args:
            ...
            query_builder_class: LegacyQueryBuilder or LocGovQueryBuilder
                (Config.query_builder_class). Required only when listing
                batches via get_all_batches(); other operations don't need it.
        """
        self.api_client = api_client
        self.processor = processor
        self.storage = storage
        self.query_builder_class = query_builder_class
        self.logger = logging.getLogger(__name__)

    def handle_captcha_during_batch_discovery(self, e: CaptchaHandlingException, 
                                             session_name: str, batch_index: int, 
                                             issue_idx: int, issue_url: str) -> bool:
        """
        Handle CAPTCHA exception during batch discovery.
        
        Args:
            e: The CaptchaHandlingException that was raised
            session_name: Name of the current batch discovery session
            batch_index: Index of the current batch being processed
            issue_idx: Index of the current issue being processed
            issue_url: URL of the issue that triggered CAPTCHA
            
        Returns:
            bool: True if processing should continue, False if it should stop
        """
        self.logger.warning(f"CAPTCHA detected while processing issue {issue_url}: {e}")
        
        if e.retry_strategy == 'global_cooling_off':
            # Global CAPTCHA protection is active - all discovery must stop
            captcha_status = e.suggested_params
            cooling_off_reason = captcha_status.get('reason', 'CAPTCHA protection active')
            
            self.logger.error(f"Global CAPTCHA protection triggered: {cooling_off_reason}")
            self.logger.error("Stopping batch discovery and waiting for cooling-off period to complete...")
            
            # Update session to mark where we stopped
            self.storage.update_batch_discovery_session(
                session_name=session_name,
                current_batch_index=batch_index,
                current_issue_index=issue_idx,
                status='captcha_blocked'
            )
            
            # Wait for the cooling-off period
            global_captcha = GlobalCaptchaManager()
            
            while True:
                can_proceed, reason = global_captcha.can_make_requests()
                if can_proceed:
                    self.logger.info("Cooling-off period completed, resuming batch discovery...")
                    break
                else:
                    self.logger.info(f"Waiting for cooling-off period: {reason}")
                    time.sleep(300)  # Check every 5 minutes
            
            # Mark session as active again and continue
            self.storage.update_batch_discovery_session(
                session_name=session_name,
                status='active'
            )
            
            return True  # Continue processing after cooling-off
        else:
            # Other CAPTCHA strategies - log and continue
            self.logger.warning(f"Non-global CAPTCHA strategy {e.retry_strategy}, continuing...")
            return True  # Continue processing
    
    def process_issue_from_batch(self, issue_data: Dict, session_name: str, 
                               batch_index: int, issue_idx: int, auto_enqueue: bool) -> Tuple[int, int]:
        """
        Process a single issue from a batch and extract its pages.
        
        Args:
            issue_data: Issue data containing URL and metadata
            session_name: Name of the current batch discovery session
            batch_index: Index of the current batch
            issue_idx: Index of this issue within the batch
            auto_enqueue: Whether to automatically enqueue discovered pages
            
        Returns:
            Tuple[int, int]: (pages_discovered, pages_enqueued)
        """
        issue_url = issue_data.get('url', '')
        if not issue_url:
            return 0, 0
        
        # Extract issue metadata from URL for fast duplicate check
        # URL format: .../lccn/sn12345678/1906-01-01/ed-1/ or .../lccn/sn12345678/1906-01-01/ed-1.json
        url_parts = issue_url.rstrip('/').replace('.json', '').split('/')
        if len(url_parts) >= 5 and 'lccn' in url_parts:
            try:
                lccn_idx = url_parts.index('lccn')
                if lccn_idx + 3 < len(url_parts):
                    lccn = url_parts[lccn_idx + 1]
                    date = url_parts[lccn_idx + 2]
                    edition_str = url_parts[lccn_idx + 3]
                    # Handle ed-1 or just 1 format
                    if edition_str.startswith('ed-'):
                        edition = int(edition_str.replace('ed-', ''))
                    else:
                        edition = 1
                    
                    # Fast check: do we already have pages for this issue?
                    existing_pages = self.storage.count_issue_pages(lccn, date, edition)
                    if existing_pages > 0:
                        self.logger.debug(f"Skipping issue {lccn}/{date}/ed-{edition} - already have {existing_pages} pages")
                        # Note: We still need to update issue index for proper resume functionality
                        # This tracks our position in the batch, not work done
                        self.storage.update_batch_discovery_session(
                            session_name=session_name,
                            current_issue_index=issue_idx
                        )
                        return 0, 0  # Skip without API call or delay
            except (ValueError, IndexError) as e:
                # If we can't parse the URL, just continue with normal processing
                self.logger.debug(f"Could not parse issue URL for fast skip: {issue_url} - {e}")
        
        try:
            # Convert issue URL to endpoint
            if issue_url.startswith(self.api_client.base_url):
                issue_endpoint = issue_url.replace(self.api_client.base_url, '')
            else:
                issue_endpoint = issue_url
            
            # Get issue details which contain the actual pages
            issue_details = self.api_client._make_request(issue_endpoint)

            # Collect pages for batch storage. parse_issue handles the per-API
            # structure (loc.gov resources[0]['files']; legacy issue['pages']) and
            # sequence numbering internally — no per-page API calls needed.
            batch_pages = self.processor.parse_issue(issue_details)
            
            # Store pages in database and enqueue atomically (critical for resume functionality)
            if batch_pages:
                if auto_enqueue:
                    # Store pages and enqueue in a single atomic operation
                    stored_count, enqueued_count = self.storage.store_pages_and_enqueue(
                        batch_pages, 
                        priority=2  # Medium priority for batch-discovered content
                    )
                    
                    # Update session progress
                    self.storage.update_batch_discovery_session(
                        session_name=session_name,
                        current_issue_index=issue_idx,
                        pages_discovered_delta=stored_count,
                        pages_enqueued_delta=enqueued_count
                    )
                    
                    return stored_count, enqueued_count
                else:
                    # Just store pages without enqueueing
                    stored_count = self.storage.store_pages(batch_pages)
                    
                    # Update session progress
                    self.storage.update_batch_discovery_session(
                        session_name=session_name,
                        current_issue_index=issue_idx,
                        pages_discovered_delta=stored_count
                    )
                    
                    return stored_count, 0
            else:
                return 0, 0
                
        except CaptchaHandlingException as e:
            # Handle CAPTCHA with dedicated method
            should_continue = self.handle_captcha_during_batch_discovery(
                e, session_name, batch_index, issue_idx, issue_url
            )
            if should_continue:
                # Retry this issue after CAPTCHA handling
                return self.process_issue_from_batch(issue_data, session_name, batch_index, issue_idx, auto_enqueue)
            else:
                return 0, 0
                
        except Exception as e:
            self.logger.error(f"Error processing issue {issue_url}: {e}")
            return 0, 0
    
    def discover_content_via_batches(self, max_batches: int = None, auto_enqueue: bool = False,
                                   batch_size: int = 100, rate_limit_delay: float = 3.0,
                                   progress_callback=None) -> Dict:
        """
        Discover content via digitization batches instead of search API.
        
        This method uses the batches.json endpoint which is designed for bulk access
        and should be much less likely to trigger CAPTCHA protection.
        
        Args:
            max_batches: Maximum number of batches to process (None for all)
            auto_enqueue: Whether to automatically add discovered pages to download queue
            batch_size: Pages to process per batch (not used for batches endpoint)
            rate_limit_delay: Delay between batch requests
            progress_callback: Optional callback for progress updates
        
        Returns:
            Dict with discovery statistics
        """
        self.logger.info("Starting batch-based content discovery...")
        
        discovered_pages = 0
        enqueued_pages = 0
        processed_batches = 0
        errors = 0
        
        try:
            # Get all batches first to know total count
            self.logger.info("Getting list of available batches...")
            if self.query_builder_class is None:
                raise ValueError(
                    "BatchDiscoveryProcessor.query_builder_class is required to list "
                    "batches — pass config.query_builder_class at construction."
                )
            builder = self.query_builder_class(ChroniclingAmericaSearchParams())
            all_batches = list(self.api_client.get_all_batches(builder))
            total_batches = len(all_batches)
            
            if max_batches:
                total_batches = min(total_batches, max_batches)
                all_batches = all_batches[:max_batches]
            
            self.logger.info(f"Will process {total_batches} of {len(all_batches)} available batches")
            
            # Check for existing session to resume from
            session_name = "batch_discovery_main"
            existing_session = self.storage.get_batch_discovery_session(session_name)
            
            start_batch_index = 0
            start_issue_index = 0
            
            if existing_session:
                start_batch_index = existing_session.get('current_batch_index', 0)
                start_issue_index = existing_session.get('current_issue_index', 0)
                discovered_pages = existing_session.get('total_pages_discovered', 0)
                enqueued_pages = existing_session.get('total_pages_enqueued', 0)
                
                self.logger.info(f"Resuming from batch {start_batch_index}, issue {start_issue_index}")
                self.logger.info(f"Previous progress: {discovered_pages} discovered, {enqueued_pages} enqueued")
            else:
                # Create new session
                self.storage.create_batch_discovery_session(
                    session_name=session_name,
                    total_batches=total_batches,
                    auto_enqueue=auto_enqueue
                )
                self.logger.info("Created new batch discovery session")
            
            # Create progress bar for batches (accounting for resume)
            batch_pbar = tqdm(
                total=total_batches,
                desc="Processing batches",
                unit="batch",
                position=0,
                initial=start_batch_index  # Start from resume position
            )
            
            for batch_index, batch in enumerate(all_batches):
                # Skip batches that were already processed
                if batch_index < start_batch_index:
                    continue
                    
                if max_batches and processed_batches >= max_batches:
                    break
                
                try:
                    batch_discovered, batch_enqueued = self._process_single_batch(
                        batch, batch_index, session_name, start_batch_index, start_issue_index,
                        auto_enqueue, batch_pbar
                    )
                    
                    discovered_pages += batch_discovered
                    enqueued_pages += batch_enqueued
                    processed_batches += 1
                    
                    # Update batch progress bar
                    batch_pbar.update(1)
                    batch_pbar.set_postfix({
                        'batch_pages': f'{batch_discovered:,}',
                        'total_found': f'{discovered_pages:,}',
                        'enqueued': f'{enqueued_pages:,}' if auto_enqueue else 'N/A'
                    })
                    
                    # Progress callback
                    if progress_callback:
                        progress_callback(processed_batches, discovered_pages, enqueued_pages)
                    
                    # Rate limiting delay
                    if rate_limit_delay and processed_batches > 0:
                        time.sleep(rate_limit_delay)
                        
                except Exception as e:
                    batch_name = batch.get('name', 'unknown')
                    self.logger.error(f"Error processing batch {batch_name}: {e}")
                    errors += 1
                    # Still update batch progress bar on error
                    batch_pbar.update(1)
                    continue
            
            # Close batch progress bar
            batch_pbar.close()
        
        except Exception as e:
            self.logger.error(f"Error in batch discovery: {e}")
            errors += 1
            # Close progress bars on major error
            try:
                batch_pbar.close()
            except:
                pass
        
        stats = {
            'processed_batches': processed_batches,
            'discovered_pages': discovered_pages,
            'enqueued_pages': enqueued_pages,
            'errors': errors,
            'method': 'batch_discovery'
        }
        
        # Complete the session
        try:
            self.storage.complete_batch_discovery_session(session_name)
        except:
            pass  # Don't fail on session cleanup
        
        self.logger.info(f"Batch discovery complete: {processed_batches} batches, "
                        f"{discovered_pages} pages discovered, {enqueued_pages} enqueued")
        
        return stats
    
    def _process_single_batch(self, batch: Dict, batch_index: int, session_name: str,
                            start_batch_index: int, start_issue_index: int, 
                            auto_enqueue: bool, batch_pbar) -> Tuple[int, int]:
        """
        Process a single batch and return discovered/enqueued counts.
        
        Returns:
            Tuple[int, int]: (batch_discovered, batch_enqueued)
        """
        # Extract batch information
        batch_name = batch.get('name', 'unknown')
        batch_url = batch.get('url', '')
        batch_page_count = batch.get('page_count', 0)
        
        # Update session with current batch
        self.storage.update_batch_discovery_session(
            session_name=session_name,
            current_batch_index=batch_index,
            current_batch_name=batch_name,
            current_issue_index=0,  # Reset issue index for new batch
            total_issues_in_batch=0  # Will be updated when we get batch details
        )
        
        # Update batch progress bar
        batch_pbar.set_description(f"Processing {batch_name[:20]}...")
        batch_pbar.set_postfix({
            'pages': f'{batch_page_count:,}',
            'found': '0'
        })
        
        batch_discovered = 0
        batch_enqueued = 0
        
        # Get detailed batch information to find pages
        if batch_url:
            # Convert batch URL to JSON format if needed
            if not batch_url.endswith('.json'):
                batch_url = batch_url.rstrip('/') + '.json'
            
            # Extract endpoint from full URL
            if batch_url.startswith(self.api_client.base_url):
                endpoint = batch_url.replace(self.api_client.base_url, '')
            else:
                endpoint = batch_url
            
            batch_details = self.api_client._make_request(endpoint)
            
            # Process issues from this batch (batches contain issues, not pages directly)
            batch_issues = batch_details.get('issues', [])
            
            # Update session with total issues in this batch
            self.storage.update_batch_discovery_session(
                session_name=session_name,
                total_issues_in_batch=len(batch_issues)
            )
            
            # Determine starting issue index for this batch
            current_issue_start = 0
            if batch_index == start_batch_index:
                # This is the batch we're resuming from
                current_issue_start = start_issue_index
            
            # Create progress bar for issues in this batch (accounting for resume)
            issue_pbar = tqdm(
                total=len(batch_issues),
                desc=f"Issues in {batch_name[:15]}",
                unit="issue",
                position=1,
                leave=False,
                initial=current_issue_start  # Start from resume position
            )
            
            # Track skipped issues for reporting
            issues_skipped = 0
            
            for issue_idx, issue_data in enumerate(batch_issues, 1):
                # Skip issues that were already processed (only in resume batch)
                if batch_index == start_batch_index and issue_idx <= start_issue_index:
                    continue
                
                # Process this issue using the extracted method
                issue_discovered, issue_enqueued = self.process_issue_from_batch(
                    issue_data, session_name, batch_index, issue_idx, auto_enqueue
                )
                
                # Track if this issue was skipped (no pages discovered means it was skipped)
                if issue_discovered == 0 and issue_enqueued == 0:
                    issues_skipped += 1
                
                # Update counters
                batch_discovered += issue_discovered
                batch_enqueued += issue_enqueued
                
                # Update issue progress bar with skip count
                issue_pbar.set_postfix({
                    'pages': issue_discovered,
                    'discovered': batch_discovered,
                    'skipped': issues_skipped
                })
                
                # Update issue progress bar
                issue_pbar.update(1)
            
            # Close issue progress bar
            issue_pbar.close()
            
            # Log summary if we skipped many issues
            if issues_skipped > 0:
                skip_percent = (issues_skipped / len(batch_issues)) * 100
                self.logger.info(f"Batch {batch_name}: Skipped {issues_skipped}/{len(batch_issues)} issues ({skip_percent:.1f}%) - already discovered")
        
        return batch_discovered, batch_enqueued