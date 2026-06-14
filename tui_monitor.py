#!/usr/bin/env python3
"""
Rich TUI Monitor for Batch Discovery and Downloads

A comprehensive terminal user interface for monitoring automated batch discovery
and download processes. Runs both processes in the background and provides
real-time progress updates, status monitoring, and estimated completion times.
"""

import sys
import os
import time
import subprocess
import threading
import signal
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from queue import Queue, Empty

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / 'src'))

from newsagger.storage import NewsStorage
# Import BatchMapper and LocApiClient lazily to avoid blocking during module import

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.progress import (
    Progress, 
    SpinnerColumn, 
    TextColumn, 
    BarColumn, 
    TaskProgressColumn,
    TimeRemainingColumn,
    MofNCompleteColumn
)
from rich.table import Table
from rich.layout import Layout
from rich.text import Text
from rich.align import Align


@dataclass
class ProcessStatus:
    """Status information for a background process."""
    name: str
    command: List[str]
    process: Optional[subprocess.Popen] = None
    is_running: bool = False
    last_update: Optional[datetime] = None
    start_time: Optional[datetime] = None
    status_text: str = "Not Started"
    error_count: int = 0
    restart_count: int = 0
    stall_detected_time: Optional[datetime] = None
    last_auto_restart: Optional[datetime] = None
    auto_restart_count: int = 0


@dataclass
class ProgressStats:
    """Progress statistics for monitoring."""
    # Batch Discovery
    total_batches: int = 0
    batches_discovered: int = 0
    current_batch: str = ""
    current_batch_progress: float = 0.0
    discovery_rate_per_hour: float = 0.0
    
    # Real-time discovery progress
    current_issue_index: int = 0
    total_issues_in_batch: int = 0
    total_pages_discovered: int = 0
    total_pages_enqueued: int = 0
    discovery_rate_per_minute: float = 0.0
    issues_per_minute: float = 0.0
    
    # Downloads
    total_queue_items: int = 0
    items_downloaded: int = 0
    download_rate_per_hour: float = 0.0
    download_size_mb: float = 0.0
    
    # Rate Limiting
    is_rate_limited: bool = False
    cooldown_remaining_minutes: float = 0.0
    rate_limit_reason: str = ""
    
    # Enhanced Rate Limiting Details
    current_request_delay: float = 0.0
    requests_per_minute: int = 0
    last_request_time: Optional[datetime] = None
    next_request_time: Optional[datetime] = None
    captcha_backoff_active: bool = False
    backoff_multiplier: float = 1.0
    
    # Estimates
    estimated_discovery_completion: Optional[datetime] = None
    estimated_download_completion: Optional[datetime] = None
    
    # Stall Detection
    downloads_stalled: bool = False
    downloads_stall_reason: str = ""
    last_download_time: Optional[datetime] = None


class BackgroundProcessManager:
    """Manages background discovery and download processes."""
    
    def __init__(self, db_path: str, downloads_dir: str, log_dir: str = "logs", parallel_workers: int = 8, file_concurrency: int = 6):
        self.db_path = db_path
        self.downloads_dir = downloads_dir
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(exist_ok=True)
        self.parallel_workers = parallel_workers
        self.file_concurrency = file_concurrency
        
        # Process definitions - use absolute path to main.py
        main_py_path = str(Path(__file__).parent / "main.py")
        
        self.discovery_process = ProcessStatus(
            name="Batch Discovery",
            command=[
                sys.executable, main_py_path, "discover-via-batches", 
                "--auto-enqueue"
            ]
        )
        
        self.download_process = ProcessStatus(
            name="Downloads",
            command=[
                sys.executable, main_py_path, "process-downloads",
                "--max-items", "50", "--continuous", "--max-idle-minutes", "30",
                "--parallel-workers", str(self.parallel_workers),
                "--file-concurrency", str(self.file_concurrency)
            ]
        )
        
        self.processes = [self.discovery_process, self.download_process]
        self.shutdown_requested = False
        
    def start_process(self, process_status: ProcessStatus) -> bool:
        """Start a background process."""
        if process_status.is_running:
            return True
            
        try:
            # Setup log files
            log_file = self.log_dir / f"{process_status.name.lower().replace(' ', '_')}.log"
            
            # Start process with logging
            # Set up environment to ensure correct paths
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path(__file__).parent / "src")
            env["DATABASE_PATH"] = self.db_path
            env["DOWNLOAD_DIR"] = self.downloads_dir
            
            # Use current working directory
            working_dir = Path.cwd()
            
            process_status.process = subprocess.Popen(
                process_status.command,
                stdout=open(log_file, 'a'),
                stderr=subprocess.STDOUT,
                cwd=working_dir,
                env=env
            )
            
            process_status.is_running = True
            process_status.status_text = "Starting..."
            process_status.last_update = datetime.now()
            process_status.start_time = datetime.now()
            return True
            
        except Exception as e:
            process_status.status_text = f"Failed to start: {e}"
            process_status.error_count += 1
            return False
    
    def check_process_health(self, process_status: ProcessStatus) -> bool:
        """Check if a process is still running and healthy."""
        if not process_status.process:
            return False
            
        # Check if process is still alive
        poll_result = process_status.process.poll()
        if poll_result is not None:
            # Process has terminated
            process_status.is_running = False
            if poll_result == 0:
                process_status.status_text = "Completed"
            else:
                process_status.status_text = f"Exited with code {poll_result}"
                process_status.error_count += 1
            return False
        
        process_status.status_text = "Running"
        process_status.last_update = datetime.now()
        return True
    
    def restart_process(self, process_status: ProcessStatus) -> bool:
        """Restart a failed process."""
        if process_status.is_running:
            self.stop_process(process_status)
        
        process_status.restart_count += 1
        return self.start_process(process_status)
    
    def stop_process(self, process_status: ProcessStatus):
        """Stop a running process."""
        if process_status.process and process_status.is_running:
            try:
                process_status.process.terminate()
                process_status.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process_status.process.kill()
                process_status.process.wait()
            except:
                pass
            
            process_status.is_running = False
            process_status.status_text = "Stopped"
    
    def start_all(self):
        """Start all background processes."""
        for process_status in self.processes:
            self.start_process(process_status)
    
    def stop_all(self):
        """Stop all background processes."""
        self.shutdown_requested = True
        for process_status in self.processes:
            self.stop_process(process_status)
    
    def monitor_processes(self) -> List[ProcessStatus]:
        """Monitor all processes and restart if needed."""
        for process_status in self.processes:
            if not self.check_process_health(process_status) and not self.shutdown_requested:
                # Auto-restart failed processes (with backoff)
                if process_status.restart_count < 5:
                    time.sleep(min(2 ** process_status.restart_count, 60))  # Exponential backoff
                    self.restart_process(process_status)
        
        return self.processes


