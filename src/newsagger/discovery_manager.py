"""
Discovery Management Module

Coordinates the discovery and tracking of available periodicals and facets
for systematic downloading from the Library of Congress API.
"""

import logging
from typing import Dict, List, Optional, Tuple, Generator
from datetime import datetime
from tqdm import tqdm
from .rate_limited_client import LocApiClient, CaptchaHandlingException, GlobalCaptchaManager
from .batch_discovery import BatchDiscoveryProcessor
from .processor import NewsDataProcessor
from .storage import NewsStorage

from .discovery.facet_processor import (
    FacetStatusValidator,
    FacetSearchParamsBuilder,
    FacetDiscoveryContext
)


class DiscoveryManager:
    """Manages discovery of available content and tracks download progress."""
    
    def __init__(self, api_client: LocApiClient, processor: NewsDataProcessor, 
                 storage: NewsStorage, query_builder_class):
        self.api_client = api_client
        self.processor = processor
        self.storage = storage
        self.query_builder_class = query_builder_class
        self.logger = logging.getLogger(__name__)
        
        # Initialize batch discovery processor
        self.batch_processor = BatchDiscoveryProcessor(api_client, processor, storage, query_builder_class)

    def discover_all_periodicals(self, max_newspapers: int = None) -> int:
        """
        Discover all available periodicals and store them for tracking.
        Returns the number of new periodicals discovered.
        """
        self.logger.info("Starting periodical discovery...")
        
        discovered_count = 0
        
        if max_newspapers:
            # Limited discovery
            for newspaper in self.api_client.get_newspapers_with_details(max_newspapers):
                periodical_data = self._convert_newspaper_to_periodical(newspaper)
                stored = self.storage.store_periodicals([periodical_data])
                discovered_count += stored
        else:
            # Full discovery - get all newspapers
            batch_size = 100
            for newspapers_batch in self._get_newspapers_in_batches(batch_size):
                periodicals = [self._convert_newspaper_to_periodical(n) for n in newspapers_batch]
                stored = self.storage.store_periodicals(periodicals)
                discovered_count += stored
                
                if discovered_count % 500 == 0:
                    self.logger.info(f"Discovered {discovered_count} periodicals so far...")
        
        self.logger.info(f"Periodical discovery completed. Found {discovered_count} periodicals.")
        return discovered_count
    
    def _convert_newspaper_to_periodical(self, newspaper: Dict) -> Dict:
        """Convert API newspaper response to periodical tracking format."""
        return {
            'lccn': newspaper.get('lccn'),
            'title': newspaper.get('title'),
            'state': newspaper.get('state'),
            'city': self._extract_city(newspaper.get('place_of_publication', [])),
            'start_year': self._parse_year(newspaper.get('start_year')),
            'end_year': self._parse_year(newspaper.get('end_year')),
            'frequency': newspaper.get('frequency'),
            'language': self._extract_primary_language(newspaper.get('language', [])),
            'subject': self._extract_primary_subject(newspaper.get('subject', [])),
            'url': newspaper.get('url')
        }
    
    def _get_newspapers_in_batches(self, batch_size: int) -> Generator[List[Dict], None, None]:
        """Get all newspapers in batches to avoid memory issues."""
        batch = []
        for newspaper in self.api_client.get_all_newspapers():
            batch.append(newspaper)
            if len(batch) >= batch_size:
                yield batch
                batch = []
        
        if batch:  # Yield final batch
            yield batch
    
    def discover_periodical_issues(self, lccn: str) -> int:
        """
        Discover all issues for a specific periodical.
        Returns the number of issues discovered.
        """
        self.logger.info(f"Discovering issues for periodical {lccn}...")
        
        try:
            # Get detailed newspaper info including issues
            newspaper_detail = self.api_client.get_newspaper_issues(lccn)
            issues = newspaper_detail.get('issues', [])
            
            issues_count = 0
            for issue in issues:
                issue_date = self._parse_issue_date(issue.get('date_issued'))
                if issue_date:
                    issue_id = self.storage.store_periodical_issue(
                        lccn=lccn,
                        issue_date=issue_date,
                        edition_count=1,  # Will be updated during page discovery
                        pages_count=0,    # Will be updated during page discovery
                        issue_url=issue.get('url')
                    )
                    issues_count += 1
            
            # Update periodical discovery progress
            self.storage.update_periodical_discovery(
                lccn=lccn,
                total_issues=issues_count,
                issues_discovered=issues_count,
                complete=True
            )
            
            self.logger.info(f"Discovered {issues_count} issues for {lccn}")
            return issues_count
            
        except Exception as e:
            self.logger.error(f"Failed to discover issues for {lccn}: {e}")
            return 0
    
    def create_date_range_facets(self, start_year: int, end_year: int, 
                               facet_size_years: int = 1, 
                               estimate_items: bool = False,
                               rate_limit_delay: float = None) -> List[int]:
        """
        Create date range facets for systematic downloading with resumption support.
        
        Args:
            start_year: Starting year for facets
            end_year: Ending year for facets  
            facet_size_years: Years per facet
            estimate_items: Whether to estimate items per facet (makes API calls)
            rate_limit_delay: Extra delay between estimations to avoid rate limiting
            
        Returns list of facet IDs created (only new ones, not existing).
        """
        # Check for existing facets to enable resumption
        existing_facets = self.storage.get_search_facets(facet_type='date_range')
        existing_ranges = {f['facet_value'] for f in existing_facets}
        
        facet_ids = []
        all_years = list(range(start_year, end_year + 1, facet_size_years))
        total_facets = len(all_years)
        created_count = 0
        skipped_count = 0
        
        if estimate_items:
            # Count how many new facets would need estimation
            new_facets_count = 0
            for year in all_years:
                facet_end_year = min(year + facet_size_years - 1, end_year)
                facet_value = f"{year}/{facet_end_year}" if year != facet_end_year else f"{year}/{year}"
                if facet_value not in existing_ranges:
                    new_facets_count += 1
            
            if new_facets_count > 0:
                self.logger.warning(f"Will make {new_facets_count} API calls for estimation - this may trigger rate limiting!")
                if rate_limit_delay is None:
                    rate_limit_delay = 5.0  # Extra 5 seconds between calls
        
        # Use tqdm for progress tracking
        from tqdm import tqdm
        
        self.logger.info(f"Creating date facets from {start_year} to {end_year} (resuming if interrupted)")
        
        with tqdm(total=total_facets, desc="Creating facets") as pbar:
            for i, year in enumerate(all_years):
                facet_end_year = min(year + facet_size_years - 1, end_year)
                facet_value = f"{year}/{facet_end_year}" if year != facet_end_year else f"{year}/{year}"
                
                # Skip if facet already exists (resumption support)
                if facet_value in existing_ranges:
                    skipped_count += 1
                    pbar.set_description(f"Skipping {facet_value} (exists)")
                    pbar.update(1)
                    continue
                
                pbar.set_description(f"Creating {facet_value}")
                
                # Estimate items for this date range (optional)
                estimated_items = 0
                if estimate_items:
                    try:
                        estimate = self.api_client.estimate_download_size((str(year), str(facet_end_year)))
                        estimated_items = estimate.get('total_pages', 0)
                        
                        # Add extra delay to avoid rate limiting
                        if rate_limit_delay and created_count < (total_facets - skipped_count - 1):
                            import time
                            time.sleep(rate_limit_delay)
                            
                    except Exception as e:
                        self.logger.warning(f"Failed to estimate items for {facet_value}: {e}")
                        estimated_items = 0
                
                # Create and immediately save the facet
                try:
                    facet_id = self.storage.create_search_facet(
                        facet_type='date_range',
                        facet_value=facet_value,
                        facet_query='',  # Empty query for broad date search
                        estimated_items=estimated_items
                    )
                    facet_ids.append(facet_id)
                    created_count += 1
                    
                    if estimate_items:
                        pbar.set_postfix(items=f"{estimated_items:,}", created=created_count)
                    else:
                        pbar.set_postfix(created=created_count, skipped=skipped_count)
                        
                except Exception as e:
                    self.logger.error(f"Failed to create facet {facet_value}: {e}")
                    # Continue with next facet rather than failing entirely
                
                pbar.update(1)
        
        # Summary logging
        if created_count > 0:
            if estimate_items:
                self.logger.info(f"Created {created_count} new date facets with estimation")
            else:
                self.logger.info(f"Created {created_count} new date facets (estimation skipped to avoid rate limiting)")
                self.logger.info("Use 'estimate-facets' or 'auto-discover-facets' later to get item counts")
        
        if skipped_count > 0:
            self.logger.info(f"Skipped {skipped_count} existing facets (resumption)")
        
        return facet_ids
    
    def create_state_facets(self, states: List[str] = None) -> List[int]:
        """
        Create state-based facets for systematic downloading.
        Returns list of facet IDs created.
        """
        if states is None:
            # Get all states from discovered periodicals
            periodicals = self.storage.get_periodicals()
            states = list(set(p['state'] for p in periodicals if p['state']))
        
        facet_ids = []
        for state in states:
            # Get periodicals count for estimation
            state_periodicals = self.storage.get_periodicals(state=state)
            estimated_items = len(state_periodicals) * 1000  # Rough estimate
            
            facet_id = self.storage.create_search_facet(
                facet_type='state',
                facet_value=state,
                facet_query='',
                estimated_items=estimated_items
            )
            facet_ids.append(facet_id)
            
            self.logger.info(f"Created state facet {state} with ~{estimated_items} estimated items")
        
        return facet_ids
    
    def populate_download_queue(self, priority_states: List[str] = None, 
                              priority_date_ranges: List[str] = None) -> int:
        """
        Populate the download queue with discovered facets and periodicals.
        Returns the number of items added to the queue.
        """
        queue_items_added = 0
        
        # Add high-priority date ranges (e.g., significant historical events)
        if priority_date_ranges:
            for date_range in priority_date_ranges:
                facets = self.storage.get_search_facets(facet_type='date_range')
                for facet in facets:
                    if facet['facet_value'] in priority_date_ranges:
                        queue_id = self.storage.add_to_download_queue(
                            queue_type='facet',
                            reference_id=str(facet['id']),
                            priority=1,  # Highest priority
                            estimated_size_mb=facet['estimated_items'] * 2,  # 2MB per item estimate
                            estimated_time_hours=facet['estimated_items'] * 3 / 3600  # 3 seconds per item
                        )
                        queue_items_added += 1
        
        # Add high-priority states
        if priority_states:
            for state in priority_states:
                facets = self.storage.get_search_facets(facet_type='state')
                for facet in facets:
                    if facet['facet_value'] in priority_states:
                        queue_id = self.storage.add_to_download_queue(
                            queue_type='facet',
                            reference_id=str(facet['id']),
                            priority=2,  # High priority
                            estimated_size_mb=facet['estimated_items'] * 2,
                            estimated_time_hours=facet['estimated_items'] * 3 / 3600
                        )
                        queue_items_added += 1
        
        # Add remaining completed facets with normal priority
        completed_facets = self.storage.get_search_facets(status='completed')
        for facet in completed_facets:
            # Check if not already in queue
            existing_queue = self.storage.get_download_queue()
            facet_ref = str(facet['id'])
            if not any(q['reference_id'] == facet_ref and q['queue_type'] == 'facet' 
                      for q in existing_queue):
                queue_id = self.storage.add_to_download_queue(
                    queue_type='facet',
                    reference_id=facet_ref,
                    priority=5,  # Normal priority
                    estimated_size_mb=facet['actual_items'] * 2,
                    estimated_time_hours=facet['actual_items'] * 3 / 3600
                )
                queue_items_added += 1
        
        # Add high-value periodicals
        discovered_periodicals = self.storage.get_periodicals(discovery_complete=True)
        for periodical in discovered_periodicals:
            # Prioritize based on historical significance, size, etc.
            priority = self._calculate_periodical_priority(periodical)
            
            queue_id = self.storage.add_to_download_queue(
                queue_type='periodical',
                reference_id=periodical['lccn'],
                priority=priority,
                estimated_size_mb=periodical['total_issues'] * 50,  # 50MB per issue estimate
                estimated_time_hours=periodical['total_issues'] * 30 / 3600  # 30 seconds per issue
            )
            queue_items_added += 1
        
        self.logger.info(f"Added {queue_items_added} items to download queue")
        return queue_items_added
    
    def get_discovery_summary(self) -> Dict:
        """Get a comprehensive summary of discovery progress."""
        stats = self.storage.get_discovery_stats()
        
        # Add additional context
        undiscovered_periodicals = self.storage.get_periodicals(discovery_complete=False)
        ready_facets = self.storage.get_search_facets(status='completed')
        next_queue_items = self.storage.get_download_queue(status='queued', limit=5)
        
        summary = {
            'discovery_stats': stats,
            'undiscovered_periodicals_count': len(undiscovered_periodicals),
            'ready_facets_count': len(ready_facets),
            'next_downloads': [
                {
                    'type': item['queue_type'],
                    'reference': item['reference_id'],
                    'priority': item['priority'],
                    'estimated_size_mb': item['estimated_size_mb'],
                    'estimated_time_hours': item['estimated_time_hours']
                }
                for item in next_queue_items
            ]
        }
        
        return summary
    
    def discover_facet_content(self, facet_id: int, batch_size: int = 100, max_items: int = None, 
                              progress_callback=None) -> int:
        """
        Systematically discover all content for a specific facet.
        Returns the number of items discovered.
        
        Args:
            facet_id: ID of the facet to discover
            batch_size: Number of items per API call
            max_items: Maximum items to discover (None for unlimited)
            progress_callback: Optional callback function for progress updates
        """
        facet = self.storage.get_search_facet(facet_id)
        if not facet:
            raise ValueError(f"Facet {facet_id} not found")
        
        self.logger.info(f"Discovering content for facet {facet_id}: {facet['facet_type']} = {facet['facet_value']}")
        
        # Validate and fix any CAPTCHA interruption issues
        status_validator = FacetStatusValidator(self.storage, self.logger)
        facet = status_validator.validate_and_fix_facet_status(facet)
        
        # Initialize discovery context for resume and progress management
        discovery_context = FacetDiscoveryContext(facet, batch_size, max_items)
        
        # Resume capability logging
        if discovery_context.resume_from_page > 1:
            self.logger.info(f"Resuming facet {facet_id} discovery from page {discovery_context.resume_from_page}")
        else:
            # Update facet status to discovering
            self.logger.debug(f"Setting facet {facet_id} status to discovering")
            self.storage.update_facet_discovery(facet_id, status='discovering')
            self.logger.debug(f"Facet {facet_id} status updated")
        
        # Initialize search parameter builder with batch size adjustment
        params_builder = FacetSearchParamsBuilder(self.logger)
        adjusted_batch_size = params_builder.adjust_batch_size_for_facet(facet, batch_size)
        discovery_context.batch_size = adjusted_batch_size
        
        try:
            while discovery_context.should_continue_discovery():
                # Build search parameters using the extracted builder
                search_params = params_builder.build_search_params(
                    facet, discovery_context.current_page, discovery_context.batch_size
                )
                
                # Handle special case for state facets with no periodicals
                if facet['facet_type'] == 'state':
                    state_periodicals = self.storage.get_periodicals(state=facet['facet_value'])
                    if not state_periodicals:
                        self.logger.warning(f"No periodicals found for state {facet['facet_value']}")
                        # Mark this facet as completed with 0 items and continue to next facet
                        self.storage.update_facet_discovery(
                            facet_id, 
                            actual_items=0,
                            items_discovered=0,
                            status='completed'
                        )
                        self.logger.info(f"Completed discovery for facet {facet_id}: 0 items (no periodicals for state)")
                        return 0
                    
                    # Use the first few LCCNs from the state for more focused search
                    sample_lccns = [p['lccn'] for p in state_periodicals[:5]]  # Limit to 5 newspapers
                    if sample_lccns:
                        search_params['andtext'] = f"lccn:({' OR '.join(sample_lccns)})"
                    else:
                        search_params['andtext'] = facet['facet_value']
                elif facet.get('query'):
                    search_params['andtext'] = facet['query']
                
                # Perform search with timeout handling
                try:
                    self.logger.debug(f"Searching facet {facet_id} page {discovery_context.current_page} with params: {search_params}")
                    self.logger.debug(f"About to call search_pages for facet {facet_id}")
                    #TODO: Needs replacement
                    response = self.api_client.search_pages(**search_params)
                    self.logger.debug(f"Got API response for facet {facet_id}, processing...")
                    pages = self.processor.process_search_response(response, deduplicate=True)
                    self.logger.debug(f"Processed response for facet {facet_id}, got {len(pages)} pages")
                    
                    if not pages:
                        self.logger.debug(f"No results on page {discovery_context.current_page} for facet {facet_id}, ending discovery")
                        break
                        
                    self.logger.debug(f"Found {len(pages)} pages on page {discovery_context.current_page} for facet {facet_id}")
                    
                except CaptchaHandlingException:
                    # Let CAPTCHA exceptions propagate to the global handler
                    # Don't catch these - they need to bubble up to stop ALL discovery
                    raise
                    
                except Exception as e:
                    self.logger.warning(f"Search failed for facet {facet_id} page {discovery_context.current_page}: {e}")
                    
                    # Check if this is a legacy CAPTCHA-related error
                    if 'captcha' in str(e).lower():
                        discovery_context.discovery_interrupted = True
                        discovery_context.interruption_reason = 'captcha'
                        self.logger.warning(f"Legacy CAPTCHA interruption on facet {facet_id} page {discovery_context.current_page} - marking for recovery")
                        break
                    # For certain errors, we should stop rather than continue
                    elif 'timeout' in str(e).lower() or 'connection' in str(e).lower():
                        discovery_context.discovery_interrupted = True
                        discovery_context.interruption_reason = 'timeout'
                        self.logger.warning(f"Network issue on facet {facet_id}, stopping discovery")
                        break
                    elif discovery_context.current_page == 1:
                        # If first page fails, this facet might be problematic
                        self.logger.error(f"First page failed for facet {facet_id}, marking as error")
                        raise
                    else:
                        # If later pages fail, we can still save what we found
                        discovery_context.discovery_interrupted = True
                        discovery_context.interruption_reason = 'other_error'
                        self.logger.warning(f"Page {discovery_context.current_page} failed for facet {facet_id}, stopping at page {discovery_context.current_page-1}")
                        break
                
                # Apply max_items limit before storing
                remaining_items = discovery_context.get_remaining_items()
                if remaining_items is not None and len(pages) > remaining_items:
                    pages = pages[:remaining_items]
                    self.logger.info(f"Limiting to {remaining_items} items to stay under max_items ({max_items}) for facet {facet_id}")
                
                # Store discovered pages
                self.logger.debug(f"About to store {len(pages)} pages for facet {facet_id}")
                stored_count = self.storage.store_pages(pages)
                self.logger.debug(f"Stored {stored_count} pages for facet {facet_id}")
                discovery_context.update_progress(stored_count)
                
                # Update facet progress with batch-level tracking
                self.storage.update_facet_discovery(
                    facet_id, 
                    items_discovered=discovery_context.total_discovered,
                    current_page=discovery_context.current_page,
                    batch_size=discovery_context.batch_size
                )
                
                # Call progress callback if provided
                if progress_callback:
                    progress_callback({
                        'facet_id': facet_id,
                        'page': discovery_context.current_page,
                        'batch_items': stored_count,
                        'total_discovered': discovery_context.total_discovered,
                        'facet_value': facet['facet_value']
                    })
                
                # Provide periodic status updates for long-running facets
                if discovery_context.current_page % 10 == 0 and discovery_context.current_page > 0:  # Every 10 pages
                    self.logger.info(f"Facet {facet_id} ({facet['facet_value']}): page {discovery_context.current_page}, {discovery_context.total_discovered:,} items discovered so far")
                
                self.logger.debug(f"Discovered {stored_count} items on page {discovery_context.current_page} for facet {facet_id}")
                
                # Check if we've reached the limit or last page
                if len(pages) < discovery_context.batch_size:
                    # Last page
                    break
                
                discovery_context.current_page += 1
            
            # Handle completion based on whether discovery was interrupted
            if discovery_context.discovery_interrupted:
                # Discovery was interrupted - handle based on reason
                if discovery_context.interruption_reason == 'captcha':
                    # Mark for CAPTCHA recovery
                    import time
                    retry_time = time.time() + 3600  # 1 hour cooling-off
                    retry_message = f"CAPTCHA interruption at page {discovery_context.current_page} - retry after: {time.ctime(retry_time)}"
                    self.storage.update_facet_discovery(
                        facet_id,
                        status='captcha_retry',
                        error_message=retry_message,
                        items_discovered=discovery_context.total_discovered,
                        current_page=discovery_context.current_page  # This will set resume_from_page automatically
                    )
                    self.logger.info(f"Marked facet {facet_id} for CAPTCHA retry (discovered {discovery_context.total_discovered} items, resume from page {discovery_context.current_page})")
                elif discovery_context.interruption_reason == 'timeout' and discovery_context.total_discovered > 0:
                    # Partial completion due to timeouts
                    self.storage.update_facet_discovery(
                        facet_id,
                        actual_items=discovery_context.total_discovered,
                        items_discovered=discovery_context.total_discovered,
                        status='completed',
                        error_message=f"Completed with timeouts at page {discovery_context.current_page}"
                    )
                    self.logger.info(f"Marked facet {facet_id} as completed with timeouts ({discovery_context.total_discovered} items)")
                else:
                    # Other interruption with partial results
                    self.storage.update_facet_discovery(
                        facet_id,
                        status='needs_splitting',
                        error_message=f"Interrupted by {discovery_context.interruption_reason} at page {discovery_context.current_page} - needs splitting",
                        items_discovered=discovery_context.total_discovered,
                        resume_from_page=discovery_context.current_page
                    )
                    self.logger.info(f"Marked facet {facet_id} for splitting due to {discovery_context.interruption_reason} (discovered {discovery_context.total_discovered} items)")
            else:
                # Normal completion
                self.storage.update_facet_discovery(
                    facet_id, 
                    actual_items=discovery_context.total_discovered,
                    items_discovered=discovery_context.total_discovered,
                    status='completed'
                )
                self.logger.info(f"Completed discovery for facet {facet_id}: {discovery_context.total_discovered} items")
            
            return discovery_context.total_discovered
            
        except CaptchaHandlingException as e:
            self.logger.warning(f"CAPTCHA handling exception for facet {facet_id}: {e}")
            
            # Handle CAPTCHA exceptions with global cooling-off approach
            if e.retry_strategy == 'global_cooling_off':
                # Global CAPTCHA state - ALL discovery operations are blocked
                captcha_status = e.suggested_params
                
                self.logger.warning(
                    f"Global CAPTCHA protection active. "
                    f"Blocking ALL discovery operations. "
                    f"Reason: {captcha_status.get('reason', 'CAPTCHA detected')}"
                )
                
                # Mark this facet as blocked by global CAPTCHA (not individual retry)
                self.storage.update_facet_discovery(
                    facet_id,
                    status='captcha_blocked',
                    error_message=f"Blocked by global CAPTCHA protection: {captcha_status.get('reason', 'CAPTCHA detected')}",
                    items_discovered=discovery_context.total_discovered,
                    current_page=discovery_context.current_page  # Preserve current page for resume
                )
                
                # Re-raise to stop ALL discovery operations
                raise CaptchaHandlingException(
                    f"Global CAPTCHA protection active - stopping ALL discovery operations",
                    retry_strategy="global_cooling_off",
                    suggested_params=captcha_status
                )
            elif e.retry_strategy == 'facet_splitting_required':
                # Mark for facet splitting instead of error
                self.storage.update_facet_discovery(
                    facet_id,
                    status='needs_splitting',
                    error_message=f"CAPTCHA-triggered splitting needed: {e}",
                    items_discovered=discovery_context.total_discovered,
                    actual_items=discovery_context.total_discovered
                )
                self.logger.info(f"Marked facet {facet_id} for splitting due to CAPTCHA (discovered {discovery_context.total_discovered} items)")
                return discovery_context.total_discovered
            else:
                # Legacy handling - convert to global approach
                self.logger.warning(f"Converting legacy CAPTCHA handling to global approach for facet {facet_id}")
                self.storage.update_facet_discovery(
                    facet_id,
                    status='captcha_blocked',
                    error_message=f"Legacy CAPTCHA converted to global protection: {e}",
                    items_discovered=discovery_context.total_discovered,
                    current_page=discovery_context.current_page
                )
                
                # Re-raise as global CAPTCHA
                raise CaptchaHandlingException(
                    f"Legacy CAPTCHA converted to global protection",
                    retry_strategy="global_cooling_off",
                    suggested_params={'reason': 'Legacy CAPTCHA detection'}
                )
                
        except Exception as e:
            error_message = str(e)
            
            # Ensure discovery_context is available for error handling
            if 'discovery_context' not in locals():
                discovery_context = FacetDiscoveryContext(facet, batch_size, max_items)
            
            # Handle CAPTCHA-related errors from the old error format
            if 'captcha' in error_message.lower():
                self.logger.warning(f"Legacy CAPTCHA error for facet {facet_id}: {e}")
                # Mark for retry instead of permanent error
                import time
                retry_time = time.time() + 7200  # 2 hour delay
                retry_message = f"Legacy CAPTCHA error - retry scheduled: {error_message}. Retry after: {time.ctime(retry_time)}"
                self.storage.update_facet_discovery(
                    facet_id,
                    status='captcha_retry',
                    error_message=retry_message,
                    items_discovered=discovery_context.total_discovered
                )
                self.logger.info(f"Scheduled facet {facet_id} for CAPTCHA retry due to legacy error (discovered {discovery_context.total_discovered} items)")
                return discovery_context.total_discovered
            
            # Handle timeout errors more gracefully
            elif 'timeout' in error_message.lower():
                self.logger.warning(f"Timeout discovering content for facet {facet_id}. Consider using smaller batches or more specific searches.")
                # Mark as partial completion if we discovered some items
                if discovery_context.total_discovered > 0:
                    self.storage.update_facet_discovery(
                        facet_id,
                        actual_items=discovery_context.total_discovered,
                        items_discovered=discovery_context.total_discovered,
                        status='completed',
                        error_message=f"Completed with timeouts: {error_message}"
                    )
                    self.logger.info(f"Marked facet {facet_id} as completed with {discovery_context.total_discovered} items despite timeouts")
                    return discovery_context.total_discovered
                else:
                    self.storage.update_facet_discovery(
                        facet_id,
                        status='error',
                        error_message=f"Timeout before discovering any items: {error_message}"
                    )
            else:
                self.logger.error(f"Error discovering content for facet {facet_id}: {e}")
                self.storage.update_facet_discovery(
                    facet_id,
                    status='error',
                    error_message=error_message
                )
            raise
    
    def enqueue_facet_content(self, facet_id: int, max_items: int = None, 
                             progress_callback=None) -> int:
        """
        Enqueue all discovered content from a facet for download.
        Returns the number of items enqueued.
        
        Args:
            facet_id: ID of the facet to enqueue
            max_items: Maximum items to enqueue (None for all)
            progress_callback: Optional callback for progress updates
        """
        facet = self.storage.get_search_facet(facet_id)
        if not facet:
            raise ValueError(f"Facet {facet_id} not found")
        
        # Get all pages discovered for this facet that aren't already downloaded
        pages = self.storage.get_pages_for_facet(facet_id, downloaded=False)
        
        if max_items:
            pages = pages[:max_items]
        
        enqueued_count = 0
        for i, page in enumerate(pages):
            # Estimate download size and time
            estimated_size_mb = 1.0  # Rough estimate: 1MB per page
            estimated_time_hours = 0.1  # Rough estimate: 0.1 hours per page
            
            # Add to download queue
            self.storage.add_to_download_queue(
                queue_type='page',
                reference_id=page['item_id'],
                priority=self._calculate_priority(facet, page),
                estimated_size_mb=estimated_size_mb,
                estimated_time_hours=estimated_time_hours
            )
            enqueued_count += 1
            
            # Call progress callback every 100 items
            if progress_callback and (i + 1) % 100 == 0:
                progress_callback({
                    'facet_id': facet_id,
                    'enqueued_count': enqueued_count,
                    'total_pages': len(pages),
                    'current_item': i + 1
                })
        
        self.logger.info(f"Enqueued {enqueued_count} items from facet {facet_id}")
        return enqueued_count
    
    def _calculate_priority(self, facet: Dict, page: Dict) -> int:
        """Calculate download priority for a page based on facet and page metadata."""
        priority = 5  # Default priority
        
        # Higher priority for certain date ranges
        if facet['facet_type'] == 'date_range':
            year_range = facet['facet_value']
            if '1906' in year_range:  # San Francisco earthquake
                priority = 1
            elif any(year in year_range for year in ['1917', '1918', '1919']):  # WWI era
                priority = 2
        
        # Higher priority for certain states
        if facet['facet_type'] == 'state':
            if facet['facet_value'] in ['California', 'New York', 'Illinois']:
                priority = max(1, priority - 1)
        
        return priority
    
    # Helper methods
    
    def _extract_city(self, place_list: List[str]) -> Optional[str]:
        """Extract city name from place of publication list."""
        if not place_list:
            return None
        
        # Take first place and extract city (before first comma)
        place = place_list[0] if isinstance(place_list, list) else str(place_list)
        return place.split(',')[0].strip() if ',' in place else place.strip()
    
    def _extract_primary_language(self, language_list: List[str]) -> Optional[str]:
        """Extract primary language from language list."""
        if not language_list:
            return None
        return language_list[0] if isinstance(language_list, list) else str(language_list)
    
    def _extract_primary_subject(self, subject_list: List[str]) -> Optional[str]:
        """Extract primary subject from subject list."""
        if not subject_list:
            return None
        return subject_list[0] if isinstance(subject_list, list) else str(subject_list)
    
    def _parse_year(self, year_str: Optional[str]) -> Optional[int]:
        """Parse year string to integer."""
        if not year_str:
            return None
        try:
            # Extract 4-digit year
            import re
            match = re.search(r'\b(\d{4})\b', str(year_str))
            return int(match.group(1)) if match else None
        except (ValueError, AttributeError):
            return None
    
    def _parse_issue_date(self, date_str: str) -> Optional[str]:
        """Parse issue date to YYYY-MM-DD format."""
        if not date_str:
            return None
        
        try:
            # Handle various date formats from LOC API
            if len(date_str) == 10 and date_str.count('-') == 2:
                return date_str  # Already in YYYY-MM-DD format
            elif len(date_str) == 8 and date_str.isdigit():
                # YYYYMMDD format
                return f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
            else:
                # Try to parse with datetime
                from datetime import datetime
                parsed = datetime.strptime(date_str, '%Y-%m-%d')
                return parsed.strftime('%Y-%m-%d')
        except Exception:
            self.logger.warning(f"Could not parse date: {date_str}")
            return None
    
    def _calculate_periodical_priority(self, periodical: Dict) -> int:
        """Calculate download priority for a periodical (1=highest, 10=lowest)."""
        priority = 5  # Default priority
        
        # Prioritize by date range (more recent = higher priority)
        if periodical.get('end_year'):
            if periodical['end_year'] >= 1950:
                priority -= 1  # Recent news
            elif periodical['end_year'] >= 1900:
                priority -= 0  # 20th century
            else:
                priority += 1  # Historical
        
        # Prioritize by frequency (daily papers = higher priority)
        if periodical.get('frequency'):
            freq = periodical['frequency'].lower()
            if 'daily' in freq:
                priority -= 1
            elif 'weekly' in freq:
                priority += 0
            else:
                priority += 1
        
        # Prioritize by size (more issues = higher value)
        if periodical.get('total_issues', 0) > 1000:
            priority -= 1
        
        return max(1, min(10, priority))  # Clamp between 1-10
    
    def process_captcha_recovery(self) -> Dict[str, int]:
        """
        Process facets marked for CAPTCHA retry and facet splitting.
        Returns statistics about recovery operations performed.
        """
        import time
        current_time = int(time.time())
        stats = {
            'retries_processed': 0,
            'splits_processed': 0,
            'retries_scheduled': 0,
            'errors': 0
        }
        
        # Process facets marked for CAPTCHA retry
        retry_facets = self.storage.get_search_facets(status='captcha_retry')
        for facet in retry_facets:
            facet_id = facet['id']
            
            # For now, treat all captcha_retry facets as ready for retry
            # TODO: Implement proper retry time tracking
            next_retry_time = 0  # Always ready for retry
            
            if current_time >= next_retry_time:
                self.logger.info(f"Processing CAPTCHA retry for facet {facet_id}")
                try:
                    # Reset status to pending for retry
                    self.storage.update_facet_discovery(facet_id, status='pending')
                    
                    # Attempt discovery with more conservative settings
                    discovered = self.discover_facet_content(
                        facet_id, 
                        batch_size=10,  # Very small batches
                        max_items=1000  # Limit to reduce CAPTCHA risk
                    )
                    stats['retries_processed'] += 1
                    self.logger.info(f"CAPTCHA retry successful for facet {facet_id}: {discovered} items")
                    
                except Exception as e:
                    if 'captcha' in str(e).lower():
                        # Schedule another retry with longer delay
                        retry_time = current_time + 14400  # 4 hours
                        retry_message = f"Retry failed, rescheduled: {e}. Retry after: {time.ctime(retry_time)}"
                        self.storage.update_facet_discovery(
                            facet_id,
                            status='captcha_retry',
                            error_message=retry_message
                        )
                        stats['retries_scheduled'] += 1
                        self.logger.warning(f"CAPTCHA retry failed for facet {facet_id}, rescheduled for 4 hours")
                    else:
                        self.logger.error(f"Non-CAPTCHA error during retry for facet {facet_id}: {e}")
                        stats['errors'] += 1
            else:
                # Still waiting for retry time
                if next_retry_time > 0:
                    remaining_wait = (next_retry_time - current_time) / 3600
                    self.logger.debug(f"Facet {facet_id} still cooling off, {remaining_wait:.1f} hours remaining")
                else:
                    # No retry time found - assume ready for retry
                    self.logger.debug(f"Facet {facet_id} has no retry time - treating as ready for retry")
        
        # Process facets marked for splitting
        split_facets = self.storage.get_search_facets(status='needs_splitting')
        for facet in split_facets:
            try:
                split_count = self.split_facet_for_captcha_recovery(facet['id'])
                stats['splits_processed'] += split_count
                self.logger.info(f"Split facet {facet['id']} into {split_count} smaller facets")
            except Exception as e:
                self.logger.error(f"Error splitting facet {facet['id']}: {e}")
                stats['errors'] += 1
        
        return stats
    
    def split_facet_for_captcha_recovery(self, facet_id: int) -> int:
        """
        Split a facet that triggered CAPTCHA into smaller, more manageable facets.
        Returns the number of new facets created.
        """
        facet = self.storage.get_search_facet(facet_id)
        if not facet:
            raise ValueError(f"Facet {facet_id} not found")
        
        splits_created = 0
        
        if facet['facet_type'] == 'date_range':
            # Split date range into smaller chunks
            try:
                start_date, end_date = facet['facet_value'].split('/')
                start_year = int(start_date)
                end_year = int(end_date)
                
                if start_year == end_year:
                    # Single year - split by quarters
                    quarters = [
                        (f"{start_year}0101", f"{start_year}0331"),  # Q1
                        (f"{start_year}0401", f"{start_year}0630"),  # Q2  
                        (f"{start_year}0701", f"{start_year}0930"),  # Q3
                        (f"{start_year}1001", f"{start_year}1231"),  # Q4
                    ]
                    
                    for i, (q_start, q_end) in enumerate(quarters):
                        quarter_name = f"Q{i+1}"
                        self.storage.create_search_facet(
                            facet_type='date_range',
                            facet_value=f"{q_start}/{q_end}",
                            query=facet.get('query'),
                            estimated_items=facet.get('estimated_items', 0) // 4,
                            priority=facet.get('priority', 5) - 1  # Higher priority for splits
                        )
                        splits_created += 1
                        self.logger.info(f"Created quarterly facet for {start_year} {quarter_name}: {q_start}/{q_end}")
                
                else:
                    # Multi-year range - split by individual years
                    for year in range(start_year, end_year + 1):
                        self.storage.create_search_facet(
                            facet_type='date_range',
                            facet_value=f"{year}/{year}",
                            query=facet.get('query'),
                            estimated_items=facet.get('estimated_items', 0) // (end_year - start_year + 1),
                            priority=facet.get('priority', 5) - 1  # Higher priority for splits
                        )
                        splits_created += 1
                        self.logger.info(f"Created yearly facet: {year}/{year}")
                        
            except (ValueError, AttributeError) as e:
                self.logger.error(f"Could not parse date range for facet {facet_id}: {facet['facet_value']}")
                raise
        
        elif facet['facet_type'] == 'state':
            # For state facets, split by combining with date ranges
            state = facet['facet_value']
            current_year = datetime.now().year
            
            # Create smaller date-limited state searches
            year_ranges = [
                (1900, 1920), (1921, 1940), (1941, 1960), 
                (1961, 1980), (1981, 2000), (2001, current_year)
            ]
            
            for start_year, end_year in year_ranges:
                # Create combined facet with state + date constraints
                combined_query = f"state:{state} AND date:[{start_year} TO {end_year}]"
                self.storage.create_search_facet(
                    facet_type='combined',
                    facet_value=f"state:{state}|date_range:{start_year}/{end_year}",
                    query=combined_query,
                    estimated_items=facet.get('estimated_items', 0) // len(year_ranges),
                    priority=facet.get('priority', 5) - 1
                )        
                splits_created += 1
                self.logger.info(f"Created combined state-date facet: state:{state}|{start_year}/{end_year}")

        # Mark original facet as split
        self.storage.update_facet_discovery(
            facet_id,
            status='split_completed',
            error_message=f"Split into {splits_created} smaller facets due to CAPTCHA"
        )
        
        return splits_created
    
    def get_captcha_recovery_status(self) -> Dict:
        """Get status of facets needing CAPTCHA recovery."""
        import time
        current_time = int(time.time())
        
        retry_facets = self.storage.get_search_facets(status='captcha_retry')
        split_facets = self.storage.get_search_facets(status='needs_splitting')
        
        ready_for_retry = 0
        waiting_for_retry = 0
        
        for facet in retry_facets:
            # For now, treat all as ready for retry
            ready_for_retry += 1
        
        return {
            'ready_for_retry': ready_for_retry,
            'waiting_for_retry': waiting_for_retry,
            'needs_splitting': len(split_facets),
            'total_recovery_needed': ready_for_retry + waiting_for_retry + len(split_facets)
        }
    
    def fix_incorrectly_completed_facets(self) -> Dict[str, int]:
        """
        Fix facets that were incorrectly marked as 'completed' when they were actually 
        interrupted by CAPTCHA or other errors. 
        
        Returns statistics about facets fixed.
        """
        stats = {
            'facets_fixed': 0,
            'facets_checked': 0,
            'errors': 0
        }
        
        # Get all facets marked as completed
        completed_facets = self.storage.get_search_facets(status='completed')
        
        for facet in completed_facets:
            stats['facets_checked'] += 1
            facet_id = facet['id']
            error_message = facet.get('error_message', '')
            
            try:
                # Check multiple indicators of CAPTCHA interruption:
                # 1. Error message mentions CAPTCHA/stopped/manual intervention
                # 2. Facet has current_page but no error_message (interrupted mid-discovery)
                # 3. resume_from_page is set (indicates interruption)
                
                captcha_indicators = []
                
                # Check error message
                if error_message and any(keyword in error_message.lower() for keyword in ['captcha', 'stopped at page', 'manual intervention']):
                    captcha_indicators.append(f"error message: {error_message}")
                
                # Check for mid-discovery interruption (current_page set but no error)
                current_page = facet.get('current_page')
                if current_page and current_page > 1 and not error_message:
                    captcha_indicators.append(f"interrupted at page {current_page} with no error message")
                
                # Check if resume_from_page is set
                resume_from_page = facet.get('resume_from_page')
                if resume_from_page and resume_from_page > 1:
                    captcha_indicators.append(f"resume page set to {resume_from_page}")
                
                if captcha_indicators:
                    self.logger.info(f"Found incorrectly completed facet {facet_id}: {'; '.join(captcha_indicators)}")
                    
                    # Determine resume page
                    resume_page = current_page if current_page else resume_from_page if resume_from_page else 1
                    
                    # If we have a current_page, resume from the next page (the one that failed)
                    if current_page and current_page > 1:
                        resume_page = current_page + 1
                    
                    # Extract page from error message if available
                    if error_message and 'stopped at page' in error_message:
                        try:
                            import re
                            page_match = re.search(r'stopped at page (\d+)', error_message)
                            if page_match:
                                resume_page = int(page_match.group(1)) + 1  # Resume from next page
                        except Exception:
                            pass
                    
                    # Mark for CAPTCHA retry
                    import time
                    retry_message = f"Fixed incorrectly completed facet - was interrupted (indicators: {'; '.join(captcha_indicators)}). Retry after: {time.ctime(time.time() + 3600)}"
                    self.storage.update_facet_discovery(
                        facet_id,
                        status='captcha_retry',
                        error_message=retry_message,
                        current_page=resume_page  # This will set resume_from_page automatically
                    )
                    
                    stats['facets_fixed'] += 1
                    self.logger.info(f"Fixed facet {facet_id}: marked for CAPTCHA retry, resume from page {resume_page}")
                    
            except Exception as e:
                self.logger.error(f"Error fixing facet {facet_id}: {e}")
                stats['errors'] += 1
        
        return stats
    
    def _handle_captcha_during_batch_discovery(self, e: CaptchaHandlingException, 
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
            import time
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
    
    def _process_issue_from_batch(self, issue_data: Dict, session_name: str, 
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
        
        try:
            # Convert issue URL to endpoint
            if issue_url.startswith(self.api_client.base_url):
                issue_endpoint = issue_url.replace(self.api_client.base_url, '')
            else:
                issue_endpoint = issue_url
            
            # Get issue details which contain the actual pages
            issue_details = self.api_client._make_request(issue_endpoint)
            issue_pages = issue_details.get('pages', [])
            
            # Collect pages for batch storage
            batch_pages = []
            for page_data in issue_pages:
                # Process page from issue data without individual API calls (much faster!)
                page = self.processor.process_page_from_issue(page_data, issue_details)
                if page:
                    batch_pages.append(page)
            
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
            should_continue = self._handle_captcha_during_batch_discovery(
                e, session_name, batch_index, issue_idx, issue_url
            )
            if should_continue:
                # Retry this issue after CAPTCHA handling
                return self._process_issue_from_batch(issue_data, session_name, batch_index, issue_idx, auto_enqueue)
            else:
                return 0, 0
                
        except Exception as e:
            self.logger.error(f"Error processing issue {issue_url}: {e}")
            return 0, 0
    
    def _parse_retry_time_from_message(self, error_message: str) -> int:
        """
        Parse retry time from error message that contains 'Retry after: <timestamp>'.
        Returns 0 if no retry time found (meaning ready to retry now).
        """
        if not error_message:
            return 0
        
        try:
            # Look for pattern "Retry after: <timestamp>"
            import re
            import time
            
            retry_pattern = r'Retry after: (.+?)(?:\.|$)'
            match = re.search(retry_pattern, error_message)
            
            if match:
                time_str = match.group(1).strip()
                # Parse the timestamp back to epoch time
                retry_time = time.mktime(time.strptime(time_str, '%a %b %d %H:%M:%S %Y'))
                return int(retry_time)
            else:
                # No retry time found - assume ready to retry
                return 0
                
        except Exception as e:
            self.logger.debug(f"Could not parse retry time from message: {error_message} - {e}")
            return 0  # Assume ready to retry if parsing fails
    
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
        # Delegate to the dedicated batch processor
        return self.batch_processor.discover_content_via_batches(
            max_batches=max_batches,
            auto_enqueue=auto_enqueue,
            batch_size=batch_size,
            rate_limit_delay=rate_limit_delay,
            progress_callback=progress_callback
        )