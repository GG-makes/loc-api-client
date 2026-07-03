"""
Chronicling America API Response Processing

Architecture
------------
ResponseProcessor (ABC)
    Abstract base class enforcing a common interface across API versions.
    Owns two parsing methods:

        parse_newspapers(response)
            Parses a newspaper list response into a list of NewspaperInfo.
            Stable API surface — do not change its signature. Both subclasses
            must accept the same argument and return the same type.

        parse_pages(response)
            Parses a page search response into a list of PageInfo.
            Stable API surface — same guarantee as parse_newspapers.

    Also owns:

        strip_base_url(url)
            Strips the API-version-specific base URL from a full URL,
            returning a relative endpoint path suitable for _make_request.
            Centralises the URL stripping logic previously scattered across
            batch_discovery.py, discovery_manager.py, and processor.py.

DeduplicationMixin
    Optional mixin that adds item-level deduplication to any ResponseProcessor
    subclass via multiple inheritance. Preserves the deduplicate=False escape
    hatch from the original NewsDataProcessor.process_search_response.

    Usage:
        class DeduplicatedLocGovResponseProcessor(DeduplicationMixin, LocGovResponseProcessor):
            pass

    The MRO ensures DeduplicationMixin.parse_pages wraps the subclass
    implementation correctly. Always place DeduplicationMixin before the
    ResponseProcessor subclass in the inheritance list.

NewspaperUtilsMixin
    Optional mixin providing filtering, summary, validation, and page
    estimation utilities that operate on already-parsed data structures.
    These methods are API-agnostic and do not strictly belong here —
    a dedicated newspaper_utils.py module would be a better home.
    Kept as a mixin temporarily to avoid a larger refactor.
    TODO: move to newspaper_utils.py when convenient.

Relationship to api_params.py
------------------------------
api_params.py owns the request side (building query parameters).
response_processor.py owns the response side (parsing API responses).
Together they form a complete API version abstraction:

    QueryBuilder subclass       → what to ask the API
    ResponseProcessor subclass  → how to read what it says back

LegacyResponseProcessor(ResponseProcessor)
    Targets responses from: https://chroniclingamerica.loc.gov/
    Conventions:
        - Newspaper lists returned under 'newspapers' key
        - Page search results returned under 'items' key
        - Full URLs prefixed with https://chroniclingamerica.loc.gov/
        - Dates in YYYYMMDD or YYYY-MM-DD format

LocGovResponseProcessor(ResponseProcessor)
    Targets responses from: https://www.loc.gov/collections/chronicling-america/
    Conventions:
        - Page search results returned under 'results' key
        - Dates already in YYYY-MM-DD format (confirmed)
        - Item IDs point to http://www.loc.gov/resource/... (confirmed)
        - LCCN in 'number_lccn' list field (confirmed)
        - Edition in 'number_edition' list field (confirmed)
        - Newspaper title in 'partof_title' list field (confirmed)
        - 'resources' in search results contains only {'url', 'files'} —
          PDF/JP2/OCR URLs are on the item detail endpoint, not search results
        - Item detail response uses top-level 'item', 'resource', 'pagination' keys (confirmed)
        - Issue detail 'resources' is a list containing one dict with a 'files'
          key — files is a list of lists, each inner list is one page (confirmed)
        - All pages in 'files' consistently have the full mimetype set (confirmed)
        - OCR URL on issue pages is under 'fulltext_service' on text/plain entry (confirmed)
        - OCR URL on item detail is under 'fulltext_file' on resource dict (confirmed)
        - 'image' key on item detail resource is confirmed as JP2/IIIF URL (confirmed)
        - Newspaper list results at pages[2]['children'][0]['results'] (confirmed)
        - Newspaper list fields are mixed dict/list — language, location_state,
          partof_title, subject_ethnicity are dicts with label/value keys (confirmed)
        - number_first_issue['label'] and number_last_issue['label'] give dates (confirmed)
        - Batch filtering uses fa=batch: parameter on standard search endpoint (confirmed)
        - Batch list available via datasets/batch-summary/ as static JSON (confirmed)
        - description field in search results contains OCR text snippet (confirmed)

Confirmation status
-------------------
Confirmed via LOC Jupyter notebooks and live API calls:
    - Search result item fields: id, date, number_lccn, number_edition,
      partof_title, location_state, location_city, location_county, language,
      batch, publication_frequency, resources (structure), segmentof, image_url,
      mime_type, original_format, pagination structure, description (OCR snippet)
    - Search results location: pages[1]['children'][0]['results'] (not top-level)
    - Pagination: pagination.total for filtered count, pagination.next for next URL
    - Coverage dates: 1736-08-03 to 1963-11-30 (replaces legacy assumption of 1836)
    - Item detail fields: item.newspaper_title, item.date, item.number_lccn,
      item.location_state, item.location_city, item.batch, item.contributor_names,
      resource.pdf, resource.image, resource.fulltext_file, pagination.current
    - Issue detail: resources[0]['files'] is list of lists, one per page
    - Issue page file mimetypes: image/jp2, application/pdf, text/xml,
      image/jpeg (x2), application/json, text/plain — consistent across all pages
    - Newspaper list: results at pages[2]['children'][0]['results'];
      fields are mixed dict/list — language, location_state, partof_title,
      subject_ethnicity are dicts with label/value/class keys; number_lccn
      remains a plain list; number_first_issue/number_last_issue are dicts
      with 'label' key containing YYYY-MM-DD date string
    - Batch filtering: fa=batch:<name> on standard search endpoint confirmed
    - Batch list: datasets/batch-summary/ returns static JSON with batch,
      archive_name, issue_count, page_count, lccns, ingested, url per batch
    - description field in search results confirmed as OCR text snippet
      (not full page text — truncated to ~1000 chars; full text via fulltext_file)

All response structures fully confirmed. No outstanding unknowns.

Compatibility guarantee
-----------------------
Code that consumed LegacyResponseProcessor can switch to LocGovResponseProcessor
by changing one word at the call site:

    # Before
    processor = LegacyResponseProcessor()
    newspapers = processor.parse_newspapers(response)

    # After
    processor = LocGovResponseProcessor()
    newspapers = processor.parse_newspapers(response)

No changes to storage, discovery, or download logic are required.

Relationship to api_params.py
------------------------------
api_params.py owns the request side (building query parameters).
response_processor.py owns the response side (parsing API responses).
Together they form a complete API version abstraction:

    QueryBuilder subclass      → what to ask the API
    ResponseProcessor subclass → how to read what it says back
"""

