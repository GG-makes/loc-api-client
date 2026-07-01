"""
Tests for api_params.py

Coverage areas
--------------
1. Compatibility with existing fixtures
   LegacyQueryBuilder produces identical output to the pre-refactor
   api_client.py parameter construction. If these pass alongside the
   old client tests, the refactor is safe.

2. Internal logic
   _format_date for both builders, _build_fa_filters combinations,
   from_cli normalization edge cases.

3. Operator preservation
   OR/PHRASE survive into LocGovQueryBuilder and are silently dropped
   by LegacyQueryBuilder without raising. The operator remains on the
   params object after a legacy build, ready for migration.

4. from_facet compatibility contract
   The August 3rd person: a real facet dict from the pre-migration
   database produces valid params for both builders unchanged.

5. split_date_range correctness
   Chunk boundaries, field preservation, edge cases.

6. Multiple states and builder independence
   Only first state is used by both builders. Legacy and loc.gov output
   share no keys for the same input.

7. dateFilterType / chronam date format contract
   Confirmed against chronam's _solrize_date (see MIGRATION.md Open API
   Questions #1/#2): both date1 and date2 as bare years -> 'yearRange',
   dates kept unconverted; anything else -> 'range', dates formatted to
   chronam's MM/DD/YYYY wire format.
"""

import pytest
from datetime import datetime

