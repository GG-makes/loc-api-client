"""
Chronicling America API Parameter Construction

Architecture
------------
ChroniclingAmericaSearchParams
    A normalized, API-agnostic representation of search intent. Holds lists
    where the CLI or database may supply comma-separated strings, and stores
    values in a canonical form that both builders can translate from.

QueryBuilder (ABC)
    Abstract base class enforcing a common interface across API versions.
    Owns two ingestion classmethods:

        from_facet(facet)
            Ingests a facet row as returned by storage.get_search_facets().
            This method is stable API surface — it exists to allow users with
            pre-August 2025 databases to resume their queries against either
            API version without modifying their stored data. Do not remove or
            change its signature.

        from_cli(...)
            Ingests raw CLI arguments. May be overridden by subclasses as CLI
            options evolve. LegacyQueryBuilder.from_cli accepts the pre-2025
            CLI argument shapes; LocGovQueryBuilder.from_cli may eventually
            accept new options (e.g. OR/PHRASE operators) as those are exposed
            in the CLI.

LegacyQueryBuilder(QueryBuilder)
    Targets: https://chroniclingamerica.loc.gov/search/pages/results/
    Conventions: dates as MM/DD/YYYY, state as title case, andtext, format=json

LocGovQueryBuilder(QueryBuilder)
    Targets: https://www.loc.gov/collections/chronicling-america/
    Conventions: dates as YYYY-MM-DD, state as lowercase, qs/ops, fo=json

Compatibility guarantee
-----------------------
Users whose discovery runs were interrupted before August 4, 2025 can resume
against the new API by changing one word at their call site:

    # Before
    LegacyQueryBuilder.from_facet(facet).build()

    # After
    LocGovQueryBuilder.from_facet(facet).build()

No database migration or CLI changes are required.
"""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Generator, List, Literal, Optional

# ---------------------------------------------------------------------------
# Normalized search intent
# ---------------------------------------------------------------------------



class ChroniclingAmericaSearchParams:
    """
    Normalized, API-agnostic representation of a Chronicling America search.

    This class holds *what* you want to search for. The QueryBuilder subclasses
    handle *how* to express that in a specific API version's parameter format.

    Do not instantiate this directly from CLI arguments or raw database values —
    use QueryBuilder.from_cli() or QueryBuilder.from_facet() instead, which
    normalize inputs before constructing this object.

    Attributes:
        search_text:
            Text to search for in newspaper OCR content.
        search_operator:
            How to combine search terms. "AND", "OR", or "PHRASE".
            Note: the legacy API only supports AND. This value is preserved
            here so it survives a migration to LocGovQueryBuilder.
        date1:
            Start of the date range as "YYYY" or "YYYY-MM-DD".
        date2:
            End of the date range as "YYYY" or "YYYY-MM-DD".
        states:
            List of lowercase full state names, e.g. ["california", "oregon"].
            Each builder formats these appropriately for its API version.
        lccn:
            LCCN to filter results to a specific newspaper title.
        batch:
            Digitization batch name to filter results.
        page:
            Page number for paginated results (1-indexed). Accessible for 
            Legacy API only.New api accepts 'next'
        rows:
            Results per page. Capped at 1000 by both builders.
        sort:
            Sort order. "date", "relevance", or "title".
    """

    def __init__(
        self,
        search_text: Optional[str] = None,
        search_operator: Literal["AND", "OR", "PHRASE"] = "AND",
        date1: Optional[str] = None,
        date2: Optional[str] = None,
        states: Optional[List[str]] = None,
        lccn: Optional[str] = None,
        batch: Optional[str] = None,
        page: int = 1,
        rows: int = 1000,
        sort: Literal["date", "relevance", "title"] = "date",
    ) -> None:
        self.search_text = search_text
        self.search_operator = search_operator
        self.date1 = date1
        self.date2 = date2
        self.states = states or []
        self.lccn = lccn
        self.batch = batch
        self.page = page
        self.rows = rows
        self.sort = sort

    def __repr__(self) -> str:
        return (
            f"ChroniclingAmericaSearchParams("
            f"search_text={self.search_text!r}, "
            f"date1={self.date1!r}, "
            f"date2={self.date2!r}, "
            f"states={self.states!r}, "
            f"page={self.page}, "
            f"rows={self.rows}"
            f")"
        )


