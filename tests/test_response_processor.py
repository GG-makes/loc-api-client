"""
Tests for response_processor.py

Test categories used in this file:
    [REFACTORED]    Tests covering functionality that existed in processor.py but has
                    changed in response_processor.py. from_api_response and
                    from_search_result classmethods no longer exist — parsing is now
                    done by the processor, not the dataclass. These tests document
                    what changed and why, not just that it works.

    [PORTABLE]      Tests adapted from test_processor.py with minimal changes.
                    Logic is the same; only the call site has changed to reflect
                    the new class structure. Assertions are kept identical where
                    possible so regressions are obvious.

    [LEGACY]        Tests for LegacyResponseProcessor. Cover the pre-August 2025
                    chroniclingamerica.loc.gov response format. Mirror the
                    [LOCGOV] tests 1:1 so behavioural parity is visible at a glance.

    [LOCGOV]        Tests for LocGovResponseProcessor. Cover the confirmed post-August
                    2025 loc.gov response format. Mirror the [LEGACY] tests 1:1.

    [LOCGOV_EXTRA]  Additional tests for response structure unique to the new API:
                    nested results location, mixed dict/list newspaper fields, issue
                    resources[0]['files'] structure, parse_pagination, parse_batch_list.
                    No legacy equivalent exists for these.

    [COMPAT]        Explicit compatibility guarantee tests. Assert that both processors
                    return the same Python types from equivalent methods, so downstream
                    code can swap processors without type errors.

    [MIXIN]         Structural isolation tests for DeduplicationMixin and
                    NewspaperUtilsMixin. Test the mixin behaviour independently of
                    which processor subclass they are combined with.

    [REGRESSION]    Tests for edge cases discovered during live API investigation.
                    Protects against silent failures from API response variations
                    that would not be caught by happy-path tests.
"""

import pytest
from datetime import datetime
from unittest.mock import patch

from newsagger.processor_new import (
    NewspaperInfo,
    PageInfo,
    LegacyResponseProcessor,
    LocGovResponseProcessor,
    DeduplicationMixin,
    NewspaperUtilsMixin,
    ResponseProcessor,
)


# ---------------------------------------------------------------------------
# Fixtures — legacy response shapes
# ---------------------------------------------------------------------------

@pytest.fixture
def legacy_newspaper_item():
    """Single newspaper entry from legacy newspapers.json response."""
    return {
        'lccn': 'sn84038012',
        'title': 'The San Francisco Call',
        'state': 'California',
        'place_of_publication': ['San Francisco, Calif.'],
        'start_year': '1895',
        'end_year': '1913',
        'frequency': 'Daily',
        'subject': ['San Francisco (Calif.)--Newspapers'],
        'language': ['English'],
        'url': 'https://chroniclingamerica.loc.gov/lccn/sn84038012/',
    }

@pytest.fixture
def legacy_newspapers_response(legacy_newspaper_item):
    """Legacy newspapers.json response envelope."""
    return {'newspapers': [legacy_newspaper_item]}

@pytest.fixture
def legacy_page_item():
    """Single page entry from legacy search results."""
    return {
        'id': '/lccn/sn84038012/1906-04-18/ed-1/seq-1/',
        'lccn': 'sn84038012',
        'title': 'The San Francisco Call',
        'date': '19060418',
        'sequence': 1,
        'edition': 1,
        'url': 'https://chroniclingamerica.loc.gov/lccn/sn84038012/1906-04-18/ed-1/seq-1/',
        'ocr_eng': 'Full OCR text content here.',
    }

@pytest.fixture
def legacy_search_response(legacy_page_item):
    """Legacy page search response envelope."""
    return {'items': [legacy_page_item]}

@pytest.fixture
def legacy_page_details():
    """Legacy single page detail response."""
    return {
        'sequence': 1,
        'title': {
            'name': 'The San Francisco Call',
            'url': 'https://chroniclingamerica.loc.gov/lccn/sn84038012.json',
        },
        'issue': {'date_issued': '1906-04-18'},
        'pdf': 'https://chroniclingamerica.loc.gov/lccn/sn84038012/1906-04-18/ed-1/seq-1.pdf',
        'jp2': 'https://chroniclingamerica.loc.gov/lccn/sn84038012/1906-04-18/ed-1/seq-1.jp2',
        'text': 'https://chroniclingamerica.loc.gov/lccn/sn84038012/1906-04-18/ed-1/seq-1/ocr.txt',
    }

@pytest.fixture
def legacy_issue_details():
    """Legacy issue detail response."""
    return {
        'date_issued': '1906-04-18',
        'url': 'https://chroniclingamerica.loc.gov/lccn/sn84038012/1906-04-18/ed-1/',
        'title': {'name': 'The San Francisco Call', 'url': '...'},
        'pages': [
            {'url': 'https://chroniclingamerica.loc.gov/lccn/sn84038012/1906-04-18/ed-1/seq-1.json', 'sequence': 1},
            {'url': 'https://chroniclingamerica.loc.gov/lccn/sn84038012/1906-04-18/ed-1/seq-2.json', 'sequence': 2},
        ],
    }


# ---------------------------------------------------------------------------
# Fixtures — loc.gov response shapes (all confirmed via live API)
# ---------------------------------------------------------------------------

@pytest.fixture
def locgov_newspaper_item():
    """Single title entry from loc.gov newspaper list — confirmed mixed dict/list structure."""
    return {
        'id': 'http://www.loc.gov/item/sn84038012/',
        'title': 'The San Francisco Call (San Francisco, Calif.) 1895-1913',
        'number_lccn': ['sn84038012'],
        'location_city': ['san francisco'],
        'location_county': ['san francisco'],
        'location_state': {
            'class': 'location_state',
            'label': 'California',
            'value': 'california',
        },
        'language': {
            'class': 'language',
            'label': 'English',
            'value': 'english',
        },
        'partof_title': {
            'class': 'title',
            'label': 'The San Francisco Call (San Francisco, Calif.) 1895-1913',
            'value': 'the san francisco call (san francisco, calif.) 1895-1913',
            'url': 'https://www.loc.gov/item/sn84038012/',
        },
        'subject_ethnicity': {'class': 'subject_ethnicity', 'label': '', 'value': ''},
        'number_first_issue': {
            'label': '1895-01-01',
            'url': 'https://www.loc.gov/item/sn84038012/1895-01-01/ed-1',
        },
        'number_last_issue': {
            'label': '1913-12-31',
            'url': 'https://www.loc.gov/item/sn84038012/1913-12-31/ed-1',
        },
        'number_issue_count': {'label': '5000', 'value': '5000'},
        'url': 'https://www.loc.gov/item/sn84038012/',
    }

@pytest.fixture
def locgov_newspapers_response(locgov_newspaper_item):
    """loc.gov newspaper list response — results nested at pages[2]['children'][0]['results']."""
    return {
        'pages': [
            {'slug': 'about-this-collection', 'children': [{'results': []}]},
            {'slug': '', 'content': '$search_default'},
            {'slug': 'titles', 'children': [{'results': [locgov_newspaper_item]}]},
            {'slug': 'datasets', 'children': [{'results': []}]},
        ],
        'pagination': {'current': 1, 'total': 4684, 'of': 4684, 'next': None, 'perpage': 25},
    }

