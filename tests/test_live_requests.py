"""
Live integration tests for LocGovQueryBuilder request construction.

These tests make real HTTP calls to www.loc.gov to verify that
LocGovQueryBuilder produces params the API accepts and that the
response shape matches what LocGovResponseProcessor expects.

They are skipped by default. Run with:

    pytest -m live tests/test_live_requests.py -v

One test per concern. Each uses c=1 to minimize response size —
we are testing the query, not downloading content.
"""

import time
import pytest
import requests

from newsagger.api_params import (
    ChroniclingAmericaSearchParams,
    LocGovQueryBuilder,
)

HEADERS = {"User-Agent": "Newsagger/0.1.0 (Educational Archive Tool - Rate Limited)"}
DELAY = 3.5  # seconds between requests — matches config minimum


def _get(builder, *, count_only=False):
    """Rate-limited request with 429 backoff matching main client behaviour."""
    params = builder.build_count_only() if count_only else builder.build()
    for attempt in range(3):
        time.sleep(DELAY * (3 ** attempt))  # 3.5s, 10.5s, 31.5s
        resp = requests.get(
            builder.base_url, headers=HEADERS, params=params, timeout=30
        )
        if resp.status_code != 429:
            break
    resp.raise_for_status()
    return resp.json()

class TestLocGovRequestsLive:
    pytestmark = [pytest.mark.live, pytest.mark.usefixtures("live_api_available")]
    def test_minimal_query_accepted(self):
        """Bare params accepted; response has flat results/pagination keys."""
        data = _get(LocGovQueryBuilder(ChroniclingAmericaSearchParams(rows=1)))
        assert "results" in data
        assert "pagination" in data
        assert isinstance(data["results"], list)
        assert data["pagination"]["total"] > 0

    def test_at_param_produces_flat_shape(self):
        """
        Confirms at=results,pagination is honoured — results NOT nested under
        pages[1]['children'][0]. If this fails, LocGovResponseProcessor.parse_pages
        will silently return empty lists.
        """
        data = _get(LocGovQueryBuilder(ChroniclingAmericaSearchParams(rows=1)))
        assert "results" in data, "results key missing — at= param not honoured"
        assert "pages" not in data, "full nested shape returned — at= param ignored"

    def test_date_range_filter_accepted(self):
        """dates (single range param), full ISO date required; results fall within the requested year."""
        data = _get(LocGovQueryBuilder(ChroniclingAmericaSearchParams(
            date1="1906", date2="1906", rows=1
        )))
        assert len(data["results"]) > 0
        assert data["results"][0]["date"].startswith("1906")

    def test_state_filter_accepted(self):
        """location_state accepted; result has matching state."""
        data = _get(LocGovQueryBuilder(ChroniclingAmericaSearchParams(
            states=["california"], rows=1
        )))
        assert len(data["results"]) > 0
        result = data["results"][0]
        assert "california" in [s.lower() for s in result.get("location_state", [])]

    def test_combined_date_and_state_accepted(self):
        """date + state in one request — a new-API capability not available in legacy."""
        data = _get(LocGovQueryBuilder(ChroniclingAmericaSearchParams(
            date1="1906", date2="1906", states=["california"], rows=1
        )))
        assert len(data["results"]) > 0
        result = data["results"][0]
        assert result["date"].startswith("1906")
        assert "california" in [s.lower() for s in result.get("location_state", [])]

    def test_lccn_filter_accepted(self):
        """fa=number_lccn: accepted; result is from the correct newspaper."""
        LCCN = "sn83030214"
        data = _get(LocGovQueryBuilder(ChroniclingAmericaSearchParams(
            lccn=LCCN, rows=1
        )))
        assert len(data["results"]) > 0
        assert LCCN in data["results"][0].get("number_lccn", [])

    def test_text_search_accepted(self):
        """q= and op= accepted; results present."""
        data = _get(LocGovQueryBuilder(ChroniclingAmericaSearchParams(
            search_text="earthquake", search_operator="AND", rows=1
        )))
        assert data["pagination"]["total"] > 0

    def test_count_only_returns_pagination_total(self):
        """build_count_only() accepted; pagination.total is an integer."""
        builder = LocGovQueryBuilder(ChroniclingAmericaSearchParams(
            date1="1906", date2="1906"
        ))
        data = _get(builder, count_only=True)
        assert "pagination" in data
        total = data["pagination"]["total"]
        assert isinstance(total, int) and total > 0

    def test_pagination_advances_correctly(self):
        """pagination.next URL advances through results — page 2 has different item IDs than page 1."""
        data1 = _get(LocGovQueryBuilder(ChroniclingAmericaSearchParams(
            date1="1906", date2="1906", rows=5
        )))
        next_url = data1.get("pagination", {}).get("next")
        assert next_url, "No pagination.next — cannot test pagination advance"

        time.sleep(DELAY)
        resp = requests.get(next_url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        data2 = resp.json()

        ids1 = {r["id"] for r in data1["results"]}
        ids2 = {r["id"] for r in data2["results"]}
        # loc.gov's default ordering isn't a stable unique sort, so a single item on
        # the page boundary can repeat across result pages (dedup + INSERT OR IGNORE
        # absorb it). The point here is that pagination ADVANCES — page 2 brings new
        # items — not that the two pages are perfectly disjoint.
        new_on_page2 = ids2 - ids1
        assert new_on_page2, "pagination did not advance — page 2 has no new item IDs"
        assert len(new_on_page2) >= 3, (
            f"page 2 mostly repeats page 1 ({len(new_on_page2)}/{len(ids2)} new) — "
            "pagination may not be advancing"
        )


    def test_result_fields_match_processor_expectations(self):
        """
        Spot-check that result item keys match what LocGovResponseProcessor
        reads. A mismatch here means parse_pages silently produces None/empty
        values without raising.
        """
        data = _get(LocGovQueryBuilder(ChroniclingAmericaSearchParams(
            date1="1906", date2="1906", rows=1
        )))
        result = data["results"][0]

        assert "id" in result or "url" in result   # item_id
        assert "date" in result                     # already YYYY-MM-DD
        assert "number_lccn" in result              # list
        assert "number_edition" in result           # list
        assert "partof_title" in result             # list
        assert isinstance(result["number_lccn"], list)
        assert isinstance(result["number_edition"], list)
        assert isinstance(result["partof_title"], list)

    def test_batch_list_endpoint_accepted(self):
        """build_batch_list() params accepted; datasets key present."""
        time.sleep(DELAY)
        builder = LocGovQueryBuilder(ChroniclingAmericaSearchParams())
        resp = requests.get(
            builder.batch_list_url,
            headers=HEADERS,
            params=builder.build_batch_list(),
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        assert "datasets" in data
        assert len(data["datasets"]) > 0
        
class TestLocGovImageUrlsLive:
    """
    Establishes that PDF/JP2 URLs cannot be constructed from search results
    alone and a second item-detail request is required per page.
    """
    pytestmark = [pytest.mark.live, pytest.mark.usefixtures("live_api_available")]

    @pytest.fixture(scope="class")
    def sample_page(self):
        """One search result reused across all tests in this class."""
        time.sleep(DELAY)
        builder = LocGovQueryBuilder(ChroniclingAmericaSearchParams(
            date1="1906", date2="1906", rows=1
        ))
        resp = requests.get(
            builder.base_url, headers=HEADERS, params=builder.build(), timeout=30
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
        assert results, "No results — cannot run image URL tests"
        return results[0]

    def test_item_detail_has_resource_pdf_and_image(self, sample_page):
        """
        Item detail endpoint has resource.pdf and resource.image — the
        authoritative image URLs per LocGovResponseProcessor.parse_page_details().
        """
        page_id = sample_page.get("id") or sample_page.get("url", "")
        sep = "&" if "?" in page_id else "?"
        detail_url = f"{page_id.rstrip('/')}{sep}fo=json"

        time.sleep(DELAY)
        resp = requests.get(detail_url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        resource = resp.json().get("resource", {})

        assert "pdf" in resource, f"resource.pdf missing from item detail: {detail_url}"
        assert "image" in resource, f"resource.image missing from item detail: {detail_url}"

    def test_compare_constructed_vs_detail_urls(self, sample_page):
        """
        Records whether the constructed URL matches item detail's resource.pdf.
        Not a hard assertion — prints findings for investigation.
        Both may work but via different hosts (loc.gov vs tile.loc.gov).
        """
        page_id = sample_page.get("id") or sample_page.get("url", "")
        constructed_pdf = f"{page_id.split('?')[0].rstrip('/')}.pdf"

        sep = "&" if "?" in page_id else "?"
        detail_url = f"{page_id.rstrip('/')}{sep}fo=json"

        time.sleep(DELAY)
        detail_pdf = (
            requests.get(detail_url, headers=HEADERS, timeout=30)
            .json()
            .get("resource", {})
            .get("pdf", "")
        )

        print(f"\n  page id:     {page_id}")
        print(f"  constructed: {constructed_pdf}")
        print(f"  detail:      {detail_pdf}")
        print(f"  match:       {constructed_pdf == detail_pdf}")