class ProgressMonitor:
    """Monitors database and provides real-time progress statistics."""
    
    def __init__(self, db_path: str, downloads_dir: str):
        self.db_path = db_path
        self.downloads_dir = downloads_dir
        self.storage = NewsStorage(db_path) if Path(db_path).exists() else None
        # Don't initialize API client or batch components during __init__ to avoid blocking
        self.api_client = None
        self.batch_mapper = None
        self.session_tracker = None
        
        self._last_stats = ProgressStats()
        self._total_batches_cache = None
        self._cache_time = None
        self._downloads_size_cache = None
        self._downloads_size_cache_time = None
        self._stats_cache = None
        self._stats_cache_time = None
    
    def get_progress_stats(self) -> ProgressStats:
        """Get current progress statistics using simple database queries with timeout protection."""
        if not self.storage or not Path(self.db_path).exists():
            return ProgressStats()
        
        # Use cache to reduce database hits on slow storage (5 second cache)
        from datetime import datetime as dt
        now = dt.now()
        if (self._stats_cache is not None and 
            self._stats_cache_time is not None and
            (now - self._stats_cache_time).total_seconds() < 5):
            return self._stats_cache
        
        stats = ProgressStats()
        
        try:
            # Set shorter timeout for slow storage
            timeout = 1.0  # 1 second timeout for slow drives
            # Use basic database queries instead of complex batch tracking
            stats.total_batches = 25  # Known estimate from previous analysis
            
            # Get download queue statistics with timeout protection
            try:
                with sqlite3.connect(self.db_path, timeout=timeout) as conn:
                    cursor = conn.cursor()
                    
                    # Use a single optimized query instead of multiple queries
                    cursor.execute("""
                        SELECT 
                            SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as completed,
                            SUM(CASE WHEN status = 'queued' THEN 1 ELSE 0 END) as queued,
                            SUM(CASE WHEN status = 'in_progress' THEN 1 ELSE 0 END) as in_progress,
                            SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) as active,
                            COUNT(*) as total
                        FROM download_queue
                    """)
                    
                    result = cursor.fetchone()
                    if result:
                        stats.items_downloaded = result[0] or 0
                        queued_count = result[1] or 0
                        in_progress_count = result[2] or 0
                        active_count = result[3] or 0
                        stats.total_queue_items = result[4] or 0
                
            except Exception as e:
                # TODO: Close any open connection in the exception
                # Fallback to storage methods if direct queries fail
                try:
                    queued_items = self.storage.get_download_queue(status='queued')
                    completed_items = self.storage.get_download_queue(status='completed')
                    in_progress_items = self.storage.get_download_queue(status='in_progress')
                    
                    stats.total_queue_items = len(queued_items) + len(completed_items) + len(in_progress_items)
                    stats.items_downloaded = len(completed_items)
                except:
                    # If both fail, show zero values
                    stats.total_queue_items = 0
                    stats.items_downloaded = 0
            
            # Get batch discovery data with timeout protection
            try:
                with sqlite3.connect(self.db_path, timeout=timeout) as conn:
                    cursor = conn.cursor()
                    
                    # Single optimized query for all batch session data
                    cursor.execute("""
                        SELECT 
                            current_batch_name,
                            current_batch_index,
                            total_batches,
                            current_issue_index,
                            total_issues_in_batch,
                            total_pages_discovered,
                            total_pages_enqueued,
                            datetime(updated_at, 'localtime') as last_update
                        FROM batch_discovery_sessions
                        WHERE session_name = 'batch_discovery_main'
                        ORDER BY updated_at DESC
                        LIMIT 1
                    """)
                    session = cursor.fetchone()
                    if session:
                        stats.current_batch = session[0] or ""
                        stats.batches_discovered = session[1] or 0
                        stats.total_batches = session[2] or 25
                        stats.current_issue_index = session[3] or 0
                        stats.total_issues_in_batch = session[4] or 0
                        stats.total_pages_discovered = session[5] or 0
                        stats.total_pages_enqueued = session[6] or 0
                        
                        # Calculate batch progress
                        if stats.total_batches > 0:
                            stats.current_batch_progress = (stats.batches_discovered / stats.total_batches) * 100
                        
                        # Calculate discovery rates from recent activity
                        if session[7]:  # last_update timestamp
                            from datetime import datetime as dt2, timedelta
                            last_update = dt2.strptime(session[7], '%Y-%m-%d %H:%M:%S')
                            time_since_update = (dt2.now() - last_update).total_seconds()
                            
                            # Estimate rate based on recent activity and issue processing speed
                            # If the session was updated recently, discovery is active
                            if time_since_update < 10:  # Updated within last 10 seconds
                                # Estimate based on average issue processing (8 pages per issue, 5 seconds per issue)
                                estimated_pages_per_minute = (8 * 60) / 5  # ~96 pages/min
                                estimated_issues_per_minute = 60 / 5      # ~12 issues/min
                                
                                stats.discovery_rate_per_minute = estimated_pages_per_minute
                                stats.discovery_rate_per_hour = estimated_pages_per_minute * 60
                                stats.issues_per_minute = estimated_issues_per_minute
                            
                            # Try to get actual rate from download queue additions
                            cursor.execute("""
                                SELECT COUNT(*) 
                                FROM download_queue 
                                WHERE created_at > datetime('now', '-1 minute')
                            """)
                            recent_additions = cursor.fetchone()[0]
                            if recent_additions > 0:
                                # Override estimate with actual data
                                stats.discovery_rate_per_minute = recent_additions
                                stats.discovery_rate_per_hour = recent_additions * 60
                    
            except Exception as e:
                # TODO: Close any open connection in the exception
                # If query fails, keep default values
                pass
            
            # Simple rate limiting check - if we have very few recent items, might be rate limited
            # This is a simplified heuristic using the actual counts
            if stats.total_queue_items > 0:
                queued_ratio = (stats.total_queue_items - stats.items_downloaded) / stats.total_queue_items
                if queued_ratio > 0.9:  # More than 90% still queued suggests slow processing
                    stats.is_rate_limited = True
                    stats.rate_limit_reason = "High queue backlog (possible rate limiting)"
                    stats.cooldown_remaining_minutes = 0  # Unknown without session tracking
            
            # Check database estimates first, fall back to directory scan if needed
            try:
                with sqlite3.connect(self.db_path, timeout=timeout) as conn:
                    cursor = conn.cursor()
                    
                    cursor.execute("""
                        SELECT SUM(estimated_size_mb) 
                        FROM download_queue 
                        WHERE status = 'completed'
                    """)
                    result = cursor.fetchone()
                    db_estimate = result[0] if result and result[0] else 0
                    
                
                # If database estimate is missing/zero, calculate from directory
                if db_estimate <= 0:
                    stats.download_size_mb = self._calculate_downloads_directory_size()
                else:
                    stats.download_size_mb = db_estimate
                    
            except:
                # TODO: close any open connection in the exception
                # Final fallback - try directory scan, then estimate
                try:
                    stats.download_size_mb = self._calculate_downloads_directory_size()
                except:
                    # Last resort estimate
                    stats.download_size_mb = stats.items_downloaded * 3.0  # ~3MB average per item
            
        except Exception as e:
            # Return last known stats if database query fails
            stats = self._last_stats
        
        # Calculate estimates based on current progress
        self._calculate_estimates(stats)
        
        # Collect rate limiting data
        self._collect_rate_limiting_data(stats)
        
        # Cache the results
        self._stats_cache = stats
        self._stats_cache_time = now
        self._last_stats = stats
        return stats
    
    def _calculate_downloads_directory_size(self) -> float:
        """Calculate total size of downloads directory in MB with caching."""
        now = datetime.now()
        
        # Cache for 30 seconds to avoid expensive directory scans on every update
        if (self._downloads_size_cache is not None and 
            self._downloads_size_cache_time is not None and
            (now - self._downloads_size_cache_time).total_seconds() < 30):
            return self._downloads_size_cache
        
        if not self.downloads_dir or not Path(self.downloads_dir).exists():
            self._downloads_size_cache = 0.0
            self._downloads_size_cache_time = now
            return 0.0
        
        total_size_bytes = 0
        downloads_path = Path(self.downloads_dir)
        
        try:
            # Walk through all files in downloads directory
            for file_path in downloads_path.rglob('*'):
                if file_path.is_file():
                    try:
                        total_size_bytes += file_path.stat().st_size
                    except (OSError, PermissionError):
                        # Skip files we can't read
                        continue
        except Exception:
            # If directory traversal fails, return cached value or 0
            if self._downloads_size_cache is not None:
                return self._downloads_size_cache
            return 0.0
        
        # Convert bytes to MB and cache the result
        total_size_mb = total_size_bytes / (1024 * 1024)
        self._downloads_size_cache = total_size_mb
        self._downloads_size_cache_time = now
        
        return total_size_mb
    
    def _calculate_batch_progress(self, session_info: Dict) -> float:
        """Calculate progress within current batch."""
        current_index = session_info.get('current_batch_index', 0)
        total_batches = session_info.get('total_batches', 1)
        
        if total_batches > 0:
            return (current_index / total_batches) * 100
        return 0.0
    
    def _calculate_estimates(self, stats: ProgressStats):
        """Calculate estimated completion times from database activity."""
        now = datetime.now()
        
        # Try to calculate actual rates from database session activity
        try:
            with sqlite3.connect(self.db_path, timeout=timeout) as conn:
                cursor = conn.cursor()
                
                # Discovery estimate based on current batch progress and remaining batches
                if stats.batches_discovered > 0 and stats.batches_discovered < stats.total_batches:
                    # Calculate based on current batch being ~81% complete (from logs)
                    # Current batch az_chrysocolla has ~1213 issues, we're at issue ~984 (81%)
                    current_batch_remaining_issues = 1213 - 984  # ~229 issues left in current batch
                    remaining_complete_batches = stats.total_batches - stats.batches_discovered - 1  # -1 for current batch
                    
                    # Estimate 5 seconds per issue (from log patterns) and 1000 issues per batch average
                    seconds_per_issue = 5.0
                    issues_per_batch = 1000  # Conservative average
                    
                    # Time for current batch completion
                    current_batch_seconds = current_batch_remaining_issues * seconds_per_issue
                    
                    # Time for remaining complete batches  
                    remaining_batches_seconds = remaining_complete_batches * issues_per_batch * seconds_per_issue
                    
                    total_seconds_remaining = current_batch_seconds + remaining_batches_seconds
                    
                    if total_seconds_remaining > 0:
                        stats.discovery_rate_per_hour = 3600 / seconds_per_issue  # Issues per hour
                        hours_remaining = total_seconds_remaining / 3600
                        stats.estimated_discovery_completion = now + timedelta(hours=hours_remaining)
                
                # Download estimate based on completed vs queued items
                if stats.items_downloaded > 0 and stats.total_queue_items > stats.items_downloaded:
                    # Get download completion activity in multiple time windows
                    cursor.execute("""
                        SELECT 
                            (SELECT COUNT(*) FROM download_queue WHERE status = 'completed' AND updated_at > datetime('now', '-5 minutes')) as last_5_min,
                            (SELECT COUNT(*) FROM download_queue WHERE status = 'completed' AND updated_at > datetime('now', '-15 minutes')) as last_15_min,
                            (SELECT COUNT(*) FROM download_queue WHERE status = 'completed' AND updated_at > datetime('now', '-1 hour')) as last_hour
                    """)
                    result = cursor.fetchone()
                    completions_5min = result[0] if result else 0
                    completions_15min = result[1] if result else 0
                    completions_hour = result[2] if result else 0
                    
                    # Use the most recent non-zero rate, preferring shorter time windows
                    if completions_5min > 0:
                        # Use 5-minute rate if active
                        stats.download_rate_per_hour = completions_5min * 12  # Scale to hourly
                    elif completions_15min > 0:
                        # Use 15-minute rate if active
                        stats.download_rate_per_hour = completions_15min * 4  # Scale to hourly
                    elif completions_hour > 0:
                        # Use hourly rate as fallback
                        stats.download_rate_per_hour = completions_hour
                    else:
                        # No recent activity - set rate to 0
                        stats.download_rate_per_hour = 0
                        
                    # Check for download stalls and determine reason
                    if completions_5min == 0 and stats.total_queue_items > stats.items_downloaded:
                        stats.downloads_stalled = True
                        
                        # Try to determine why downloads are stalled
                        # Check for CAPTCHA first
                        if stats.captcha_backoff_active:
                            stats.downloads_stall_reason = "CAPTCHA cooldown active"
                        else:
                            # Check for items stuck in progress
                            cursor.execute("""
                                SELECT COUNT(*), MIN(updated_at) 
                                FROM download_queue 
                                WHERE status = 'in_progress'
                            """)
                            in_progress_result = cursor.fetchone()
                            in_progress_count = in_progress_result[0] if in_progress_result else 0
                            oldest_in_progress = in_progress_result[1] if in_progress_result and in_progress_result[1] else None
                            
                            if in_progress_count > 0 and oldest_in_progress:
                                # Check how long items have been in progress
                                oldest_time = datetime.strptime(oldest_in_progress, '%Y-%m-%d %H:%M:%S')
                                stuck_minutes = (now - oldest_time).total_seconds() / 60
                                
                                if stuck_minutes > 10:  # Items stuck for over 10 minutes
                                    stats.downloads_stall_reason = f"{in_progress_count} items stuck in progress for {int(stuck_minutes)}m"
                                else:
                                    stats.downloads_stall_reason = f"Processing {in_progress_count} items (slow network?)"
                            else:
                                # Check if there are any active items
                                cursor.execute("SELECT COUNT(*) FROM download_queue WHERE status = 'active'")
                                active_count = cursor.fetchone()[0]
                                
                                if active_count == 0:
                                    stats.downloads_stall_reason = "No items in active status"
                                else:
                                    stats.downloads_stall_reason = "Unknown - check process logs"
                    else:
                        stats.downloads_stalled = False
                        stats.downloads_stall_reason = ""
                        
                    # Get last download time
                    cursor.execute("""
                        SELECT MAX(updated_at) 
                        FROM download_queue 
                        WHERE status = 'completed'
                    """)
                    last_download_result = cursor.fetchone()
                    if last_download_result and last_download_result[0]:
                        stats.last_download_time = datetime.strptime(last_download_result[0], '%Y-%m-%d %H:%M:%S')
                        
                    # Calculate ETA only if downloads are actually happening
                    if stats.download_rate_per_hour > 0:
                        remaining_items = stats.total_queue_items - stats.items_downloaded
                        hours_remaining = remaining_items / stats.download_rate_per_hour
                        stats.estimated_download_completion = now + timedelta(hours=hours_remaining)
                    else:
                        # Downloads stalled - no ETA
                        stats.estimated_download_completion = None
                
            
        except Exception as e:
            # If we can't calculate from database, don't show estimates
            pass
    
    def _collect_rate_limiting_data(self, stats: ProgressStats):
        """Collect rate limiting data from the RateLimitedRequestManager."""
        try:
            # Import the rate limited client here to avoid circular imports
            from newsagger.rate_limited_client import RateLimitedRequestManager
            
            # Get rate limiting stats from the singleton instance
            rate_manager = RateLimitedRequestManager()
            rate_stats = rate_manager.get_request_stats()
            
            # Populate enhanced rate limiting fields
            stats.current_request_delay = rate_stats['min_delay_seconds']
            stats.requests_per_minute = rate_stats['requests_last_minute']
            
            # Convert timestamps to datetime objects
            if rate_stats['last_request_time']:
                stats.last_request_time = datetime.fromtimestamp(rate_stats['last_request_time'])
            
            if rate_stats['next_request_time']:
                stats.next_request_time = datetime.fromtimestamp(rate_stats['next_request_time'])
            
            # Update existing rate limiting detection with more accurate data
            stats.captcha_backoff_active = rate_stats['captcha_blocked']
            stats.backoff_multiplier = rate_stats['captcha_multiplier']
            
            # Override simple rate limiting detection with actual CAPTCHA status
            if rate_stats['captcha_blocked']:
                stats.is_rate_limited = True
                stats.rate_limit_reason = rate_stats['captcha_reason']
                stats.cooldown_remaining_minutes = rate_stats['captcha_cooling_off_hours'] * 60
            elif rate_stats['at_rate_limit'] or rate_stats['current_delay_active']:
                stats.is_rate_limited = True
                stats.rate_limit_reason = f"Rate limiting active ({rate_stats['requests_last_minute']}/{rate_stats['max_requests_per_minute']} req/min)"
                stats.cooldown_remaining_minutes = 0
            else:
                # Check if we're in a throttled state (0 requests but processes running)
                if stats.requests_per_minute == 0:
                    # Check if discovery is actually making progress to distinguish idle vs active
                    # If recent progress was made, this is normal (discovery process has separate rate limiter)
                    # If no recent progress, this might indicate a problem
                    stats.is_rate_limited = False  # Don't show as rate limited if processes are working
                    stats.rate_limit_reason = "Separate process (discovery active)"
                    stats.cooldown_remaining_minutes = 0
                else:
                    # Not rate limited if we have accurate data
                    if stats.rate_limit_reason == "High queue backlog (possible rate limiting)":
                        stats.is_rate_limited = False
                        stats.rate_limit_reason = ""
            
        except Exception as e:
            # If we can't get rate limiting data, keep existing detection
            pass