@pytest.fixture
def locgov_page_item():
    """Single page entry from loc.gov search results — confirmed field names."""
    return {
        'id': 'http://www.loc.gov/resource/sn84038012/1906-04-18/ed-1/?sp=1',
        'date': '1906-04-18',
        'number_lccn': ['sn84038012'],
        'number_edition': ['1'],
        'number_page': ['0000000001'],
        'partof_title': ['the san francisco call (san francisco, calif.) 1895-1913'],
        'location_state': ['california'],
        'location_city': ['san francisco'],
        'language': ['english'],
        'batch': ['curiv_benicia_ver01'],
        'publication_frequency': ['daily'],
        'resources': [{'url': 'https://www.loc.gov/resource/sn84038012/1906-04-18/ed-1/?sp=1', 'files': 7}],
        'description': ['Sample OCR text from the front page of the San Francisco Call.'],
        'mime_type': ['image/jp2', 'application/pdf', 'text/xml', 'image/jpeg', 'image/jpeg', 'application/json', 'text/plain'],
        'url': 'https://www.loc.gov/resource/sn84038012/1906-04-18/ed-1/?sp=1&q=earthquake',
    }

@pytest.fixture
def locgov_search_response(locgov_page_item):
    """loc.gov page search response — flat shape from at=results,pagination."""
    return {
        'results': [locgov_page_item],
        'pagination': {
            'current': 1,
            'total': 2103648,
            'of': 23745202,
            'next': 'https://www.loc.gov/collections/chronicling-america/?c=25&dl=page&fo=json&sp=2',
            'perpage': 25,
            'from': 1,
            'to': 25,
        },
    }

@pytest.fixture
def locgov_page_details():
    """loc.gov item detail response — confirmed field names from live API."""
    return {
        'item': {
            'date': '1906-04-18',
            'newspaper_title': ['The San Francisco Call'],
            'number_lccn': ['sn84038012'],
            'location_state': ['california'],
            'location_city': ['san francisco'],
            'batch': ['curiv_benicia_ver01'],
            'contributor_names': ['University of California, Riverside'],
        },
        'resource': {
            'pdf': 'https://tile.loc.gov/storage-services/service/ndnp/curiv/batch_curiv_benicia_ver01/data/sn84038012/0001/1906041801/0001.pdf',
            'image': 'https://tile.loc.gov/image-services/iiif/service:ndnp:curiv:batch_curiv_benicia_ver01:data:sn84038012:0001:1906041801:0001/full/pct:6.25/0/default.jpg',
            'fulltext_file': 'https://tile.loc.gov/text-services/word-coordinates-service?segment=/service/ndnp/curiv/batch_curiv_benicia_ver01/data/sn84038012/0001/1906041801/0001.xml&format=alto_xml&full_text=1',
            'word_coordinates': 'https://tile.loc.gov/text-services/word-coordinates-service',
            'url': 'https://www.loc.gov/resource/sn84038012/1906-04-18/ed-1/',
        },
        'pagination': {'current': 1},
    }

@pytest.fixture
def locgov_issue_details():
    """loc.gov issue detail response — confirmed resources[0]['files'] structure."""
    page_files = [
        {'mimetype': 'image/jp2', 'url': 'https://tile.loc.gov/storage-services/service/ndnp/curiv/batch_curiv_benicia_ver01/data/sn84038012/0001/1906041801/0001.jp2', 'use': ''},
        {'mimetype': 'application/pdf', 'url': 'https://tile.loc.gov/storage-services/service/ndnp/curiv/batch_curiv_benicia_ver01/data/sn84038012/0001/1906041801/0001.pdf', 'use': ''},
        {'mimetype': 'text/xml', 'url': 'https://tile.loc.gov/storage-services/service/ndnp/curiv/batch_curiv_benicia_ver01/data/sn84038012/0001/1906041801/0001.xml', 'use': ''},
        {'mimetype': 'image/jpeg', 'url': 'https://tile.loc.gov/.../full/pct:6.25/0/default.jpg', 'levels': 1},
        {'mimetype': 'image/jpeg', 'url': 'https://tile.loc.gov/.../full/pct:12.5/0/default.jpg', 'levels': 1},
        {'mimetype': 'application/json', 'title': 'Image 1 of The San Francisco Call, April 18 1906', 'info': 'https://tile.loc.gov/.../info.json'},
        {'mimetype': 'text/plain', 'fulltext_service': 'https://tile.loc.gov/text-services/word-coordinates-service?segment=/service/ndnp/curiv/batch_curiv_benicia_ver01/data/sn84038012/0001/1906041801/0001.xml&format=alto_xml&full_text=1', 'use': 'text'},
    ]
    return {
        'item': {
            'date': '1906-04-18',
            'date_issued': '1906-04-18',
            'newspaper_title': 'The San Francisco Call',
            'number_lccn': ['sn84038012'],
            'number_edition': ['1'],
        },
        'resources': [
            {
                'files': [page_files, page_files],  # Two pages
                'image': 'https://tile.loc.gov/.../full/pct:6.25/0/default.jpg',
                'url': 'https://www.loc.gov/resource/sn84038012/1906-04-18/ed-1/',
                'word_coordinates': 'https://tile.loc.gov/text-services/word-coordinates-service',
            }
        ],
    }

@pytest.fixture
def locgov_batch_list_response():
    """loc.gov datasets/batch-summary/ response — confirmed per-batch shape."""
    return {
        'datasets': [
            {
                'batch': 'curiv_benicia_ver01',
                'archive_name': 'curiv_benicia_ver01.tar.bz2',
                'archive_created': '2019-07-11T14:31:17+00:00',
                'identifier': 'service:ndnp:curiv:batch_curiv_benicia_ver01',
                'ingested': '2014-11-21T15:57:18-05:00',
                'issue_count': 399,
                'lccns': ['sn92070146', 'sn92070143'],
                'page_count': 3761,
                'sha1': 'b332e33bb1882222a225c1ec182096800cdcd7ef',
                'sha256': '61e5e81b45c940a419da4d86259059cdbb9b2e0ed24420a7ac21f6582abb60fc',
                'size': 181952148,
                'url': 'https://chroniclingamerica.loc.gov/data/ocr/curiv_benicia_ver01.tar.bz2',
            },
            {
                'batch': 'okhi_durant_ver01',
                'archive_name': 'okhi_durant_ver01.tar.bz2',
                'archive_created': '2019-06-26T03:45:30+00:00',
                'identifier': 'service:ndnp:okhi:batch_okhi_durant_ver01',
                'ingested': '2014-11-21T20:47:33-05:00',
                'issue_count': 211,
                'lccns': ['sn85042345'],
                'page_count': 10514,
                'sha1': '9add0f6f1202cb7e0093444488def1d056f202f7',
                'sha256': 'f29a2b4c34360d88f8f4b958dd9df4c95e7beb2bb1f8074008fc31916a75c77e',
                'size': 700571080,
                'url': 'https://chroniclingamerica.loc.gov/data/ocr/okhi_durant_ver01.tar.bz2',
            },
        ]
    }

@pytest.fixture
def legacy_processor():
    return LegacyResponseProcessor()

@pytest.fixture
def locgov_processor():
    return LocGovResponseProcessor()


# ---------------------------------------------------------------------------
# Category 1: REFACTORED tests
# These document what changed from processor.py — from_api_response and
# from_search_result no longer exist as classmethods on the dataclasses.
# Parsing is now the responsibility of the ResponseProcessor subclass.
# ---------------------------------------------------------------------------

