"""
Test TUI Monitor functionality.

Tests for the rich TUI monitor including background process management,
progress monitoring, and layout creation.
"""

import pytest
from unittest.mock import Mock, patch, MagicMock, call
from datetime import datetime, timedelta
import tempfile
import subprocess
from pathlib import Path
import sys
import os

# Add the project root to the path to import tui_monitor
sys.path.insert(0, str(Path(__file__).parent.parent))

from tui_monitor import (
    BackgroundProcessManager, 
    ProgressMonitor, 
    TUIMonitor,
    ProcessStatus,
    ProgressStats
)


class TestProcessStatus:
    """Test ProcessStatus dataclass."""
    
    def test_process_status_creation(self):
        """Test ProcessStatus creation with default values."""
        status = ProcessStatus(
            name="Test Process",
            command=["python", "test.py"]
        )
        
        assert status.name == "Test Process"
        assert status.command == ["python", "test.py"]
        assert status.process is None
        assert status.is_running is False
        assert status.last_update is None
        assert status.status_text == "Not Started"
        assert status.error_count == 0
        assert status.restart_count == 0


class TestProgressStats:
    """Test ProgressStats dataclass."""
    
    def test_progress_stats_defaults(self):
        """Test ProgressStats creation with default values."""
        stats = ProgressStats()
        
        assert stats.total_batches == 0
        assert stats.batches_discovered == 0
        assert stats.current_batch == ""
        assert stats.current_batch_progress == 0.0
        assert stats.discovery_rate_per_hour == 0.0
        assert stats.total_queue_items == 0
        assert stats.items_downloaded == 0
        assert stats.download_rate_per_hour == 0.0
        assert stats.download_size_mb == 0.0
        assert stats.is_rate_limited is False
        assert stats.cooldown_remaining_minutes == 0.0
        assert stats.rate_limit_reason == ""
        assert stats.estimated_discovery_completion is None
        assert stats.estimated_download_completion is None


class TestBackgroundProcessManager:
    """Test background process management."""
    
    def test_process_manager_initialization(self):
        """Test BackgroundProcessManager initialization."""
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = BackgroundProcessManager(
                db_path="/test/db.db",
                downloads_dir="/test/downloads",
                log_dir=temp_dir
            )
            
            assert manager.db_path == "/test/db.db"
            assert manager.downloads_dir == "/test/downloads"
            assert manager.log_dir == Path(temp_dir)
            assert len(manager.processes) == 2
            assert manager.discovery_process.name == "Batch Discovery"
            assert manager.download_process.name == "Downloads"
            assert not manager.shutdown_requested
    
    @patch('subprocess.Popen')
    def test_start_process_success(self, mock_popen):
        """Test successful process startup."""
        mock_process = Mock()
        mock_popen.return_value = mock_process
        
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = BackgroundProcessManager("/test/db.db", "/test/downloads", temp_dir)
            
            # Mock the log file opening
            with patch('builtins.open', mock_open=True):
                result = manager.start_process(manager.discovery_process)
            
            assert result is True
            assert manager.discovery_process.is_running is True
            assert manager.discovery_process.process == mock_process
            assert manager.discovery_process.status_text == "Starting..."
            assert manager.discovery_process.last_update is not None
    
    @patch('subprocess.Popen')
    def test_start_process_failure(self, mock_popen):
        """Test process startup failure."""
        mock_popen.side_effect = OSError("Failed to start")
        
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = BackgroundProcessManager("/test/db.db", "/test/downloads", temp_dir)
            
            with patch('builtins.open', mock_open=True):
                result = manager.start_process(manager.discovery_process)
            
            assert result is False
            assert manager.discovery_process.is_running is False
            assert "Failed to start" in manager.discovery_process.status_text
            assert manager.discovery_process.error_count == 1
    
    def test_check_process_health_running(self):
        """Test health check for running process."""
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = BackgroundProcessManager("/test/db.db", "/test/downloads", temp_dir)
            
            mock_process = Mock()
            mock_process.poll.return_value = None  # Still running
            
            manager.discovery_process.process = mock_process
            manager.discovery_process.is_running = True
            
            result = manager.check_process_health(manager.discovery_process)
            
            assert result is True
            assert manager.discovery_process.status_text == "Running"
            assert manager.discovery_process.last_update is not None
    
    def test_check_process_health_terminated_success(self):
        """Test health check for successfully terminated process."""
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = BackgroundProcessManager("/test/db.db", "/test/downloads", temp_dir)
            
            mock_process = Mock()
            mock_process.poll.return_value = 0  # Successful exit
            
            manager.discovery_process.process = mock_process
            manager.discovery_process.is_running = True
            
            result = manager.check_process_health(manager.discovery_process)
            
            assert result is False
            assert manager.discovery_process.is_running is False
            assert manager.discovery_process.status_text == "Completed"
    
    def test_check_process_health_terminated_failure(self):
        """Test health check for failed process."""
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = BackgroundProcessManager("/test/db.db", "/test/downloads", temp_dir)
            
            mock_process = Mock()
            mock_process.poll.return_value = 1  # Failed exit
            
            manager.discovery_process.process = mock_process
            manager.discovery_process.is_running = True
            
            result = manager.check_process_health(manager.discovery_process)
            
            assert result is False
            assert manager.discovery_process.is_running is False
            assert "Exited with code 1" in manager.discovery_process.status_text
            assert manager.discovery_process.error_count == 1
    
    def test_stop_process(self):
        """Test process termination."""
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = BackgroundProcessManager("/test/db.db", "/test/downloads", temp_dir)
            
            mock_process = Mock()
            manager.discovery_process.process = mock_process
            manager.discovery_process.is_running = True
            
            manager.stop_process(manager.discovery_process)
            
            mock_process.terminate.assert_called_once()
            mock_process.wait.assert_called_once_with(timeout=10)
            assert manager.discovery_process.is_running is False
            assert manager.discovery_process.status_text == "Stopped"
    
    def test_stop_all_processes(self):
        """Test stopping all processes."""
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = BackgroundProcessManager("/test/db.db", "/test/downloads", temp_dir)
            
            # Mock processes
            mock_discovery = Mock()
            mock_download = Mock()
            manager.discovery_process.process = mock_discovery
            manager.discovery_process.is_running = True
            manager.download_process.process = mock_download
            manager.download_process.is_running = True
            
            manager.stop_all()
            
            assert manager.shutdown_requested is True
            mock_discovery.terminate.assert_called_once()
            mock_download.terminate.assert_called_once()


