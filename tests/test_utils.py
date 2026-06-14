"""
Tests for utility functions and decorators.
"""
import time
import logging
import pytest
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch
import requests

from src.newsagger.utils import (
    RetryConfig,
    retry_with_backoff,
    retry_on_request_failure,
    retry_on_network_failure,
    DatabaseOperationMixin,
    ProgressTracker
)


class TestRetryConfig:
    """Test RetryConfig class."""
    
    def test_default_config(self):
        """Test default configuration values."""
        config = RetryConfig()
        assert config.max_attempts == 3
        assert config.base_delay == 1.0
        assert config.exponential_base == 2.0
        assert config.max_delay == 300.0
        assert config.retry_on == (requests.exceptions.RequestException,)
        assert isinstance(config.logger, logging.Logger)
    
    def test_custom_config(self):
        """Test custom configuration values."""
        logger = Mock()
        config = RetryConfig(
            max_attempts=5,
            base_delay=2.0,
            exponential_base=1.5,
            max_delay=600.0,
            retry_on=(ValueError, TypeError),
            logger=logger
        )
        assert config.max_attempts == 5
        assert config.base_delay == 2.0
        assert config.exponential_base == 1.5
        assert config.max_delay == 600.0
        assert config.retry_on == (ValueError, TypeError)
        assert config.logger is logger


class TestRetryWithBackoff:
    """Test retry_with_backoff decorator."""
    
    def test_successful_function_no_retry(self):
        """Test function that succeeds on first attempt."""
        mock_func = Mock(return_value="success")
        
        @retry_with_backoff(max_attempts=3)
        def test_func():
            return mock_func()
        
        result = test_func()
        assert result == "success"
        assert mock_func.call_count == 1
    
    def test_function_succeeds_after_retries(self):
        """Test function that succeeds after some failures."""
        mock_func = Mock()
        mock_func.side_effect = [
            requests.exceptions.RequestException("fail 1"),
            requests.exceptions.RequestException("fail 2"),
            "success"
        ]
        
        with patch('time.sleep'):  # Mock sleep to speed up test
            @retry_with_backoff(max_attempts=3, base_delay=0.1)
            def test_func():
                return mock_func()
            
            result = test_func()
            assert result == "success"
            assert mock_func.call_count == 3
    
    def test_function_fails_all_attempts(self):
        """Test function that fails all retry attempts."""
        mock_func = Mock()
        mock_func.side_effect = requests.exceptions.RequestException("always fails")
        
        with patch('time.sleep'):  # Mock sleep to speed up test
            @retry_with_backoff(max_attempts=3, base_delay=0.1)
            def test_func():
                return mock_func()
            
            with pytest.raises(requests.exceptions.RequestException, match="always fails"):
                test_func()
            
            assert mock_func.call_count == 3
    
    def test_non_retryable_exception_immediate_failure(self):
        """Test that non-retryable exceptions are not retried."""
        mock_func = Mock()
        mock_func.side_effect = ValueError("not retryable")
        
        @retry_with_backoff(max_attempts=3, retry_on=(requests.exceptions.RequestException,))
        def test_func():
            return mock_func()
        
        with pytest.raises(ValueError, match="not retryable"):
            test_func()
        
        assert mock_func.call_count == 1  # No retries
    
    def test_exponential_backoff_delays(self):
        """Test that exponential backoff calculates correct delays."""
        mock_func = Mock()
        mock_func.side_effect = [
            requests.exceptions.RequestException("fail 1"),
            requests.exceptions.RequestException("fail 2"),
            "success"
        ]
        
        with patch('time.sleep') as mock_sleep:
            @retry_with_backoff(max_attempts=3, base_delay=2.0, exponential_base=2.0)
            def test_func():
                return mock_func()
            
            result = test_func()
            assert result == "success"
            
            # Check sleep was called with exponential delays: 2.0, 4.0
            expected_calls = [2.0, 4.0]
            actual_calls = [call[0][0] for call in mock_sleep.call_args_list]
            assert actual_calls == expected_calls
    
    def test_max_delay_cap(self):
        """Test that delays are capped at max_delay."""
        mock_func = Mock()
        mock_func.side_effect = [
            requests.exceptions.RequestException("fail 1"),
            requests.exceptions.RequestException("fail 2"),
            "success"
        ]
        
        with patch('time.sleep') as mock_sleep:
            @retry_with_backoff(
                max_attempts=3, 
                base_delay=100.0, 
                exponential_base=3.0,
                max_delay=150.0
            )
            def test_func():
                return mock_func()
            
            result = test_func()
            assert result == "success"
            
            # Check that delays are capped: 100.0, 150.0 (capped from 300.0)
            actual_calls = [call[0][0] for call in mock_sleep.call_args_list]
            assert actual_calls == [100.0, 150.0]
    
    def test_custom_config_object(self):
        """Test using custom RetryConfig object."""
        mock_func = Mock()
        mock_func.side_effect = [ValueError("fail"), "success"]
        
        config = RetryConfig(
            max_attempts=2,
            base_delay=0.5,
            retry_on=(ValueError,)
        )
        
        with patch('time.sleep'):
            @retry_with_backoff(config)
            def test_func():
                return mock_func()
            
            result = test_func()
            assert result == "success"
            assert mock_func.call_count == 2
    
    def test_preserves_function_metadata(self):
        """Test that decorator preserves original function metadata."""
        @retry_with_backoff(max_attempts=1)
        def example_function():
            """Example docstring."""
            return "test"
        
        assert example_function.__name__ == "example_function"
        assert example_function.__doc__ == "Example docstring."
    
    def test_function_with_arguments(self):
        """Test decorator works with functions that take arguments."""
        @retry_with_backoff(max_attempts=1)
        def test_func(a, b, c=None):
            return f"{a}-{b}-{c}"
        
        result = test_func("x", "y", c="z")
        assert result == "x-y-z"