class TestRefactoredAPI:
    """[REFACTORED] Documents the API change from processor.py to response_processor.py."""

    def test_newspaper_info_has_no_from_api_response(self):
        """[REFACTORED] NewspaperInfo no longer has from_api_response classmethod.
        Parsing is now done by ResponseProcessor.parse_newspapers().
        This test asserts the old interface is gone to prevent accidental use."""
        assert not hasattr(NewspaperInfo, 'from_api_response'), (
            "NewspaperInfo.from_api_response was removed. "
            "Use LegacyResponseProcessor.parse_newspapers() instead."
        )

    def test_page_info_has_no_from_search_result(self):
        """[REFACTORED] PageInfo no longer has from_search_result classmethod.
        Parsing is now done by ResponseProcessor.parse_pages().
        This test asserts the old interface is gone to prevent accidental use."""
        assert not hasattr(PageInfo, 'from_search_result'), (
            "PageInfo.from_search_result was removed. "
            "Use LegacyResponseProcessor.parse_pages() instead."
        )

    def test_newspaper_info_is_plain_dataclass(self):
        """[REFACTORED] NewspaperInfo is now a plain dataclass — no factory methods.
        Can be constructed directly with keyword arguments."""
        newspaper = NewspaperInfo(
            lccn='sn84038012',
            title='The San Francisco Call',
            place_of_publication=['San Francisco, Calif.'],
            start_year=1895,
            end_year=1913,
            frequency='Daily',
            subject=['San Francisco (Calif.)--Newspapers'],
            language=['English'],
            url='https://chroniclingamerica.loc.gov/lccn/sn84038012/',
        )
        assert newspaper.lccn == 'sn84038012'
        assert newspaper.start_year == 1895

    def test_page_info_is_plain_dataclass(self):
        """[REFACTORED] PageInfo is now a plain dataclass — no factory methods."""
        page = PageInfo(
            item_id='lccn/sn84038012/1906-04-18/ed-1/seq-1',
            lccn='sn84038012',
            title='The San Francisco Call',
            date='1906-04-18',
            edition=1,
            sequence=1,
            page_url='https://chroniclingamerica.loc.gov/lccn/sn84038012/1906-04-18/ed-1/seq-1',
            pdf_url='https://chroniclingamerica.loc.gov/lccn/sn84038012/1906-04-18/ed-1/seq-1.pdf',
            jp2_url=None,
            ocr_text='Sample OCR text.',
            word_count=None,
        )
        assert page.lccn == 'sn84038012'
        assert page.date == '1906-04-18'

    def test_parsing_newspapers_requires_processor(self, legacy_processor, legacy_newspapers_response):
        """[REFACTORED] The old processor.process_newspapers_response() is now
        LegacyResponseProcessor.parse_newspapers(). Asserts the new call site works."""
        newspapers = legacy_processor.parse_newspapers(legacy_newspapers_response)
        assert len(newspapers) == 1
        assert newspapers[0].lccn == 'sn84038012'

    def test_parsing_pages_requires_processor(self, legacy_processor, legacy_search_response):
        """[REFACTORED] The old processor.process_search_response() is now
        LegacyResponseProcessor.parse_pages(). Asserts the new call site works."""
        pages = legacy_processor.parse_pages(legacy_search_response)
        assert len(pages) == 1
        assert pages[0].lccn == 'sn84038012'


# ---------------------------------------------------------------------------
# Category 2: PORTABLE tests
# Adapted from test_processor.py. Logic identical; call site updated.
# ---------------------------------------------------------------------------

class TestNewspaperInfoPortable:
    """[PORTABLE] NewspaperInfo dataclass behaviour — adapted from test_processor.py."""

    def test_parse_year_valid(self):
        """[PORTABLE] Year parsing with valid inputs."""
        assert NewspaperInfo._parse_year('1900') == 1900
        assert NewspaperInfo._parse_year('From 1895 to 1913') == 1895
        assert NewspaperInfo._parse_year('Published in 2020') == 2020

    def test_parse_year_invalid(self):
        """[PORTABLE] Year parsing with invalid inputs."""
        assert NewspaperInfo._parse_year(None) is None
        assert NewspaperInfo._parse_year('') is None
        assert NewspaperInfo._parse_year('No year here') is None
        assert NewspaperInfo._parse_year('123') is None


class TestPageInfoPortable:
    """[PORTABLE] PageInfo dataclass behaviour — adapted from test_processor.py."""

    def test_format_date_yyyymmdd(self):
        """[PORTABLE] Date normalisation from YYYYMMDD format."""
        assert PageInfo._format_date('19060418') == '1906-04-18'

    def test_format_date_already_iso(self):
        """[PORTABLE] Date already in ISO format passes through unchanged."""
        assert PageInfo._format_date('1906-04-18') == '1906-04-18'

    def test_format_date_empty(self):
        """[PORTABLE] Empty date returns empty string."""
        assert PageInfo._format_date('') == ''

    def test_format_date_unrecognised(self):
        """[PORTABLE] Unrecognised format passes through as-is."""
        assert PageInfo._format_date('April 18 1906') == 'April 18 1906'


class TestNewsDataProcessorPortable:
    """[PORTABLE] Utility method tests — adapted from test_processor.py.
    These methods now live in NewspaperUtilsMixin but behaviour is identical."""

    @pytest.fixture
    def processor(self):
        """Processor combining all mixins with LegacyResponseProcessor."""
        class FullProcessor(DeduplicationMixin, NewspaperUtilsMixin, LegacyResponseProcessor):
            pass
        return FullProcessor()

    @pytest.fixture
    def sample_newspapers(self):
        return [
            NewspaperInfo('ca1', 'CA Paper', ['San Francisco, California'], 1900, 1920, 'Daily', [], ['English'], ''),
            NewspaperInfo('ny1', 'NY Paper', ['New York, New York'], 1900, 1920, 'Daily', [], ['English'], ''),
            NewspaperInfo('ca2', 'CA Paper 2', ['Los Angeles, California'], 1900, 1920, 'Daily', [], ['English'], ''),
            NewspaperInfo('es1', 'Spanish Paper', ['Miami, Florida'], 1900, 1920, 'Daily', [], ['Spanish'], ''),
        ]

    def test_filter_by_state(self, processor, sample_newspapers):
        """[PORTABLE] Filter newspapers by state."""
        filtered = processor.filter_newspapers_by_criteria(sample_newspapers, state='California')
        assert len(filtered) == 2
        assert all('California' in p for n in filtered for p in n.place_of_publication)

    def test_filter_by_language(self, processor, sample_newspapers):
        """[PORTABLE] Filter newspapers by language."""
        filtered = processor.filter_newspapers_by_criteria(sample_newspapers, language='English')
        assert len(filtered) == 3

    def test_filter_by_year_range(self, processor):
        """[PORTABLE] Filter newspapers by year range."""
        newspapers = [
            NewspaperInfo('old1', 'Old', ['City'], 1850, 1880, 'Daily', [], ['English'], ''),
            NewspaperInfo('new1', 'New', ['City'], 1900, 1920, 'Daily', [], ['English'], ''),
            NewspaperInfo('lap1', 'Overlap', ['City'], 1880, 1910, 'Daily', [], ['English'], ''),
        ]
        filtered = processor.filter_newspapers_by_criteria(newspapers, start_year=1890, end_year=1920)
        assert len(filtered) == 2
        lccns = [n.lccn for n in filtered]
        assert 'new1' in lccns
        assert 'lap1' in lccns

    def test_get_newspaper_summary(self, processor, sample_newspapers):
        """[PORTABLE] Summary statistics generation."""
        summary = processor.get_newspaper_summary(sample_newspapers)
        assert summary['total_newspapers'] == 4
        assert summary['languages']['English'] == 3
        assert summary['languages']['Spanish'] == 1

    def test_get_newspaper_summary_empty(self, processor):
        """[PORTABLE] Summary of empty list."""
        assert processor.get_newspaper_summary([]) == {'total_newspapers': 0}

    def test_reset_deduplication(self, processor, legacy_search_response):
        """[PORTABLE] Deduplication cache resets correctly."""
        processor.parse_pages(legacy_search_response)
        assert len(processor._seen_items) == 1
        processor.reset_deduplication()
        assert len(processor._seen_items) == 0
        pages = processor.parse_pages(legacy_search_response)
        assert len(pages) == 1


