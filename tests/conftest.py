"""
Pytest configuration and shared fixtures.
"""

import pytest
import os
import requests
import tempfile
import sqlite3
from pathlib import Path
from unittest.mock import Mock
import gc
import time

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))

from newsagger.config import Config
from newsagger.storage import NewsStorage

HEADERS = {"User-Agent": "Newsagger/0.1.0 (Educational Archive Tool - Rate Limited)"}

@pytest.fixture(scope="session", autouse=False)
def live_api_available():
    """Skip live tests if API is unreachable or not returning 200."""
    try:
        resp = requests.get(
            "https://www.loc.gov/collections/chronicling-america/",
            headers=HEADERS,
            params={"fo": "json", "c": 1, "at": "results,pagination"},
            timeout=10,
        )
        if resp.status_code != 200:
            pytest.skip(f"loc.gov returned {resp.status_code} — skipping live tests")
    except requests.exceptions.ConnectionError:
        pytest.skip("No network access — skipping live tests")
def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "live: requires live network access to loc.gov. Do not run with -n (xdist)."
    )

@pytest.fixture
def temp_db():
    """Create a temporary database for testing."""
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = f.name

    yield db_path

    # Cleanup - retry loop needed on Windows where SQLite releases locks asynchronously
    gc.collect()

    for _ in range(10):
        try:
            Path(db_path).unlink(missing_ok=True)
            break
        except PermissionError:
            time.sleep(0.1)

@pytest.fixture
def storage(temp_db):
    """Create a NewsStorage instance with temporary database."""
    return NewsStorage(temp_db)

#TODO: Figure out what's a useful replacement to this test. 
# @pytest.fixture
# def processor():
#     """Create a NewsDataProcessor instance."""
#     return NewsDataProcessor()


@pytest.fixture
def test_config():
    """Create a test configuration."""
    config = Config()
    config.database_path = ':memory:'
    config.request_delay = 0.1  # Fast for testing
    config.log_level = 'WARNING'  # Reduce noise
    return config


@pytest.fixture
def sample_newspaper_data():
    """Sample newspaper API response data."""
    return {
        'lccn': 'sn84038012',
        'title': 'The San Francisco Call',
        'place_of_publication': ['San Francisco, Calif.'],
        'start_year': '1895',
        'end_year': '1913',
        'frequency': 'Daily',
        'subject': ['San Francisco (Calif.)--Newspapers'],
        'language': ['English'],
        'url': 'https://chroniclingamerica.loc.gov/lccn/sn84038012/'
    }


@pytest.fixture
def sample_page_data():
    """Sample page search result data."""
    return {
        'id': 'item123',
        'lccn': 'sn84038012',
        'title': 'The San Francisco Call',
        'date': '1906-04-18',
        'edition': 1,
        'sequence': 1,
        'url': 'https://chroniclingamerica.loc.gov/lccn/sn84038012/1906-04-18/ed-1/seq-1/',
        'pdf_url': 'https://chroniclingamerica.loc.gov/lccn/sn84038012/1906-04-18/ed-1/seq-1.pdf',
        'image_url': ['https://chroniclingamerica.loc.gov/lccn/sn84038012/1906-04-18/ed-1/seq-1.jp2']
    }


@pytest.fixture
def sample_newspapers_response(sample_newspaper_data):
    """Sample newspapers API response."""
    return {
        'newspapers': [sample_newspaper_data],
        'totalItems': 1,
        'totalPages': 1
    }


@pytest.fixture
def sample_search_response(sample_page_data):
    """Sample search API response."""
    return {
        'items': [sample_page_data],
        'totalItems': 1,
        'pagination': {'current': 1, 'total': 1}
    }


@pytest.fixture
def mock_requests():
    """Mock requests session for API testing."""
    return Mock()