import re
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Set
from urllib import response


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared data structures
# These are API-agnostic. Both processors return the same types.
# ---------------------------------------------------------------------------

@dataclass
class NewspaperInfo:
    """
    Normalised representation of newspaper metadata.

    Populated by ResponseProcessor.parse_newspapers(). Fields are a superset
    of what both API versions return — fields unavailable in a given version
    are set to None or empty list.
    """
    lccn: str
    title: str
    place_of_publication: List[str]
    start_year: Optional[int]
    end_year: Optional[int]
    frequency: Optional[str]
    subject: List[str]
    language: List[str]
    url: str

    @staticmethod
    def _parse_year(year_str: Optional[str]) -> Optional[int]:
        """Parse year string to integer, handling various formats."""
        if not year_str:
            return None
        try:
            match = re.search(r'\b(\d{4})\b', str(year_str))
            return int(match.group(1)) if match else None
        except (ValueError, AttributeError):
            return None


@dataclass
class PageInfo:
    """
    Normalised representation of newspaper page metadata.

    Populated by ResponseProcessor.parse_pages(). The item_id is always
    a relative path (no base URL prefix) suitable for use as a storage key
    and for constructing download URLs.
    """
    item_id: str
    lccn: str
    title: str
    date: str
    edition: int
    sequence: int
    page_url: str
    pdf_url: Optional[str]
    jp2_url: Optional[str]
    ocr_text: Optional[str]  # text content for legacy API; URL to OCR file for loc.gov API
    word_count: Optional[int]

    @staticmethod
    def _format_date(date_str: str) -> str:
        """Normalise date to YYYY-MM-DD regardless of input format."""
        if not date_str:
            return ''
        if len(date_str) == 8 and date_str.isdigit():
            return f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
        if len(date_str) == 10 and date_str.count('-') == 2:
            return date_str
        return date_str


# ---------------------------------------------------------------------------
# Abstract base class
# ---------------------------------------------------------------------------

