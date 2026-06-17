""""
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
        - Issue detail 'resources' is a list of lists — each inner list is one
          page containing file dicts keyed by mimetype (confirmed)
        - OCR URL on issue pages is under 'fulltext_service' on text/plain entry (confirmed)
        - OCR URL on item detail is under 'fulltext_file' on resource dict (confirmed)
        - 'image' key on item detail resource is confirmed as JP2/IIIF URL (confirmed)
        - Newspaper list response structure: NOT YET CONFIRMED against live API
        - Issue detail response structure: NOT YET CONFIRMED against live API
        - Batch list/detail response structure: NOT YET CONFIRMED against live API
          (inferred from batch_utils.py and batch_discovery.py usage patterns)

Confirmation status
-------------------
Confirmed via LOC Jupyter notebooks (github.com/nwy/Chronicling-America-API):
    - Search result item fields: id, date, number_lccn, number_edition,
      partof_title, location_state, location_city, location_county, language,
      batch, publication_frequency, resources (structure), segmentof, image_url,
      mime_type, original_format, pagination structure
    - Item detail fields: item.newspaper_title, item.date, item.number_lccn,
      item.location_state, item.location_city, item.batch, item.contributor_names,
      resource.pdf, pagination.current

Not yet confirmed against live API:
    - Newspaper list response (parse_newspapers)
    - Batch list/detail response
    - OCR text field name and location
    - Whether resources[0] is consistently the cover/thumbnail entry that should be skipped 

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

    @abstractmethod
    def parse_page_from_issue(self, page_data: Dict, issue_details: Dict) -> Optional[PageInfo]:
        """
        Parse page data extracted from an issue detail response.

        Faster than fetching individual page endpoints since the page data
        is already available from the parent issue response.

        Args:
            page_data:     A single page entry from the issue's pages list.
            issue_details: The parent issue response dict, used for title,
                           date, and LCCN that are not on the page entry.

        Returns:
            PageInfo, or None if parsing fails.
        """

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

        WARNING: This response structure has NOT been confirmed against a live
        loc.gov API call. The field names below are inferred from the search
        result item structure (which is confirmed) and may differ for the
        newspaper list endpoint. Validate before relying on this in production.

        Assumed loc.gov response shape:
            {
                "results": [
                    {
                        "id": "...",
                        "title": "...",
                        "number_lccn": ["sn..."],
                        "location_state": ["california"],
                        "language": ["english"],
                        "partof_title": ["newspaper title..."],
                        "publication_frequency": ["daily"],
                        ...
                    },
                    ...
                ]
            }
        """
        results = []
        for item in response.get('results', []):
            try:
                # location_state confirmed as list of lowercase strings in search results.
                # Assumed same for newspaper list — unconfirmed.
                place = item.get('location_state', []) or item.get('state', [])
                if isinstance(place, str):
                    place = [place]

                # number_lccn confirmed as list in search results
                lccn_list = item.get('number_lccn', [])
                lccn = lccn_list[0] if lccn_list else ''

                # partof_title confirmed in search results; assumed here — unconfirmed
                title_list = item.get('partof_title', []) or [item.get('title', '')]
                title = title_list[0] if title_list else ''

                # publication_frequency confirmed in search results; assumed here — unconfirmed
                freq_list = item.get('publication_frequency', [])
                frequency = freq_list[0] if freq_list else None

                results.append(NewspaperInfo(
                    lccn=lccn,
                    title=title,
                    place_of_publication=place,
                    start_year=NewspaperInfo._parse_year(item.get('date', '')[:4] if item.get('date') else None),
                    end_year=None,  # Not available in list response
                    frequency=frequency,
                    subject=item.get('subject', []),
                    language=item.get('language', []),
                    url=item.get('url', '') or item.get('id', ''),
                ))
            except Exception:
                logger.warning(f"Failed to parse loc.gov newspaper item: {item.get('id')}")
        return results

    def parse_pages(self, response: Dict) -> List[PageInfo]:
        """
        Parse loc.gov page search response.

        Confirmed loc.gov search result item shape (via LOC Jupyter notebooks):
            {
                "id": "http://www.loc.gov/resource/sn.../YYYY-MM-DD/ed-1/?sp=N",
                "date": "YYYY-MM-DD",               # already ISO format
                "number_lccn": ["sn..."],            # list
                "number_edition": ["1"],             # list
                "partof_title": ["newspaper title"], # list
                "location_state": ["california"],    # list, lowercase
                "location_city": ["el centro"],      # list, lowercase
                "language": ["english"],             # list, lowercase
                "batch": ["batch_name_ver01"],       # list
                "publication_frequency": ["daily"],  # list
                "resources": [{"url": "...", "files": N}],  # no pdf/image/ocr here
                "image_url": ["https://tile.loc.gov/..."],  # IIIF thumbnail URLs
                "mime_type": ["image/jp2", "application/pdf", "text/plain", ...],
                "segmentof": ["http://www.loc.gov/resource/.../ed-1/"],
                "original_format": ["newspaper"],
                "url": "https://www.loc.gov/resource/.../?sp=N&q=..."
            }

        Note: 'resources' in search results contains only the resource URL and
        file count — NOT pdf/image/ocr URLs. Those are available on the item
        detail endpoint (parse_page_details). PDF/JP2 URLs are constructed from
        the item ID path for now; OCR URL is unconfirmed.
        """
        results = []
        for item in response.get('results', []):
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

                # sequence not directly in search result — extract from id/url path
                sequence = self._extract_sequence_from_url(item_id)

                # page_url: use the resource URL without query string if available
                page_url = item.get('id', '') or item.get('url', '')
                # strip query string
                base = page_url.split('?')[0].rstrip('/')

                # PDF/JP2: construct from item_id path since resources list
                # in search results does not contain these URLs directly.
                # These are available via parse_page_details on the item endpoint.
                lccn_path = lccn_list[0] if lccn_list else ''
                # image_url list contains IIIF thumbnail URLs — not suitable as pdf_url
                # Falling back to constructed paths; validate against item detail endpoint.
                pdf_url = f"{base}.pdf" if base else None   # unconfirmed construction
                jp2_url = f"{base}.jp2" if base else None   # unconfirmed construction
                # OCR field name not confirmed in search results
                ocr_text = None  # fetch via parse_page_details for confirmed OCR URL

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
                    "newspaper_title": "...",   # string, not list, on issue item
                    "number_edition": ["1"],
                    ...
                },
                "resources": [
                    [   # each inner list = one page
                        {"mimetype": "image/jp2", "url": "https://tile.loc.gov/...jp2"},
                        {"mimetype": "application/pdf", "url": "https://tile.loc.gov/...pdf"},
                        {"mimetype": "text/xml", "url": "https://tile.loc.gov/...xml"},
                        {"mimetype": "image/jpeg", "url": "...thumbnail..."},
                        {"mimetype": "image/jpeg", "url": "...thumbnail..."},
                        {"mimetype": "application/json", "title": "Image N of ...", ...},
                        {"mimetype": "text/plain", "fulltext_service": "https://tile.loc.gov/text-services/..."}
                    ],
                    ...  # one inner list per page in the issue
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

        Iterates over issue_details['resources'] and calls parse_page_from_issue
        for each page, assigning sequence numbers positionally.

        Args:
            issue_details: Full issue response dict.

        Returns:
            List of PageInfo, one per page in the issue.
        """
        pages = []
        for sequence, page_files in enumerate(issue_details.get('resources', []), start=1):
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
        """Validate that a date range is within LOC data bounds (1836 to present)."""
        try:
            if len(date1) == 4:
                date1 += '-01-01'
            if len(date2) == 4:
                date2 += '-12-31'

            start = datetime.strptime(date1, '%Y-%m-%d')
            end = datetime.strptime(date2, '%Y-%m-%d')

            return (
                start >= datetime(1836, 1, 1)
                and end <= datetime.now()
                and start <= end
            )

        except ValueError:
            return False