from newsagger.api_params import (
    ChroniclingAmericaSearchParams,
    LegacyQueryBuilder,
    LocGovQueryBuilder,
    QueryBuilder,
    split_date_range,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def search_params():
    """Params with search text and date range, matching common CLI usage."""
    return ChroniclingAmericaSearchParams(
        search_text="earthquake",
        date1="1906",
        date2="1906",
        states=["california"],
    )


@pytest.fixture
def date_range_facet():
    """A facet row as returned by storage.get_search_facets() — date_range type."""
    return {
        "id": 42,
        "facet_type": "date_range",
        "facet_value": "1906/1906",
        "estimated_items": 1500,
        "items_discovered": 0,
        "status": "pending",
        "error_message": None,
    }


@pytest.fixture
def state_facet():
    """A facet row as returned by storage.get_search_facets() — state type."""
    return {
        "id": 7,
        "facet_type": "state",
        "facet_value": "California",
        "estimated_items": 5000,
        "items_discovered": 0,
        "status": "pending",
        "error_message": None,
    }


@pytest.fixture
def multi_year_params():
    """Params with a three-year date range for split_date_range testing."""
    return ChroniclingAmericaSearchParams(
        search_text="war",
        date1="1910",
        date2="1912",
        states=["texas"],
        rows=50,
    )


# ---------------------------------------------------------------------------
# 1. Compatibility with existing fixtures
# ---------------------------------------------------------------------------

class TestLegacyCompatibility:
    """
    Prove LegacyQueryBuilder reproduces chronam's documented date contract
    (see MIGRATION.md Open API Questions #1/#2), not the pre-refactor
    api_client.py tests' assumptions — those were found to assert behavior
    chronam never actually had (see test_year_only_dates_formatted_as_mm_dd_yyyy
    and test_date2_defaults_to_current_year_when_only_date1_given below).
    """

    def test_bare_year_pair_uses_year_range_unconverted(self):
        """
        Both date1 and date2 as bare years -> dateFilterType='yearRange',
        chronam expects the dates to stay as plain years (see
        rate_limited_client.search_pages / chronam core/index.py).
        """
        result = LegacyQueryBuilder.from_cli(date1="1906", date2="1907").build()
        assert result["dateFilterType"] == "yearRange"
        assert result["date1"] == "1906"
        assert result["date2"] == "1907"

    def test_full_date_converted_to_mm_dd_yyyy(self):
        """Both dates already specific -> 'range', formatted to chronam's MM/DD/YYYY."""
        result = LegacyQueryBuilder.from_cli(
            date1="1906-04-18", date2="1906-04-19"
        ).build()
        assert result["dateFilterType"] == "range"
        assert result["date1"] == "04/18/1906"
        assert result["date2"] == "04/19/1906"

    def test_search_text_passed_as_andtext(self):
        result = LegacyQueryBuilder.from_cli(text="earthquake").build()
        assert result["andtext"] == "earthquake"

    def test_andtext_absent_when_no_text(self):
        result = LegacyQueryBuilder.from_cli(text="").build()
        assert "andtext" not in result

    def test_format_json(self):
        result = LegacyQueryBuilder(ChroniclingAmericaSearchParams()).build()
        assert result["format"] == "json"

    def test_rows_capped_at_1000(self):
        result = LegacyQueryBuilder.from_cli(rows=2000).build()
        assert result["rows"] == 1000

    def test_default_sort_is_date(self):
        result = LegacyQueryBuilder.from_cli().build()
        assert result["sort"] == "date"

    def test_page_passed_through(self):
        result = LegacyQueryBuilder.from_cli(page=3).build()
        assert result["page"] == 3

    def test_only_date1_given_passes_through_unconverted_no_date2(self):
        """
        Mirrors rate_limited_client.search_pages exactly: the date-handling
        block only runs when BOTH date1 and date2 are present. With only
        date1, no dateFilterType is added and no date2 is synthesized —
        the pre-refactor test asserting a 'defaults to current year'
        fallback (test_date2_defaults_to_current_year_when_only_date1_given)
        was found to test a fabricated behavior with no basis in the
        production logic it claimed to mirror.
        """
        result = LegacyQueryBuilder.from_cli(date1="1906").build()
        assert result["date1"] == "1906"
        assert "date2" not in result
        assert "dateFilterType" not in result

    def test_no_date_keys_when_neither_given(self):
        result = LegacyQueryBuilder(ChroniclingAmericaSearchParams()).build()
        assert "date1" not in result
        assert "date2" not in result

    def test_from_facet_bare_year_pair_uses_year_range(self):
        """facet_value '1906/1907' is a bare-year pair -> 'yearRange', unconverted."""
        facet = {"facet_type": "date_range", "facet_value": "1906/1907"}
        result = LegacyQueryBuilder.from_facet(facet).build()
        assert result["dateFilterType"] == "yearRange"
        assert result["date1"] == "1906"
        assert result["date2"] == "1907"


# ---------------------------------------------------------------------------
# 2. Internal logic
# ---------------------------------------------------------------------------

class TestLegacyBuilderInternals:
    """_format_date edge cases and from_cli normalization for legacy builder."""

    def setup_method(self):
        self.builder = LegacyQueryBuilder(ChroniclingAmericaSearchParams())

    def test_format_date_year_start(self):
        assert self.builder._format_date("1906", is_end_date=False) == "01/01/1906"

    def test_format_date_year_end(self):
        assert self.builder._format_date("1906", is_end_date=True) == "12/31/1906"

    def test_format_date_full_date(self):
        assert self.builder._format_date("1906-04-18") == "04/18/1906"

    def test_format_date_non_zero_padded_iso_input(self):
        """
        chronam input isn't guaranteed zero-padded (e.g. CLI free-text).
        _format_date must zero-pad month/day on output regardless of
        input padding — a length==10 check alone would silently miss this.
        """
        assert self.builder._format_date("1906-4-1") == "04/01/1906"
        assert self.builder._format_date("1906-4-18") == "04/18/1906"
        assert self.builder._format_date("1906-11-1") == "11/01/1906"

    def test_format_date_malformed_dash_input_passes_through(self):
        """Non-digit components must not raise — fall through to passthrough."""
        assert self.builder._format_date("1906-ab-18") == "1906-ab-18"

    def test_format_date_unrecognised_passthrough(self):
        assert self.builder._format_date("invalid") == "invalid"

    def test_from_cli_coerces_integer_dates_to_string(self):
        """CLI year options are type=int — must be coerced before _format_date."""
        builder = LegacyQueryBuilder.from_cli(date1=1906, date2=1907)
        assert builder.params.date1 == "1906"
        assert builder.params.date2 == "1907"

    def test_from_cli_lowercases_and_splits_states(self):
        builder = LegacyQueryBuilder.from_cli(states="California, New York")
        assert builder.params.states == ["california", "new york"]

    def test_from_cli_none_states_produces_empty_list(self):
        builder = LegacyQueryBuilder.from_cli(states=None)
        assert builder.params.states == []

    def test_format_date_none_raises_legacy(self):
        """None slipping through to _format_date should raise clearly, not obscurely."""
        builder = LegacyQueryBuilder(ChroniclingAmericaSearchParams())
        with pytest.raises((TypeError, AttributeError)):
            builder._format_date(None)


class TestDateFilterType:
    """
    Direct coverage of _date_filter_type and its effect on build(), per
    chronam core/index.py's `date_filter_type in ("range", "yearRange")`
    branch. No prior test asserted dateFilterType's value directly.
    """

    def test_both_bare_years_is_year_range(self):
        builder = LegacyQueryBuilder(ChroniclingAmericaSearchParams())
        assert builder._date_filter_type("1906", "1907") == "yearRange"

    def test_mixed_year_and_full_date_is_range(self):
        builder = LegacyQueryBuilder(ChroniclingAmericaSearchParams())
        assert builder._date_filter_type("1906", "1906-06-01") == "range"
        assert builder._date_filter_type("1906-06-01", "1906") == "range"

    def test_both_full_dates_is_range(self):
        builder = LegacyQueryBuilder(ChroniclingAmericaSearchParams())
        assert builder._date_filter_type("1906-06-01", "1906-06-30") == "range"

    def test_build_mixed_pair_formats_only_required_field(self):
        """
        date1 already specific, date2 a bare year -> 'range'; date1 passes
        through _format_date unchanged (no-op on non-year input), date2
        gets boundary-expanded.
        """
        result = LegacyQueryBuilder.from_cli(
            date1="1906-06-15", date2="1907"
        ).build()
        assert result["dateFilterType"] == "range"
        assert result["date1"] == "06/15/1906"
        assert result["date2"] == "12/31/1907"


class TestLocGovBuilderInternals:
    """_format_date and _build_fa_filters for loc.gov builder."""

    def setup_method(self):
        self.builder = LocGovQueryBuilder(ChroniclingAmericaSearchParams())

    def test_format_date_year_start(self):
        assert self.builder._format_date("1906", is_end_date=False) == "1906-01-01"

    def test_format_date_year_end(self):
        assert self.builder._format_date("1906", is_end_date=True) == "1906-12-31"

    def test_format_date_full_date_passthrough(self):
        assert self.builder._format_date("1906-04-18") == "1906-04-18"

    def test_format_date_unrecognised_passthrough(self):
        assert self.builder._format_date("invalid") == "invalid"

    def test_fa_filters_lccn_only(self):
        params = ChroniclingAmericaSearchParams(lccn="sn83045201")
        filters = LocGovQueryBuilder(params)._build_fa_filters()
        assert filters == ["number_lccn:sn83045201"]

    def test_fa_filters_batch_only(self):
        params = ChroniclingAmericaSearchParams(batch="batch_ca_goldenstate_ver01")
        filters = LocGovQueryBuilder(params)._build_fa_filters()
        assert filters == ["batch:batch_ca_goldenstate_ver01"]

    def test_fa_filters_both(self):
        params = ChroniclingAmericaSearchParams(
            lccn="sn83045201", batch="batch_ca_goldenstate_ver01"
        )
        filters = LocGovQueryBuilder(params)._build_fa_filters()
        assert len(filters) == 2
        assert "number_lccn:sn83045201" in filters
        assert "batch:batch_ca_goldenstate_ver01" in filters

    def test_fa_absent_from_build_when_no_filters(self):
        result = LocGovQueryBuilder(ChroniclingAmericaSearchParams()).build()
        assert "fa" not in result

    def test_fa_is_list_in_build_when_filters_present(self):
        params = ChroniclingAmericaSearchParams(
            lccn="sn83045201", batch="batch_ca_goldenstate_ver01"
        )
        result = LocGovQueryBuilder(params).build()
        assert isinstance(result["fa"], list)

    def test_from_cli_accepts_operator(self):
        builder = LocGovQueryBuilder.from_cli(text="flood", operator="OR")
        assert builder.params.search_operator == "OR"

    def test_format_date_none_raises_locgov(self):
        builder = LocGovQueryBuilder(ChroniclingAmericaSearchParams())
        with pytest.raises((TypeError, AttributeError)):
            builder._format_date(None)

# ---------------------------------------------------------------------------
# 3. Operator preservation
# ---------------------------------------------------------------------------

class TestOperatorHandling:

    def test_legacy_drops_or_silently(self):
        params = ChroniclingAmericaSearchParams(
            search_text="flood", search_operator="OR"
        )
        result = LegacyQueryBuilder(params).build()
        assert result["andtext"] == "flood"
        assert "ops" not in result

    def test_legacy_does_not_raise_on_unsupported_operator(self):
        params = ChroniclingAmericaSearchParams(
            search_text="test", search_operator="PHRASE"
        )
        try:
            LegacyQueryBuilder(params).build()
        except Exception as exc:
            pytest.fail(f"LegacyQueryBuilder raised unexpectedly: {exc}")

    def test_operator_remains_on_params_after_legacy_build(self):
        """
        The operator must survive on the params object so it can be picked
        up by LocGovQueryBuilder after migration without re-ingesting input.
        """
        params = ChroniclingAmericaSearchParams(
            search_text="test", search_operator="OR"
        )
        builder = LegacyQueryBuilder(params)
        builder.build()
        assert builder.params.search_operator == "OR"

    def test_locgov_passes_and(self):
        params = ChroniclingAmericaSearchParams(
            search_text="earthquake", search_operator="AND"
        )
        assert LocGovQueryBuilder(params).build()["ops"] == "AND"

    def test_locgov_passes_or(self):
        params = ChroniclingAmericaSearchParams(
            search_text="flood", search_operator="OR"
        )
        assert LocGovQueryBuilder(params).build()["ops"] == "OR"

    def test_locgov_passes_phrase(self):
        params = ChroniclingAmericaSearchParams(
            search_text="san francisco", search_operator="PHRASE"
        )
        assert LocGovQueryBuilder(params).build()["ops"] == "PHRASE"

    def test_qs_and_ops_absent_when_no_text(self):
        result = LocGovQueryBuilder(ChroniclingAmericaSearchParams()).build()
        assert "qs" not in result
        assert "ops" not in result


# ---------------------------------------------------------------------------
# 4. from_facet compatibility contract
# ---------------------------------------------------------------------------

class TestFromFacetCompatibility:
    """
    The August 3rd person's tests. A real facet dict from the pre-migration
    database should produce valid params for both builders without modification.
    """

    def test_date_range_facet_legacy(self, date_range_facet):
        """facet_value '1906/1906' is a bare-year pair -> 'yearRange', unconverted."""
        result = LegacyQueryBuilder.from_facet(date_range_facet).build()
        assert result["dateFilterType"] == "yearRange"
        assert result["date1"] == "1906"
        assert result["date2"] == "1906"

    def test_date_range_facet_locgov(self, date_range_facet):
        result = LocGovQueryBuilder.from_facet(date_range_facet).build()
        assert result["dates"] == "1906-01-01/1906-12-31"

    def test_state_facet_legacy(self, state_facet):
        result = LegacyQueryBuilder.from_facet(state_facet).build()
        assert result["state"] == "California"

    def test_state_facet_locgov(self, state_facet):
        result = LocGovQueryBuilder.from_facet(state_facet).build()
        assert "location_state" not in result
        assert "location_state:california" in result.get("fa", [])

    def test_same_signature_both_builders(self, date_range_facet):
        """
        Core compatibility guarantee — both builders accept from_facet
        with identical arguments and produce non-empty dicts.
        """
        legacy = LegacyQueryBuilder.from_facet(date_range_facet).build()
        locgov = LocGovQueryBuilder.from_facet(date_range_facet).build()
        assert legacy
        assert locgov

    def test_irrelevant_database_fields_do_not_raise(self, date_range_facet):
        try:
            LegacyQueryBuilder.from_facet(date_range_facet)
            LocGovQueryBuilder.from_facet(date_range_facet)
        except Exception as exc:
            pytest.fail(f"from_facet raised on extra database fields: {exc}")

    def test_kwargs_supplement_facet_data(self, date_range_facet):
        result = LegacyQueryBuilder.from_facet(
            date_range_facet, search_text="earthquake", rows=50
        ).build()
        assert result["andtext"] == "earthquake"
        assert result["rows"] == 50

    def test_multi_year_range_facet(self):
        """facet_value '1900/1910' is a bare-year pair -> 'yearRange' on the legacy side."""
        facet = {"facet_type": "date_range", "facet_value": "1900/1910"}
        legacy_result = LegacyQueryBuilder.from_facet(facet).build()
        assert legacy_result["dateFilterType"] == "yearRange"
        assert legacy_result["date1"] == "1900"
        assert legacy_result["date2"] == "1910"
        assert LocGovQueryBuilder.from_facet(facet).build()["dates"] == "1900-01-01/1910-12-31"    

    def test_combined_facet_legacy(self):
        facet = {"facet_type": "combined", "facet_value": "state:California|date_range:1906/1906"}
        result = LegacyQueryBuilder.from_facet(facet).build()
        assert result["state"] == "California"
        assert result["dateFilterType"] == "yearRange"
        assert result["date1"] == "1906"
        assert result["date2"] == "1906"

    def test_combined_facet_locgov(self):
        facet = {"facet_type": "combined", "facet_value": "state:California|date_range:1906/1906"}
        result = LocGovQueryBuilder.from_facet(facet).build()
        assert "location_state" not in result
        assert "location_state:california" in result.get("fa", [])
        assert result["dates"] == "1906-01-01/1906-12-31"

# ---------------------------------------------------------------------------
# 5. split_date_range
# ---------------------------------------------------------------------------

class TestSplitDateRange:

    def test_single_year_produces_one_chunk(self):
        params = ChroniclingAmericaSearchParams(date1="1906", date2="1906")
        chunks = list(split_date_range(params, chunk_years=1))
        assert len(chunks) == 1
        assert chunks[0].date1 == "1906"
        assert chunks[0].date2 == "1906"

    def test_three_year_range_produces_three_chunks(self):
        params = ChroniclingAmericaSearchParams(date1="1906", date2="1908")
        chunks = list(split_date_range(params, chunk_years=1))
        assert len(chunks) == 3
        assert [c.date1 for c in chunks] == ["1906", "1907", "1908"]

    def test_chunk_boundaries_do_not_overlap(self):
        params = ChroniclingAmericaSearchParams(date1="1900", date2="1910")
        chunks = list(split_date_range(params, chunk_years=1))
        for i in range(len(chunks) - 1):
            assert int(chunks[i].date2) < int(chunks[i + 1].date1)

    def test_last_chunk_does_not_exceed_date2(self):
        params = ChroniclingAmericaSearchParams(date1="1900", date2="1905")
        chunks = list(split_date_range(params, chunk_years=2))
        assert int(chunks[-1].date2) <= 1905

    def test_decade_chunk_size(self):
        params = ChroniclingAmericaSearchParams(date1="1900", date2="1929")
        chunks = list(split_date_range(params, chunk_years=10))
        assert len(chunks) == 3
        assert chunks[0].date2 == "1909"
        assert chunks[1].date1 == "1910"

    def test_all_fields_preserved_in_chunks(self, multi_year_params):
        for chunk in split_date_range(multi_year_params, chunk_years=1):
            assert chunk.search_text == multi_year_params.search_text
            assert chunk.states == multi_year_params.states
            assert chunk.rows == multi_year_params.rows

    def test_states_list_is_copied_not_shared(self):
        """Mutating one chunk's states must not affect siblings."""
        params = ChroniclingAmericaSearchParams(
            date1="1900", date2="1901", states=["california"]
        )
        chunks = list(split_date_range(params, chunk_years=1))
        chunks[0].states.append("oregon")
        assert "oregon" not in chunks[1].states

    def test_raises_without_date1(self):
        with pytest.raises(ValueError, match="date1"):
            list(split_date_range(ChroniclingAmericaSearchParams()))

    def test_date2_defaults_to_current_year(self):
        current_year = datetime.now().year
        params = ChroniclingAmericaSearchParams(date1=str(current_year))
        chunks = list(split_date_range(params, chunk_years=1))
        assert chunks[-1].date2 == str(current_year)

    def test_chunks_produce_valid_legacy_params(self, multi_year_params):
        for chunk in split_date_range(multi_year_params, chunk_years=1):
            result = LegacyQueryBuilder(chunk).build()
            assert "date1" in result and "date2" in result

    def test_chunks_produce_valid_locgov_params(self, multi_year_params):
        for chunk in split_date_range(multi_year_params, chunk_years=1):
            result = LocGovQueryBuilder(chunk).build()
            assert "dates" in result


# ---------------------------------------------------------------------------
# 6. Multiple states and builder independence
# ---------------------------------------------------------------------------

class TestStatesAndBuilderIndependence:

    LEGACY_ONLY_KEYS = {"format", "andtext", "state", "date1", "date2", "dateFilterType"}
    LOCGOV_ONLY_KEYS = {"fo", "qs", "ops", "dates", "dl"}

    def test_legacy_uses_first_state_only(self):
        params = ChroniclingAmericaSearchParams(states=["california", "oregon"])
        assert LegacyQueryBuilder(params).build()["state"] == "California"

    def test_locgov_uses_first_state_only(self):
        params = ChroniclingAmericaSearchParams(states=["california", "oregon"])
        result = LocGovQueryBuilder(params).build()
        assert "location_state:california" in result.get("fa", [])
        assert "location_state:oregon" not in result.get("fa", [])
        assert "location_state" not in result

    def test_legacy_state_is_title_case(self):
        params = ChroniclingAmericaSearchParams(states=["new york"])
        assert LegacyQueryBuilder(params).build()["state"] == "New York"

    def test_locgov_state_is_lowercase(self):
        params = ChroniclingAmericaSearchParams(states=["California"])
        result = LocGovQueryBuilder(params).build()
        assert "location_state:california" in result.get("fa", [])
        assert "location_state" not in result

    def test_no_state_key_when_states_empty_legacy(self):
        result = LegacyQueryBuilder(ChroniclingAmericaSearchParams()).build()
        assert "state" not in result

    def test_no_location_state_key_when_states_empty_locgov(self):
        result = LocGovQueryBuilder(ChroniclingAmericaSearchParams()).build()
        assert "location_state" not in result

    def test_no_locgov_keys_in_legacy_output(self, search_params):
        result = LegacyQueryBuilder(search_params).build()
        overlap = set(result.keys()) & self.LOCGOV_ONLY_KEYS
        assert not overlap, f"Legacy output contained loc.gov keys: {overlap}"

    def test_no_legacy_keys_in_locgov_output(self, search_params):
        result = LocGovQueryBuilder(search_params).build()
        overlap = set(result.keys()) & self.LEGACY_ONLY_KEYS
        assert not overlap, f"Loc.gov output contained legacy keys: {overlap}"

    def test_abstract_base_cannot_be_instantiated(self):
        with pytest.raises(TypeError):
            QueryBuilder(ChroniclingAmericaSearchParams())

    def test_both_builders_accept_same_params_object(self, search_params):
        """The same params object can drive either builder."""
        assert isinstance(LegacyQueryBuilder(search_params).build(), dict)
        assert isinstance(LocGovQueryBuilder(search_params).build(), dict)


# ---------------------------------------------------------------------------
# 7. Full build() contract vs MIGRATION.md parameter mapping
# ---------------------------------------------------------------------------

class TestLocGovBuildContract:
    """
    Asserts LocGovQueryBuilder.build() produces exactly the keys/values
    documented in MIGRATION.md's parameter mapping tables. These tests
    exist to catch silent regressions in key names (e.g. accidentally
    emitting 'page' instead of 'sp', or 'rows' instead of 'c') that
    internals-only tests won't catch, since they assert on the helper
    methods rather than the final dict.
    """

    def test_minimal_build_has_only_base_keys(self):
        """No search/date/state/filters set — only the always-present keys."""
        params = ChroniclingAmericaSearchParams()
        result = LocGovQueryBuilder(params).build()
        assert result == {
            "fo": "json",
            "dl": "page",
            "at": "results,pagination",
            "sp": 1,
            "c": 1000,
        }

    def test_direct_substitution_keys_renamed_correctly(self):
        """andtext->qs, rows->c, page->sp, format=json->fo=json."""
        params = ChroniclingAmericaSearchParams(
            search_text="flood", page=3, rows=50,
        )
        result = LocGovQueryBuilder(params).build()
        assert result["qs"] == "flood"
        assert result["c"] == 50
        assert result["sp"] == 3
        assert result["fo"] == "json"
        assert "andtext" not in result
        assert "rows" not in result
        assert "page" not in result
        assert "format" not in result

    def test_date_keys_renamed_and_iso_formatted(self):
        """date1/date2 -> start_date/end_date, full ISO date required."""
        params = ChroniclingAmericaSearchParams(date1="1906", date2="1907")
        result = LocGovQueryBuilder(params).build()
        assert result["dates"] == "1906-01-01/1907-12-31"
        assert "date1" not in result
        assert "date2" not in result
        assert "start_date" not in result
        assert "end_date" not in result

    def test_state_key_renamed_to_location_state(self):
        """state= -> location_state=, lowercase (not title case like legacy)."""
        params = ChroniclingAmericaSearchParams(states=["california"])
        result = LocGovQueryBuilder(params).build()
        # new:
        assert "location_state:california" in result.get("fa", [])
        assert "location_state" not in result
        assert "state" not in result

    def test_lccn_moved_to_fa_filter_attribute_pattern(self):
        """lccn= -> fa=number_lccn:{lccn}, not a direct top-level param."""
        params = ChroniclingAmericaSearchParams(lccn="sn83045201")
        result = LocGovQueryBuilder(params).build()
        assert result["fa"] == ["number_lccn:sn83045201"]
        assert "lccn" not in result

    def test_operator_param_has_no_legacy_equivalent(self):
        """ops= is new; only present when search text triggers it."""
        params = ChroniclingAmericaSearchParams(
            search_text="flood", search_operator="PHRASE",
        )
        result = LocGovQueryBuilder(params).build()
        assert result["ops"] == "PHRASE"

    def test_full_query_matches_migration_doc_example(self):
        """
        Composite case exercising every mapped field at once, matching
        MIGRATION.md's documented parameter set for a typical search.
        """
        params = ChroniclingAmericaSearchParams(
            search_text="earthquake",
            search_operator="OR",
            date1="1906",
            date2="1906",
            states=["california"],
            lccn="sn83045201",
            batch="batch_ca_goldenstate_ver01",
            page=2,
            rows=100,
        )
        result = LocGovQueryBuilder(params).build()
        assert result == {
            "fo": "json",
            "dl": "page",
            "at": "results,pagination",
            "sp": 2,
            "c": 100,
            "qs": "earthquake",
            "ops": "OR",
            "dates": "1906-01-01/1906-12-31",
            "fa": [
                "number_lccn:sn83045201",
                "batch:batch_ca_goldenstate_ver01",
                "location_state:california",
            ],
        }


class TestLegacyBuildContract:
    """
    Mirrors TestLocGovBuildContract for LegacyQueryBuilder, so a future
    change to shared logic (e.g. a refactor that merges the two builders)
    can't silently break the legacy key names either.
    """

    def test_minimal_build_has_only_base_keys(self):
        params = ChroniclingAmericaSearchParams()
        result = LegacyQueryBuilder(params).build()
        assert result == {
            "format": "json",
            "page": 1,
            "rows": 1000,
            "sort": "date",
        }

    def test_full_query_uses_legacy_key_names(self):
        """date1/date2 are a bare-year pair -> dateFilterType='yearRange', unconverted."""
        params = ChroniclingAmericaSearchParams(
            search_text="earthquake",
            date1="1906",
            date2="1906",
            states=["california"],
            page=2,
            rows=100,
        )
        result = LegacyQueryBuilder(params).build()
        assert result == {
            "format": "json",
            "page": 2,
            "rows": 100,
            "sort": "date",
            "andtext": "earthquake",
            "dateFilterType": "yearRange",
            "date1": "1906",
            "date2": "1906",
            "state": "California",
        }
        # new-API-only params must never appear in legacy output
        for key in ("qs", "ops", "dates", "location_state", "fa", "sp", "c", "fo", "dl"):
            assert key not in result

    def test_full_query_with_specific_dates_uses_range_and_mm_dd_yyyy(self):
        """A non-bare-year pair exercises the other branch of the same contract."""
        params = ChroniclingAmericaSearchParams(
            search_text="earthquake",
            date1="1906-04-18",
            date2="1906-04-20",
            states=["california"],
            page=2,
            rows=100,
        )
        result = LegacyQueryBuilder(params).build()
        assert result == {
            "format": "json",
            "page": 2,
            "rows": 100,
            "sort": "date",
            "andtext": "earthquake",
            "dateFilterType": "range",
            "date1": "04/18/1906",
            "date2": "04/20/1906",
            "state": "California",
        }


# ---------------------------------------------------------------------------
# 8. Documented gaps — MIGRATION.md params with no implementation yet
# ---------------------------------------------------------------------------

class TestUnimplementedMigrationParameters:
    """
    MIGRATION.md lists these as new loc.gov parameters with no legacy
    equivalent: front_pages_only, location_city, location_county,
    partof_title, subject_ethnicity. None are currently exposed on
    ChroniclingAmericaSearchParams or LocGovQueryBuilder.

    These tests are intentionally xfail — they document the gap so it
    shows up in CI output rather than being silently forgotten, and they
    should be flipped to real assertions as each parameter is implemented.
    """

    @pytest.mark.xfail(reason="location_city not yet implemented", strict=True)
    def test_location_city_filter(self):
        params = ChroniclingAmericaSearchParams(location_city="oakland")
        result = LocGovQueryBuilder(params).build()
        assert result["location_city"] == "oakland"

    @pytest.mark.xfail(reason="location_county not yet implemented", strict=True)
    def test_location_county_filter(self):
        params = ChroniclingAmericaSearchParams(location_county="alameda")
        result = LocGovQueryBuilder(params).build()
        assert result["location_county"] == "alameda"

    @pytest.mark.xfail(reason="partof_title not yet implemented", strict=True)
    def test_partof_title_filter(self):
        params = ChroniclingAmericaSearchParams(partof_title="san francisco call")
        result = LocGovQueryBuilder(params).build()
        assert result["partof_title"] == "san francisco call"

    @pytest.mark.xfail(reason="front_pages_only not yet implemented", strict=True)
    def test_front_pages_only_filter(self):
        params = ChroniclingAmericaSearchParams(front_pages_only=True)
        result = LocGovQueryBuilder(params).build()
        assert result["front_pages_only"] == "true"

    @pytest.mark.xfail(reason="subject_ethnicity not yet implemented", strict=True)
    def test_subject_ethnicity_filter(self):
        params = ChroniclingAmericaSearchParams(subject_ethnicity="german")
        result = LocGovQueryBuilder(params).build()
        assert result["subject_ethnicity"] == "german"

class TestBatchListing:
    """build_batch_list, fetch_all_batches, and the batch/newspaper endpoint properties."""

    def test_legacy_batch_list_url(self):
        builder = LegacyQueryBuilder(ChroniclingAmericaSearchParams())
        assert builder.batch_list_url == "https://chroniclingamerica.loc.gov/batches.json"

    def test_locgov_batch_list_url(self):
        builder = LocGovQueryBuilder(ChroniclingAmericaSearchParams())
        assert builder.batch_list_url == "https://www.loc.gov/collections/chronicling-america/datasets/batch-summary/"

    def test_legacy_newspaper_list_url(self):
        builder = LegacyQueryBuilder(ChroniclingAmericaSearchParams())
        assert builder.newspaper_list_url == "https://chroniclingamerica.loc.gov/newspapers.json"

    def test_locgov_newspaper_list_url(self):
        builder = LocGovQueryBuilder(ChroniclingAmericaSearchParams())
        assert builder.newspaper_list_url == "https://www.loc.gov/collections/chronicling-america/titles/"

    def test_legacy_batch_list_response_key(self):
        assert LegacyQueryBuilder.batch_list_response_key == 'batches'

    def test_locgov_batch_list_response_key(self):
        assert LocGovQueryBuilder.batch_list_response_key == 'datasets'

    def test_legacy_build_batch_list_params(self):
        builder = LegacyQueryBuilder(ChroniclingAmericaSearchParams())
        assert builder.build_batch_list(page=2, rows=50) == {
            'format': 'json', 'page': 2, 'rows': 50,
        }

    def test_legacy_build_batch_list_caps_rows(self):
        builder = LegacyQueryBuilder(ChroniclingAmericaSearchParams())
        assert builder.build_batch_list(rows=5000)['rows'] == 1000

    def test_locgov_build_batch_list_is_fo_json_only(self):
        """
        Confirmed live (2026-06-28): fo=json is mandatory, c=/sp= have no
        effect. No page/rows params here at all — accepting them would
        imply pagination support that doesn't exist.
        """
        builder = LocGovQueryBuilder(ChroniclingAmericaSearchParams())
        assert builder.build_batch_list() == {'fo': 'json'}

    def test_locgov_build_batch_list_takes_no_arguments(self):
        builder = LocGovQueryBuilder(ChroniclingAmericaSearchParams())
        with pytest.raises(TypeError):
            builder.build_batch_list(page=1)

    def test_fetch_all_batches_default_single_fetch(self):
        """Base class default (used by LocGovQueryBuilder): one fetch, no pagination."""
        builder = LocGovQueryBuilder(ChroniclingAmericaSearchParams())
        calls = []

        def fake_fetch(url, params):
            calls.append((url, params))
            return {'datasets': [{'batch': 'a'}, {'batch': 'b'}]}

        result = list(builder.fetch_all_batches(fake_fetch))

        assert len(calls) == 1
        assert calls[0] == (builder.batch_list_url, {'fo': 'json'})
        assert result == [{'batch': 'a'}, {'batch': 'b'}]

    def test_fetch_all_batches_legacy_paginates(self):
        """LegacyQueryBuilder's override: loops pages until exhausted."""
        builder = LegacyQueryBuilder(ChroniclingAmericaSearchParams())
        pages = [
            {'batches': [{'batch': 'p1a'}, {'batch': 'p1b'}], 'totalPages': 2},
            {'batches': [{'batch': 'p2a'}], 'totalPages': 2},
        ]
        calls = []

        def fake_fetch(url, params):
            calls.append(params['page'])
            return pages[params['page'] - 1]

        result = list(builder.fetch_all_batches(fake_fetch))

        assert calls == [1, 2]
        assert result == [{'batch': 'p1a'}, {'batch': 'p1b'}, {'batch': 'p2a'}]

    def test_fetch_all_batches_legacy_stops_on_empty_page(self):
        builder = LegacyQueryBuilder(ChroniclingAmericaSearchParams())

        def fake_fetch(url, params):
            if params['page'] == 1:
                return {'batches': [{'batch': 'only'}], 'totalPages': 99}
            return {'batches': [], 'totalPages': 99}

        result = list(builder.fetch_all_batches(fake_fetch))
        assert result == [{'batch': 'only'}]