class TestConvenienceDecorators:
    """Test convenience decorator functions."""
    
    def test_retry_on_request_failure(self):
        """Test retry_on_request_failure decorator."""
        mock_func = Mock()
        mock_func.side_effect = [
            requests.exceptions.RequestException("network error"),
            "success"
        ]
        
        with patch('time.sleep'):
            @retry_on_request_failure(max_attempts=2, base_delay=0.1)
            def test_func():
                return mock_func()
            
            result = test_func()
            assert result == "success"
            assert mock_func.call_count == 2
    
    def test_retry_on_network_failure(self):
        """Test retry_on_network_failure decorator."""
        mock_func = Mock()
        mock_func.side_effect = [
            requests.exceptions.ConnectionError("connection failed"),
            "success"
        ]
        
        with patch('time.sleep'):
            @retry_on_network_failure(max_attempts=2, base_delay=0.1)
            def test_func():
                return mock_func()
            
            result = test_func()
            assert result == "success"
            assert mock_func.call_count == 2
    
    def test_network_failure_different_exceptions(self):
        """Test that network failure decorator handles multiple exception types."""
        exceptions_to_test = [
            requests.exceptions.ConnectionError("connection"),
            requests.exceptions.Timeout("timeout"),
            requests.exceptions.ChunkedEncodingError("chunked")
        ]
        
        for exception in exceptions_to_test:
            mock_func = Mock()
            mock_func.side_effect = [exception, "success"]
            
            with patch('time.sleep'):
                @retry_on_network_failure(max_attempts=2, base_delay=0.1)
                def test_func():
                    return mock_func()
                
                result = test_func()
                assert result == "success"
                assert mock_func.call_count == 2
    
    def test_network_failure_ignores_other_request_exceptions(self):
        """Test that network failure decorator doesn't retry other request exceptions."""
        mock_func = Mock()
        mock_func.side_effect = requests.exceptions.HTTPError("HTTP 404")
        
        @retry_on_network_failure(max_attempts=3)
        def test_func():
            return mock_func()
        
        with pytest.raises(requests.exceptions.HTTPError):
            test_func()
        
        assert mock_func.call_count == 1  # No retries
    
    def test_logger_integration(self):
        """Test that custom logger is used for retry messages."""
        mock_logger = Mock()
        mock_func = Mock()
        mock_func.side_effect = [
            requests.exceptions.RequestException("fail"),
            "success"
        ]
        
        with patch('time.sleep'):
            @retry_on_request_failure(max_attempts=2, base_delay=0.1, logger=mock_logger)
            def test_func():
                return mock_func()
            
            result = test_func()
            assert result == "success"
            
            # Check that logger was called for warning
            assert mock_logger.warning.called
            warning_call = mock_logger.warning.call_args[0][0]
            assert "test_func failed" in warning_call
            assert "Retrying in" in warning_call