class ResponseProcessor(ABC):
    """
    Abstract base class for Chronicling America API response processors.

    Subclasses must implement:
        base_url        — the API version's root URL (property)
        parse_newspapers(response) — parse a newspaper list response
        parse_pages(response)      — parse a page search response

    strip_base_url is provided as a concrete method since the stripping
    logic is identical for both versions — only the URL prefix differs.
    """

    @property
    @abstractmethod
    def base_url(self) -> str:
        """Root URL for this API version. Used by strip_base_url."""

    def strip_base_url(self, url: str) -> str:
        """
        Strip the API-version-specific base URL from a full URL.

        Returns a relative endpoint path suitable for passing to
        _make_request. If the URL does not start with base_url,
        returns the URL unchanged.

        This centralises the stripping logic previously duplicated in
        batch_discovery.py, discovery_manager.py, and processor.py.

        Args:
            url: Full URL, possibly prefixed with this processor's base_url.

        Returns:
            Relative path, or the original URL if no prefix matched.
        """
        if url.startswith(self.base_url):
            return url[len(self.base_url):]
        return url

    @abstractmethod
    def parse_newspapers(self, response: Dict) -> List[NewspaperInfo]:
        """
        Parse a newspaper list API response into NewspaperInfo objects.

        Args:
            response: Raw JSON response dict from the API.

        Returns:
            List of NewspaperInfo. Empty list if no results or parse error.
        """

    @abstractmethod
    def parse_pages(self, response: Dict) -> List[PageInfo]:
        """
        Parse a page search API response into PageInfo objects.

        Args:
            response: Raw JSON response dict from the API.

        Returns:
            List of PageInfo. Empty list if no results or parse error.
        """

    @abstractmethod
    def parse_page_details(self, page_details: Dict, page_url: str = '') -> Optional[PageInfo]:
        """
        Parse a single page detail response into a PageInfo object.

        Called when fetching a direct page endpoint rather than a search
        result, e.g. lccn/sn123/1906-04-18/ed-1/seq-1.json.

        Args:
            page_details: Raw JSON response dict from the page endpoint.
            page_url:     The URL used to fetch the page, used to extract
                          edition, sequence, and item_id when not in the response.

        Returns:
            PageInfo, or None if parsing fails.
        """

    def parse_page_from_issue(self, page_data, issue_details: Dict, *args, **kwargs) -> Optional[PageInfo]:
        """
        Parse page data extracted from an issue detail response.

        Note: This method has different signatures in LegacyResponseProcessor
        and LocGovResponseProcessor due to fundamental differences in how the
        two APIs structure issue responses:

            LegacyResponseProcessor.parse_page_from_issue(page_data: Dict, issue_details: Dict)
                page_data is a single dict with 'url' and 'sequence' fields.

            LocGovResponseProcessor.parse_page_from_issue(page_data: List, issue_details: Dict, sequence: int)
                page_data is a list of file dicts keyed by mimetype.
                sequence is assigned positionally by parse_issue.

        Callers should prefer parse_issue(issue_details) which handles the
        correct iteration pattern for each API version automatically.

        The base class implementation raises NotImplementedError — subclasses
        must override this method.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} must implement parse_page_from_issue. "
            "Consider calling parse_issue(issue_details) instead, which handles "
            "the correct iteration pattern for this API version."
        )

    def parse_issue(self, issue_details: Dict) -> List[PageInfo]:
        """
        Parse all pages from an issue detail response.

        This is the preferred public API for extracting pages from an issue,
        as it handles the correct iteration pattern for each API version.
        Subclasses should override this method alongside parse_page_from_issue.

        The base class implementation returns an empty list — subclasses
        must override this method.
        """
        return []

    def _extract_lccn_from_url(self, url: str) -> str:
        """
        Extract LCCN from a URL containing /lccn/{lccn}/.
        Returns empty string if not found.
        """
        if url and '/lccn/' in url:
            parts = url.split('/lccn/')
            if len(parts) > 1:
                return parts[1].split('/')[0].replace('.json', '')
        return ''

    def _extract_edition_from_url(self, url: str) -> int:
        """
        Extract edition number from a URL containing /ed-{n}/.
        Returns 1 if not found.
        """
        if url:
            for part in url.strip('/').split('/'):
                if part.startswith('ed-'):
                    try:
                        return int(part.split('-')[1])
                    except (IndexError, ValueError):
                        pass
        return 1

    def _extract_sequence_from_url(self, url: str) -> int:
        """
        Extract sequence number from a URL containing /seq-{n}/.
        Returns 1 if not found.
        """
        if url:
            for part in url.strip('/').split('/'):
                if part.startswith('seq-'):
                    try:
                        return int(part.split('-')[1])
                    except (IndexError, ValueError):
                        pass
        return 1


# ---------------------------------------------------------------------------
# Deduplication mixin
# ---------------------------------------------------------------------------

class DeduplicationMixin:
    """
    Mixin that adds item-level deduplication to any ResponseProcessor subclass.

    Wraps parse_pages to filter out items whose item_id has already been seen
    in this processor instance's lifetime. Preserves the deduplicate=False
    escape hatch from the original NewsDataProcessor.process_search_response.

    Usage:
        class DeduplicatedLocGovResponseProcessor(DeduplicationMixin, LocGovResponseProcessor):
            pass

        processor = DeduplicatedLocGovResponseProcessor()
        pages = processor.parse_pages(response)              # deduplicates
        pages = processor.parse_pages(response, deduplicate=False)  # skips
        processor.reset_deduplication()                      # clear cache
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._seen_items: Set[str] = set()

    def is_duplicate(self, item_id: str) -> bool:
        """
        Check if item_id has been seen before, registering it if not.

        Returns True if the item is a duplicate (already seen).
        Returns False and registers the item if it is new.
        """
        if item_id in self._seen_items:
            return True
        self._seen_items.add(item_id)
        return False

    def reset_deduplication(self):
        """Clear the deduplication cache."""
        self._seen_items.clear()

    def parse_pages(self, response: Dict, deduplicate: bool = True) -> List[PageInfo]:
        """
        Parse pages and optionally deduplicate by item_id.

        Args:
            response:     Raw JSON response dict from the API.
            deduplicate:  When True (default), items already seen in this
                          session are filtered out. When False, all items
                          are returned and the seen-items cache is not updated.

        Returns:
            List of PageInfo, filtered if deduplicate=True.
        """
        pages = super().parse_pages(response)
        if not deduplicate:
            return pages
        return [p for p in pages if not self.is_duplicate(p.item_id)]


# ---------------------------------------------------------------------------
# Legacy processor — pre-August 2025
# ---------------------------------------------------------------------------