# ---------------------------------------------------------------------------
# Date range splitting helper
# ---------------------------------------------------------------------------

def split_date_range(
    params: ChroniclingAmericaSearchParams,
    chunk_years: int = 1,
) -> Generator[ChroniclingAmericaSearchParams, None, None]:
    """
    Split a wide date range into smaller ChroniclingAmericaSearchParams
    instances to stay under the 100,000-item deep paging limit.

    Yields one params instance per chunk, inheriting all other fields
    (search_text, states, lccn, etc.) from the original.

    Works with both builders — LegacyQueryBuilder will format the resulting
    date1/date2 as MM/DD/YYYY, LocGovQueryBuilder as YYYY-MM-DD.

    Args:
        params:
            The source params object. Must have date1 set. If date2 is not
            set, defaults to the current year.
        chunk_years:
            Number of years per chunk. Defaults to 1 (one year per query).

    Example:
        params = ChroniclingAmericaSearchParams(date1="1900", date2="1920")
        for chunk in split_date_range(params, chunk_years=1):
            results = LocGovQueryBuilder(chunk).build()
    """
    if params.date1 is None:
        raise ValueError("split_date_range requires date1 to be set")

    start_year = int(params.date1[:4])
    end_year = int(params.date2[:4]) if params.date2 else datetime.now().year

    year = start_year
    while year <= end_year:
        chunk_end = min(year + chunk_years - 1, end_year)
        yield ChroniclingAmericaSearchParams(
            search_text=params.search_text,
            search_operator=params.search_operator,
            date1=str(year),
            date2=str(chunk_end),
            states=list(params.states),
            lccn=params.lccn,
            batch=params.batch,
            page=params.page,
            rows=params.rows,
            sort=params.sort,
        )
        year += chunk_years


# ---------------------------------------------------------------------------
# Abstract base class
# ---------------------------------------------------------------------------