# ---------------------------------------------------------------------------
# Category 3: LEGACY and LOCGOV mirrored tests
# Each test exists in both classes. Differences are noted inline.
# ---------------------------------------------------------------------------

class TestLegacyParseNewspapers:
    """[LEGACY] parse_newspapers — legacy chroniclingamerica.loc.gov format."""

    def test_parses_lccn(self, legacy_processor, legacy_newspapers_response):
        """[LEGACY] LCCN extracted from top-level 'lccn' field."""
        result = legacy_processor.parse_newspapers(legacy_newspapers_response)
        assert result[0].lccn == 'sn84038012'

    def test_parses_title(self, legacy_processor, legacy_newspapers_response):
        """[LEGACY] Title extracted from 'title' field."""
        result = legacy_processor.parse_newspapers(legacy_newspapers_response)
        assert result[0].title == 'The San Francisco Call'

    def test_parses_state(self, legacy_processor, legacy_newspapers_response):
        """[LEGACY] State from 'state' field (plain string)."""
        result = legacy_processor.parse_newspapers(legacy_newspapers_response)
        assert 'California' in result[0].place_of_publication

    def test_parses_start_year(self, legacy_processor, legacy_newspapers_response):
        """[LEGACY] Start year from 'start_year' string field."""
        result = legacy_processor.parse_newspapers(legacy_newspapers_response)
        assert result[0].start_year == 1895

    def test_parses_end_year(self, legacy_processor, legacy_newspapers_response):
        """[LEGACY] End year from 'end_year' string field."""
        result = legacy_processor.parse_newspapers(legacy_newspapers_response)
        assert result[0].end_year == 1913

    def test_parses_language(self, legacy_processor, legacy_newspapers_response):
        """[LEGACY] Language from 'language' list field."""
        result = legacy_processor.parse_newspapers(legacy_newspapers_response)
        assert 'English' in result[0].language

    def test_empty_response(self, legacy_processor):
        """[LEGACY] Empty newspapers list returns empty result."""
        result = legacy_processor.parse_newspapers({'newspapers': []})
        assert result == []

    def test_missing_key_returns_empty(self, legacy_processor):
        """[LEGACY] Response missing 'newspapers' key returns empty result."""
        result = legacy_processor.parse_newspapers({})
        assert result == []

    def test_returns_newspaper_info_instances(self, legacy_processor, legacy_newspapers_response):
        """[LEGACY] Returns list of NewspaperInfo dataclasses."""
        result = legacy_processor.parse_newspapers(legacy_newspapers_response)
        assert all(isinstance(n, NewspaperInfo) for n in result)

    def test_parses_state_field(self, legacy_processor, legacy_newspapers_response):
        """[LEGACY] state field from the 'state' key."""
        result = legacy_processor.parse_newspapers(legacy_newspapers_response)
        assert result[0].state == 'California'

    def test_city_none_when_state_field_present(self, legacy_processor, legacy_newspapers_response):
        """[LEGACY] With an explicit 'state', place becomes [state] and no city is derivable."""
        result = legacy_processor.parse_newspapers(legacy_newspapers_response)
        assert result[0].city is None

    def test_splits_city_state_from_place_string(self, legacy_processor):
        """[LEGACY] Without a 'state' field, city/state split from the free-text place string."""
        response = {'newspapers': [{
            'lccn': 'sn111', 'title': 'Example',
            'place_of_publication': ['Sacramento, Calif.'],
            'start_year': '1900', 'end_year': '1910',
            'language': ['English'], 'subject': [],
            'url': 'https://example.com/',
        }]}
        result = legacy_processor.parse_newspapers(response)
        assert result[0].city == 'Sacramento'
        assert result[0].state == 'Calif.'

class TestLocGovParseNewspapers:
    """[LOCGOV] parse_newspapers — confirmed loc.gov title list format."""

    def test_parses_lccn(self, locgov_processor, locgov_newspapers_response):
        """[LOCGOV] LCCN from 'number_lccn' plain list field."""
        result = locgov_processor.parse_newspapers(locgov_newspapers_response)
        assert result[0].lccn == 'sn84038012'

    def test_parses_title(self, locgov_processor, locgov_newspapers_response):
        """[LOCGOV] Title from plain 'title' string field."""
        result = locgov_processor.parse_newspapers(locgov_newspapers_response)
        assert 'San Francisco Call' in result[0].title

    def test_parses_state_from_dict(self, locgov_processor, locgov_newspapers_response):
        """[LOCGOV] State from 'location_state' dict — uses 'label' key (Title Case).
        NOTE: Unlike legacy, location_state is a dict not a string/list."""
        result = locgov_processor.parse_newspapers(locgov_newspapers_response)
        assert 'California' in result[0].place_of_publication

    def test_parses_start_year_from_first_issue(self, locgov_processor, locgov_newspapers_response):
        """[LOCGOV] Start year from number_first_issue['label'] date string.
        NOTE: Unlike legacy 'start_year' field, this is a date not a year."""
        result = locgov_processor.parse_newspapers(locgov_newspapers_response)
        assert result[0].start_year == 1895

    def test_parses_end_year_from_last_issue(self, locgov_processor, locgov_newspapers_response):
        """[LOCGOV] End year from number_last_issue['label'] date string."""
        result = locgov_processor.parse_newspapers(locgov_newspapers_response)
        assert result[0].end_year == 1913

    def test_parses_language_from_dict(self, locgov_processor, locgov_newspapers_response):
        """[LOCGOV] Language from 'language' dict — uses 'label' key.
        NOTE: Unlike legacy, language is a dict not a list."""
        result = locgov_processor.parse_newspapers(locgov_newspapers_response)
        assert 'English' in result[0].language

    def test_empty_response(self, locgov_processor):
        """[LOCGOV] Response with empty results list returns empty result."""
        response = {'pages': [
            {}, {},
            {'children': [{'results': []}]},
            {},
        ]}
        result = locgov_processor.parse_newspapers(response)
        assert result == []

    def test_missing_pages_returns_empty(self, locgov_processor):
        """[LOCGOV] Response missing 'pages' key returns empty result."""
        result = locgov_processor.parse_newspapers({})
        assert result == []

    def test_returns_newspaper_info_instances(self, locgov_processor, locgov_newspapers_response):
        """[LOCGOV] Returns list of NewspaperInfo dataclasses."""
        result = locgov_processor.parse_newspapers(locgov_newspapers_response)
        assert all(isinstance(n, NewspaperInfo) for n in result)

    def test_parses_state_field(self, locgov_processor, locgov_newspapers_response):
        """[LOCGOV] state field from location_state['label']."""
        result = locgov_processor.parse_newspapers(locgov_newspapers_response)
        assert result[0].state == 'California'

    def test_parses_city_from_location_city(self, locgov_processor, locgov_newspapers_response):
        """[LOCGOV] city field from the location_city list (first entry, lowercase as returned)."""
        result = locgov_processor.parse_newspapers(locgov_newspapers_response)
        assert result[0].city == 'san francisco'