class LegacyResponseProcessor(ResponseProcessor):
    """
    Parses responses from the pre-August 2025 Chronicling America API.

    Target base URL: https://chroniclingamerica.loc.gov/

    Key conventions:
        - Newspaper lists returned under 'newspapers' key
        - Page search results returned under 'items' key
        - Full URLs prefixed with https://chroniclingamerica.loc.gov/
    """

    @property
    def base_url(self) -> str:
        return "https://chroniclingamerica.loc.gov/"

    def parse_newspapers(self, response: Dict) -> List[NewspaperInfo]:
        """
        Parse legacy newspaper list response.

        Legacy response shape:
            {
                "newspapers": [
                    {"lccn": "...", "title": "...", "state": "...", "url": "..."},
                    ...
                ]
            }
        """
        results = []
        for item in response.get('newspapers', []):
            if not isinstance(item, dict):
                logger.warning(f"Skipping non-dict item in parse_pages: {type(item)}")
                continue
            try:
                place = [item['state']] if item.get('state') else item.get('place_of_publication', [])
                results.append(NewspaperInfo(
                    lccn=item.get('lccn', ''),
                    title=item.get('title', ''),
                    place_of_publication=place,
                    start_year=NewspaperInfo._parse_year(item.get('start_year')),
                    end_year=NewspaperInfo._parse_year(item.get('end_year')),
                    frequency=item.get('frequency'),
                    subject=item.get('subject', []),
                    language=item.get('language', []),
                    url=item.get('url', ''),
                ))
            except Exception:
                logger.warning(f"Failed to parse legacy newspaper item: {item.get('lccn')}")
        return results

    def parse_pages(self, response: Dict) -> List[PageInfo]:
        """
        Parse legacy page search response.

        Legacy response shape:
            {
                "items": [
                    {"id": "...", "title": "...", "date": "...", ...},
                    ...
                ]
            }
        """
        results = []
        for item in response.get('items', []):
            if not isinstance(item, dict):
                logger.warning(f"Skipping non-dict item in parse_pages: {type(item)}")
                continue
            try:
                item_id = self.strip_base_url(item.get('id', '') or item.get('url', ''))
                item_id = item_id.strip('/').replace('.json', '')

                date = PageInfo._format_date(item.get('date', ''))
                page_url = item.get('url', '')
                base = page_url.rstrip('/')

                results.append(PageInfo(
                    item_id=item_id,
                    lccn=item.get('lccn', '') or self._extract_lccn_from_url(item_id),
                    title=item.get('title', ''),
                    date=date,
                    edition=item.get('edition') or self._extract_edition_from_url(item_id),
                    sequence=item.get('sequence') or self._extract_sequence_from_url(item_id),
                    page_url=base,
                    pdf_url=f"{base}.pdf",
                    jp2_url=f"{base}.jp2",
                    ocr_text=item.get('ocr_eng'),  # actual OCR text content from legacy search results
                    word_count=None,
                ))
            except Exception:
                logger.warning(f"Failed to parse legacy page item: {item.get('id')}")
        return results

    def parse_page_details(self, page_details: Dict, page_url: str = '') -> Optional[PageInfo]:
        """
        Parse a single legacy page detail response.

        Legacy page detail shape:
            {
                "sequence": 1,
                "title": {"name": "...", "url": "..."},
                "issue": {"date_issued": "..."},
                "pdf": "...",
                "jp2": "...",
                "text": "..."   # URL to OCR text file
            }
        """
        try:
            sequence = page_details.get('sequence', 1)
            title_info = page_details.get('title', {})
            title = title_info.get('name', 'Unknown Title')
            title_url = title_info.get('url', '')
            lccn = self._extract_lccn_from_url(title_url)

            issue_info = page_details.get('issue', {})
            date = issue_info.get('date_issued', '')

            edition = self._extract_edition_from_url(page_url)

            item_id = ''
            if page_url:
                item_id = self.strip_base_url(page_url).replace('.json', '')

            base = page_url.replace('.json', '') if page_url else ''

            return PageInfo(
                item_id=item_id,
                lccn=lccn,
                title=title,
                date=date,
                edition=edition,
                sequence=sequence,
                page_url=base,
                pdf_url=page_details.get('pdf', ''),
                jp2_url=page_details.get('jp2', ''),
                ocr_text=page_details.get('text', ''),  # URL to OCR text file
                word_count=None,
            )
        except Exception as e:
            logger.error(f"Failed to parse legacy page details: {e}")
            return None

    def parse_page_from_issue(self, page_data: Dict, issue_details: Dict) -> Optional[PageInfo]:
        """
        Parse a page entry from a legacy issue detail response.

        Args:
            page_data:     Single entry from issue_details['pages'].
            issue_details: Parent issue response containing title, date, LCCN.
        """
        try:
            page_url = page_data.get('url', '')
            sequence = page_data.get('sequence', 1)

            if not page_url:
                return None

            title_info = issue_details.get('title', {})
            title = title_info.get('name', 'Unknown Title')
            date = issue_details.get('date_issued', '')

            issue_url = issue_details.get('url', '')
            lccn = self._extract_lccn_from_url(issue_url)
            edition = self._extract_edition_from_url(page_url)

            item_id = self.strip_base_url(page_url).replace('.json', '')
            base = page_url.replace('.json', '')

            return PageInfo(
                item_id=item_id,
                lccn=lccn,
                title=title,
                date=date,
                edition=edition,
                sequence=sequence,
                page_url=base,
                pdf_url=f"{base}.pdf",
                jp2_url=f"{base}.jp2",
                ocr_text=f"{base}/ocr.txt",
                word_count=None,
            )
        except Exception as e:
            logger.error(f"Failed to parse legacy page from issue: {e}")
            return None