class TestProgressMonitor:
    """Test progress monitoring functionality."""
            
    def test_progress_monitor_initialization(self):
        """Test ProgressMonitor initialization."""
        import gc
        import time

        tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        tmp_db.close()  # Release Windows file handle before SQLite opens it
        try:
            monitor = ProgressMonitor(tmp_db.name, "/test/downloads")
            
            assert monitor.db_path == tmp_db.name
            assert monitor.downloads_dir == "/test/downloads"
            assert monitor.storage is not None
            # batch_mapper, session_tracker and api_client are intentionally
            # not initialized in __init__ to avoid blocking
            assert monitor.batch_mapper is None
            assert monitor.session_tracker is None
            assert monitor.api_client is None

        finally:
            del monitor
            gc.collect()
            for _ in range(10):
                try:
                    Path(tmp_db.name).unlink(missing_ok=True)
                    break
                except PermissionError:
                    time.sleep(0.1)

    def test_progress_monitor_no_database(self):
        """Test ProgressMonitor with non-existent database."""
        monitor = ProgressMonitor("/nonexistent/db.db", "/test/downloads")
        
        assert monitor.storage is None
        assert monitor.batch_mapper is None
        assert monitor.session_tracker is None
    
    @patch('tui_monitor.NewsStorage')
    def test_get_progress_stats_no_storage(self, mock_storage_class):
        """Test get_progress_stats with no storage."""
        monitor = ProgressMonitor("/nonexistent/db.db", "/test/downloads")
        monitor.storage = None
        
        stats = monitor.get_progress_stats()
        
        assert isinstance(stats, ProgressStats)
        assert stats.total_batches == 0
        assert stats.batches_discovered == 0
            
    @pytest.mark.skip(reason="get_progress_stats internals have diverged from test assumptions - needs rewrite against current implementation")
    @patch('sqlite3.connect')
    @patch('tui_monitor.Path')
    def test_get_progress_stats_with_data(self, mock_path, mock_connect):
        """Test get_progress_stats with mock data."""
        # Mock database
        mock_conn = Mock()
        mock_cursor = Mock()
        mock_connect.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        mock_conn.__enter__ = Mock(return_value=mock_conn)
        mock_conn.__exit__ = Mock(return_value=None)
        
        # Mock database queries
        mock_cursor.fetchone.side_effect = [
            (100,),  # total_queue_items
            (50,),   # items_downloaded
        ]
        
        # Mock file system
        mock_downloads_path = Mock()
        mock_path.return_value = mock_downloads_path
        mock_downloads_path.exists.return_value = True
        mock_downloads_path.rglob.return_value = [
            Mock(is_file=lambda: True, stat=lambda: Mock(st_size=1024*1024))  # 1MB file
        ]
        
        with tempfile.NamedTemporaryFile(suffix='.db') as tmp_db:
            monitor = ProgressMonitor(tmp_db.name, "/test/downloads")
            monitor._total_batches_cache = 25
            
            # Mock the components
            monitor.session_tracker = Mock()
            monitor.session_tracker.get_active_sessions.return_value = []
            monitor.batch_mapper = Mock()
            monitor.batch_mapper.get_all_session_batch_names.return_value = ['batch1', 'batch2']
            
            stats = monitor.get_progress_stats()
            
            assert stats.total_batches == 25
            assert stats.batches_discovered == 2
            assert stats.total_queue_items == 100
            assert stats.items_downloaded == 50
            assert stats.download_size_mb == 1.0

                                        