class QueryBuilder(ABC):
    """
    Abstract base class for Chronicling America API query builders.

    Subclasses must implement build() and base_url.

    Ingestion classmethods
    ----------------------
    from_facet(facet)
        Stable API surface. Ingests a facet row from storage.get_search_facets().
        Both subclasses inherit this unchanged. Do not alter its signature —
        users with pre-August 2025 databases depend on it for resume capability.

    from_cli(...)
        Ingests raw CLI argument values. May be overridden by subclasses as
        the CLI evolves. LegacyQueryBuilder provides the baseline implementation
        matching the pre-2025 CLI argument shapes.
    """
    # Names of params this builder version emits that have no equivalent
    VERSION_SPECIFIC_PARAMS: frozenset = frozenset()

    def __init__(self, params: ChroniclingAmericaSearchParams) -> None:
        self.params = params

    # ------------------------------------------------------------------
    # Ingestion classmethods — shared entry points
    # ------------------------------------------------------------------

    @classmethod
    def from_facet(cls, facet: dict, **kwargs) -> "QueryBuilder":
        """
        Ingest a facet row as returned by storage.get_search_facets().

        Stable API surface for pre-August 2025 database compatibility.
        Handles both date_range and state facet types as stored in the
        legacy database schema.

        Args:
            facet:
                A dict with at minimum 'facet_type' and 'facet_value' keys,
                as returned by storage.get_search_facets(). Additional fields
                (estimated_items, status, etc.) are ignored.
            **kwargs:
                Any ChroniclingAmericaSearchParams fields to override or
                supplement, e.g. rows=50, search_text="flood".

        Facet type handling:
            date_range: facet_value "1906/1906" → date1="1906", date2="1906"
            state:      facet_value "California" → states=["california"]
            combined:   facet_value "state:California|date_range:1906/1906" → states=["california"], date1="1906", date2="1906"

        Example:
            # Resume a pre-migration database against the new API
            for facet in storage.get_search_facets(status='pending'):
                params = LocGovQueryBuilder.from_facet(facet)
                results = params.build()
        """
        facet_type = facet.get("facet_type")
        facet_value = facet.get("facet_value", "")

        date1 = kwargs.pop("date1", None)
        date2 = kwargs.pop("date2", None)
        states = kwargs.pop("states", [])

        if facet_type == "date_range" and "/" in facet_value:
            parts = facet_value.split("/")
            date1 = parts[0]
            date2 = parts[1]

        elif facet_type == "state":
            states = [facet_value.lower()]

        elif facet_type == "combined" and "|" in facet_value:
            for part in facet_value.split("|"):
                if ":" not in part:
                    continue
                key, value = part.split(":", 1)
                if key == "state":
                    states = [value.lower()]
                elif key == "date_range" and "/" in value:
                    date1, date2 = value.split("/")

        params = ChroniclingAmericaSearchParams(
            date1=date1,
            date2=date2,
            states=states,
            **kwargs,
        )
        return cls(params)

    @classmethod
    def from_cli(
        cls,
        text: Optional[str] = None,
        date1: Optional[str] = None,
        date2: Optional[str] = None,
        states: Optional[str] = None,
        rows: int = 1000,
        page: int = 1,
        sort: str = "date",
        lccn: Optional[str] = None,
        batch: Optional[str] = None,
    ) -> "QueryBuilder":
        """
        Ingest raw CLI argument values.

        Handles the normalization that the CLI layer produces:
            - states as a comma-separated string e.g. "California,New York"
              → normalized to ["california", "new york"]
            - date1/date2 as strings (CLI may pass ints for year-only values)
            - rows capped at 1000

        This implementation matches the pre-August 2025 CLI argument shapes.
        LocGovQueryBuilder may override this to accept new options (e.g.
        search_operator) as those are exposed in the CLI.

        Args:
            text:       Search text (maps to --text or positional TEXT argument)
            date1:      Start year or date (maps to --date1 or --start-year)
            date2:      End year or date (maps to --date2 or --end-year)
            states:     Comma-separated state names (maps to --states)
            rows:       Results per page (maps to --batch-size or --rows)
            page:       Page number (maps to --page)
            sort:       Sort order (maps to --sort)
            lccn:       LCCN filter (maps to --lccn)
            batch:      Batch name filter (maps to --batch)

        Example:
            builder = LegacyQueryBuilder.from_cli(
                text="earthquake",
                date1="1906",
                date2="1906",
                states="California,Oregon",
            )
            api_params = builder.build()
        """
        normalized_states = []
        if states:
            normalized_states = [s.strip().lower() for s in states.split(",")]

        params = ChroniclingAmericaSearchParams(
            search_text=text,
            search_operator="AND",  # legacy CLI had no operator option
            date1=str(date1) if date1 is not None else None,
            date2=str(date2) if date2 is not None else None,
            states=normalized_states,
            lccn=lccn,
            batch=batch,
            page=page,
            rows=min(rows, 1000),
            sort=sort,
        )
        return cls(params)

    # ------------------------------------------------------------------
    # Abstract interface
    # ------------------------------------------------------------------

    @abstractmethod
    def build(self) -> dict:
        """
        Build and return the API parameter dictionary.

        Returns:
            A dict suitable for passing as query parameters to the
            relevant Chronicling America API endpoint.
        """
        ...

    @property
    @abstractmethod
    def base_url(self) -> str:
        """The base URL for the API endpoint this builder targets."""
        ...

    def fetch_all_batches(self, fetch) -> Generator[dict, None, None]:
        """
        Default: one non-paginated fetch. `fetch` is a callable —
        fetch(url, params) -> dict — supplied by the caller, so this
        method owns the *shape* of the fetch (single vs. paginated),
        not the transport itself.
        """
        response = fetch(self.batch_list_url, self.build_batch_list())
        yield from response.get(self.batch_list_response_key, [])

