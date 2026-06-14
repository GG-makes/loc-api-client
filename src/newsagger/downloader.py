"""
Download Processor Module

Handles the actual downloading of files from the Library of Congress API,
processing the download queue and managing file storage.
"""

import os
import logging
import requests
import hashlib
import concurrent.futures
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from datetime import datetime
from tqdm import tqdm
import time
import mimetypes

from .storage import NewsStorage
from .rate_limited_client import LocApiClient
from .utils import retry_on_network_failure, ProgressTracker


class DownloadProcessor:
    """Processes download queue and manages file downloads."""
    
    def __init__(self, storage: NewsStorage, api_client: LocApiClient, 
                 download_dir: str = None, 
                 file_types: List[str] = None,
                 parallel_workers: int = None,
                 file_concurrency: int = None):
        self.storage = storage
        self.api_client = api_client
        
        # Use config default if download_dir not specified
        if download_dir is None:
            from .config import Config
            download_dir = Config().download_dir
        
        self.download_dir = Path(download_dir)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.logger = logging.getLogger(__name__)
        
        # Configure which file types to download
        # Default: download all available types
        self.file_types = file_types or ['pdf', 'jp2', 'ocr', 'metadata']
        
        # Configure parallel processing
        # Default to CPU core count, with reasonable bounds
        if parallel_workers is None:
            parallel_workers = max(1, min(os.cpu_count() or 4, 16))  # 1-16 workers
        self.parallel_workers = parallel_workers
        self.logger.info(f"Configured for parallel queue processing with {self.parallel_workers} workers")
        
        # Configure file download concurrency
        # Default to 6 concurrent file downloads per item (PDF, JP2, etc.)
        if file_concurrency is None:
            file_concurrency = 6  # Good balance for concurrent file downloads
        self.file_concurrency = max(1, min(file_concurrency, 12))  # 1-12 concurrent downloads
        self.logger.info(f"Configured for concurrent file downloads with {self.file_concurrency} workers per item")
        
        # Set up download session with appropriate headers
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Newsagger/0.1.0 (Educational Archive Tool)'
        })
    
    def process_queue(self, max_items: int = None, max_size_mb: float = None, 
                     dry_run: bool = False, continuous: bool = False, 
                     max_idle_minutes: int = 10) -> Dict:
        """
        Process the download queue.
        
        Args:
            max_items: Maximum number of items to process (per batch if continuous)
            max_size_mb: Maximum total size to download
            dry_run: If True, only simulate processing
            continuous: If True, continuously check for new items
            max_idle_minutes: In continuous mode, stop after this many minutes without new items
        
        Returns statistics about the download session.
        """
        if continuous:
            return self._process_queue_continuous(max_items, max_size_mb, dry_run, max_idle_minutes)
        else:
            return self._process_queue_single_batch(max_items, max_size_mb, dry_run)
    
    def _process_queue_single_batch(self, max_items: int = None, max_size_mb: float = None, 
                                   dry_run: bool = False) -> Dict:
        """Parallel queue processing implementation."""
        self.logger.info(f"Starting parallel download queue processing with {self.parallel_workers} workers...")
        
        # Get queued items ordered by priority
        queue_items = self.storage.get_download_queue(status='queued')
        if not queue_items:
            self.logger.info("No items in download queue")
            return {"downloaded": 0, "errors": 0, "skipped": 0, "total_size_mb": 0}
        
        # Apply limits
        if max_items:
            queue_items = queue_items[:max_items]
        
        # Filter by size limit
        if max_size_mb:
            filtered_items = []
            total_size = 0
            for item in queue_items:
                if total_size + item['estimated_size_mb'] <= max_size_mb:
                    filtered_items.append(item)
                    total_size += item['estimated_size_mb']
                else:
                    break
            queue_items = filtered_items
        
        if dry_run:
            self.logger.info(f"DRY RUN: Would download {len(queue_items)} items")
            total_size = sum(item['estimated_size_mb'] for item in queue_items)
            return {
                "would_download": len(queue_items),
                "estimated_size_mb": total_size,
                "dry_run": True
            }
        
        # Process downloads with parallel workers
        return self._process_items_parallel(queue_items)
    
    def _process_items_parallel(self, queue_items: List[Dict]) -> Dict:
        """Process queue items in parallel using ThreadPoolExecutor."""
        import threading
        
        start_time = datetime.now()
        total_size_mb = 0
        
        # Thread-safe data structures for collecting results
        results_lock = threading.Lock()
        batch_updates = []
        
        def process_single_item(item):
            """Process a single queue item (thread-safe)."""
            try:
                # Mark item as active
                self.storage.update_queue_item(item['id'], status='active')
                
                # Process the download based on queue type
                result = self._process_queue_item(item)
                
                # Thread-safe result collection
                with results_lock:
                    if result['success']:
                        nonlocal total_size_mb
                        total_size_mb += result.get('size_mb', 0)
                        
                        batch_updates.append({
                            'id': item['id'],
                            'status': 'completed',
                            'progress_percent': 100.0,
                            'error_message': None,
                            'size_mb': result.get('size_mb', 0)
                        })
                        
                        return {'success': True, 'item_id': item['id'], 'size_mb': result.get('size_mb', 0)}
                    else:
                        batch_updates.append({
                            'id': item['id'],
                            'status': 'failed',
                            'error_message': result.get('error', 'Unknown error'),
                            'progress_percent': 0,
                            'size_mb': 0
                        })
                        
                        return {'success': False, 'item_id': item['id'], 'error': result.get('error', 'Unknown error')}
                
            except Exception as e:
                self.logger.error(f"Error processing queue item {item['id']}: {e}")
                
                with results_lock:
                    batch_updates.append({
                        'id': item['id'],
                        'status': 'failed',
                        'error_message': str(e),
                        'progress_percent': 0,
                        'size_mb': 0
                    })
                
                return {'success': False, 'item_id': item['id'], 'error': str(e)}
        
        # Process items in parallel with progress tracking
        with ProgressTracker(total=len(queue_items), desc="Processing downloads", unit="files") as progress:
            with concurrent.futures.ThreadPoolExecutor(max_workers=self.parallel_workers) as executor:
                # Submit all tasks
                future_to_item = {executor.submit(process_single_item, item): item for item in queue_items}
                
                # Collect results as they complete
                completed_results = []
                for future in concurrent.futures.as_completed(future_to_item):
                    result = future.result()
                    completed_results.append(result)
                    
                    # Update progress
                    if result['success']:
                        progress.update(success=True)
                        with results_lock:
                            progress.set_postfix(size_mb=f"{total_size_mb:.1f}")
                    else:
                        progress.update(success=False)
                        self.logger.warning(f"Failed to process item {result['item_id']}: {result.get('error', 'Unknown error')}")
                    
                    # Process database updates in smaller batches for faster UI updates
                    with results_lock:
                        if len(batch_updates) >= 5:  # Smaller batches for faster progress updates
                            self._process_batch_updates(batch_updates.copy())
                            batch_updates.clear()
        
        # Process any remaining batch updates
        if batch_updates:
            self._process_batch_updates(batch_updates)
        
        # Calculate final statistics
        end_time = datetime.now()
        success_count = sum(1 for r in completed_results if r['success'])
        error_count = len(completed_results) - success_count
        
        stats = {
            "downloaded": success_count,
            "errors": error_count,
            "skipped": 0,  # No skipped items in this implementation
            "total_size_mb": total_size_mb,
            "start_time": start_time,
            "end_time": end_time,
            "duration_minutes": (end_time - start_time).total_seconds() / 60,
            "parallel_workers": self.parallel_workers
        }
        
        self.logger.info(f"Parallel download processing complete: {stats['downloaded']} downloaded, "
                        f"{stats['errors']} errors, {stats['total_size_mb']:.1f} MB total "
                        f"using {self.parallel_workers} workers")
        
        return stats
    
    def _process_queue_continuous(self, max_items: int = None, max_size_mb: float = None, 
                                 dry_run: bool = False, max_idle_minutes: int = 10) -> Dict:
        """
        Continuously process download queue items as they become available.
        
        Args:
            max_items: Maximum items per batch (not total limit)
            max_size_mb: Total size limit across all batches
            dry_run: If True, only simulate processing
            max_idle_minutes: Stop after this many minutes without new items
        """
        import time
        import signal
        from datetime import datetime, timedelta
        
        # Set up signal handling for graceful shutdown
        shutdown_requested = False
        force_quit = False
        
        def graceful_signal_handler(signum, frame):
            nonlocal shutdown_requested
            shutdown_requested = True
            self.logger.info("Shutdown signal received, finishing current batch...")
        
        def force_quit_handler(signum, frame):
            nonlocal force_quit
            force_quit = True
            self.logger.info("Force quit signal received, stopping immediately...")
            # Restore default handler for immediate exit on second SIGQUIT
            if hasattr(signal, "SIGQUIT"):
                signal.signal(signal.SIGQUIT, signal.SIG_DFL)
        
        # Register signal handlers
        signal.signal(signal.SIGINT, graceful_signal_handler)   # Ctrl+C - graceful
        signal.signal(signal.SIGTERM, graceful_signal_handler)  # Terminate - graceful  
        if hasattr(signal, "SIGQUIT"):
            signal.signal(signal.SIGQUIT, force_quit_handler)       # Ctrl+\ - immediate - not for windows
        
        self.logger.info("Starting continuous download queue processing...")
        self.logger.info(f"Will stop after {max_idle_minutes} minutes without new items")
        self.logger.info("Press Ctrl+C to stop gracefully, Ctrl+\\ to force quit immediately")
        
        # Global tracking across all batches
        global_stats = {
            "downloaded": 0,
            "errors": 0, 
            "skipped": 0,
            "total_size_mb": 0.0,
            "batches_processed": 0,
            "start_time": datetime.now()
        }
        
        last_activity_time = datetime.now()
        batch_size = max_items or 50  # Default batch size
        poll_interval = 30  # Check for new items every 30 seconds
        
        self.logger.info(f"Batch size: {batch_size}, Poll interval: {poll_interval}s")
        
        while not shutdown_requested and not force_quit:
            # Check if we should stop due to inactivity
            time_since_activity = datetime.now() - last_activity_time
            if time_since_activity > timedelta(minutes=max_idle_minutes):
                self.logger.info(f"Stopping after {max_idle_minutes} minutes without new items")
                break
            
            # Check for new items in queue
            queue_items = self.storage.get_download_queue(status='queued')
            
            if not queue_items:
                self.logger.debug(f"No items in queue, waiting {poll_interval}s...")
                # Use interruptible sleep
                self._interruptible_sleep(poll_interval, lambda: shutdown_requested or force_quit)
                continue
            
            # Process a batch of items
            batch_items = queue_items[:batch_size]
            
            # Check size limit (cumulative across all batches)
            if max_size_mb:
                filtered_batch = []
                for item in batch_items:
                    estimated_new_total = global_stats["total_size_mb"] + item['estimated_size_mb']
                    if estimated_new_total <= max_size_mb:
                        filtered_batch.append(item)
                    else:
                        self.logger.info(f"Size limit reached ({max_size_mb} MB), stopping")
                        break
                
                if not filtered_batch:
                    self.logger.info("No more items can be processed within size limit")
                    break
                    
                batch_items = filtered_batch
            
            if not batch_items:
                time.sleep(poll_interval)
                continue
            
            self.logger.info(f"Processing batch {global_stats['batches_processed'] + 1} with {len(batch_items)} items")
            
            if dry_run:
                # Simulate processing for dry run
                batch_size_mb = sum(item['estimated_size_mb'] for item in batch_items)
                global_stats["would_download"] = global_stats.get("would_download", 0) + len(batch_items)
                global_stats["estimated_size_mb"] = global_stats.get("estimated_size_mb", 0) + batch_size_mb
                global_stats["batches_processed"] += 1
                
                self.logger.info(f"DRY RUN: Would download {len(batch_items)} items ({batch_size_mb:.1f} MB)")
                
                # Mark items as processed in dry run
                for item in batch_items:
                    self.storage.update_queue_item(item['id'], status='completed')
                
                last_activity_time = datetime.now()
                self._interruptible_sleep(poll_interval, lambda: shutdown_requested)
                continue
            
            # Check for shutdown before processing
            if shutdown_requested or force_quit:
                reason = "Force quit" if force_quit else "Shutdown"
                self.logger.info(f"{reason} requested, stopping before processing batch")
                break
            
            # Process the batch using parallel processing
            try:
                batch_stats = self._process_items_parallel(batch_items)
                
                # Update global stats
                global_stats["downloaded"] += batch_stats.get("downloaded", 0)
                global_stats["errors"] += batch_stats.get("errors", 0)
                global_stats["skipped"] += batch_stats.get("skipped", 0)
                global_stats["total_size_mb"] += batch_stats.get("total_size_mb", 0)
                global_stats["batches_processed"] += 1
                
                self.logger.info(f"Batch complete: {batch_stats.get('downloaded', 0)} downloaded, "
                               f"{batch_stats.get('errors', 0)} errors, "
                               f"{batch_stats.get('total_size_mb', 0):.1f} MB")
                
                # Update activity time if we processed items
                if batch_stats.get("downloaded", 0) > 0:
                    last_activity_time = datetime.now()
                
            except Exception as e:
                self.logger.error(f"Error processing batch: {e}")
                global_stats["errors"] += len(batch_items)
                self._interruptible_sleep(poll_interval, lambda: shutdown_requested or force_quit)
            
            # Brief pause between batches to avoid overwhelming the system (but check for shutdown)
            if not shutdown_requested and not force_quit:
                self._interruptible_sleep(5, lambda: shutdown_requested or force_quit)
        
        # Final statistics
        global_stats["end_time"] = datetime.now()
        global_stats["duration_minutes"] = (global_stats["end_time"] - global_stats["start_time"]).total_seconds() / 60
        
        self.logger.info(f"Continuous processing complete: {global_stats['downloaded']} downloaded, "
                        f"{global_stats['errors']} errors, {global_stats['total_size_mb']:.1f} MB total "
                        f"across {global_stats['batches_processed']} batches")
        
        # Log final shutdown reason
        if force_quit:
            self.logger.info("Processing force quit via SIGQUIT")
        elif shutdown_requested:
            self.logger.info("Processing stopped due to shutdown signal")
        
        return global_stats
    
    def _interruptible_sleep(self, total_seconds: float, should_stop_func=None):
        """Sleep that can be interrupted by checking a condition."""
        if total_seconds <= 0:
            return
            
        # Sleep in small increments to allow interruption
        check_interval = min(1.0, total_seconds / 10)  # Check every second or 10% of sleep time
        remaining = total_seconds
        
        while remaining > 0:
            if should_stop_func and should_stop_func():
                break
            
            sleep_time = min(check_interval, remaining)
            time.sleep(sleep_time)
            remaining -= sleep_time
    
    def _process_batch_items(self, queue_items: List[Dict], should_stop_func=None) -> Dict:
        """Process a batch of queue items using the existing logic."""
        # Re-use the existing processing logic but without the initial queue fetch
        total_size_mb = 0
        start_time = datetime.now()
        batch_updates = []
        
        # Process downloads with progress tracking and batched database updates
        
        with ProgressTracker(total=len(queue_items), desc=f"Downloading batch", unit="files") as progress:
            for i, item in enumerate(queue_items):
                # Check for shutdown before processing each item
                if should_stop_func and should_stop_func():
                    self.logger.info(f"Shutdown requested, stopping after {i}/{len(queue_items)} items")
                    break
                
                # Mark as in-progress
                self.storage.update_queue_item(item['id'], status='in_progress')
                
                try:
                    # Process the item
                    result = self._process_queue_item(item)
                    
                    if result['success']:
                        progress.update(1, success=True)
                        total_size_mb += result.get('size_mb', 0)
                        
                        # Mark as completed
                        batch_updates.append({
                            'id': item['id'],
                            'status': 'completed',
                            'progress_percent': 100
                        })
                        
                    else:
                        progress.update(1, success=False)
                        
                        # Mark as failed
                        batch_updates.append({
                            'id': item['id'],
                            'status': 'failed',
                            'error_message': result.get('error')
                        })
                
                except Exception as e:
                    self.logger.error(f"Error processing queue item {item['id']}: {e}")
                    progress.update(1, success=False)
                    
                    batch_updates.append({
                        'id': item['id'],
                        'status': 'failed',
                        'error_message': str(e)
                    })
                
                # Process database updates in smaller batches for faster UI updates
                if len(batch_updates) >= 3:
                    self._process_batch_updates(batch_updates)
                    batch_updates = []
            
            # Process remaining updates
            if batch_updates:
                self._process_batch_updates(batch_updates)
        
        # Get final statistics
        final_stats = progress.get_stats()
        end_time = datetime.now()
        
        return {
            "downloaded": final_stats['success'],
            "errors": final_stats['errors'],
            "skipped": final_stats['skipped'],
            "total_size_mb": total_size_mb,
            "start_time": start_time,
            "end_time": end_time,
            "duration_minutes": (end_time - start_time).total_seconds() / 60
        }
    
    def _process_queue_item(self, queue_item: Dict) -> Dict:
        """Process a single queue item based on its type."""
        queue_type = queue_item['queue_type']
        reference_id = queue_item['reference_id']
        
        try:
            if queue_type == 'page':
                return self._download_page(reference_id)
            elif queue_type == 'facet':
                return self._download_facet_content(reference_id)
            elif queue_type == 'periodical':
                return self._download_periodical(reference_id)
            else:
                return {
                    'success': False,
                    'error': f"Unknown queue type: {queue_type}"
                }
        except Exception as e:
            return {
                'success': False,
                'error': str(e)
            }
    
    def _download_page(self, item_id: str) -> Dict:
        """Download a specific newspaper page."""
        # Get page metadata from storage
        page_data = self.storage.get_page_by_item_id(item_id)
        
        if not page_data:
            return {
                'success': False,
                'error': f"Page {item_id} not found in storage"
            }
        
        # Quick check if already downloaded to avoid unnecessary work
        if page_data.get('downloaded'):
            self.logger.debug(f"Page {item_id} already downloaded, skipping")
            return {
                'success': True,
                'skipped': True,
                'size_mb': 0
            }
        
        # Create directory structure: downloads/lccn/year/month/
        page_date = page_data['date']
        year = page_date[:4] if len(page_date) >= 4 else 'unknown'
        month = page_date[5:7] if len(page_date) >= 7 else 'unknown'
        
        download_path = self.download_dir / page_data['lccn'] / year / month
        download_path.mkdir(parents=True, exist_ok=True)
        
        downloaded_files = []
        total_size = 0
        
        # Clean the item_id to make it safe for filenames
        safe_item_id = item_id.replace('/', '_').replace('\\', '_').replace(':', '_')
        
        # Prepare downloads for concurrent execution
        download_tasks = []
        
        # Add PDF download if available and requested
        if 'pdf' in self.file_types and page_data.get('pdf_url'):
            download_tasks.append({
                'url': page_data['pdf_url'],
                'path': download_path / f"{safe_item_id}.pdf",
                'type': 'pdf'
            })
        
        # Add JP2 download if available and requested
        if 'jp2' in self.file_types and page_data.get('jp2_url'):
            download_tasks.append({
                'url': page_data['jp2_url'],
                'path': download_path / f"{safe_item_id}.jp2",
                'type': 'jp2'
            })
        
        # Execute downloads concurrently for better performance
        if download_tasks:
            download_results = self._download_files_concurrent(download_tasks)
            for result in download_results:
                if result['success']:
                    downloaded_files.append(result['file_path'])
                    total_size += result['size_mb']
        
        # Save OCR text if available and requested
        if 'ocr' in self.file_types and page_data.get('ocr_text'):
            text_path = download_path / f"{safe_item_id}_ocr.txt"
            try:
                with open(text_path, 'w', encoding='utf-8') as f:
                    f.write(page_data['ocr_text'])
                downloaded_files.append(str(text_path))
            except Exception as e:
                self.logger.warning(f"Failed to save OCR text for {item_id}: {e}")
        
        # Save metadata if requested
        if 'metadata' in self.file_types:
            metadata_path = download_path / f"{safe_item_id}_metadata.json"
            try:
                import json
                metadata = {
                    'item_id': page_data['item_id'],
                    'lccn': page_data['lccn'],
                    'title': page_data['title'],
                    'date': page_data['date'],
                    'edition': page_data['edition'],
                    'sequence': page_data['sequence'],
                    'page_url': page_data['page_url'],
                    'download_date': datetime.now().isoformat(),
                    'files': downloaded_files,
                    'file_types_requested': self.file_types
                }
                with open(metadata_path, 'w', encoding='utf-8') as f:
                    json.dump(metadata, f, indent=2)
                downloaded_files.append(str(metadata_path))
            except Exception as e:
                self.logger.warning(f"Failed to save metadata for {item_id}: {e}")
        
        if downloaded_files:
            # Mark as downloaded in storage
            self.storage.mark_page_downloaded(item_id)
            
            return {
                'success': True,
                'size_mb': total_size,
                'files': downloaded_files
            }
        else:
            return {
                'success': False,
                'error': "No files were successfully downloaded"
            }
    
    def _download_facet_content(self, facet_id: str) -> Dict:
        """Download all content from a facet."""
        try:
            facet_id_int = int(facet_id)
        except ValueError:
            return {
                'success': False,
                'error': f"Invalid facet ID: {facet_id}"
            }
        
        # Get all pages for this facet that aren't downloaded
        pages = self.storage.get_pages_for_facet(facet_id_int, downloaded=False)
        if not pages:
            return {
                'success': True,
                'skipped': True,
                'message': "All pages already downloaded or no pages found"
            }
        
        downloaded_count = 0
        total_size = 0
        errors = []
        
        for page in pages:
            try:
                result = self._download_page(page['item_id'])
                if result['success'] and not result.get('skipped'):
                    downloaded_count += 1
                    total_size += result.get('size_mb', 0)
                elif not result['success']:
                    errors.append(f"Page {page['item_id']}: {result.get('error', 'Unknown error')}")
            except Exception as e:
                errors.append(f"Page {page['item_id']}: {str(e)}")
        
        if errors and len(errors) == len(pages):
            return {
                'success': False,
                'error': f"Failed to download any pages. Errors: {'; '.join(errors[:3])}"
            }
        
        return {
            'success': True,
            'downloaded_pages': downloaded_count,
            'size_mb': total_size,
            'errors': len(errors),
            'total_pages': len(pages)
        }
    
    def _download_periodical(self, lccn: str) -> Dict:
        """Download all available content for a periodical."""
        # Get all pages for this periodical that aren't downloaded
        pages = self.storage.get_pages(lccn=lccn, downloaded_only=False)
        undownloaded_pages = [p for p in pages if not p.get('downloaded')]
        
        if not undownloaded_pages:
            return {
                'success': True,
                'skipped': True,
                'message': "All pages already downloaded or no pages found"
            }
        
        downloaded_count = 0
        total_size = 0
        errors = []
        
        for page in undownloaded_pages:
            try:
                result = self._download_page(page['item_id'])
                if result['success'] and not result.get('skipped'):
                    downloaded_count += 1
                    total_size += result.get('size_mb', 0)
                elif not result['success']:
                    errors.append(f"Page {page['item_id']}: {result.get('error', 'Unknown error')}")
            except Exception as e:
                errors.append(f"Page {page['item_id']}: {str(e)}")
        
        return {
            'success': True,
            'downloaded_pages': downloaded_count,
            'size_mb': total_size,
            'errors': len(errors),
            'total_pages': len(undownloaded_pages)
        }
    
    @retry_on_network_failure(max_attempts=3, base_delay=2.0)
    def _perform_http_download(self, url: str, local_path: Path) -> int:
        """
        Perform the actual HTTP download with retry logic.
        Returns the number of bytes downloaded.
        Raises exceptions on failure (to be caught by retry decorator).
        """
        # Rate limiting is handled centrally by the API client
        
        # Download with streaming to handle large files
        response = self.session.get(url, stream=True, timeout=120)
        response.raise_for_status()
        
        # Get file size from headers
        total_size = int(response.headers.get('content-length', 0))
        
        # Write file with progress tracking and larger chunks
        downloaded_size = 0
        with open(local_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=65536):  # 64KB chunks
                if chunk:
                    f.write(chunk)
                    downloaded_size += len(chunk)
        
        # Verify download
        if total_size > 0 and downloaded_size != total_size:
            local_path.unlink()  # Remove incomplete file
            raise requests.exceptions.RequestException(
                f"Download incomplete: {downloaded_size}/{total_size} bytes"
            )
        
        return downloaded_size
    
    def _download_files_concurrent(self, download_tasks: List[Dict]) -> List[Dict]:
        """
        Download multiple files concurrently for better performance.
        
        Args:
            download_tasks: List of dicts with 'url', 'path', and 'type' keys
            
        Returns:
            List of download results
        """
        import concurrent.futures
        import threading
        
        # Create a session per thread to avoid conflicts
        thread_local = threading.local()
        
        def get_session():
            if not hasattr(thread_local, 'session'):
                thread_local.session = requests.Session()
                thread_local.session.headers.update({
                    'User-Agent': 'Newsagger/0.1.0 (Educational Archive Tool)'
                })
            return thread_local.session
        
        def download_single_file(task):
            """Download a single file using thread-local session with retries."""
            max_retries = 3
            base_delay = 2.0
            
            for attempt in range(max_retries):
                try:
                    url = task['url']
                    local_path = task['path']
                    file_type = task['type']
                    
                    # Check if file already exists
                    if local_path.exists():
                        size_mb = local_path.stat().st_size / (1024 * 1024)
                        return {
                            'success': True,
                            'file_path': str(local_path),
                            'size_mb': size_mb,
                            'skipped': True,
                            'type': file_type
                        }
                    
                    session = get_session()
                    
                    # Download with streaming
                    response = session.get(url, stream=True, timeout=120)
                    response.raise_for_status()
                    
                    # Get file size
                    total_size = int(response.headers.get('content-length', 0))
                    
                    # Write file with larger chunks for better I/O performance
                    downloaded_size = 0
                    with open(local_path, 'wb') as f:
                        for chunk in response.iter_content(chunk_size=65536):  # 64KB chunks
                            if chunk:
                                f.write(chunk)
                                downloaded_size += len(chunk)
                    
                    # Verify download
                    if total_size > 0 and downloaded_size != total_size:
                        local_path.unlink()  # Remove incomplete file
                        raise requests.exceptions.RequestException(
                            f"Download incomplete: {downloaded_size}/{total_size} bytes"
                        )
                    
                    size_mb = downloaded_size / (1024 * 1024)
                    return {
                        'success': True,
                        'file_path': str(local_path),
                        'size_mb': size_mb,
                        'type': file_type
                    }
                    
                except Exception as e:
                    # Clean up partial file if it exists
                    if local_path.exists():
                        try:
                            local_path.unlink()
                        except:
                            pass
                    
                    # If this is the last attempt, return failure
                    if attempt == max_retries - 1:
                        return {
                            'success': False,
                            'error': str(e),
                            'type': task.get('type', 'unknown'),
                            'attempts': max_retries
                        }
                    
                    # Otherwise, sleep and retry (exponential backoff)
                    import time
                    delay = base_delay * (2 ** attempt)
                    time.sleep(delay)
            
            # Should never reach here, but just in case
            return {
                'success': False,
                'error': 'Max retries exceeded',
                'type': task.get('type', 'unknown'),
                'attempts': max_retries
            }
        
        # Execute downloads concurrently using configured file concurrency
        results = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.file_concurrency) as executor:
            future_to_task = {executor.submit(download_single_file, task): task for task in download_tasks}
            
            for future in concurrent.futures.as_completed(future_to_task):
                result = future.result()
                results.append(result)
                
                # Only log failures after all retries are exhausted
                if not result['success']:
                    task = future_to_task[future]
                    attempts = result.get('attempts', 1)
                    self.logger.warning(f"Failed to download {task['type']} file after {attempts} attempts: {result['error']}")
        
        return results

    def _download_file(self, url: str, local_path: Path) -> Dict:
        """Download a single file from URL to local path."""
        try:
            # Check if file already exists
            if local_path.exists():
                size_mb = local_path.stat().st_size / (1024 * 1024)
                return {
                    'success': True,
                    'file_path': str(local_path),
                    'size_mb': size_mb,
                    'skipped': True
                }
            
            # Perform download with retry logic
            downloaded_size = self._perform_http_download(url, local_path)
            size_mb = downloaded_size / (1024 * 1024)
            
            return {
                'success': True,
                'file_path': str(local_path),
                'size_mb': size_mb
            }
            
        except requests.exceptions.RequestException as e:
            return {
                'success': False,
                'error': f"Download failed: {str(e)}"
            }
        except Exception as e:
            return {
                'success': False,
                'error': f"Unexpected error: {str(e)}"
            }
    
    def resume_failed_downloads(self) -> Dict:
        """Resume processing of failed download queue items."""
        failed_items = self.storage.get_download_queue(status='failed')
        if not failed_items:
            self.logger.info("No failed downloads to resume")
            return {"resumed": 0}
        
        self.logger.info(f"Resuming {len(failed_items)} failed downloads...")
        
        # Reset failed items to queued status
        for item in failed_items:
            self.storage.update_queue_item(
                item['id'],
                status='queued',
                error_message=None
            )
        
        return {"resumed": len(failed_items)}
    
    def reset_stuck_downloads(self) -> Dict:
        """Reset stuck active downloads back to queued status."""
        active_items = self.storage.get_download_queue(status='active')
        if not active_items:
            self.logger.info("No stuck downloads to reset")
            return {"reset": 0}
        
        self.logger.info(f"Resetting {len(active_items)} stuck active downloads...")
        
        # Reset active items to queued status
        for item in active_items:
            self.storage.update_queue_item(
                item['id'],
                status='queued',
                error_message=None,
                progress_percent=0
            )
        
        return {"reset": len(active_items)}
    
    def _process_batch_updates(self, updates: List[Dict]) -> None:
        """Process a batch of queue item updates efficiently."""
        if not updates:
            return
            
        try:
            # Use a single transaction for all updates
            with self.storage._get_connection() as conn:
                cursor = conn.cursor()
                for update in updates:
                    cursor.execute(
                        """
                        UPDATE download_queue 
                        SET status = ?, progress_percent = ?, error_message = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                        """,
                        (update['status'], update.get('progress_percent'), 
                         update.get('error_message'), update['id'])
                    )
                conn.commit()
            self.logger.debug(f"Batch updated {len(updates)} queue items")
        except Exception as e:
            self.logger.error(f"Error in batch update: {e}")
            # Fallback to individual updates if batch fails
            for update in updates:
                try:
                    self.storage.update_queue_item(
                        update['id'],
                        status=update['status'],
                        progress_percent=update.get('progress_percent'),
                        error_message=update.get('error_message')
                    )
                except Exception as fallback_error:
                    self.logger.error(f"Fallback update failed for {update['id']}: {fallback_error}")
    
    def get_download_stats(self) -> Dict:
        """Get comprehensive download statistics."""
        queue_stats = self.storage.get_download_queue_stats()
        storage_stats = self.storage.get_storage_stats()
        
        # Calculate disk usage
        total_disk_usage = 0
        if self.download_dir.exists():
            for file_path in self.download_dir.rglob('*'):
                if file_path.is_file():
                    total_disk_usage += file_path.stat().st_size
        
        disk_usage_mb = total_disk_usage / (1024 * 1024)
        
        return {
            'queue_stats': queue_stats,
            'storage_stats': storage_stats,
            'disk_usage_mb': round(disk_usage_mb, 2),
            'download_directory': str(self.download_dir),
            'files_on_disk': sum(1 for _ in self.download_dir.rglob('*') if _.is_file()) if self.download_dir.exists() else 0
        }
    
    def cleanup_incomplete_downloads(self) -> Dict:
        """Remove incomplete or corrupted download files."""
        cleaned_files = 0
        freed_space_mb = 0
        
        if not self.download_dir.exists():
            return {"cleaned_files": 0, "freed_space_mb": 0}
        
        # Look for files that might be incomplete
        for file_path in self.download_dir.rglob('*'):
            if file_path.is_file():
                # Check for zero-byte files
                if file_path.stat().st_size == 0:
                    self.logger.debug(f"Removing zero-byte file: {file_path}")
                    file_path.unlink()
                    cleaned_files += 1
                
                # Check for very small PDF files (likely corrupted)
                elif file_path.suffix.lower() == '.pdf' and file_path.stat().st_size < 1024:
                    self.logger.debug(f"Removing suspiciously small PDF: {file_path}")
                    size_mb = file_path.stat().st_size / (1024 * 1024)
                    freed_space_mb += size_mb
                    file_path.unlink()
                    cleaned_files += 1
        
        return {
            "cleaned_files": cleaned_files,
            "freed_space_mb": round(freed_space_mb, 2)
        }