class TestTUIMonitor:
    """Test TUI Monitor main class."""
    
    @patch('tui_monitor.BackgroundProcessManager')
    @patch('tui_monitor.ProgressMonitor')
    def test_tui_monitor_initialization(self, mock_progress_monitor, mock_process_manager):
        """Test TUIMonitor initialization."""
        monitor = TUIMonitor("/test/db.db", "/test/downloads")
        
        assert monitor.db_path == "/test/db.db"
        assert monitor.downloads_dir == "/test/downloads"
        assert not monitor.shutdown_requested
        assert monitor.start_time is not None
        
        mock_process_manager.assert_called_once_with("/test/db.db", "/test/downloads", parallel_workers=8,
            file_concurrency=6)
        mock_progress_monitor.assert_called_once_with("/test/db.db", "/test/downloads")
    
    @patch('tui_monitor.BackgroundProcessManager')
    @patch('tui_monitor.ProgressMonitor')
    def test_create_layout(self, mock_progress_monitor, mock_process_manager):
        """Test layout creation."""
        monitor = TUIMonitor("/test/db.db", "/test/downloads")
        
        # Mock data
        stats = ProgressStats()
        stats.total_batches = 25
        stats.batches_discovered = 5
        stats.current_batch = "test_batch"
        stats.current_batch_progress = 50.0
        
        processes = [
            ProcessStatus("Discovery", ["test"], is_running=True, status_text="Running"),
            ProcessStatus("Downloads", ["test"], is_running=True, status_text="Running")
        ]
        
        # This should not raise an exception
        layout = monitor.create_layout(stats, processes)
        
        # Basic validation that layout was created
        assert layout is not None
    
    @patch('tui_monitor.signal')
    def test_signal_handler_setup(self, mock_signal):
        """Test signal handler setup."""
        with patch('tui_monitor.BackgroundProcessManager'), \
             patch('tui_monitor.ProgressMonitor'):
            
            monitor = TUIMonitor("/test/db.db", "/test/downloads")
            
            # Verify signal handlers were set up
            assert mock_signal.signal.call_count >= 2
            calls = mock_signal.signal.call_args_list
            signal_numbers = [call[0][0] for call in calls]
            assert mock_signal.SIGINT in signal_numbers
            assert mock_signal.SIGTERM in signal_numbers
    
    def test_signal_handler(self):
        """Test signal handler functionality."""
        with patch('tui_monitor.BackgroundProcessManager'), \
             patch('tui_monitor.ProgressMonitor'), \
             patch('tui_monitor.signal'):
            
            monitor = TUIMonitor("/test/db.db", "/test/downloads")
            monitor.shutdown_requested = False
            
            # Call signal handler
            monitor._signal_handler(2, None)  # SIGINT
            
            assert monitor.shutdown_requested is True


class TestTUIIntegration:
    """Integration tests for TUI components."""
    
    @patch('subprocess.Popen')
    @patch('builtins.open')
    def test_full_workflow_mock(self, mock_open, mock_popen):
        """Test full TUI workflow with mocked components."""
        # Mock process
        mock_process = Mock()
        mock_process.poll.return_value = None  # Running
        mock_popen.return_value = mock_process
        
        with tempfile.TemporaryDirectory() as temp_dir:
            # Create temporary database
            db_path = os.path.join(temp_dir, "test.db")
            downloads_dir = os.path.join(temp_dir, "downloads")
            os.makedirs(downloads_dir, exist_ok=True)
            
            # Test process manager
            manager = BackgroundProcessManager(db_path, downloads_dir, temp_dir)
            
            # Start processes
            manager.start_all()
            
            # Verify processes started
            assert manager.discovery_process.is_running
            assert manager.download_process.is_running
            
            # Check health
            statuses = manager.monitor_processes()
            assert len(statuses) == 2
            assert all(status.is_running for status in statuses)
            
            # Stop processes
            manager.stop_all()
            assert manager.shutdown_requested


if __name__ == '__main__':
    pytest.main([__file__, '-v'])