class TestDatabaseOperationMixin:
    """Test DatabaseOperationMixin class."""

    @pytest.fixture
    def temp_db(self):
        """Create a temporary database for testing."""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp:
            db_path = tmp.name
        
        # Create test table
        with sqlite3.connect(db_path) as conn:
            conn.execute("""
                CREATE TABLE test_table (
                    id INTEGER PRIMARY KEY,
                    name TEXT,
                    status TEXT,
                    value INTEGER,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    completed_at TIMESTAMP
                )
            """)
            conn.execute("""
                INSERT INTO test_table (id, name, status, value)
                VALUES (1, 'test_item', 'pending', 100)
            """)
            conn.commit()
        
        yield db_path
        
        # Cleanup - retry loop needed on Windows where SQLite releases locks asynchronously
        import gc
        import time
        gc.collect()
        
        for _ in range(10):
            try:
                Path(db_path).unlink(missing_ok=True)
                break
            except PermissionError:
                time.sleep(0.1)
                
    def test_mixin_inheritance(self, temp_db):
        """Test that mixin can be inherited and used."""
        class TestClass(DatabaseOperationMixin):
            def __init__(self, db_path):
                self.db_path = db_path
        
        test_obj = TestClass(temp_db)
        assert hasattr(test_obj, '_build_dynamic_update')
        assert hasattr(test_obj, '_build_conditional_update')
    
    def test_build_dynamic_update_simple(self, temp_db):
        """Test basic dynamic update functionality."""
        class TestClass(DatabaseOperationMixin):
            def __init__(self, db_path):
                self.db_path = db_path
        
        test_obj = TestClass(temp_db)
        
        # Update with simple values
        test_obj._build_dynamic_update(
            'test_table', 'id', 1,
            name='updated_name',
            value=200
        )
        
        # Verify the update
        with sqlite3.connect(temp_db) as conn:
            result = conn.execute("SELECT name, value FROM test_table WHERE id = 1").fetchone()
            assert result[0] == 'updated_name'
            assert result[1] == 200
    
    def test_build_dynamic_update_ignore_none(self, temp_db):
        """Test that None values are ignored in updates."""
        class TestClass(DatabaseOperationMixin):
            def __init__(self, db_path):
                self.db_path = db_path
        
        test_obj = TestClass(temp_db)
        
        # Update with None values (should be ignored)
        test_obj._build_dynamic_update(
            'test_table', 'id', 1,
            name='new_name',
            value=None,  # Should be ignored
            status=None  # Should be ignored
        )
        
        # Verify only non-None values were updated
        with sqlite3.connect(temp_db) as conn:
            result = conn.execute("SELECT name, status, value FROM test_table WHERE id = 1").fetchone()
            assert result[0] == 'new_name'
            assert result[1] == 'pending'  # Unchanged
            assert result[2] == 100  # Unchanged
    
    def test_build_dynamic_update_no_timestamp(self, temp_db):
        """Test update without automatic timestamp."""
        class TestClass(DatabaseOperationMixin):
            def __init__(self, db_path):
                self.db_path = db_path
        
        test_obj = TestClass(temp_db)
        
        # Get original timestamp
        with sqlite3.connect(temp_db) as conn:
            original_time = conn.execute("SELECT updated_at FROM test_table WHERE id = 1").fetchone()[0]
        
        # Update without timestamp
        test_obj._build_dynamic_update(
            'test_table', 'id', 1,
            include_timestamp=False,
            name='no_timestamp_update'
        )
        
        # Verify timestamp wasn't changed
        with sqlite3.connect(temp_db) as conn:
            new_time = conn.execute("SELECT updated_at FROM test_table WHERE id = 1").fetchone()[0]
            assert new_time == original_time
    
    def test_build_dynamic_update_empty_updates(self, temp_db):
        """Test that method handles empty updates gracefully."""
        class TestClass(DatabaseOperationMixin):
            def __init__(self, db_path):
                self.db_path = db_path
        
        test_obj = TestClass(temp_db)
        
        # Get original data
        with sqlite3.connect(temp_db) as conn:
            original = conn.execute("SELECT name, value FROM test_table WHERE id = 1").fetchone()
        
        # Call with no actual updates (all None)
        test_obj._build_dynamic_update(
            'test_table', 'id', 1,
            include_timestamp=False,  # No timestamp update
            name=None,
            value=None
        )
        
        # Verify nothing changed
        with sqlite3.connect(temp_db) as conn:
            result = conn.execute("SELECT name, value FROM test_table WHERE id = 1").fetchone()
            assert result == original
    
    def test_build_conditional_update_basic(self, temp_db):
        """Test basic conditional update functionality."""
        class TestClass(DatabaseOperationMixin):
            def __init__(self, db_path):
                self.db_path = db_path
        
        test_obj = TestClass(temp_db)
        
        # Update with conditional logic
        test_obj._build_conditional_update(
            'test_table', 'id', 1,
            {'status': 'completed', 'value': 300},
            conditional_updates={
                'completed': {'completed_at': 'CURRENT_TIMESTAMP'}
            }
        )
        
        # Verify the update
        with sqlite3.connect(temp_db) as conn:
            result = conn.execute("SELECT status, value, completed_at FROM test_table WHERE id = 1").fetchone()
            assert result[0] == 'completed'
            assert result[1] == 300
            assert result[2] is not None  # completed_at was set
    
    def test_build_conditional_update_no_conditions(self, temp_db):
        """Test conditional update without matching conditions."""
        class TestClass(DatabaseOperationMixin):
            def __init__(self, db_path):
                self.db_path = db_path
        
        test_obj = TestClass(temp_db)
        
        # Update with status that doesn't have conditional updates
        test_obj._build_conditional_update(
            'test_table', 'id', 1,
            {'status': 'processing', 'value': 400},
            conditional_updates={
                'completed': {'completed_at': 'CURRENT_TIMESTAMP'}
            }
        )
        
        # Verify basic update worked but no conditional updates applied
        with sqlite3.connect(temp_db) as conn:
            result = conn.execute("SELECT status, value, completed_at FROM test_table WHERE id = 1").fetchone()
            assert result[0] == 'processing'
            assert result[1] == 400
            assert result[2] is None  # completed_at was not set
    
    def test_build_conditional_update_multiple_conditions(self, temp_db):
        """Test conditional update with multiple status conditions."""
        class TestClass(DatabaseOperationMixin):
            def __init__(self, db_path):
                self.db_path = db_path
        
        test_obj = TestClass(temp_db)
        
        # Test 'active' status
        test_obj._build_conditional_update(
            'test_table', 'id', 1,
            {'status': 'active'},
            conditional_updates={
                'active': {'name': 'active_item'},
                'completed': {'completed_at': 'CURRENT_TIMESTAMP'}
            }
        )
        
        # Verify active condition was applied
        with sqlite3.connect(temp_db) as conn:
            result = conn.execute("SELECT status, name, completed_at FROM test_table WHERE id = 1").fetchone()
            assert result[0] == 'active'
            assert result[1] == 'active_item'
            assert result[2] is None  # completed_at not set for 'active' status
    
    def test_database_error_handling(self, temp_db):
        """Test that database errors are properly raised."""
        class TestClass(DatabaseOperationMixin):
            def __init__(self, db_path):
                self.db_path = db_path
        
        test_obj = TestClass(temp_db)
        
        # Try to update non-existent table
        with pytest.raises(sqlite3.OperationalError):
            test_obj._build_dynamic_update(
                'nonexistent_table', 'id', 1,
                name='test'
            )