class TUIMonitor:
    """Rich TUI for monitoring batch discovery and downloads."""
    
    def __init__(self, db_path: str = "data/newsagger.db", 
                 downloads_dir: str = "downloads",
                 parallel_workers: int = 8,
                 file_concurrency: int = 6):
        self.db_path = db_path
        self.downloads_dir = downloads_dir
        
        self.console = Console()
        self.process_manager = BackgroundProcessManager(db_path, downloads_dir, parallel_workers=parallel_workers, file_concurrency=file_concurrency)
        self.progress_monitor = ProgressMonitor(db_path, downloads_dir)
        
        self.shutdown_requested = False
        self.start_time = datetime.now()
        
        # Setup signal handlers
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
    
    def _signal_handler(self, signum, frame):
        """Handle shutdown signals."""
        self.shutdown_requested = True
    
    def _check_and_handle_download_stalls(self, stats: ProgressStats, processes: List[ProcessStatus]):
        """Check for download stalls and auto-restart if needed."""
        # Find the download process
        download_process = None
        for process in processes:
            if "Downloads" in process.name:
                download_process = process
                break
        
        if not download_process:
            return
        
        now = datetime.now()
        
        # Check if downloads are stalled
        if stats.downloads_stalled and download_process.is_running:
            # Mark when stall was first detected
            if download_process.stall_detected_time is None:
                download_process.stall_detected_time = now
                download_process.status_text = f"Stalled: {stats.downloads_stall_reason}"
            
            # Calculate how long downloads have been stalled
            stall_duration = now - download_process.stall_detected_time
            stall_minutes = stall_duration.total_seconds() / 60
            
            # Auto-restart if stalled for over 60 minutes (but not if CAPTCHA is active)
            if stall_minutes > 60 and not stats.captcha_backoff_active:
                # Check if we've already restarted recently (avoid restart loops)
                if download_process.last_auto_restart:
                    time_since_restart = (now - download_process.last_auto_restart).total_seconds() / 60
                    if time_since_restart < 30:  # Don't restart more than once per 30 minutes
                        return
                
                # Perform auto-restart
                download_process.status_text = "Auto-restarting due to prolonged stall..."
                download_process.last_auto_restart = now
                download_process.auto_restart_count += 1
                
                # Stop the process
                self.process_manager.stop_process(download_process)
                time.sleep(2)  # Brief pause
                
                # Start it again
                self.process_manager.start_process(download_process)
                download_process.stall_detected_time = None  # Reset stall detection
                download_process.status_text = f"Auto-restarted (#{download_process.auto_restart_count})"
        else:
            # Downloads not stalled, reset stall detection
            if download_process.stall_detected_time is not None:
                download_process.stall_detected_time = None
                if download_process.is_running:
                    download_process.status_text = "Running"
    
    def create_layout(self, stats: ProgressStats, processes: List[ProcessStatus]) -> Layout:
        """Create the TUI layout."""
        layout = Layout()
        
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="main"),
            Layout(name="footer", size=3)
        )
        
        layout["main"].split_row(
            Layout(name="left"),
            Layout(name="right")
        )
        
        layout["left"].split_column(
            Layout(name="discovery", ratio=2),
            Layout(name="downloads", ratio=2),
            Layout(name="processes", ratio=2)
        )
        
        layout["right"].split_column(
            Layout(name="stats"),
            Layout(name="rate_limiting"),
            Layout(name="estimates")
        )
        
        # Header
        runtime = datetime.now() - self.start_time
        header_text = Text.assemble(
            ("LoC Archive Monitor", "bold cyan"),
            " | ",
            ("Runtime: ", "dim"),
            (str(runtime).split('.')[0], "green"),
            " | ",
            ("DB: ", "dim"),
            (Path(self.db_path).name, "yellow")
        )
        layout["header"].update(Panel(Align.center(header_text), style="bold"))
        
        # Discovery Panel
        layout["discovery"].update(self._create_discovery_panel(stats))
        
        # Downloads Panel  
        layout["downloads"].update(self._create_downloads_panel(stats))
        
        # Process Status Panel
        layout["processes"].update(self._create_process_panel(processes))
        
        # Statistics Panel
        layout["stats"].update(self._create_stats_panel(stats))
        
        # Rate Limiting Panel
        layout["rate_limiting"].update(self._create_rate_limiting_panel(stats))
        
        # Estimates Panel
        layout["estimates"].update(self._create_estimates_panel(stats))
        
        # Footer
        footer_text = "Press Ctrl+C to stop all processes and exit"
        layout["footer"].update(Panel(Align.center(footer_text), style="dim"))
        
        return layout
    
    def _create_discovery_panel(self, stats: ProgressStats) -> Panel:
        """Create batch discovery progress panel with real-time tqdm-style display."""
        content = []
        
        # Overall batch progress
        batch_progress = Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TaskProgressColumn(),
        )
        
        overall_task = batch_progress.add_task(
            "Batches",
            total=max(stats.total_batches, 1),
            completed=stats.batches_discovered
        )
        content.append(batch_progress)
        
        # Current batch issue progress (tqdm-style)
        if stats.current_batch and stats.total_issues_in_batch > 0:
            issue_progress = Progress(
                TextColumn("[cyan]Issues in {task.fields[batch_name]}"),
                BarColumn(),
                MofNCompleteColumn(),
                TaskProgressColumn(),
                TextColumn("[dim]{task.fields[rate]:.1f} issues/min"),
            )
            
            issue_task = issue_progress.add_task(
                "issues",
                total=stats.total_issues_in_batch,
                completed=stats.current_issue_index,
                batch_name=stats.current_batch[:15],
                rate=stats.issues_per_minute
            )
            content.append(issue_progress)
        
        # Real-time discovery stats (tqdm-style counters)
        stats_progress = Progress(
            TextColumn("[green]Pages Discovered"),
            BarColumn(bar_width=None),
            TextColumn("[bold green]{task.completed:,}"),
            TextColumn("[dim]({task.fields[rate]:.1f}/min)"),
        )
        
        pages_task = stats_progress.add_task(
            "pages",
            total=None,  # Unknown total
            completed=stats.total_pages_discovered,
            rate=stats.discovery_rate_per_minute
        )
        content.append(stats_progress)
        
        # Enqueue progress
        if stats.total_pages_enqueued > 0:
            enqueue_progress = Progress(
                TextColumn("[blue]Pages Enqueued"),
                BarColumn(bar_width=None),
                TextColumn("[bold blue]{task.completed:,}"),
            )
            
            enqueue_task = enqueue_progress.add_task(
                "enqueued",
                total=None,
                completed=stats.total_pages_enqueued
            )
            content.append(enqueue_progress)
        
        return Panel(
            Group(*content),
            title="Real-time Discovery Progress",
            border_style="cyan"
        )
    
    def _create_downloads_panel(self, stats: ProgressStats) -> Panel:
        """Create downloads progress panel with consistent rate display."""
        content = []
        
        # Main download progress
        download_progress = Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TaskProgressColumn(),
        )
        
        download_task = download_progress.add_task(
            "Queue Progress",
            total=max(stats.total_queue_items + stats.items_downloaded, 1),
            completed=stats.items_downloaded
        )
        content.append(download_progress)
        
        # Downloads with rate (matching discovery panel style)
        download_rate_per_minute = stats.download_rate_per_hour / 60 if stats.download_rate_per_hour > 0 else 0
        
        # Show stall status if downloads are stalled
        if stats.downloads_stalled:
            stall_text = Text()
            stall_text.append("⚠️ DOWNLOADS STALLED", style="bold red")
            stall_text.append(f" - {stats.downloads_stall_reason}", style="yellow")
            content.append(stall_text)
            
            # Show time since last download
            if stats.last_download_time:
                time_since = datetime.now() - stats.last_download_time
                minutes_since = int(time_since.total_seconds() / 60)
                content.append(Text(f"Last download: {minutes_since} minutes ago", style="dim"))
            
            # Check if we're approaching auto-restart threshold
            download_process = next((p for p in self.process_manager.processes if "Downloads" in p.name), None)
            if download_process and download_process.stall_detected_time:
                stall_duration = (datetime.now() - download_process.stall_detected_time).total_seconds() / 60
                if stall_duration > 45:  # Show warning when approaching 60 minute threshold
                    remaining = 60 - stall_duration
                    content.append(Text(f"Auto-restart in {int(remaining)} minutes", style="bold yellow"))
        
        download_stats = Progress(
            TextColumn("[blue]Items Downloaded"),
            BarColumn(bar_width=None),
            TextColumn("[bold blue]{task.completed:,}"),
            TextColumn("[dim]({task.fields[rate]:.1f}/min)"),
        )
        
        download_stats_task = download_stats.add_task(
            "downloads",
            total=None,
            completed=stats.items_downloaded,
            rate=download_rate_per_minute
        )
        content.append(download_stats)
        
        # Download size
        if stats.download_size_mb > 0:
            size_progress = Progress(
                TextColumn("[green]Data Downloaded"),
                BarColumn(bar_width=None),
                TextColumn("[bold green]{task.fields[size_text]}"),
            )
            
            if stats.download_size_mb > 1024:
                size_text = f"{stats.download_size_mb/1024:.1f} GB"
            else:
                size_text = f"{stats.download_size_mb:.0f} MB"
            
            size_task = size_progress.add_task(
                "size",
                total=None,
                completed=stats.download_size_mb,
                size_text=size_text
            )
            content.append(size_progress)
        
        return Panel(
            Group(*content),
            title="Downloads Progress",
            border_style="green"
        )
    
    def _create_process_panel(self, processes: List[ProcessStatus]) -> Panel:
        """Create detailed process status panel."""
        table = Table(show_header=True, header_style="bold magenta", box=None)
        table.add_column("Process", style="cyan", no_wrap=True)
        table.add_column("PID", justify="center", style="yellow")
        table.add_column("Status", justify="center")
        table.add_column("Uptime", justify="right", style="blue")
        table.add_column("Restarts", justify="center", style="red")
        table.add_column("Auto-Restarts", justify="center", style="magenta")
        
        for process in processes:
            # Determine status and color
            if process.is_running:
                status = "[green]Running[/green]"
                pid = str(process.process.pid) if process.process else "N/A"
                uptime = self._format_uptime(process.start_time) if process.start_time else "N/A"
            elif process.error_count > 0:
                status = "[red]Error[/red]"
                pid = "---"
                uptime = "---"
            else:
                status = "[yellow]Stopped[/yellow]"
                pid = "---"
                uptime = "---"
            
            # Add command preview and status text
            cmd_preview = " ".join(process.command[2:4]) if len(process.command) > 3 else "N/A"
            name_with_status = f"{process.name}\n[dim]{cmd_preview}[/dim]"
            if process.status_text and process.status_text not in ["Running", "Starting..."]:
                name_with_status += f"\n[italic red]{process.status_text[:40]}[/italic red]"
            
            table.add_row(
                name_with_status,
                pid,
                status,
                uptime,
                str(process.restart_count),
                str(process.auto_restart_count)
            )
        
        # Add summary row
        running_count = sum(1 for p in processes if p.is_running)
        table.add_row(
            "[bold]Total[/bold]",
            "",
            f"[green]{running_count}/{len(processes)}[/green]",
            "",
            "",
            "",
            style="dim"
        )
        
        return Panel(table, title="Processes", border_style="magenta")
    
    def _format_uptime(self, start_time: datetime) -> str:
        """Format process uptime as a human-readable string."""
        if not start_time:
            return "N/A"
        
        uptime = datetime.now() - start_time
        total_seconds = int(uptime.total_seconds())
        
        if total_seconds < 60:
            return f"{total_seconds}s"
        elif total_seconds < 3600:
            minutes = total_seconds // 60
            seconds = total_seconds % 60
            return f"{minutes}m {seconds}s"
        else:
            hours = total_seconds // 3600
            minutes = (total_seconds % 3600) // 60
            return f"{hours}h {minutes}m"
    
    def _create_stats_panel(self, stats: ProgressStats) -> Panel:
        """Create statistics panel."""
        table = Table(show_header=False, box=None)
        table.add_column("Metric", style="cyan")
        table.add_column("Value", style="white")
        
        table.add_row("Total Batches", str(stats.total_batches))
        table.add_row("Discovered", str(stats.batches_discovered))
        table.add_row("Queue Items", f"{stats.total_queue_items:,}")
        table.add_row("Downloaded", f"{stats.items_downloaded:,}")
        
        if stats.download_size_mb > 1024:
            size_display = f"{stats.download_size_mb/1024:.1f} GB"
        else:
            size_display = f"{stats.download_size_mb:.0f} MB"
        table.add_row("Total Size", size_display)
        
        return Panel(table, title="Statistics", border_style="blue")
    
    def _create_estimates_panel(self, stats: ProgressStats) -> Panel:
        """Create estimates panel."""
        content = []
        
        if stats.estimated_discovery_completion:
            eta = stats.estimated_discovery_completion.strftime("%H:%M:%S")
            content.append(f"Discovery ETA: {eta}")
        else:
            content.append("Discovery ETA: Calculating...")
        
        if stats.estimated_download_completion:
            eta = stats.estimated_download_completion.strftime("%H:%M:%S")
            content.append(f"Download ETA: {eta}")
        elif stats.downloads_stalled:
            content.append(f"Download ETA: Stalled - {stats.downloads_stall_reason}")
        else:
            content.append("Download ETA: Calculating...")
        
        if not content:
            content = ["Calculating estimates..."]
        
        return Panel(
            "\n".join(content),
            title="Estimates",
            border_style="yellow"
        )
    
    def _create_rate_limiting_panel(self, stats: ProgressStats) -> Panel:
        """Create rate limiting status panel."""
        content = []
        
        # Get real-time CAPTCHA status from GlobalCaptchaManager
        try:
            from newsagger.rate_limited_client import GlobalCaptchaManager
            captcha_manager = GlobalCaptchaManager()
            captcha_status = captcha_manager.get_status()
            
            if captcha_status['blocked']:
                # Calculate remaining time more accurately
                if captcha_status['last_captcha_time']:
                    current_time = time.time()
                    cooling_off_hours = captcha_status['cooling_off_hours']
                    end_time = captcha_status['last_captcha_time'] + (cooling_off_hours * 3600)
                    remaining_seconds = max(0, end_time - current_time)
                    remaining_minutes = remaining_seconds / 60
                    
                    content.append(f"[bold red]🚫 CAPTCHA DETECTED - ALL OPERATIONS PAUSED[/bold red]")
                    content.append(f"[red]{captcha_status['reason']}[/red]")
                    
                    if remaining_minutes > 0:
                        hours = int(remaining_minutes // 60)
                        minutes = int(remaining_minutes % 60)
                        seconds = int(remaining_seconds % 60)
                        if hours > 0:
                            content.append(f"[bold red]Time Remaining: {hours}h {minutes}m {seconds}s[/bold red]")
                        elif minutes > 0:
                            content.append(f"[bold red]Time Remaining: {minutes}m {seconds}s[/bold red]")
                        else:
                            content.append(f"[bold red]Time Remaining: {seconds}s[/bold red]")
                        
                        # Show when operations will resume
                        resume_time = datetime.fromtimestamp(end_time).strftime("%H:%M:%S")
                        content.append(f"[yellow]Resume Time: {resume_time}[/yellow]")
                    
                    content.append(f"[yellow]Consecutive CAPTCHAs: {captcha_status['consecutive_captchas']}[/yellow]")
                    content.append(f"[yellow]Backoff Multiplier: {captcha_status['cooling_off_multiplier']:.1f}x[/yellow]")
                else:
                    content.append(f"[red]🚫 CAPTCHA Block Active[/red]")
                    content.append(f"[red]{captcha_status['reason']}[/red]")
            else:
                # No CAPTCHA block, show normal rate limiting status
                if stats.is_rate_limited and not stats.captcha_backoff_active:
                    if "Throttled/Idle" in stats.rate_limit_reason:
                        content.append(f"[yellow]⏸️ Throttled/Idle State[/yellow]")
                        content.append(f"[dim]{stats.rate_limit_reason}[/dim]")
                    else:
                        content.append(f"[yellow]⚠️ Rate Limited[/yellow]")
                        content.append(f"[yellow]{stats.rate_limit_reason}[/yellow]")
                else:
                    if stats.requests_per_minute > 0:
                        content.append("[green]✅ Normal Operation[/green]")
                        content.append(f"[green]Active: {stats.requests_per_minute} req/min[/green]")
                    elif "Separate process" in stats.rate_limit_reason:
                        content.append("[green]✅ Background Processes Active[/green]")
                        content.append("[dim]Discovery & downloads running[/dim]")
                    else:
                        content.append("[blue]ℹ️ Monitor Mode[/blue]")
                        content.append("[dim]Monitoring external processes[/dim]")
        except Exception as e:
            # Fallback to original display if we can't get CAPTCHA status
            if stats.is_rate_limited:
                if stats.captcha_backoff_active:
                    content.append(f"[red]🚫 CAPTCHA Blocked[/red]")
                    content.append(f"[red]Reason: {stats.rate_limit_reason}[/red]")
                    if stats.cooldown_remaining_minutes > 0:
                        hours = int(stats.cooldown_remaining_minutes // 60)
                        minutes = int(stats.cooldown_remaining_minutes % 60)
                        content.append(f"[red]Cooldown: {hours}h {minutes}m[/red]")
                    content.append(f"[yellow]Backoff: {stats.backoff_multiplier:.1f}x[/yellow]")
                elif "Throttled/Idle" in stats.rate_limit_reason:
                    content.append(f"[yellow]⏸️ Throttled/Idle State[/yellow]")
                    content.append(f"[dim]{stats.rate_limit_reason}[/dim]")
                else:
                    content.append(f"[yellow]⚠️ Rate Limited[/yellow]")
                    content.append(f"[yellow]{stats.rate_limit_reason}[/yellow]")
            else:
                if stats.requests_per_minute > 0:
                    content.append("[green]✅ Normal Operation[/green]")
                    content.append(f"[green]Active: {stats.requests_per_minute} req/min[/green]")
                elif "Separate process" in stats.rate_limit_reason:
                    content.append("[green]✅ Background Processes Active[/green]")
                    content.append("[dim]Discovery & downloads running[/dim]")
                else:
                    content.append("[blue]ℹ️ Monitor Mode[/blue]")
                    content.append("[dim]Monitoring external processes[/dim]")
        
        # Request rate information
        content.append("")
        content.append(f"[cyan]API Configuration:[/cyan]")
        content.append(f"  Max limit: 12 req/min")
        content.append(f"  Min delay: {stats.current_request_delay:.1f}s")
        if stats.discovery_rate_per_minute > 0:
            content.append(f"  Discovery active: {stats.discovery_rate_per_minute:.1f} pages/min")
        
        # Timing information
        if stats.last_request_time:
            time_str = stats.last_request_time.strftime("%H:%M:%S")
            content.append(f"  Last request: {time_str}")
        
        if stats.next_request_time:
            now = datetime.now()
            if stats.next_request_time > now:
                wait_seconds = (stats.next_request_time - now).total_seconds()
                content.append(f"  Next allowed: {wait_seconds:.1f}s")
            else:
                content.append(f"  Next allowed: Now")
        
        # Visual rate limiting indicator - only show if actually in CAPTCHA cooldown
        if stats.captcha_backoff_active and stats.cooldown_remaining_minutes > 0:
            content.append("")
            # Create a simple progress bar for cooldown
            cooldown_progress = Progress(
                TextColumn("[red]Cooldown"),
                BarColumn(),
                TextColumn("{task.percentage:>3.0f}%"),
            )
            
            # Calculate progress (assuming original cooldown was the current backoff hours)
            total_cooldown_minutes = stats.backoff_multiplier * 60  # 1 hour base * multiplier
            progress_percent = max(0, (total_cooldown_minutes - stats.cooldown_remaining_minutes) / total_cooldown_minutes * 100)
            
            cooldown_task = cooldown_progress.add_task(
                "Cooldown",
                total=100,
                completed=progress_percent
            )
            
            return Panel(
                Group(*[Text(line) for line in content], cooldown_progress),
                title="Rate Limiting",
                border_style="red" if stats.captcha_backoff_active else ("yellow" if stats.is_rate_limited else "green")
            )
        
        return Panel(
            "\n".join(content),
            title="API Status",
            border_style="red" if stats.captcha_backoff_active else ("yellow" if stats.is_rate_limited else "green")
        )
    
    def run(self):
        """Run the TUI monitor."""
        self.console.print("[bold green]Starting LoC Archive Monitor...[/bold green]")
        
        # Start background processes
        self.console.print("[dim]Starting background processes...[/dim]")
        self.process_manager.start_all()
        
        # Give processes time to start
        self.console.print("[dim]Waiting for processes to start...[/dim]")
        time.sleep(2)
        
        self.console.print("[dim]Initializing TUI display...[/dim]")
        with Live(console=self.console, refresh_per_second=2, screen=True) as live:
            while not self.shutdown_requested:
                try:
                    # Monitor processes
                    processes = self.process_manager.monitor_processes()
                    
                    # Get progress stats with timeout protection
                    try:
                        stats = self.progress_monitor.get_progress_stats()
                    except Exception as e:
                        # Use default stats if database is slow
                        stats = ProgressStats()
                        stats.rate_limit_reason = f"Database timeout: {str(e)[:50]}"
                    
                    # Check for download stalls and auto-restart if needed
                    self._check_and_handle_download_stalls(stats, processes)
                    
                    # Update display
                    layout = self.create_layout(stats, processes)
                    live.update(layout)
                    
                    # Sleep before next update
                    time.sleep(1)
                    
                except KeyboardInterrupt:
                    break
                except Exception as e:
                    # Log errors but keep running
                    pass
        
        # Cleanup
        self.console.print("\n[yellow]Shutting down...[/yellow]")
        self.process_manager.stop_all()
        self.console.print("[green]Shutdown complete.[/green]")


def main():
    """Main entry point."""
    import argparse
    
    parser = argparse.ArgumentParser(description="TUI Monitor for LoC Archive Discovery and Downloads")
    parser.add_argument(
        '--db-path',
        type=str,
        default="data/newsagger.db",
        help='Path to database file'
    )
    parser.add_argument(
        '--downloads-dir', 
        type=str,
        default="downloads",
        help='Path to downloads directory'
    )
    parser.add_argument(
        '--parallel-workers',
        type=int,
        default=8,
        help='Number of parallel download workers (default: 8)'
    )
    parser.add_argument(
        '--file-concurrency',
        type=int,
        default=6,
        help='Number of concurrent file downloads per item (default: 6)'
    )
    
    args = parser.parse_args()
    
    # Check if database directory exists
    db_dir = Path(args.db_path).parent
    if not db_dir.exists():
        print(f"Error: Database directory '{db_dir}' does not exist.")
        print("Please create the directory or update the path.")
        sys.exit(1)
    
    # Create and run monitor
    monitor = TUIMonitor(args.db_path, args.downloads_dir, args.parallel_workers, args.file_concurrency)
    monitor.run()


if __name__ == '__main__':
    main()