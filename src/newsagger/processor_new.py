"""
Chronicling America API Response Processing

Architecture
------------
ChroniclingAmericaResponse
    A normalised, API-agnostic representation of a parsed API response.
    Holds the results of either a newspaper list or a page search response
    in a consistent structure regardless of which API version produced it.

ResponseProcessor (ABC)
    Abstract base class enforcing a common interface across API versions.
    Owns two parsing classmethods:

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
        - Newspaper lists returned under 'results' key
        - Page search results returned under 'results' key
        - Full URLs prefixed with https://www.loc.gov/
        - Dates in YYYYMMDD or YYYY-MM-DD format

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
"""

import re
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional


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
    ocr_url: Optional[str]
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
                    ocr_url=f"{base}/ocr.txt",
                    word_count=None,
                ))
            except Exception:
                logger.warning(f"Failed to parse legacy page item: {item.get('id')}")
        return results


# ---------------------------------------------------------------------------
# Current processor — post-August 2025
# ---------------------------------------------------------------------------

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

        loc.gov response shape:
            {
                "results": [
                    {
                        "id": "...",
                        "title": "...",
                        "location_state": [...],
                        "language": [...],
                        ...
                    },
                    ...
                ]
            }
        """
        results = []
        for item in response.get('results', []):
            try:
                # loc.gov returns state as a list under 'location_state'
                place = item.get('location_state', []) or item.get('state', [])
                if isinstance(place, str):
                    place = [place]

                lccn = item.get('number_lccn', [''])[0] if isinstance(
                    item.get('number_lccn'), list) else item.get('number_lccn', '')

                results.append(NewspaperInfo(
                    lccn=lccn,
                    title=item.get('title', ''),
                    place_of_publication=place,
                    start_year=NewspaperInfo._parse_year(item.get('date', '')[:4] if item.get('date') else None),
                    end_year=None,  # Not directly available in list response
                    frequency=None,  # Not available in list response
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

        loc.gov response shape:
            {
                "results": [
                    {
                        "id": "...",
                        "title": "...",
                        "date": "...",
                        "resources": [{"pdf": "...", "image": "...", ...}],
                        ...
                    },
                    ...
                ]
            }
        """
        results = []
        for item in response.get('results', []):
            try:
                item_id = self.strip_base_url(item.get('id', '') or item.get('url', ''))
                item_id = item_id.strip('/').replace('.json', '')

                date = PageInfo._format_date(item.get('date', ''))

                # loc.gov provides resource URLs in a 'resources' list
                resources = item.get('resources', [{}])
                resource = resources[0] if resources else {}

                page_url = item.get('url', '') or item.get('id', '')
                base = page_url.rstrip('/')

                results.append(PageInfo(
                    item_id=item_id,
                    lccn=self._extract_lccn_from_url(item_id),
                    title=item.get('title', ''),
                    date=date,
                    edition=self._extract_edition_from_url(item_id),
                    sequence=self._extract_sequence_from_url(item_id),
                    page_url=base,
                    pdf_url=resource.get('pdf', f"{base}.pdf"),
                    jp2_url=resource.get('image', f"{base}.jp2"),
                    ocr_url=resource.get('fulltext_derivative', f"{base}/ocr.txt"),
                    word_count=None,
                ))
            except Exception:
                logger.warning(f"Failed to parse loc.gov page item: {item.get('id')}")
        return results