class TestProgressTracker:
    """Test ProgressTracker context manager."""
    
    def test_basic_context_manager(self):
        """Test basic context manager functionality."""
        with ProgressTracker(total=10, desc="Test") as tracker:
            assert tracker.total == 10
            assert tracker.initial_desc == "Test"
            assert tracker._pbar is not None
            assert tracker.stats['processed'] == 0
    
    def test_update_success(self):
        """Test updating progress with successful operations."""
        with ProgressTracker(total=5) as tracker:
            tracker.update(count=2, success=True)
            
            assert tracker.stats['processed'] == 2
            assert tracker.stats['success'] == 2
            assert tracker.stats['errors'] == 0
            assert tracker.stats['skipped'] == 0
    
    def test_update_errors(self):
        """Test updating progress with errors."""
        with ProgressTracker(total=5) as tracker:
            tracker.update(count=1, success=False)
            tracker.update(count=1, success=True)
            
            assert tracker.stats['processed'] == 2
            assert tracker.stats['success'] == 1
            assert tracker.stats['errors'] == 1
            assert tracker.stats['skipped'] == 0
    
    def test_update_skipped(self):
        """Test updating progress with skipped items."""
        with ProgressTracker(total=5) as tracker:
            tracker.update(count=2, skipped=True)
            tracker.update(count=1, success=True)
            
            assert tracker.stats['processed'] == 3
            assert tracker.stats['success'] == 1
            assert tracker.stats['errors'] == 0
            assert tracker.stats['skipped'] == 2
    
    def test_increment_error(self):
        """Test incrementing error count without updating progress."""
        with ProgressTracker(total=5) as tracker:
            tracker.increment_error(count=2)
            
            assert tracker.stats['processed'] == 0  # No progress update
            assert tracker.stats['errors'] == 2
    
    def test_set_description(self):
        """Test setting custom description."""
        with patch('src.newsagger.utils.progress.tqdm') as mock_tqdm:
            mock_pbar = Mock()
            mock_tqdm.return_value = mock_pbar
            
            with ProgressTracker() as tracker:
                tracker.set_description("New description")
                mock_pbar.set_description.assert_called_once_with("New description")
    
    def test_set_custom_postfix(self):
        """Test setting custom postfix information."""
        with patch('src.newsagger.utils.progress.tqdm') as mock_tqdm:
            mock_pbar = Mock()
            mock_tqdm.return_value = mock_pbar
            
            with ProgressTracker() as tracker:
                tracker.set_postfix(custom="value", another=123)
                mock_pbar.set_postfix.assert_called_with(custom="value", another=123)
    
    def test_get_stats(self):
        """Test getting current statistics."""
        with ProgressTracker(total=10) as tracker:
            tracker.update(count=3, success=True)
            tracker.update(count=1, success=False)
            tracker.update(count=2, skipped=True)
            
            stats = tracker.get_stats()
            
            assert stats['processed'] == 6
            assert stats['success'] == 3
            assert stats['errors'] == 1
            assert stats['skipped'] == 2
            assert 'elapsed_seconds' in stats
            assert stats['elapsed_seconds'] >= 0
    
    def test_no_rate_display(self):
        """Test progress tracker without rate display."""
        with patch('src.newsagger.utils.progress.tqdm') as mock_tqdm:
            mock_pbar = Mock()
            mock_tqdm.return_value = mock_pbar
            
            with ProgressTracker(show_rate=False) as tracker:
                tracker.update(count=1, success=True)
                
                # Check that postfix doesn't include rate
                call_args = mock_pbar.set_postfix.call_args
                if call_args:
                    postfix_args = call_args[1] if call_args[1] else call_args[0][0] if call_args[0] else {}
                    assert 'rate' not in str(postfix_args)
    
    def test_unknown_total(self):
        """Test progress tracker with unknown total."""
        with patch('src.newsagger.utils.progress.tqdm') as mock_tqdm:
            mock_pbar = Mock()
            mock_tqdm.return_value = mock_pbar
            
            with ProgressTracker(total=None, desc="Unknown total") as tracker:
                mock_tqdm.assert_called_once_with(total=None, desc="Unknown total", unit="item")
                tracker.update(count=5)
                assert tracker.stats['processed'] == 5
    
    def test_custom_unit(self):
        """Test progress tracker with custom unit."""
        with patch('src.newsagger.utils.progress.tqdm') as mock_tqdm:
            mock_pbar = Mock()
            mock_tqdm.return_value = mock_pbar
            
            with ProgressTracker(unit="files") as tracker:
                mock_tqdm.assert_called_once_with(total=None, desc="Processing", unit="files")
    
    def test_context_manager_cleanup(self):
        """Test that progress bar is properly closed on exit."""
        with patch('src.newsagger.utils.progress.tqdm') as mock_tqdm:
            mock_pbar = Mock()
            mock_tqdm.return_value = mock_pbar
            
            with ProgressTracker() as tracker:
                pass  # Just enter and exit
            
            mock_pbar.close.assert_called_once()