class TestLegacyParsePages:
    """[LEGACY] parse_pages — legacy search result format."""

    def test_parses_item_id(self, legacy_processor, legacy_search_response):
        """[LEGACY] item_id stripped from URL path."""
        result = legacy_processor.parse_pages(legacy_search_response)
        assert 'sn84038012' in result[0].item_id

    def test_parses_date_yyyymmdd(self, legacy_processor, legacy_search_response):
        """[LEGACY] Date normalised from YYYYMMDD to YYYY-MM-DD."""
        result = legacy_processor.parse_pages(legacy_search_response)
        assert result[0].date == '1906-04-18'

    def test_parses_ocr_from_ocr_eng(self, legacy_processor, legacy_search_response):
        """[LEGACY] OCR text from 'ocr_eng' field — actual text content."""
        result = legacy_processor.parse_pages(legacy_search_response)
        assert result[0].ocr_text == 'Full OCR text content here.'

    def test_parses_lccn(self, legacy_processor, legacy_search_response):
        """[LEGACY] LCCN from 'lccn' field."""
        result = legacy_processor.parse_pages(legacy_search_response)
        assert result[0].lccn == 'sn84038012'

    def test_reads_from_items_key(self, legacy_processor):
        """[LEGACY] Results read from 'items' key."""
        response = {'items': [{'id': 'test', 'lccn': 'sn123', 'title': 'T', 'date': '19060101'}]}
        result = legacy_processor.parse_pages(response)
        assert len(result) == 1

    def test_empty_items(self, legacy_processor):
        """[LEGACY] Empty items list returns empty result."""
        result = legacy_processor.parse_pages({'items': []})
        assert result == []

    def test_returns_page_info_instances(self, legacy_processor, legacy_search_response):
        """[LEGACY] Returns list of PageInfo dataclasses."""
        result = legacy_processor.parse_pages(legacy_search_response)
        assert all(isinstance(p, PageInfo) for p in result)


class TestLocGovParsePages:
    """[LOCGOV] parse_pages — confirmed loc.gov search result format."""

    def test_parses_item_id(self, locgov_processor, locgov_search_response):
        """[LOCGOV] item_id stripped from loc.gov resource URL."""
        result = locgov_processor.parse_pages(locgov_search_response)
        assert 'sn84038012' in result[0].item_id

    def test_parses_date_already_iso(self, locgov_processor, locgov_search_response):
        """[LOCGOV] Date already in YYYY-MM-DD — no conversion applied."""
        result = locgov_processor.parse_pages(locgov_search_response)
        assert result[0].date == '1906-04-18'

    def test_parses_ocr_from_description(self, locgov_processor, locgov_search_response):
        """[LOCGOV] OCR text from 'description' field — snippet, not full text.
        NOTE: Unlike legacy ocr_eng, this is a truncated snippet (~1000 chars)."""
        result = locgov_processor.parse_pages(locgov_search_response)
        assert result[0].ocr_text == 'Sample OCR text from the front page of the San Francisco Call.'

    def test_parses_lccn_from_list(self, locgov_processor, locgov_search_response):
        """[LOCGOV] LCCN from 'number_lccn' list field."""
        result = locgov_processor.parse_pages(locgov_search_response)
        assert result[0].lccn == 'sn84038012'

    def test_reads_from_flat_results_key(self, locgov_processor, locgov_search_response):
        """[LOCGOV] Results read from top-level 'results' key — at= normalized flat shape."""
        result = locgov_processor.parse_pages(locgov_search_response)
        assert len(result) == 1
        
    def test_empty_results(self, locgov_processor):
        """[LOCGOV] Empty results list returns empty result."""
        response = {'pages': [{}, {'children': [{'results': []}]}, {}, {}]}
        result = locgov_processor.parse_pages(response)
        response = {'results': [], 'pagination': {}}

    def test_returns_page_info_instances(self, locgov_processor, locgov_search_response):
        """[LOCGOV] Returns list of PageInfo dataclasses."""
        result = locgov_processor.parse_pages(locgov_search_response)
        assert all(isinstance(p, PageInfo) for p in result)


class TestLegacyParsePageDetails:
    """[LEGACY] parse_page_details — legacy single page endpoint format."""

    def test_parses_sequence(self, legacy_processor, legacy_page_details):
        """[LEGACY] Sequence from 'sequence' field."""
        result = legacy_processor.parse_page_details(legacy_page_details, 'https://chroniclingamerica.loc.gov/lccn/sn84038012/1906-04-18/ed-1/seq-1/')
        assert result.sequence == 1

    def test_parses_date(self, legacy_processor, legacy_page_details):
        """[LEGACY] Date from issue.date_issued."""
        result = legacy_processor.parse_page_details(legacy_page_details, 'https://chroniclingamerica.loc.gov/lccn/sn84038012/1906-04-18/ed-1/seq-1/')
        assert result.date == '1906-04-18'

    def test_parses_pdf_url(self, legacy_processor, legacy_page_details):
        """[LEGACY] PDF URL from 'pdf' field."""
        result = legacy_processor.parse_page_details(legacy_page_details, '')
        assert result.pdf_url == legacy_page_details['pdf']

    def test_parses_ocr_url(self, legacy_processor, legacy_page_details):
        """[LEGACY] OCR URL from 'text' field."""
        result = legacy_processor.parse_page_details(legacy_page_details, '')
        assert result.ocr_text == legacy_page_details['text']

    def test_returns_none_on_error(self, legacy_processor):
        """[LEGACY] Returns None on malformed input."""
        result = legacy_processor.parse_page_details(None, '')
        assert result is None


class TestLocGovParsePageDetails:
    """[LOCGOV] parse_page_details — confirmed loc.gov item detail format."""

    def test_parses_sequence_from_pagination(self, locgov_processor, locgov_page_details):
        """[LOCGOV] Sequence from pagination.current field."""
        result = locgov_processor.parse_page_details(locgov_page_details, 'https://www.loc.gov/resource/sn84038012/1906-04-18/ed-1/?sp=1')
        assert result.sequence == 1

    def test_parses_date_from_item(self, locgov_processor, locgov_page_details):
        """[LOCGOV] Date from item.date field — already YYYY-MM-DD."""
        result = locgov_processor.parse_page_details(locgov_page_details, '')
        assert result.date == '1906-04-18'

    def test_parses_pdf_url(self, locgov_processor, locgov_page_details):
        """[LOCGOV] PDF URL from resource.pdf field — confirmed key name."""
        result = locgov_processor.parse_page_details(locgov_page_details, '')
        assert result.pdf_url == locgov_page_details['resource']['pdf']

    def test_parses_jp2_url(self, locgov_processor, locgov_page_details):
        """[LOCGOV] JP2 URL from resource.image field — confirmed key name."""
        result = locgov_processor.parse_page_details(locgov_page_details, '')
        assert result.jp2_url == locgov_page_details['resource']['image']

    def test_parses_ocr_url_from_fulltext_file(self, locgov_processor, locgov_page_details):
        """[LOCGOV] OCR URL from resource.fulltext_file — confirmed key name.
        NOTE: Unlike legacy 'text' field, this is fulltext_file."""
        result = locgov_processor.parse_page_details(locgov_page_details, '')
        assert result.ocr_text == locgov_page_details['resource']['fulltext_file']

    def test_returns_none_on_error(self, locgov_processor):
        """[LOCGOV] Returns None on malformed input."""
        result = locgov_processor.parse_page_details(None, '')
        assert result is None


class TestLegacyParseIssue:
    """[LEGACY] parse_page_from_issue and parse_issue — legacy issue format."""

    def test_parse_page_from_issue(self, legacy_processor, legacy_issue_details):
        """[LEGACY] Parses single page from issue pages list."""
        page_data = legacy_issue_details['pages'][0]
        result = legacy_processor.parse_page_from_issue(page_data, legacy_issue_details)
        assert result is not None
        assert result.sequence == 1
        assert result.date == '1906-04-18'
        assert result.lccn == 'sn84038012'

    def test_parse_page_from_issue_missing_url(self, legacy_processor, legacy_issue_details):
        """[LEGACY] Returns None when page_data has no URL."""
        result = legacy_processor.parse_page_from_issue({}, legacy_issue_details)
        assert result is None