class LocGovResponseProcessor(ResponseProcessor):
    """
    Parses responses from the post-August 2025 loc.gov API.

    Target base URL: https://www.loc.gov/

    Key conventions:
        - Newspaper lists returned under 'results' key
        - Page search results returned under 'results' key
        - Full URLs prefixed with https://www.loc.gov/
        - OCR text available via 'resources' list in each result

    Note:
        Both newspaper and page responses use 'results' in the loc.gov API.
        The distinction is made by the endpoint called, not the response shape.
    """

    @property
    def base_url(self) -> str:
        return "https://www.loc.gov/"

    def parse_newspapers(self, response: Dict) -> List[NewspaperInfo]:
        """
        Parse loc.gov newspaper list response.

        Confirmed loc.gov newspaper list structure (via live API call):
            Results at: pages[2]['children'][0]['results']

            Title result shape — NOTE: many fields are dicts, not lists:
            {
                "id": "http://www.loc.gov/item/sn85026945/",
                "title": "The Abbeville Banner (Abbeville, S.C.) 1847-1869",
                "number_lccn": ["sn85026945"],          # plain list
                "location_city": ["abbeville"],          # plain list
                "location_county": ["abbeville"],        # plain list
                "location_str": "Abbeville, South Carolina",
                "location_state": {                      # DICT not list
                    "class": "location_state",
                    "label": "South Carolina",
                    "value": "south carolina"
                },
                "language": {                            # DICT not list
                    "class": "language",
                    "label": "English",
                    "value": "english"
                },
                "partof_title": {                        # DICT not list
                    "class": "title",
                    "label": "The Abbeville Banner...",
                    "value": "the abbeville banner...",
                    "url": "https://www.loc.gov/item/sn85026945/"
                },
                "subject_ethnicity": {                   # DICT not list
                    "class": "subject_ethnicity",
                    "label": "",
                    "value": ""
                },
                "number_first_issue": {                  # DICT with date label
                    "label": "1847-03-03",
                    "url": "https://www.loc.gov/item/sn85026945/1847-03-03/ed-1"
                },
                "number_last_issue": {                   # DICT with date label
                    "label": "1869-09-29",
                    "url": "..."
                },
                "number_issue_count": {"label": "254", "value": "254"},
                "url": "https://www.loc.gov/item/sn85026945/"
            }
        """
        # Results nested at pages[2]['children'][0]['results']
        try:
            pages = response.get('pages', [])
            items_list = []
            if len(pages) > 2:
                children = pages[2].get('children', [])
                if children:
                    items_list = children[0].get('results', [])
        except (IndexError, KeyError, TypeError):
            items_list = []

        results = []
        for item in items_list:
            if not isinstance(item, dict):
                logger.warning(f"Skipping non-dict item in parse_newspapers: {type(item)}")
                continue
            try:
                # number_lccn is a plain list
                lccn_list = item.get('number_lccn', [])
                lccn = lccn_list[0] if lccn_list else ''

                # title is a plain string
                title = item.get('title', '')

                # location_state is a dict with 'label' key (Title Case)
                loc_state = item.get('location_state', {})
                if isinstance(loc_state, dict):
                    place = [loc_state.get('label', '')] if loc_state.get('label') else []
                elif isinstance(loc_state, list):
                    place = loc_state
                else:
                    place = []

                # language is a dict with 'label' key
                lang = item.get('language', {})
                if isinstance(lang, dict):
                    language = [lang.get('label', '')] if lang.get('label') else []
                elif isinstance(lang, list):
                    language = lang
                else:
                    language = []

                # number_first_issue and number_last_issue are dicts with 'label' as YYYY-MM-DD
                first_issue = item.get('number_first_issue', {})
                last_issue = item.get('number_last_issue', {})
                start_year = NewspaperInfo._parse_year(
                    first_issue.get('label', '')[:4] if isinstance(first_issue, dict) else None
                )
                end_year = NewspaperInfo._parse_year(
                    last_issue.get('label', '')[:4] if isinstance(last_issue, dict) else None
                )

                # subject_ethnicity is a dict — not a standard subject list
                # No subject headings available in title list response

                results.append(NewspaperInfo(
                    lccn=lccn,
                    title=title,
                    place_of_publication=place,
                    start_year=start_year,
                    end_year=end_year,
                    frequency=None,  # not available in title list response
                    subject=[],
                    language=language,
                    url=item.get('url', '') or item.get('id', ''),
                ))
            except Exception:
                logger.warning(f"Failed to parse loc.gov newspaper item: {item.get('id')}")
        return results

    def parse_pages(self, response: Dict) -> List[PageInfo]:
        """
        Parse loc.gov page search response.

        Response shape assumed here (flat, trimmed by at=results,pagination in
        LocGovQueryBuilder.build()):

            {
                "results": [
                    {
                        "id": "http://www.loc.gov/resource/sn.../YYYY-MM-DD/ed-1/?sp=N",
                        "date": "YYYY-MM-DD",
                        "number_lccn": ["sn..."],
                        "number_edition": ["1"],
                        "number_page": ["0000000001"],  # zero-padded, not reliable for sequence
                        "partof_title": ["newspaper title"],
                        "location_state": ["oklahoma"],
                        "location_city": ["tulsa"],
                        "language": ["english"],
                        "batch": ["okhi_durant_ver01"],
                        "publication_frequency": ["daily"],
                        "resources": [{"url": "...", "files": 1}],  # no pdf/image/ocr here
                        "mime_type": ["image/jp2", "application/pdf", ...],
                        "segmentof": ["http://www.loc.gov/resource/.../ed-1/"],
                        "description": ["OCR text snippet..."],
                        "url": "https://www.loc.gov/resource/.../?sp=N&q=..."
                    },
                    ...
                ],
                "pagination": {
                    "current": 1,
                    "of": 23745202,    # total items in collection (unfiltered)
                    "total": 2103648,  # total filtered results
                    "next": "https://...",
                    "last": "https://...",
                    "perpage": 25,
                    "from": 1,
                    "to": 25
                }
            }

        Without at=, results are nested at pages[1]['children'][0]['results'].
        The at= parameter in build() normalises this to a top-level key so
        parse_pages and parse_newspapers have symmetric response shapes.

        Note: 'description' contains an OCR text snippet (~1000 chars, not full
        page). Use this for bulk discovery instead of fetching item detail.
        Full text is available via the fulltext_file URL on the item detail endpoint.
        """
        # Results are nested, not at top level
        try:
            results_list = response.get('results', [])
        except (IndexError, KeyError, TypeError):
            results_list = []

        results = []
        for item in results_list:
            if not isinstance(item, dict):
                logger.warning(f"Skipping non-dict item in parse_pages: {type(item)}")
                continue
            try:
                # id confirmed as http://www.loc.gov/resource/... for newspaper pages
                item_id = item.get('id', '') or item.get('url', '')
                item_id = self.strip_base_url(item_id).strip('/').replace('.json', '')

                # date confirmed as YYYY-MM-DD — no conversion needed
                date = item.get('date', '')

                # number_lccn confirmed as list
                lccn_list = item.get('number_lccn', [])
                lccn = lccn_list[0] if lccn_list else self._extract_lccn_from_url(item_id)

                # number_edition confirmed as list of strings
                edition_list = item.get('number_edition', [])
                try:
                    edition = int(edition_list[0]) if edition_list else self._extract_edition_from_url(item_id)
                except (ValueError, TypeError):
                    edition = self._extract_edition_from_url(item_id)

                # partof_title confirmed as list
                title_list = item.get('partof_title', [])
                title = title_list[0] if title_list else item.get('title', '')

                # number_page is zero-padded string — not reliable for sequence
                # Extract from id/url path instead
                sequence = self._extract_sequence_from_url(item_id)

                # page_url: strip query string from id
                page_url = item.get('id', '') or item.get('url', '')
                base = page_url.split('?')[0].rstrip('/')

                # PDF/JP2: not in search result resources — construct from base path
                # Confirmed via item detail that these paths are valid
                pdf_url = f"{base}.pdf" if base else None
                jp2_url = f"{base}.jp2" if base else None

                # description field contains OCR text in search results (confirmed)
                description = item.get('description', [])
                ocr_text = description[0] if description else None

                results.append(PageInfo(
                    item_id=item_id,
                    lccn=lccn,
                    title=title,
                    date=date,
                    edition=edition,
                    sequence=sequence,
                    page_url=base,
                    pdf_url=pdf_url,
                    jp2_url=jp2_url,
                    ocr_text=ocr_text,
                    word_count=None,
                ))
            except Exception:
                logger.warning(f"Failed to parse loc.gov page item: {item.get('id')}")
        return results

    def parse_pagination(self, response: Dict) -> Dict:
        """
        Parse pagination metadata from a loc.gov search response.

        Confirmed pagination shape (via live API call):
            {
                "current": 1,
                "of": 23745202,    # total items in full collection (unfiltered)
                "total": 2103648,  # total results matching current query
                "next": "https://www.loc.gov/collections/chronicling-america/?...&sp=2",
                "last": "https://...",
                "previous": null,
                "perpage": 25,
                "from": 1,
                "to": 25,
                "results": "1 - 25"
            }

        Returns a simplified dict with the fields needed for discovery:
            {
                "current_page": int,
                "total_results": int,
                "next_url": str or None,
                "per_page": int,
                "from": int,
                "to": int
            }
        """
        pagination = response.get('pagination', {})
        return {
            'current_page': pagination.get('current', 1),
            'total_results': pagination.get('total', 0),
            'next_url': pagination.get('next'),
            'per_page': pagination.get('perpage', 25),
            'from': pagination.get('from', 1),
            'to': pagination.get('to', 0),
        }

    def parse_batch_list(self, response: Dict) -> List[Dict]:
        """
        Parse the batch list from the datasets/batch-summary/ endpoint.

        Confirmed structure (via live API call) — static JSON endpoint,
        not paginated. Returns a list of batch dicts directly, NOT nested
        under pages[N]['children'][N]['results'] like search responses.

        The response is a standard page JSON but the batch data lives under
        a top-level key. Each batch entry shape:
            {
                "batch": "okhi_durant_ver01",
                "archive_name": "okhi_durant_ver01.tar.bz2",
                "archive_created": "2019-06-27T09:55:45+00:00",
                "identifier": "service:ndnp:okhi:batch_okhi_durant_ver01",
                "ingested": "2014-11-21T20:47:33-05:00",
                "issue_count": 211,
                "lccns": ["sn83030214"],
                "page_count": 5241,
                "sha1": "...",
                "sha256": "...",
                "size": 700571080,
                "url": "https://chroniclingamerica.loc.gov/data/ocr/....tar.bz2"
            }

        Note: The 'url' field points to the legacy OCR bulk download archive
        on chroniclingamerica.loc.gov, not to the new API. The batch name
        (without 'batch_' prefix) is what the fa=batch: search filter expects.

        Args:
            response: Raw JSON response from datasets/batch-summary/ endpoint.

        Returns:
            List of batch dicts. Empty list if not found or parse error.
        """
        try:
            datasets = response.get('datasets', [])
            if datasets:
                return datasets
            # Fallback: some responses may nest under content key
            content = response.get('content', [])
            if isinstance(content, list):
                return content
            return []
        except Exception as e:
            logger.error(f"Failed to parse batch list: {e}")
            return []


    def parse_page_details(self, page_details: Dict, page_url: str = '') -> Optional[PageInfo]:
        """
        Parse a single loc.gov page detail response.

        Confirmed loc.gov item detail shape (via LOC Jupyter notebooks):
            {
                "item": {
                    "date": "YYYY-MM-DD",            # confirmed
                    "newspaper_title": [...],        # confirmed, list
                    "number_lccn": ["sn..."],        # confirmed, list
                    "location_state": [...],         # confirmed, list
                    "location_city": [...],          # confirmed, list
                    "batch": [...],                  # confirmed, list
                    "contributor_names": [...],      # confirmed, list
                    ...
                },
                "resource": {
                    "pdf": "https://tile.loc.gov/...",      # confirmed
                    "image": "https://tile.loc.gov/...",    # confirmed key name
                    "fulltext_file": "https://tile.loc.gov/text-services/...",  # confirmed key name
                    "word_coordinates": "https://tile.loc.gov/text-services/..."
                },
                "pagination": {"current": N}        # confirmed
            }
        """
        try:
            item_meta = page_details.get('item', {})
            resource = page_details.get('resource', {})
            pagination = page_details.get('pagination', {})

            # newspaper_title confirmed as list
            title_list = item_meta.get('newspaper_title', [])
            title = title_list[0] if title_list else 'Unknown Title'

            # number_lccn confirmed as list
            lccn_list = item_meta.get('number_lccn', [])
            lccn = lccn_list[0] if lccn_list else self._extract_lccn_from_url(page_url)

            # date confirmed as YYYY-MM-DD
            date = item_meta.get('date', '')

            # pagination.current confirmed as page sequence number
            sequence = pagination.get('current', 1)
            edition = self._extract_edition_from_url(page_url)

            item_id = self.strip_base_url(page_url).replace('.json', '') if page_url else ''
            base = page_url.replace('.json', '') if page_url else ''

            return PageInfo(
                item_id=item_id,
                lccn=lccn,
                title=title,
                date=date,
                edition=edition,
                sequence=sequence,
                page_url=base,
                pdf_url=resource.get('pdf'),           # confirmed key name
                jp2_url=resource.get('image'),         # confirmed key name
                ocr_text=resource.get('fulltext_file'),  # confirmed key name
                word_count=None,
            )
        except Exception as e:
            logger.error(f"Failed to parse loc.gov page details: {e}")
            return None

    def parse_page_from_issue(self, page_data: List, issue_details: Dict, sequence: int) -> Optional[PageInfo]:
        """
        Parse a single page's file list from a loc.gov issue detail response.

        Confirmed loc.gov issue detail structure (via live API call):
            {
                "item": {
                    "date": "YYYY-MM-DD",
                    "date_issued": "YYYY-MM-DD",
                    "number_lccn": ["sn..."],
                    "newspaper_title": "...",   # string on issue item
                    "number_edition": ["1"],
                    ...
                },
                "resources": [
                    {
                        "files": [
                            [   # each inner list = one page, consistently:
                                {"mimetype": "image/jp2", "url": "...jp2"},
                                {"mimetype": "application/pdf", "url": "...pdf"},
                                {"mimetype": "text/xml", "url": "...xml"},
                                {"mimetype": "image/jpeg", "url": "...thumbnail"},
                                {"mimetype": "image/jpeg", "url": "...thumbnail"},
                                {"mimetype": "application/json", "title": "Image N of ..."},
                                {"mimetype": "text/plain", "fulltext_service": "..."}
                            ],
                            ...  # 108 pages confirmed in one example issue
                        ],
                        "image": "...",
                        "url": "https://www.loc.gov/resource/.../ed-1/",
                        "word_coordinates": "..."
                    }
                ]
            }

        Note: The signature differs from LegacyResponseProcessor.parse_page_from_issue.
        The loc.gov version receives the page file list directly (one inner list from
        resources) and the sequence number positionally, since there is no 'url' or
        'sequence' field on individual page file entries.

        Use parse_issue to iterate over all pages in an issue response.

        Args:
            page_data:     One inner list from issue_details['resources'],
                           containing file dicts for a single page.
            issue_details: Full issue response dict for metadata.
            sequence:      1-based page sequence number (positional in resources list).
        """
        try:
            if not page_data:
                return None

            # Extract file URLs by mimetype
            jp2_url = next(
                (f['url'] for f in page_data if f.get('mimetype') == 'image/jp2'), None
            )
            pdf_url = next(
                (f['url'] for f in page_data if f.get('mimetype') == 'application/pdf'), None
            )
            # OCR text service URL — confirmed as 'fulltext_service' on text/plain entry
            ocr_text = next(
                (f.get('fulltext_service') for f in page_data
                 if f.get('mimetype') == 'text/plain'), None
            )
            # Page title from JSON manifest entry e.g. "Image 5 of The Morning Tulsa..."
            json_entry = next(
                (f for f in page_data if f.get('mimetype') == 'application/json'), {}
            )
            page_title = json_entry.get('title', '')

            # Issue metadata from item dict — confirmed field names
            item_meta = issue_details.get('item', {})

            lccn_list = item_meta.get('number_lccn', [])
            lccn = lccn_list[0] if lccn_list else ''

            # date_issued confirmed on issue item
            date = item_meta.get('date_issued', '') or item_meta.get('date', '')

            edition_list = item_meta.get('number_edition', [])
            try:
                edition = int(edition_list[0]) if edition_list else 1
            except (ValueError, TypeError):
                edition = 1

            # newspaper_title on issue item appears as string not list
            newspaper_title = item_meta.get('newspaper_title', page_title)

            # Construct item_id from jp2 URL path — most stable unique identifier
            item_id = ''
            if jp2_url:
                item_id = self.strip_base_url(jp2_url).replace('.jp2', '')
            elif pdf_url:
                item_id = self.strip_base_url(pdf_url).replace('.pdf', '')

            return PageInfo(
                item_id=item_id,
                lccn=lccn,
                title=newspaper_title,
                date=date,
                edition=edition,
                sequence=sequence,
                page_url=jp2_url or pdf_url or '',
                pdf_url=pdf_url,
                jp2_url=jp2_url,
                ocr_text=ocr_text,
                word_count=None,
            )
        except Exception as e:
            logger.error(f"Failed to parse loc.gov page from issue: {e}")
            return None

    def parse_issue(self, issue_details: Dict) -> List[PageInfo]:
        """
        Parse all pages from a loc.gov issue detail response.

        Confirmed structure (via live API call):
            issue_details['resources'] is a list containing one dict:
            {
                "files": [[...page 1 file dicts...], [...page 2...], ...],
                "image": "...",
                "url": "https://www.loc.gov/resource/.../ed-1/",
                "word_coordinates": "..."
            }

        Pages live under resources[0]['files'], not resources directly.
        Each entry in 'files' is a list of file dicts for one page,
        consistently containing: image/jp2, application/pdf, text/xml,
        image/jpeg (x2 thumbnails), application/json, text/plain.

        Args:
            issue_details: Full issue response dict.

        Returns:
            List of PageInfo, one per page in the issue.
        """
        pages = []
        resources = issue_details.get('resources', [])
        if not resources:
            return pages

        files = resources[0].get('files', [])
        for sequence, page_files in enumerate(files, start=1):
            page = self.parse_page_from_issue(page_files, issue_details, sequence)
            if page:
                pages.append(page)
        return pages