# ---------------------------------------------------------------------------
# Legacy builder — pre-August 2025
# ---------------------------------------------------------------------------
class LegacyQueryBuilder(QueryBuilder):
    """
    Builds query parameters for the pre-August 2025 Chronicling America API.

    Target endpoint:
        https://chroniclingamerica.loc.gov/search/pages/results/

    Key conventions:
        - Dates accepted by this client as "YYYY" or "YYYY-MM-DD"; submitted
          to chronam as MM/DD/YYYY (or bare YYYY for year-pair ranges), per
          chronam's _solrize_date contract. See MIGRATION.md Open API
          Questions #1/#2. chronam also accepts MM/YYYY; not implemented here
          — out of scope per migration judgement call.
        - States as title-case full names, one per request
          (the legacy API did not support multiple states per query;
           if multiple states are present, only the first is used)
        - Search text via 'andtext' (AND logic only — OR/PHRASE not supported)
        - Output format via 'format=json'
        - LCCN and batch are not supported as direct filter parameters;
          they are ignored by this builder
    """
    VERSION_SPECIFIC_PARAMS = frozenset({"dateFilterType"})
    batch_list_response_key = 'batches'

    def fetch_all_batches(self, fetch) -> Generator[dict, None, None]:
        """
        Legacy batches.json is paginated — loop until an empty page.
        This loop shape is legacy-specific; LocGovQueryBuilder inherits
        the base class's single-fetch default since its batch list
        isn't paginated at all (confirmed: c=/sp= have no effect).
        """
        page = 1
        while True:
            response = fetch(self.batch_list_url, self.build_batch_list(page=page))
            batches = response.get(self.batch_list_response_key, [])
            if not batches:
                break
            yield from batches
            page += 1
            if page > response.get('totalPages', 1):
                break

    @property
    def base_url(self) -> str:
        return "https://chroniclingamerica.loc.gov/search/pages/results/"

    @property
    def newspaper_list_url(self) -> str:
        return "https://chroniclingamerica.loc.gov/newspapers.json"

    @property
    def batch_list_url(self) -> str:
        return "https://chroniclingamerica.loc.gov/batches.json"
    
    def build_batch_list(self, page: int = 1, rows: int = 100) -> dict:
        """
        Params for the legacy batches.json endpoint. Mirrors the original
        get_batches' params exactly (format/page/rows) — unchanged from
        pre-migration behavior.
        """
        return {
            'format': 'json',
            'page': page,
            'rows': min(rows, 1000),  # Respect API limits
        }

    def build_newspaper_list(self, page: int = 1, rows: int = 1000) -> dict:
        """
        Params for the legacy newspapers.json endpoint. Mirrors the original
        get_newspapers params exactly (format/page/rows) — unchanged from
        pre-migration behavior. Paginate via page/totalPages.
        """
        return {
            'format': 'json',
            'page': page,
            'rows': min(rows, 1000),
        }
        
    def fetch_all_newspapers(self, fetch) -> Generator[dict, None, None]:
        """
        Yield each page of the legacy newspapers.json response, paginating via
        page/totalPages — the same loop shape as the pre-migration
        get_all_newspapers. Callers parse each response through
        ResponseProcessor.parse_newspapers.
        """
        page = 1
        while True:
            response = fetch(self.newspaper_list_url, self.build_newspaper_list(page=page))
            yield response
            if not response.get('newspapers') or page >= response.get('totalPages', 1):
                break
            page += 1

    def _date_filter_type(self, date1: str, date2: str) -> str:
        """
        Tells chronam how to interpret date1/date2 (see _solrize_date).
        'yearRange' when both are bare years; 'range' otherwise. Only
        meaningful when both date1 and date2 are present.
        """
        return "yearRange" if len(date1) == 4 and len(date2) == 4 else "range"

    def _format_date(self, date_str: str, is_end_date: bool = False) -> str:
        """
        Format a date string into chronam's MM/DD/YYYY wire format.

        Args:
            date_str:    "YYYY" or "YYYY-M-D"/"YYYY-MM-DD" — the two formats
                        this client's CLI accepts (see MIGRATION.md). Month/day
                        need not be zero-padded on input.
            is_end_date: When True and only a year is given, uses Dec 31.
                        When False, uses Jan 1.

        Note this formatter is more permissive than the pre-migration logic. 
        The old formatter only accepted 0-padded dates for conversion.
        """
        if len(date_str) == 4 and date_str.isdigit():
            return f"12/31/{date_str}" if is_end_date else f"01/01/{date_str}"

        if 8 <= len(date_str) <= 10 and date_str.count("-") == 2:
            parts = date_str.split("-")
            if len(parts) == 3 and all(p.isdigit() for p in parts):
                year, month, day = parts
                return f"{month.zfill(2)}/{day.zfill(2)}/{year}"

        # Unrecognised format — pass through and let chronam reject it.
        return date_str

    def build(self) -> dict:
        """
        Build legacy API parameters.

        Limitations vs LocGovQueryBuilder:
            - search_operator is ignored; legacy API is AND-only via 'andtext'
            - Only the first state is used; multiple states require multiple requests
            - lccn and batch filters are not supported; ignored silently
            - dates_facet concept is gone; use split_date_range() to chunk
              wide date ranges before calling build()
        """
        params: dict = {
            "format": "json",
            "page": self.params.page,
            "rows": min(self.params.rows, 1000),
            "sort": self.params.sort,
        }

        if self.params.search_text:
            params["andtext"] = self.params.search_text

        # Date range
        if self.params.date1 is not None and self.params.date2 is not None:
            date1, date2 = self.params.date1, self.params.date2
            filter_type = self._date_filter_type(date1, date2)
            params["dateFilterType"] = filter_type
            if filter_type == "yearRange":
                params["date1"] = date1
                params["date2"] = date2
            else:
                params["date1"] = self._format_date(date1, is_end_date=False)
                params["date2"] = self._format_date(date2, is_end_date=True)
        elif self.params.date1 is not None:
            params["date1"] = self.params.date1

        # State — title case, first entry only
        if self.params.states:
            if len(self.params.states) > 1:
                # Legacy API does not support multiple states per request.
                # Callers should iterate over states and call build() per state,
                # or use split_date_range() in combination with a state loop.
                pass
            params["state"] = self.params.states[0].title()

        return params
    
    def build_count_only(self) -> dict:
        """
        Minimal-payload params for a count-only request. Mirrors
        rate_limited_client.estimate_download_size's original params
        (rows=1, page=1). chronam has no 'at'-style response-trimming
        equivalent — rows=1 is the only available minimization.

        Note: the original estimate_download_size accepted an lccn argument
        that was never actually included in its request params — a
        pre-existing no-op in the legacy code, not fixed here. See
        MIGRATION.md's Estimate/Count Mechanism table.
        """
        params = self.build()
        params["rows"] = 1
        params["page"] = 1
        return params