class TestLocGovParseIssue:
    """[LOCGOV] parse_page_from_issue and parse_issue — confirmed loc.gov issue format."""

    def test_parse_issue_returns_all_pages(self, locgov_processor, locgov_issue_details):
        """[LOCGOV] parse_issue iterates resources[0]['files'] and returns one PageInfo per page."""
        result = locgov_processor.parse_issue(locgov_issue_details)
        assert len(result) == 2

    def test_parse_issue_assigns_sequence_positionally(self, locgov_processor, locgov_issue_details):
        """[LOCGOV] Sequence numbers assigned positionally from files list index."""
        result = locgov_processor.parse_issue(locgov_issue_details)
        assert result[0].sequence == 1
        assert result[1].sequence == 2

    def test_parse_page_from_issue_extracts_jp2(self, locgov_processor, locgov_issue_details):
        """[LOCGOV] JP2 URL extracted from image/jp2 mimetype entry."""
        page_files = locgov_issue_details['resources'][0]['files'][0]
        result = locgov_processor.parse_page_from_issue(page_files, locgov_issue_details, 1)
        assert result.jp2_url is not None
        assert '.jp2' in result.jp2_url

    def test_parse_page_from_issue_extracts_pdf(self, locgov_processor, locgov_issue_details):
        """[LOCGOV] PDF URL extracted from application/pdf mimetype entry."""
        page_files = locgov_issue_details['resources'][0]['files'][0]
        result = locgov_processor.parse_page_from_issue(page_files, locgov_issue_details, 1)
        assert result.pdf_url is not None
        assert '.pdf' in result.pdf_url

    def test_parse_page_from_issue_extracts_ocr(self, locgov_processor, locgov_issue_details):
        """[LOCGOV] OCR URL from fulltext_service on text/plain mimetype entry."""
        page_files = locgov_issue_details['resources'][0]['files'][0]
        result = locgov_processor.parse_page_from_issue(page_files, locgov_issue_details, 1)
        assert result.ocr_text is not None
        assert 'word-coordinates-service' in result.ocr_text

    def test_parse_issue_empty_files(self, locgov_processor):
        """[LOCGOV] Issue with no files returns empty list."""
        issue = {'resources': [{'files': [], 'url': '', 'image': '', 'word_coordinates': ''}], 'item': {}}
        result = locgov_processor.parse_issue(issue)
        assert result == []

    def test_parse_issue_missing_resources(self, locgov_processor):
        """[LOCGOV] Issue with no resources key returns empty list."""
        result = locgov_processor.parse_issue({})
        assert result == []


# ---------------------------------------------------------------------------
# Category 4: LOCGOV_EXTRA tests
# Additional tests for response structure unique to the new API.
# No legacy equivalent.
# ---------------------------------------------------------------------------

class TestLocGovExtra:
    """[LOCGOV_EXTRA] Tests for loc.gov-specific response structure."""

    def test_parse_pagination_total_vs_of(self, locgov_processor, locgov_search_response):
        """[LOCGOV_EXTRA] pagination.total is filtered count; pagination.of is full collection.
        These are different values — 'total' is what callers should use for result count."""
        result = locgov_processor.parse_pagination(locgov_search_response)
        assert result['total_results'] == 2103648   # filtered
        assert result['total_results'] != 23745202  # full collection

    def test_parse_pagination_next_url(self, locgov_processor, locgov_search_response):
        """[LOCGOV_EXTRA] next_url present when more pages exist."""
        result = locgov_processor.parse_pagination(locgov_search_response)
        assert result['next_url'] is not None
        assert 'sp=2' in result['next_url']

    def test_parse_pagination_missing_pagination(self, locgov_processor):
        """[LOCGOV_EXTRA] Missing pagination key returns safe defaults."""
        result = locgov_processor.parse_pagination({})
        assert result['current_page'] == 1
        assert result['total_results'] == 0
        assert result['next_url'] is None

    def test_parse_batch_list_returns_all_batches(self, locgov_processor, locgov_batch_list_response):
        """[LOCGOV_EXTRA] Batch list parses all entries from datasets key."""
        result = locgov_processor.parse_batch_list(locgov_batch_list_response)
        assert len(result) == 2

    def test_parse_batch_list_batch_name_without_prefix(self, locgov_processor, locgov_batch_list_response):
        """[LOCGOV_EXTRA] batch field is name without batch_ prefix — matches fa=batch: filter."""
        result = locgov_processor.parse_batch_list(locgov_batch_list_response)
        assert result[0]['batch'] == 'curiv_benicia_ver01'
        assert not result[0]['batch'].startswith('batch_')

    def test_parse_batch_list_has_page_count(self, locgov_processor, locgov_batch_list_response):
        """[LOCGOV_EXTRA] page_count available per batch for size estimation."""
        result = locgov_processor.parse_batch_list(locgov_batch_list_response)
        assert result[1]['page_count'] == 10514

    def test_parse_batch_list_empty(self, locgov_processor):
        """[LOCGOV_EXTRA] Empty datasets key returns empty list."""
        result = locgov_processor.parse_batch_list({'datasets': []})
        assert result == []

    def test_page_search_results_at_top_level(self, locgov_processor):
        """[LOCGOV_EXTRA] Flat 'results' key is authoritative — at= normalization.
        Nested pages structure is ignored by parse_pages."""
        response = {
            'results': [{'id': 'should-be-read', 'date': '1906-04-18',
                        'number_lccn': ['sn84038012'], 'number_edition': ['1'],
                        'partof_title': ['test title']}],
            'pagination': {'current': 1, 'total': 1},
            'pages': [{}, {'children': [{'results': []}]}, {}, {}],
        }
        result = locgov_processor.parse_pages(response)
        assert len(result) == 1
        assert result[0].item_id == 'should-be-read'
        
    def test_newspaper_list_at_pages_2_not_pages_1(self, locgov_processor, locgov_newspapers_response):
        """[LOCGOV_EXTRA] Newspaper list results at pages[2], page search at pages[1].
        These are different endpoints — nesting index differs."""
        newspapers = locgov_processor.parse_newspapers(locgov_newspapers_response)
        assert len(newspapers) == 1

        # Same response fed to parse_pages should find nothing (wrong nesting)
        pages = locgov_processor.parse_pages(locgov_newspapers_response)
        assert pages == []

    def test_strip_base_url_locgov(self, locgov_processor):
        """[LOCGOV_EXTRA] strip_base_url removes loc.gov prefix."""
        url = 'https://www.loc.gov/resource/sn84038012/1906-04-18/ed-1/'
        result = locgov_processor.strip_base_url(url)
        assert result == 'resource/sn84038012/1906-04-18/ed-1/'

    def test_strip_base_url_passthrough(self, locgov_processor):
        """[LOCGOV_EXTRA] strip_base_url passes through unrecognised URLs unchanged."""
        url = 'https://tile.loc.gov/storage-services/service/ndnp/0001.pdf'
        result = locgov_processor.strip_base_url(url)
        assert result == url


# ---------------------------------------------------------------------------
# Category 5: COMPAT tests
# Assert both processors return the same Python types from equivalent methods.
# ---------------------------------------------------------------------------