# ---------------------------------------------------------------------------
# Newspaper utility mixin
# ---------------------------------------------------------------------------

class NewspaperUtilsMixin:
    """
    Mixin providing filtering, summary, validation, and page estimation
    utilities that operate on already-parsed NewspaperInfo and PageInfo objects.

    Note: These methods are API-agnostic and do not belong in a response
    processor. A better home would be a standalone newspaper_utils.py module.
    They are kept here temporarily as a mixin to avoid a larger refactor.
    TODO: move to a dedicated utility module when convenient.

    estimate_pages_from_batch_issue contains a hard-coded legacy base URL
    (https://chroniclingamerica.loc.gov/) for URL construction. This will
    produce incorrect URLs when processing loc.gov API responses. A
    DeprecationWarning is raised when this method is called to flag the issue.
    """

    def estimate_pages_from_batch_issue(
        self,
        issue_data: Dict,
        typical_pages_per_issue: int = 8,
        base_url: str = "https://chroniclingamerica.loc.gov/",
    ) -> List[PageInfo]:
        """
        Estimate pages for an issue from batch data without fetching issue details.

        Faster but less accurate than fetching actual issue details.

        .. deprecated::
            Constructs URLs using the legacy chroniclingamerica.loc.gov base URL
            by default. Pass base_url explicitly when working with loc.gov API
            responses, or replace with a proper implementation once the new API
            batch response schema is confirmed.
        """
        import warnings
        warnings.warn(
            "estimate_pages_from_batch_issue uses legacy URL construction by default. "
            "Pass base_url='https://www.loc.gov/' when working with loc.gov API responses.",
            DeprecationWarning,
            stacklevel=2,
        )

        try:
            issue_url = issue_data.get('url', '')
            date = issue_data.get('date_issued', '')
            title_info = issue_data.get('title', {})
            title = title_info.get('name', 'Unknown Title')

            lccn = ''
            title_url = title_info.get('url', '')
            if title_url and '/lccn/' in title_url:
                parts = title_url.split('/lccn/')
                if len(parts) > 1:
                    lccn = parts[1].replace('.json', '')
            elif issue_url and '/lccn/' in issue_url:
                parts = issue_url.split('/lccn/')
                if len(parts) > 1:
                    lccn = parts[1].split('/')[0]

            edition = 1
            if issue_url:
                for part in issue_url.strip('/').split('/'):
                    if part.startswith('ed-'):
                        try:
                            edition = int(part.split('-')[1])
                            break
                        except (IndexError, ValueError):
                            pass

            base_issue = issue_url.replace('.json', '') if issue_url else ''
            estimated_pages = []

            for seq in range(1, typical_pages_per_issue + 1):
                page_url = f"{base_issue}/seq-{seq}"
                item_id = page_url.replace(base_url, '') if page_url.startswith('https://') else page_url

                estimated_pages.append(PageInfo(
                    item_id=item_id,
                    lccn=lccn,
                    title=title,
                    date=date,
                    edition=edition,
                    sequence=seq,
                    page_url=page_url,
                    pdf_url=f"{page_url}.pdf",
                    jp2_url=f"{page_url}.jp2",
                    ocr_text=f"{page_url}/ocr.txt",
                    word_count=None,
                ))

            return estimated_pages

        except Exception as e:
            logger.error(f"Failed to estimate pages from batch issue: {e}")
            return []

    def filter_newspapers_by_criteria(
        self,
        newspapers: List[NewspaperInfo],
        state: Optional[str] = None,
        language: Optional[str] = None,
        start_year: Optional[int] = None,
        end_year: Optional[int] = None,
    ) -> List[NewspaperInfo]:
        """Filter a list of NewspaperInfo by state, language, and/or year range."""
        filtered = newspapers

        if state:
            filtered = [n for n in filtered
                       if any(state.lower() in place.lower()
                              for place in n.place_of_publication)]

        if language:
            filtered = [n for n in filtered
                       if any(language.lower() in lang.lower()
                              for lang in n.language)]

        if start_year:
            filtered = [n for n in filtered
                       if n.end_year and n.end_year >= start_year]

        if end_year:
            filtered = [n for n in filtered
                       if n.start_year and n.start_year <= end_year]

        return filtered

    def get_newspaper_summary(self, newspapers: List[NewspaperInfo]) -> Dict:
        """Generate summary statistics for a list of NewspaperInfo objects."""
        if not newspapers:
            return {'total_newspapers': 0}

        states: Dict[str, int] = {}
        languages: Dict[str, int] = {}
        year_range = []

        for newspaper in newspapers:
            for place in newspaper.place_of_publication:
                state = place.split(',')[-1].strip() if ',' in place else place
                states[state] = states.get(state, 0) + 1

            for lang in newspaper.language:
                languages[lang] = languages.get(lang, 0) + 1

            if newspaper.start_year:
                year_range.append(newspaper.start_year)
            if newspaper.end_year:
                year_range.append(newspaper.end_year)

        return {
            'total_newspapers': len(newspapers),
            'states': dict(sorted(states.items(), key=lambda x: x[1], reverse=True)[:10]),
            'languages': dict(sorted(languages.items(), key=lambda x: x[1], reverse=True)[:10]),
            'year_range': (min(year_range), max(year_range)) if year_range else None,
            'sample_titles': [n.title for n in newspapers[:5]],
        }

    def validate_date_range(self, date1: str, date2: str) -> bool:
        """
        Validate that a date range is within LOC Chronicling America coverage bounds.

        Coverage bounds confirmed via live API response (site.coverage_dates):
            start: August 3, 1736
            end:   November 31, 1963

        Migration note: The previous implementation used 1836-01-01 as the start
        bound, derived from the legacy API documentation. The confirmed start date
        from the new API is 1736-08-03 — over a century earlier. This expands the
        valid date range significantly. Code that previously rejected pre-1836 dates
        will now accept them.
        """
        try:
            if len(date1) == 4:
                date1 += '-01-01'
            if len(date2) == 4:
                date2 += '-12-31'

            start = datetime.strptime(date1, '%Y-%m-%d')
            end = datetime.strptime(date2, '%Y-%m-%d')

            # Bounds confirmed from site.coverage_dates in live API response
            COVERAGE_START = datetime(1736, 8, 3)
            COVERAGE_END = datetime(1963, 11, 30)

            return (
                start >= COVERAGE_START
                and end <= COVERAGE_END
                and start <= end
            )

        except ValueError:
            return False

# ---------------------------------------------------------------------------
# Build the classes
# ---------------------------------------------------------------------------

class LegacyProcessor(DeduplicationMixin, NewspaperUtilsMixin, LegacyResponseProcessor):
    """Production legacy processor: parsing + dedup + newspaper utilities."""
    pass


class LocGovProcessor(DeduplicationMixin, NewspaperUtilsMixin, LocGovResponseProcessor):
    """Production loc.gov processor: parsing + dedup + newspaper utilities."""
    pass