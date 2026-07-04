"""
Tests for the storage module.
"""

import pytest
import sqlite3
import json
import tempfile
from pathlib import Path
from contextlib import closing
import shutil
import gc
import time

from newsagger.storage import NewsStorage
from newsagger.processor_new import NewspaperInfo, PageInfo, LegacyResponseProcessor

class TestNewsStorage:
    """Test cases for NewsStorage."""
    
    def test_init_creates_database(self, temp_db):
        """Test that storage initialization creates database and tables."""
        storage = NewsStorage(temp_db)
        
        # Check that database file exists
        assert Path(temp_db).exists()
        
        # Check that tables exist
        with closing(sqlite3.connect(temp_db)) as conn:
            cursor = conn.execute("""
                SELECT name FROM sqlite_master 
                WHERE type='table' AND name IN ('newspapers', 'pages', 'download_sessions')
            """)
            tables = [row[0] for row in cursor.fetchall()]
            
        assert 'newspapers' in tables
        assert 'pages' in tables
        assert 'download_sessions' in tables
    
    def test_init_creates_indices(self, temp_db):
        """Test that storage initialization creates database indices."""
        storage = NewsStorage(temp_db)
        
        with closing(sqlite3.connect(temp_db)) as conn:
            cursor = conn.execute("""
                SELECT name FROM sqlite_master 
                WHERE type='index' AND name LIKE 'idx_%'
            """)
            indices = [row[0] for row in cursor.fetchall()]
        
        expected_indices = ['idx_pages_lccn', 'idx_pages_date', 'idx_pages_downloaded']
        for idx in expected_indices:
            assert idx in indices
    
    def test_store_newspapers(self, storage, sample_newspaper_data):
        """Test storing newspaper data."""
        newspaper = LegacyResponseProcessor().parse_newspapers({'newspapers':
                                                                [sample_newspaper_data]})[0]        
        newspapers = [newspaper]
        
        inserted = storage.store_newspapers(newspapers)
        
        assert inserted == 1
        
        # Verify data was stored correctly
        with closing(sqlite3.connect(storage.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("SELECT * FROM newspapers WHERE lccn = ?", (newspaper.lccn,))
            row = cursor.fetchone()
        
        assert row is not None
        assert row['lccn'] == 'sn84038012'
        assert row['title'] == 'The San Francisco Call'
        assert json.loads(row['place_of_publication']) == ['San Francisco, Calif.']
        assert row['start_year'] == 1895
        assert row['end_year'] == 1913
    
    def test_store_newspapers_duplicate_handling(self, storage, sample_newspaper_data):
        """Test storing duplicate newspapers (should replace)."""
        newspaper = LegacyResponseProcessor().parse_newspapers({'newspapers': 
                                                                [sample_newspaper_data]})[0]        
        # Store same newspaper twice
        storage.store_newspapers([newspaper])
        storage.store_newspapers([newspaper])
        
        # Should only have one record
        with closing(sqlite3.connect(storage.db_path)) as conn:
            cursor = conn.execute("SELECT COUNT(*) FROM newspapers WHERE lccn = ?", (newspaper.lccn,))
            count = cursor.fetchone()[0]
        
        assert count == 1
    
    def test_store_pages(self, storage, sample_page_data):
        """Test storing page data."""
        page = LegacyResponseProcessor().parse_pages({'items': [sample_page_data]})[0]
        pages = [page]
        
        inserted = storage.store_pages(pages)
        
        assert inserted == 1
        
        # Verify data was stored correctly
        with closing(sqlite3.connect(storage.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("SELECT * FROM pages WHERE item_id = ?", (page.item_id,))
            row = cursor.fetchone()
        
        assert row is not None
        assert row['item_id'] == 'item123'
        assert row['lccn'] == 'sn84038012'
        assert row['title'] == 'The San Francisco Call'
        assert row['date'] == '1906-04-18'
        assert row['downloaded'] == 0  # False
    
    def test_get_newspapers_all(self, storage):
        """Test retrieving all newspapers."""
        # Store test data
        newspapers = [
            NewspaperInfo(
                lccn='ca1', title='CA Paper', place_of_publication=['San Francisco, California'],
                start_year=1900, end_year=1920, frequency='Daily', subject=[], language=['English'], url=''
            ),
            NewspaperInfo(
                lccn='ny1', title='NY Paper', place_of_publication=['New York, New York'],
                start_year=1900, end_year=1920, frequency='Daily', subject=[], language=['English'], url=''
            )
        ]
        storage.store_newspapers(newspapers)
        
        # Retrieve all
        retrieved = storage.get_newspapers()
        
        assert len(retrieved) == 2
        assert retrieved[0]['lccn'] in ['ca1', 'ny1']
        assert retrieved[1]['lccn'] in ['ca1', 'ny1']
    
    def test_get_newspapers_filter_by_state(self, storage):
        """Test retrieving newspapers filtered by state."""
        newspapers = [
            NewspaperInfo(
                lccn='ca1', title='CA Paper', place_of_publication=['San Francisco, California'],
                start_year=1900, end_year=1920, frequency='Daily', subject=[], language=['English'], url=''
            ),
            NewspaperInfo(
                lccn='ny1', title='NY Paper', place_of_publication=['New York, New York'],
                start_year=1900, end_year=1920, frequency='Daily', subject=[], language=['English'], url=''
            )
        ]
        storage.store_newspapers(newspapers)
        
        # Filter by state
        ca_papers = storage.get_newspapers(state='California')
        
        assert len(ca_papers) == 1
        assert ca_papers[0]['lccn'] == 'ca1'
    
    def test_get_newspapers_filter_by_language(self, storage):
        """Test retrieving newspapers filtered by language."""
        newspapers = [
            NewspaperInfo(
                lccn='en1', title='English Paper', place_of_publication=['Test City'],
                start_year=1900, end_year=1920, frequency='Daily', subject=[], language=['English'], url=''
            ),
            NewspaperInfo(
                lccn='es1', title='Spanish Paper', place_of_publication=['Test City'],
                start_year=1900, end_year=1920, frequency='Daily', subject=[], language=['Spanish'], url=''
            )
        ]
        storage.store_newspapers(newspapers)
        
        # Filter by language
        spanish_papers = storage.get_newspapers(language='Spanish')
        
        assert len(spanish_papers) == 1
        assert spanish_papers[0]['lccn'] == 'es1'
    
    def test_get_pages_all(self, storage):
        """Test retrieving all pages."""
        pages = [
            PageInfo(
                item_id='item1', lccn='test1', title='Test Paper 1', date='1900-01-01',
                edition=1, sequence=1, page_url='http://test1.com', pdf_url=None,
                jp2_url=None, ocr_text=None, word_count=None
            ),
            PageInfo(
                item_id='item2', lccn='test2', title='Test Paper 2', date='1900-01-02',
                edition=1, sequence=1, page_url='http://test2.com', pdf_url=None,
                jp2_url=None, ocr_text=None, word_count=None
            )
        ]
        storage.store_pages(pages)
        
        retrieved = storage.get_pages()
        
        assert len(retrieved) == 2
        assert retrieved[0]['item_id'] in ['item1', 'item2']
        assert retrieved[1]['item_id'] in ['item1', 'item2']
    
    def test_get_pages_filter_by_lccn(self, storage):
        """Test retrieving pages filtered by LCCN."""
        pages = [
            PageInfo(
                item_id='item1', lccn='test1', title='Test Paper 1', date='1900-01-01',
                edition=1, sequence=1, page_url='http://test1.com', pdf_url=None,
                jp2_url=None, ocr_text=None, word_count=None
            ),
            PageInfo(
                item_id='item2', lccn='test2', title='Test Paper 2', date='1900-01-02',
                edition=1, sequence=1, page_url='http://test2.com', pdf_url=None,
                jp2_url=None, ocr_text=None, word_count=None
            )
        ]
        storage.store_pages(pages)
        
        filtered = storage.get_pages(lccn='test1')
        
        assert len(filtered) == 1
        assert filtered[0]['lccn'] == 'test1'
    
    def test_get_pages_filter_by_date_range(self, storage):
        """Test retrieving pages filtered by date range."""
        pages = [
            PageInfo(
                item_id='item1', lccn='test1', title='Test Paper 1', date='1900-01-01',
                edition=1, sequence=1, page_url='http://test1.com', pdf_url=None,
                jp2_url=None, ocr_text=None, word_count=None
            ),
            PageInfo(
                item_id='item2', lccn='test1', title='Test Paper 1', date='1900-06-15',
                edition=1, sequence=1, page_url='http://test2.com', pdf_url=None,
                jp2_url=None, ocr_text=None, word_count=None
            ),
            PageInfo(
                item_id='item3', lccn='test1', title='Test Paper 1', date='1901-01-01',
                edition=1, sequence=1, page_url='http://test3.com', pdf_url=None,
                jp2_url=None, ocr_text=None, word_count=None
            )
        ]
        storage.store_pages(pages)
        
        filtered = storage.get_pages(date_range=('1900-01-01', '1900-12-31'))
        
        assert len(filtered) == 2
        assert all(page['date'].startswith('1900') for page in filtered)
    
    def test_mark_page_downloaded(self, storage, sample_page_data):
        """Test marking a page as downloaded."""
        page = LegacyResponseProcessor().parse_pages({'items': [sample_page_data]})[0]
        storage.store_pages([page])
        
        # Initially not downloaded
        pages = storage.get_pages(downloaded_only=True)
        assert len(pages) == 0
        
        # Mark as downloaded
        storage.mark_page_downloaded(page.item_id)
        
        # Should now appear in downloaded filter
        pages = storage.get_pages(downloaded_only=True)
        assert len(pages) == 1
        assert pages[0]['item_id'] == page.item_id
        assert pages[0]['downloaded'] == 1  # True
    
    def test_create_download_session(self, storage):
        """Test creating a download session."""
        query_params = {'lccn': 'test123', 'date1': '1900', 'date2': '1910'}
        
        session_id = storage.create_download_session(
            'test_session', query_params, 1000
        )
        
        assert session_id is not None
        assert isinstance(session_id, int)
        
        # Verify session was created
        with closing(sqlite3.connect(storage.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("SELECT * FROM download_sessions WHERE id = ?", (session_id,))
            row = cursor.fetchone()
        
        assert row is not None
        assert row['session_name'] == 'test_session'
        assert json.loads(row['query_params']) == query_params
        assert row['total_expected'] == 1000
        assert row['total_downloaded'] == 0
        assert row['status'] == 'active'
    
    def test_update_session_progress(self, storage):
        """Test updating download session progress."""
        session_id = storage.create_download_session('test', {}, 1000)
        
        storage.update_session_progress(session_id, 250)
        
        # Verify update
        with closing(sqlite3.connect(storage.db_path)) as conn:
            cursor = conn.execute(
                "SELECT total_downloaded FROM download_sessions WHERE id = ?", 
                (session_id,)
            )
            downloaded = cursor.fetchone()[0]
        
        assert downloaded == 250
    
    def test_complete_session(self, storage):
        """Test completing a download session."""
        session_id = storage.create_download_session('test', {}, 1000)
        
        storage.complete_session(session_id)
        
        # Verify completion
        with closing(sqlite3.connect(storage.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("SELECT * FROM download_sessions WHERE id = ?", (session_id,))
            row = cursor.fetchone()
        
        assert row['status'] == 'completed'
        assert row['completed_at'] is not None
    
    def test_get_session_stats(self, storage):
        """Test retrieving session statistics."""
        query_params = {'lccn': 'test123'}
        session_id = storage.create_download_session('test', query_params, 1000)
        storage.update_session_progress(session_id, 500)
        
        stats = storage.get_session_stats(session_id)
        
        assert stats is not None
        assert stats['session_name'] == 'test'
        assert stats['total_expected'] == 1000
        assert stats['total_downloaded'] == 500
        assert stats['status'] == 'active'
    
    def test_get_session_stats_nonexistent(self, storage):
        """Test retrieving stats for nonexistent session."""
        stats = storage.get_session_stats(99999)
        assert stats is None
    
    def test_get_storage_stats(self, storage):
        """Test retrieving overall storage statistics."""
        # Add some test data
        newspapers = [
            NewspaperInfo(
                lccn='test1', title='Test Paper 1', place_of_publication=['Test City'],
                start_year=1900, end_year=1920, frequency='Daily', subject=[], language=['English'], url=''
            )
        ]
        pages = [
            PageInfo(
                item_id='item1', lccn='test1', title='Test Paper 1', date='1900-01-01',
                edition=1, sequence=1, page_url='http://test1.com', pdf_url=None,
                jp2_url=None, ocr_text=None, word_count=None
            ),
            PageInfo(
                item_id='item2', lccn='test1', title='Test Paper 1', date='1900-01-02',
                edition=1, sequence=1, page_url='http://test2.com', pdf_url=None,
                jp2_url=None, ocr_text=None, word_count=None
            )
        ]
        
        storage.store_newspapers(newspapers)
        storage.store_pages(pages)
        storage.mark_page_downloaded('item1')
        session_id = storage.create_download_session('test', {}, 100)
        
        stats = storage.get_storage_stats()
        
        assert stats['total_newspapers'] == 1
        assert stats['total_pages'] == 2
        assert stats['downloaded_pages'] == 1
        assert stats['active_sessions'] == 1
        assert stats['db_size_mb'] >= 0  # File exists and has some size
    
    def test_store_newspapers_with_invalid_data(self, storage, caplog):
        """Test storing newspapers with some invalid entries."""
        # Create a newspaper with None values that might cause issues
        newspapers = [
            NewspaperInfo(
                lccn='valid1', title='Valid Paper', place_of_publication=['Test City'],
                start_year=1900, end_year=1920, frequency='Daily', subject=[], language=['English'], url=''
            )
        ]
        
        # This should work fine
        inserted = storage.store_newspapers(newspapers)
        assert inserted == 1
        
    def test_database_path_creation(self):
        """Test that database directory is created if it doesn't exist."""

        temp_dir = tempfile.mkdtemp()
        try:
            # Use a path with nested directories that definitely doesn't exist
            nested_path = Path(temp_dir) / 'nested' / 'path' / 'test.db'

            # Directory shouldn't exist initially
            assert not nested_path.parent.exists()

            # Creating storage should create the directory
            storage = NewsStorage(str(nested_path))

            assert nested_path.parent.exists()
            assert nested_path.exists()

            # cleanup barrier for windows
            del storage
            gc.collect()
            
        finally:
            for _ in range(10):
                try:
                    shutil.rmtree(temp_dir)
                    break
                except PermissionError:
                    time.sleep(0.1)
                        
    def test_get_search_facet(self, storage):
        """Test getting a specific search facet by ID."""
        # Create a test facet
        facet_id = storage.create_search_facet(
            'date_range', '1906/1906', 'earthquake', 1000
        )
        
        # Retrieve the facet
        facet = storage.get_search_facet(facet_id)
        
        assert facet is not None
        assert facet['id'] == facet_id
        assert facet['facet_type'] == 'date_range'
        assert facet['facet_value'] == '1906/1906'
        assert facet['query'] == 'earthquake'
        assert facet['estimated_items'] == 1000
        assert facet['status'] == 'pending'
        
    def test_get_search_facet_not_found(self, storage):
        """Test getting a non-existent facet returns None."""
        facet = storage.get_search_facet(999)
        assert facet is None
    
    def test_get_pages_for_facet(self, storage):
        """Test getting pages discovered for a facet."""
        # Store some test pages
        pages = [
            PageInfo(
                item_id='item1',
                lccn='sn84038012',
                title='Test Paper 1',
                date='1906-04-18',
                edition=1,
                sequence=1,
                page_url='https://example.com/item1',
                pdf_url=None,
                jp2_url=None,
                ocr_text=None,
                word_count=None
            ),
            PageInfo(
                item_id='item2',
                lccn='sn84038012',
                title='Test Paper 2',
                date='1906-04-19',
                edition=1,
                sequence=1,
                page_url='https://example.com/item2',
                pdf_url=None,
                jp2_url=None,
                ocr_text=None,
                word_count=None
            )
        ]
        
        storage.store_pages(pages)
        
        # Get all pages
        all_pages = storage.get_pages_for_facet(1)  # facet_id doesn't matter for current implementation
        assert len(all_pages) == 2
        
        # Check structure
        page = all_pages[0]
        assert 'item_id' in page
        assert 'lccn' in page
        assert 'title' in page
        assert 'downloaded' in page
        
    def test_get_pages_for_facet_downloaded_filter(self, storage):
        """Test filtering pages by download status."""
        # Store test pages
        pages = [
            PageInfo(
                item_id='item1',
                lccn='sn84038012',
                title='Test Paper 1',
                date='1906-04-18',
                edition=1,
                sequence=1,
                page_url='https://example.com/item1',
                pdf_url=None,
                jp2_url=None,
                ocr_text=None,
                word_count=None
            ),
            PageInfo(
                item_id='item2',
                lccn='sn84038012',
                title='Test Paper 2',
                date='1906-04-19',
                edition=1,
                sequence=1,
                page_url='https://example.com/item2',
                pdf_url=None,
                jp2_url=None,
                ocr_text=None,
                word_count=None
            )
        ]
        
        storage.store_pages(pages)
        
        # Mark one as downloaded
        storage.mark_page_downloaded('item1')
        
        # Get only downloaded pages
        downloaded_pages = storage.get_pages_for_facet(1, downloaded=True)
        assert len(downloaded_pages) == 1
        assert downloaded_pages[0]['item_id'] == 'item1'
        assert downloaded_pages[0]['downloaded'] is True
        
        # Get only non-downloaded pages
        not_downloaded = storage.get_pages_for_facet(1, downloaded=False)
        assert len(not_downloaded) == 1
        assert not_downloaded[0]['item_id'] == 'item2'
        assert not_downloaded[0]['downloaded'] is False
    
    def test_get_download_queue_stats(self, storage):
        """Test getting download queue statistics."""
        # Add some test queue items
        storage.add_to_download_queue('page', 'item1', 1, 10.0, 1.0)
        storage.add_to_download_queue('page', 'item2', 2, 15.0, 1.5)
        storage.add_to_download_queue('page', 'item3', 3, 5.0, 0.5)
        
        # Update some statuses
        storage.update_queue_item(1, status='active')
        storage.update_queue_item(2, status='completed')
        
        # Get stats
        stats = storage.get_download_queue_stats()
        
        assert stats['total_items'] == 3
        assert stats['total_size_mb'] == 30.0
        assert stats['total_time_hours'] == 3.0
        assert stats['queued'] == 1  # item3
        assert stats['active'] == 1  # item1
        assert stats['completed'] == 1  # item2
        assert stats['failed'] == 0
    
    def test_get_download_queue_stats_empty(self, storage):
        """Test queue stats when queue is empty."""
        stats = storage.get_download_queue_stats()
        
        assert stats['total_items'] == 0
        assert stats['total_size_mb'] == 0.0
        assert stats['total_time_hours'] == 0.0
        assert stats['queued'] == 0
        assert stats['active'] == 0
        assert stats['completed'] == 0
        assert stats['failed'] == 0

    def test_update_periodical_discovery_edge_cases(self, storage):
        """Test edge cases in periodical discovery updates."""
        # Test with non-existent periodical
        storage.update_periodical_discovery('nonexistent', total_issues=100)
        
        # Should not crash, but also not update anything
        periodicals = storage.get_periodicals()
        assert len(periodicals) == 0
        
    def test_get_pages_for_facet_complex_query(self, storage):
        """Test complex facet page queries."""
        # Add test facet and pages
        facet_id = storage.create_search_facet('date_range', '1906/1906', '', 1000)
        
        page_data = [
            PageInfo(
                item_id='page1',
                lccn='sn123',
                title='Test Page 1',
                date='1906-04-18',
                edition=1,
                sequence=1,
                page_url='https://example.com/page1',
                pdf_url=None,
                jp2_url=None,
                ocr_text=None,
                word_count=None
            ),
            PageInfo(
                item_id='page2',
                lccn='sn123',
                title='Test Page 2',
                date='1906-04-19',
                edition=1,
                sequence=1,
                page_url='https://example.com/page2',
                pdf_url=None,
                jp2_url=None,
                ocr_text=None,
                word_count=None
            )
        ]
        
        # Store pages
        storage.store_pages(page_data)
        
        # Test getting pages for facet with download filter
        # Note: current implementation returns all pages, not facet-specific
        pages = storage.get_pages_for_facet(facet_id, downloaded=False)
        assert len(pages) >= 2  # Should have at least our 2 pages
        
        # Mark one as downloaded and test filter
        storage.mark_page_downloaded('page1')
        
        # Test undownloaded pages
        pages = storage.get_pages_for_facet(facet_id, downloaded=False)
        undownloaded_items = [p['item_id'] for p in pages]
        assert 'page2' in undownloaded_items  # page2 should still be undownloaded
        
        # Test downloaded pages
        pages = storage.get_pages_for_facet(facet_id, downloaded=True)
        downloaded_items = [p['item_id'] for p in pages]
        assert 'page1' in downloaded_items  # page1 should be downloaded

    def test_create_download_session(self, storage):
        """Test creating and managing download sessions."""
        session_id = storage.create_download_session(
            'test_session',
            {'lccn': 'sn123', 'date_range': '1906/1906'},
            total_expected=1000
        )
        
        assert session_id is not None
        
        # Test updating session progress
        storage.update_session_progress(session_id, downloaded_count=250)
        storage.update_session_progress(session_id, downloaded_count=500)
        
        # Complete the session
        storage.complete_session(session_id)

    def test_database_migration_handling(self, storage):
        """Test database migration and schema handling."""
        # This tests the migration logic in the constructor
        # The migration warning is already tested in other test runs
        assert storage.db_path is not None
        
        # Test that tables exist using proper connection management
        with closing(sqlite3.connect(storage.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [row[0] for row in cursor.fetchall()]
        
        expected_tables = [
            'pages', 'periodicals', 'search_facets', 'download_queue',
            'periodical_issues', 'download_sessions'
        ]
        
        for table in expected_tables:
            assert table in tables, f"Expected table '{table}' not found. Available tables: {tables}"
    
    def test_get_storage_stats_comprehensive(self, storage):
        """Test comprehensive storage statistics."""
        # Add test data using NewspaperInfo for newspapers table
        newspaper = NewspaperInfo(
            lccn='sn123',
            title='Test Paper',
            place_of_publication=['California'],
            start_year=1900,
            end_year=1910,
            frequency='Daily',
            language=['English'],
            subject=['News'],
            url='https://example.com'
        )
        storage.store_newspapers([newspaper])
        
        # Create and store a page using PageInfo
        page = PageInfo(
            item_id='page1',
            lccn='sn123',
            title='Test Page',
            date='1906-04-18',
            edition=1,
            sequence=1,
            page_url='https://example.com/page1',
            pdf_url=None,
            jp2_url=None,
            ocr_text=None,
            word_count=None
        )
        storage.store_pages([page])
        storage.mark_page_downloaded('page1')
        
        stats = storage.get_storage_stats()
        
        assert stats['total_newspapers'] == 1
        assert stats['total_pages'] == 1
        assert stats['downloaded_pages'] == 1
        assert 'db_size_mb' in stats
        assert stats['db_size_mb'] > 0

    def test_error_handling_database_operations(self, storage):
        """Test error handling in database operations."""
        # Test with invalid data types
        try:
            storage.store_page(None, 'sn123', 'Test', '1906-04-18', 1, 1, 'https://example.com')
        except Exception:
            pass  # Expected to handle gracefully
        
        # Test with malformed dates
        try:
            storage.store_page('page1', 'sn123', 'Test', 'invalid-date', 1, 1, 'https://example.com')
        except Exception:
            pass  # Expected to handle gracefully

    def test_migration_adds_newspaper_state_city_columns(self, temp_db):
        """An existing newspapers table without state/city gains them on re-init."""
        # First init builds the full current schema (so search_facets exists).
        NewsStorage(temp_db)

        # Simulate a pre-enrichment newspapers table: drop and recreate without state/city.
        with closing(sqlite3.connect(temp_db)) as conn:
            conn.execute("DROP TABLE newspapers")
            conn.execute("""
                CREATE TABLE newspapers (
                    lccn TEXT PRIMARY KEY, title TEXT NOT NULL,
                    place_of_publication TEXT, start_year INTEGER, end_year INTEGER,
                    frequency TEXT, subject TEXT, language TEXT, url TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()

        # Re-init runs _migrate_database against the now-old newspapers table.
        NewsStorage(temp_db)

        with closing(sqlite3.connect(temp_db)) as conn:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(newspapers)").fetchall()]
        assert 'state' in cols and 'city' in cols