class TestCompatibilityGuarantee:
    """[COMPAT] Both processors return identical types from equivalent methods."""

    def test_parse_newspapers_return_type(self, legacy_processor, locgov_processor,
                                          legacy_newspapers_response, locgov_newspapers_response):
        """[COMPAT] Both parse_newspapers return List[NewspaperInfo]."""
        legacy = legacy_processor.parse_newspapers(legacy_newspapers_response)
        locgov = locgov_processor.parse_newspapers(locgov_newspapers_response)
        assert isinstance(legacy, list)
        assert isinstance(locgov, list)
        assert all(isinstance(n, NewspaperInfo) for n in legacy)
        assert all(isinstance(n, NewspaperInfo) for n in locgov)

    def test_parse_pages_return_type(self, legacy_processor, locgov_processor,
                                     legacy_search_response, locgov_search_response):
        """[COMPAT] Both parse_pages return List[PageInfo]."""
        legacy = legacy_processor.parse_pages(legacy_search_response)
        locgov = locgov_processor.parse_pages(locgov_search_response)
        assert all(isinstance(p, PageInfo) for p in legacy)
        assert all(isinstance(p, PageInfo) for p in locgov)

    def test_parse_page_details_return_type(self, legacy_processor, locgov_processor,
                                             legacy_page_details, locgov_page_details):
        """[COMPAT] Both parse_page_details return Optional[PageInfo]."""
        legacy = legacy_processor.parse_page_details(legacy_page_details, '')
        locgov = locgov_processor.parse_page_details(locgov_page_details, '')
        assert legacy is None or isinstance(legacy, PageInfo)
        assert locgov is None or isinstance(locgov, PageInfo)

    def test_parse_issue_return_type(self, legacy_processor, locgov_processor,
                                     legacy_issue_details, locgov_issue_details):
        """[COMPAT] Both parse_issue return List[PageInfo]."""
        legacy = legacy_processor.parse_issue(legacy_issue_details)
        locgov = locgov_processor.parse_issue(locgov_issue_details)
        assert isinstance(legacy, list)
        assert isinstance(locgov, list)
        assert all(isinstance(p, PageInfo) for p in legacy)
        assert all(isinstance(p, PageInfo) for p in locgov)

    def test_newspaper_info_fields_present_in_both(self, legacy_processor, locgov_processor,
                                                    legacy_newspapers_response, locgov_newspapers_response):
        """[COMPAT] NewspaperInfo from both processors has all required fields populated or None."""
        legacy = legacy_processor.parse_newspapers(legacy_newspapers_response)[0]
        locgov = locgov_processor.parse_newspapers(locgov_newspapers_response)[0]
        required_fields = ['lccn', 'title', 'place_of_publication', 'start_year',
                           'end_year', 'frequency', 'subject', 'language', 'url']
        for field in required_fields:
            assert hasattr(legacy, field), f"Legacy result missing field: {field}"
            assert hasattr(locgov, field), f"LocGov result missing field: {field}"

    def test_page_info_fields_present_in_both(self, legacy_processor, locgov_processor,
                                               legacy_search_response, locgov_search_response):
        """[COMPAT] PageInfo from both processors has all required fields populated or None."""
        legacy = legacy_processor.parse_pages(legacy_search_response)[0]
        locgov = locgov_processor.parse_pages(locgov_search_response)[0]
        required_fields = ['item_id', 'lccn', 'title', 'date', 'edition', 'sequence',
                           'page_url', 'pdf_url', 'jp2_url', 'ocr_text', 'word_count']
        for field in required_fields:
            assert hasattr(legacy, field), f"Legacy result missing field: {field}"
            assert hasattr(locgov, field), f"LocGov result missing field: {field}"

    def test_swap_processor_same_call_site(self, legacy_search_response, locgov_search_response):
        """[COMPAT] Downstream code can swap processors by changing one line."""
        def process_with(processor, response):
            pages = processor.parse_pages(response)
            return [p.lccn for p in pages]

        legacy_lccns = process_with(LegacyResponseProcessor(), legacy_search_response)
        locgov_lccns = process_with(LocGovResponseProcessor(), locgov_search_response)
        assert legacy_lccns == locgov_lccns


# ---------------------------------------------------------------------------
# Category 6: MIXIN isolation tests
# Test mixin behaviour independently of processor subclass.
# ---------------------------------------------------------------------------

class TestDeduplicationMixin:
    """[MIXIN] DeduplicationMixin isolation tests."""

    @pytest.fixture
    def dedup_legacy(self):
        class DeduplicatedLegacy(DeduplicationMixin, LegacyResponseProcessor):
            pass
        return DeduplicatedLegacy()

    @pytest.fixture
    def dedup_locgov(self):
        class DeduplicatedLocGov(DeduplicationMixin, LocGovResponseProcessor):
            pass
        return DeduplicatedLocGov()

    def test_deduplicates_by_item_id_legacy(self, dedup_legacy):
        """[MIXIN] Duplicate item_ids filtered from legacy response."""
        response = {'items': [
            {'id': 'item1', 'lccn': 'sn1', 'title': 'T', 'date': '19060101'},
            {'id': 'item1', 'lccn': 'sn1', 'title': 'T', 'date': '19060101'},
            {'id': 'item2', 'lccn': 'sn2', 'title': 'T', 'date': '19060101'},
        ]}
        result = dedup_legacy.parse_pages(response)
        assert len(result) == 2

    def test_deduplicates_by_item_id_locgov(self, dedup_locgov, locgov_page_item):
        """[MIXIN] Duplicate item_ids filtered from locgov response."""
        response = {'results': [locgov_page_item, locgov_page_item], 'pagination': {}}
        result = dedup_locgov.parse_pages(response)
        assert len(result) == 1

    def test_deduplicate_false_bypasses_filter(self, dedup_legacy):
        """[MIXIN] deduplicate=False returns all items including duplicates."""
        response = {'items': [
            {'id': 'item1', 'lccn': 'sn1', 'title': 'T', 'date': '19060101'},
            {'id': 'item1', 'lccn': 'sn1', 'title': 'T', 'date': '19060101'},
        ]}
        result = dedup_legacy.parse_pages(response, deduplicate=False)
        assert len(result) == 2

    def test_deduplicate_false_does_not_update_cache(self, dedup_legacy):
        """[MIXIN] deduplicate=False does not add items to seen cache."""
        response = {'items': [{'id': 'item1', 'lccn': 'sn1', 'title': 'T', 'date': '19060101'}]}
        dedup_legacy.parse_pages(response, deduplicate=False)
        assert len(dedup_legacy._seen_items) == 0

    def test_reset_clears_cache(self, dedup_legacy):
        """[MIXIN] reset_deduplication clears seen items cache."""
        response = {'items': [{'id': 'item1', 'lccn': 'sn1', 'title': 'T', 'date': '19060101'}]}
        dedup_legacy.parse_pages(response)
        assert len(dedup_legacy._seen_items) == 1
        dedup_legacy.reset_deduplication()
        assert len(dedup_legacy._seen_items) == 0

    def test_mro_resolves_correctly_legacy(self, dedup_legacy):
        """[MIXIN] MRO: DeduplicationMixin.parse_pages wraps LegacyResponseProcessor.parse_pages."""
        mro_names = [cls.__name__ for cls in type(dedup_legacy).__mro__]
        dedup_idx = mro_names.index('DeduplicationMixin')
        legacy_idx = mro_names.index('LegacyResponseProcessor')
        assert dedup_idx < legacy_idx

    def test_mro_resolves_correctly_locgov(self, dedup_locgov):
        """[MIXIN] MRO: DeduplicationMixin.parse_pages wraps LocGovResponseProcessor.parse_pages."""
        mro_names = [cls.__name__ for cls in type(dedup_locgov).__mro__]
        dedup_idx = mro_names.index('DeduplicationMixin')
        locgov_idx = mro_names.index('LocGovResponseProcessor')
        assert dedup_idx < locgov_idx


