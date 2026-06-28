"""
Tests for the configuration module.
"""

import pytest
import os
import tempfile
from pathlib import Path
from unittest.mock import patch, Mock

from newsagger.config import Config
from newsagger.api_params import LegacyQueryBuilder, LocGovQueryBuilder


class TestConfig:
    """Test cases for Config class."""
        
    def test_init_default_values(self):
        """Test configuration initialization with default values."""
        import os
        from unittest.mock import patch

        with patch.dict(os.environ, {}, clear=True), \
            patch('newsagger.config.load_dotenv'):
            config = Config()

        assert config.loc_base_url == 'https://chroniclingamerica.loc.gov/'
        assert config.request_delay >= 3.0
        assert config.max_retries == 3  # actual default in source code
        assert config.database_path == './data/newsagger.db'
        assert config.download_dir == './data/downloads'
        assert config.log_level == 'INFO'
    
    def test_init_with_env_file(self):
        """Test configuration with explicit env file."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.env', delete=False) as f:
            f.write('REQUEST_DELAY=5.0\n')
            f.write('MAX_RETRIES=5\n')
            f.write('DATABASE_PATH=/tmp/test.db\n')
            f.write('DOWNLOAD_DIR=/tmp/downloads\n')
            f.write('LOG_LEVEL=DEBUG\n')
            env_file = f.name

        try:
            with patch.dict(os.environ, {}, clear=True):
                config = Config(env_file)

            # api_version not set in this file -> defaults to LEGACY, which
            # determines loc_base_url. LOC_BASE_URL is no longer independently
            # configurable, so it's intentionally absent from this env file.
            assert config.api_version == 'LEGACY'
            assert config.loc_base_url == 'https://chroniclingamerica.loc.gov/'
            assert config.request_delay >= 3.0
            assert config.max_retries == 5
            assert config.database_path == '/tmp/test.db'
            assert config.download_dir == '/tmp/downloads'
            assert config.log_level == 'DEBUG'
        finally:
            Path(env_file).unlink()

    def test_init_from_environment(self):
        """Test configuration from environment variables."""
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, 'test.db')
            download_dir = os.path.join(temp_dir, 'downloads')

            with patch.dict(os.environ, {
                'API_VERSION': 'LOC_2026',
                'REQUEST_DELAY': '4.0',
                'MAX_RETRIES': '2',
                'DATABASE_PATH': db_path,
                'DOWNLOAD_DIR': download_dir,
                'LOG_LEVEL': 'WARNING'
            }):
                config = Config()

                assert config.api_version == 'LOC_2026'
                assert config.loc_base_url == 'www.loc.gov/collections/chronicling-america/'
                assert config.request_delay == 4.0
                assert config.max_retries == 2
                assert config.database_path == db_path
                assert config.download_dir == download_dir
                assert config.log_level == 'WARNING'
    
    @patch.dict(os.environ, {'REQUEST_DELAY': '1.0'})
    def test_enforces_minimum_delay(self):
        """Test that minimum delay is enforced."""
        config = Config()
        assert config.request_delay == 3.0  # Should be increased to minimum
    
    @patch.dict(os.environ, {'REQUEST_DELAY': '0.5'})
    def test_enforces_minimum_delay_very_low(self):
        """Test minimum delay enforcement with very low value."""
        config = Config()
        assert config.request_delay == 3.0
    
    def test_directory_creation(self):
        """Test that configuration creates necessary directories."""
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, 'nested', 'db', 'test.db')
            download_dir = os.path.join(temp_dir, 'nested', 'downloads')
            
            # Directories shouldn't exist initially
            assert not Path(db_path).parent.exists()
            assert not Path(download_dir).exists()
            
            # Set environment variables
            with patch.dict(os.environ, {
                'DATABASE_PATH': db_path,
                'DOWNLOAD_DIR': download_dir
            }):
                config = Config()
            
            # Directories should be created
            assert Path(db_path).parent.exists()
            assert Path(download_dir).exists()
    
    def test_get_api_config(self):
        """Test getting API configuration."""
        config = Config()
        api_config = config.get_api_config()
        
        expected_keys = {'base_url', 'request_delay', 'max_retries'}
        assert set(api_config.keys()) == expected_keys
        assert api_config['base_url'] == config.loc_base_url
        assert api_config['request_delay'] == config.request_delay
        assert api_config['max_retries'] == config.max_retries
    
    def test_get_storage_config(self):
        """Test getting storage configuration."""
        config = Config()
        storage_config = config.get_storage_config()
        
        expected_keys = {'db_path'}
        assert set(storage_config.keys()) == expected_keys
        assert storage_config['db_path'] == config.database_path
    
    @patch('logging.basicConfig')
    def test_setup_logging(self, mock_logging):
        """Test logging setup."""
        config = Config()
        config.log_level = 'DEBUG'
        
        config.setup_logging()
        
        # Verify logging.basicConfig was called
        mock_logging.assert_called_once()
        call_args = mock_logging.call_args
        
        # Check that level was set correctly
        assert 'level' in call_args.kwargs
        import logging
        assert call_args.kwargs['level'] == logging.DEBUG
        
        # Check that format was set
        assert 'format' in call_args.kwargs
        assert 'newsagger' in call_args.kwargs['format'] or 'name' in call_args.kwargs['format']
        
        # Check that handlers include both stream and file
        assert 'handlers' in call_args.kwargs
        handlers = call_args.kwargs['handlers']
        assert len(handlers) == 2
    
    @patch('requests.head')
    def test_validate_success(self, mock_head):
        """Test successful configuration validation."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_head.return_value = mock_response
        
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(os.environ, {'DOWNLOAD_DIR': temp_dir}):
                config = Config()
                result = config.validate()
        
        assert result is True
        mock_head.assert_called_once_with(config.loc_base_url, timeout=10)
    
    @patch('requests.head')
    def test_validate_bad_url(self, mock_head):
        """Test validation with inaccessible URL."""
        mock_response = Mock()
        mock_response.status_code = 404
        mock_head.return_value = mock_response
        
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(os.environ, {'DOWNLOAD_DIR': temp_dir}):
                config = Config()
                result = config.validate()
        
        assert result is False
    
    @patch('requests.head')
    def test_validate_network_error(self, mock_head, caplog):
        """Test validation with network error."""
        mock_head.side_effect = Exception("Network error")
        
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(os.environ, {'DOWNLOAD_DIR': temp_dir}):
                config = Config()
                result = config.validate()
        
        # Should still return True (warning only)
        assert result is True
        assert "Could not validate LOC base URL" in caplog.text
    
    def test_validate_bad_download_dir(self):
        """Test validation with non-writable download directory."""
        with tempfile.TemporaryDirectory() as temp_dir:
            bad_path = os.path.join(temp_dir, 'bad_dir')
            
            with patch.dict(os.environ, {'DOWNLOAD_DIR': bad_path}):
                config = Config()
                
                # Mock Path.touch to raise PermissionError to simulate unwritable directory
                with patch('pathlib.Path.touch', side_effect=PermissionError("Permission denied")):
                    result = config.validate()
            
            assert result is False

    @patch.dict(os.environ, {
        'API_VERSION': 'LEGACY',
        'REQUEST_DELAY': 'not-a-number',
        'MAX_RETRIES': 'not-a-number'
    })
    def test_invalid_environment_values(self):
        """Test handling of invalid environment variable values."""
        config = Config()

        assert config.loc_base_url == 'https://chroniclingamerica.loc.gov/'
        assert config.request_delay == 3.0  # Default minimum
        assert config.max_retries == 3  # Default

    def test_base_url_set_by_api_version_not_env(self):
        """
        loc_base_url is derived entirely from api_version, so the 
        two should never disagree.
        """
        with patch.dict(os.environ, {
            'API_VERSION': 'LEGACY',
            'LOC_BASE_URL': 'https://example.com',  # should be ignored
        }):
            config = Config()
            assert config.loc_base_url == 'https://chroniclingamerica.loc.gov/'
            
    
    def test_log_level_case_insensitive(self):
        """Test that log level is handled case-insensitively."""
        with patch.dict(os.environ, {'LOG_LEVEL': 'debug'}):
            config = Config()
            assert config.log_level == 'DEBUG'
        
        with patch.dict(os.environ, {'LOG_LEVEL': 'Warning'}):
            config = Config()
            assert config.log_level == 'WARNING'
        
    @patch('newsagger.config.load_dotenv')
    def test_dotenv_file_loading(self, mock_load_dotenv):
        """Test that .env file is loaded when present."""
        # Create a temporary .env file
        with tempfile.NamedTemporaryFile(mode='w', suffix='.env', delete=False) as f:
            f.write('TEST_VAR=test_value\n')
            env_file = f.name
        
        try:
            # Mock Path.exists to return True for .env file
            with patch('pathlib.Path.exists', return_value=True):
                with patch('newsagger.config.Path') as mock_path:
                    mock_path.return_value.exists.return_value = True
                    config = Config()
            
            # load_dotenv should have been called
            mock_load_dotenv.assert_called()
        finally:
            Path(env_file).unlink()
    
    def test_current_year_setting(self):
        """Test that current year is set correctly."""
        config = Config()
        from datetime import datetime
        current_year = datetime.now().year
        
        assert config.current_year == current_year
        assert isinstance(config.current_year, int)

    def test_query_builder_class_legacy(self):
        """LEGACY api_version selects LegacyQueryBuilder."""
        with patch.dict(os.environ, {'API_VERSION': 'LEGACY'}):
            config = Config()
            assert config.query_builder_class is LegacyQueryBuilder

    def test_query_builder_class_loc_2026(self):
        """LOC_2026 api_version selects LocGovQueryBuilder."""
        with patch.dict(os.environ, {'API_VERSION': 'LOC_2026'}):
            config = Config()
            assert config.query_builder_class is LocGovQueryBuilder

    def test_invalid_api_version_raises(self):
        """
        An unrecognised API_VERSION must raise clearly, not silently fall back —
        loc_base_url/query_builder_class have no sensible default to fall back to.
        """
        with patch.dict(os.environ, {'API_VERSION': 'invalid-version'}):
            with pytest.raises(ValueError, match="LEGACY or LOC_2026"):
                Config()

    @pytest.mark.parametrize("version,expected_url,expected_builder", [
        ('LEGACY', 'https://chroniclingamerica.loc.gov/', LegacyQueryBuilder),
        ('LOC_2026', 'www.loc.gov/collections/chronicling-america/', LocGovQueryBuilder),
    ])
    def test_api_version_pairs_url_and_builder_consistently(self, version, expected_url, expected_builder):
        """
        Regression guard: loc_base_url and query_builder_class must always be set
        together in the same branch — if someone later splits them back into
        independent if/elif blocks, this catches the resulting drift.
        """
        with patch.dict(os.environ, {'API_VERSION': version}):
            config = Config()
            assert config.loc_base_url == expected_url
            assert config.query_builder_class is expected_builder

    def test_get_querybuilder_config(self):
        """Test getting the active QueryBuilder class config."""
        with patch.dict(os.environ, {'API_VERSION': 'LOC_2026'}):
            config = Config()
            qb_config = config.get_querybuilder_config()

        assert set(qb_config.keys()) == {'query_builder_class'}
        assert qb_config['query_builder_class'] is config.query_builder_class
        assert qb_config['query_builder_class'] is LocGovQueryBuilder