# ---------------------------------------------------------------------------
# Current builder — post-August 2025
# ---------------------------------------------------------------------------

class LocGovQueryBuilder(QueryBuilder):
    """
    Builds query parameters for the post-August 2025 loc.gov API.

    Target endpoint:
        https://www.loc.gov/collections/chronicling-america/

    Key conventions:
        - Dates as a YYYY-MM-DD/YYYY-MM-DD range via 'dates'
        - States as lowercase full names via 'fa=location_state:'
        (same fa= filter attribute pattern as lccn and batch)
        - Search text via 'qs', operator via 'ops'
        - LCCN filter via 'fa=number_lccn:{lccn}'
        - Batch filter via 'fa=batch:{batch}'
        - Output format via 'fo=json'
        - Display level via 'dl=page'
    """
    batch_list_response_key = 'datasets'

    @property
    def base_url(self) -> str:
        return "https://www.loc.gov/collections/chronicling-america/"

    @property
    def newspaper_list_url(self) -> str:
        return "https://www.loc.gov/collections/chronicling-america/titles/"

    @property
    def batch_list_url(self) -> str:
        return "https://www.loc.gov/collections/chronicling-america/datasets/batch-summary/"
            
    def build_batch_list(self) -> dict:
        """
        Params for the loc.gov batch-summary endpoint. fo=json is mandatory,
        not a default — confirmed live (2026-06-28): omitting it returns a
        Cloudflare bot-challenge page (403), not JSON. No page/rows are used;
        c=/sp= confirmed to have no effect; the endpoint always returns its
        full ~2959-entry dataset list in one response. See MIGRATION.md's
        Batch list response section.
        """
        return {'fo': 'json'}
    
    def build_newspaper_list(self) -> dict:
        """
        Params for the loc.gov titles endpoint. fo=json is required (same as
        batch-summary). c sets page size; confirmed live 2026-07-04 that titles/
        honours c up to at least the advertised perpage_options max of 150
        (of=4685 total -> ~32 pages) and the pagination.next cursor preserves c
        across pages. Pagination itself is handled by fetch_all_newspapers.
        """
        return {'fo': 'json', 'c': 150}

    def fetch_all_newspapers(self, fetch) -> Generator[dict, None, None]:
        """
        Yield each page of the loc.gov titles response, following the
        pagination.next cursor until it is absent — the same mechanism as
        paginate_search. The next URL carries c= forward, so the page size from
        build_newspaper_list persists. Callers parse each response through
        ResponseProcessor.parse_newspapers.
        """
        response = fetch(self.newspaper_list_url, self.build_newspaper_list())
        yield response
        while True:
            next_url = response.get('pagination', {}).get('next')
            if not next_url:
                break
            response = fetch(next_url)
            yield response

    @classmethod
    def from_cli(
        cls,
        text: Optional[str] = None,
        operator: Literal["AND", "OR", "PHRASE"] = "AND",
        date1: Optional[str] = None,
        date2: Optional[str] = None,
        states: Optional[str] = None,
        rows: int = 1000,
        page: int = 1,
        sort: str = "date",
        lccn: Optional[str] = None,
        batch: Optional[str] = None,
    ) -> "LocGovQueryBuilder":
        """
        Ingest raw CLI arguments for the new API.

        Extends the base from_cli with 'operator' support, since the new
        API accepts OR and PHRASE in addition to AND.
        """
        normalized_states = []
        if states:
            normalized_states = [s.strip().lower() for s in states.split(",")]

        params = ChroniclingAmericaSearchParams(
            search_text=text,
            search_operator=operator,
            date1=str(date1) if date1 is not None else None,
            date2=str(date2) if date2 is not None else None,
            states=normalized_states,
            lccn=lccn,
            batch=batch,
            page=page,
            rows=min(rows, 1000),
            sort=sort,
        )
        return cls(params)

    def _format_date(self, date_str: str, is_end_date: bool = False) -> str:
        """
        Format a date string into YYYY-MM-DD for the loc.gov API.

        Args:
            date_str:    "YYYY" or "YYYY-MM-DD"
            is_end_date: When True and only a year is given, uses Dec 31.
                         When False, uses Jan 1.
        """
        if len(date_str) == 4 and date_str.isdigit():
            return f"{date_str}-12-31" if is_end_date else f"{date_str}-01-01"
        elif len(date_str) == 10 and date_str.count("-") == 2:
            # Already in correct format
            return date_str
        # Unrecognised format — return as-is and let the API reject it
        return date_str

    def _build_fa_filters(self) -> List[str]:
        """
        Build the list of 'fa' filter strings for the loc.gov API.

        The loc.gov API accepts multiple fa= parameters, each a
        colon-separated key:value string. The requests library encodes
        a list value as repeated parameters: &fa=...&fa=...

        Supported filters:
            number_lccn:{lccn}  — filter by newspaper LCCN
            batch:{batch}       — filter by digitization batch name
        """
        filters = []
        if self.params.lccn:
            filters.append(f"number_lccn:{self.params.lccn}")
        if self.params.batch:
            filters.append(f"batch:{self.params.batch}")
        if self.params.states:
            filters.append(f"location_state:{self.params.states[0].lower()}")
        return filters
    
    def build(self) -> dict:
        """
        Build loc.gov API parameters.

        Improvements vs LegacyQueryBuilder:
            - search_operator fully supported (AND, OR, PHRASE)
            - lccn and batch filters supported via 'fa' parameters
            - dates expressed directly as YYYY-MM-DD (no facet strings needed)

        State handling:
            Multiple states still require multiple requests, same as legacy.
            Only the first state is used. Callers should loop over states.

        Note:
            'c' is used for count. Verify against current documentation
            before relying on them in production.
        """
        params: dict = {
            "fo": "json",
            "dl": "page",
            "at": "results,pagination",
            "c": min(self.params.rows, 1000),
        }
        
        # Search text and operator
        if self.params.search_text:
            params["qs"] = self.params.search_text
            params["ops"] = self.params.search_operator

        # Date range
        if self.params.date1 is not None:
            start = self._format_date(self.params.date1, is_end_date=False)
            end = self._format_date(
                self.params.date2 if self.params.date2 is not None
                else str(datetime.now().year),
                is_end_date=True,
            )
            params["dates"] = f"{start}/{end}"

        # LCCN and batch filters
        fa_filters = self._build_fa_filters()
        if fa_filters:
            params["fa"] = fa_filters

        return params
    
    def build_count_only(self) -> dict:
        """
        Minimal-payload params for an exact count, via response['pagination']['total'].
        Discovered via investigate_new_response_format.py; 'at' is not yet in
        MIGRATION.md's parameter tables. Unlike legacy, lccn/batch filters
        (via 'fa') are fully included since build() already supports them.
        """
        params = self.build()
        params["c"] = 1
        params["at"] = "search,results,pagination"
        return params