class TestNewspaperUtilsMixin:
    """[MIXIN] NewspaperUtilsMixin isolation tests."""

    @pytest.fixture
    def utils_processor(self):
        class UtilsProcessor(NewspaperUtilsMixin, LegacyResponseProcessor):
            pass
        return UtilsProcessor()

    def test_validate_date_range_new_start_bound(self, utils_processor):
        """[MIXIN] Start bound is 1736-08-03 — not 1836 as in legacy processor.
        Dates between 1736-08-03 and 1836 are now valid."""
        assert utils_processor.validate_date_range('1750', '1800') is True
        # 1736-08-03 is the exact start — year-only expands to 1736-01-01 which is before it
        assert utils_processor.validate_date_range('1736-08-03', '1800') is True
        assert utils_processor.validate_date_range('1736-01-01', '1800') is False  # before start

    def test_validate_date_range_before_1736_invalid(self, utils_processor):
        """[MIXIN] Dates before 1736-08-03 remain invalid."""
        assert utils_processor.validate_date_range('1700', '1735') is False

    def test_validate_date_range_new_end_bound(self, utils_processor):
        """[MIXIN] End bound is 1963-11-30 — not present day.
        Dates after 1963-11-30 are invalid."""
        assert utils_processor.validate_date_range('1900', '1963-11-30') is True
        assert utils_processor.validate_date_range('1900', '1963-12-31') is False  # after end
        assert utils_processor.validate_date_range('1900', '1964') is False

    def test_validate_date_range_reversed(self, utils_processor):
        """[MIXIN] End before start is invalid."""
        assert utils_processor.validate_date_range('1910', '1900') is False

    def test_validate_date_range_invalid_format(self, utils_processor):
        """[MIXIN] Non-date strings return False."""
        assert utils_processor.validate_date_range('invalid', '1900') is False

    def test_strip_base_url_legacy(self):
        """[MIXIN] LegacyResponseProcessor.strip_base_url removes legacy prefix."""
        processor = LegacyResponseProcessor()
        url = 'https://chroniclingamerica.loc.gov/lccn/sn84038012/1906-04-18/ed-1/seq-1/'
        result = processor.strip_base_url(url)
        assert result == 'lccn/sn84038012/1906-04-18/ed-1/seq-1/'

    def test_strip_base_url_unrecognised_passthrough(self):
        """[MIXIN] strip_base_url on unrecognised URL returns unchanged."""
        processor = LegacyResponseProcessor()
        url = 'https://tile.loc.gov/storage-services/service/ndnp/0001.pdf'
        assert processor.strip_base_url(url) == url


# ---------------------------------------------------------------------------
# Category 7: REGRESSION tests
# Edge cases discovered during live API investigation.
# ---------------------------------------------------------------------------

class TestRegressionEdgeCases:
    """[REGRESSION] Edge cases from live API investigation."""

    def test_description_absent_ocr_text_is_none(self, locgov_processor):
        """[REGRESSION] Some search results have no description field (confirmed in live data).
        ocr_text should be None, not raise an error."""
        item_no_description = {
            'id': 'http://www.loc.gov/resource/sn84038012/1906-04-18/ed-1/?sp=1',
            'date': '1906-04-18',
            'number_lccn': ['sn84038012'],
            'number_edition': ['1'],
            'partof_title': ['the san francisco call'],
            # 'description' intentionally absent
        }
        response = {'results': [item_no_description], 'pagination': {}}
        result = locgov_processor.parse_pages(response)
        assert len(result) == 1
        assert result[0].ocr_text is None

    def test_description_empty_list_ocr_text_is_none(self, locgov_processor):
        """[REGRESSION] description present but empty list — ocr_text should be None."""
        item_empty_description = {
            'id': 'http://www.loc.gov/resource/sn84038012/1906-04-18/ed-1/?sp=1',
            'date': '1906-04-18',
            'number_lccn': ['sn84038012'],
            'number_edition': ['1'],
            'partof_title': ['the san francisco call'],
            'description': [],
        }
        response = {'results': [item_empty_description], 'pagination': {}}
        result = locgov_processor.parse_pages(response)
        assert result[0].ocr_text is None

    def test_location_state_as_list_in_newspaper(self, locgov_processor):
        """[REGRESSION] location_state may revert to a list in future API changes.
        Parser should handle both dict and list gracefully."""
        item_with_list_state = {
            'id': 'http://www.loc.gov/item/sn84038012/',
            'title': 'Test Paper',
            'number_lccn': ['sn84038012'],
            'location_city': ['san francisco'],
            'location_state': ['california'],  # list instead of dict
            'language': {'class': 'language', 'label': 'English', 'value': 'english'},
            'number_first_issue': {'label': '1895-01-01'},
            'number_last_issue': {'label': '1913-12-31'},
            'url': 'https://www.loc.gov/item/sn84038012/',
        }
        response = {'pages': [{}, {}, {'children': [{'results': [item_with_list_state]}]}, {}]}
        result = locgov_processor.parse_newspapers(response)
        assert len(result) == 1
        assert result[0].place_of_publication == ['california']

    def test_language_as_list_in_newspaper(self, locgov_processor):
        """[REGRESSION] language may revert to a list in future API changes.
        Parser should handle both dict and list gracefully."""
        item_with_list_language = {
            'id': 'http://www.loc.gov/item/sn84038012/',
            'title': 'Test Paper',
            'number_lccn': ['sn84038012'],
            'location_city': ['san francisco'],
            'location_state': {'class': 'location_state', 'label': 'California', 'value': 'california'},
            'language': ['english'],  # list instead of dict
            'number_first_issue': {'label': '1895-01-01'},
            'number_last_issue': {'label': '1913-12-31'},
            'url': 'https://www.loc.gov/item/sn84038012/',
        }
        response = {'pages': [{}, {}, {'children': [{'results': [item_with_list_language]}]}, {}]}
        result = locgov_processor.parse_newspapers(response)
        assert len(result) == 1
        assert result[0].language == ['english']

    def test_pages_list_shorter_than_expected(self, locgov_processor):
        """[REGRESSION] pages list with fewer than 3 entries does not raise IndexError."""
        response = {'pages': [{'slug': 'about'}]}
        result = locgov_processor.parse_newspapers(response)
        assert result == []
        result2 = locgov_processor.parse_pages(response)
        assert result2 == []

    def test_issue_missing_files_key_in_resource(self, locgov_processor):
        """[REGRESSION] resources[0] present but 'files' key absent — returns empty list."""
        issue = {
            'resources': [{'url': '...', 'image': '...', 'word_coordinates': '...'}],
            'item': {},
        }
        result = locgov_processor.parse_issue(issue)
        assert result == []

    def test_malformed_item_in_batch_skipped_gracefully(self, legacy_processor):
        """[REGRESSION] Malformed item in response does not stop processing of valid items."""
        response = {'items': [
            {'id': 'item1', 'lccn': 'sn1', 'title': 'Valid', 'date': '19060101'},
            None,  # malformed
            {'id': 'item2', 'lccn': 'sn2', 'title': 'Also Valid', 'date': '19060101'},
        ]}
        # Should not raise — valid items should be returned
        try:
            result = legacy_processor.parse_pages(response)
            valid_ids = [p.item_id for p in result if p is not None]
            assert len(valid_ids) >= 1
        except Exception as e:
            pytest.fail(f"parse_pages raised unexpectedly on malformed item: {e}")

    def test_number_first_issue_missing_label(self, locgov_processor):
        """[REGRESSION] number_first_issue present but no 'label' key — start_year is None."""
        item = {
            'id': 'http://www.loc.gov/item/sn84038012/',
            'title': 'Test Paper',
            'number_lccn': ['sn84038012'],
            'location_city': [],
            'location_state': {'label': 'California', 'value': 'california'},
            'language': {'label': 'English', 'value': 'english'},
            'number_first_issue': {'url': 'https://...'},  # no 'label'
            'number_last_issue': {'label': '1913-12-31'},
            'url': '',
        }
        response = {'pages': [{}, {}, {'children': [{'results': [item]}]}, {}]}
        result = locgov_processor.parse_newspapers(response)
        assert result[0].start_year is None