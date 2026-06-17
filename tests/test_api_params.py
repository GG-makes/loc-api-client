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
    Prove LegacyQueryBuilder reproduces the parameter dicts that the
    pre-refactor api_client.py produced. Inputs mirror the old test fixtures.
    """

    def test_year_only_dates_formatted_as_mm_dd_yyyy(self):
        """test_search_pages asserted date1=01/01/1906, date2=12/31/1907."""
        result = LegacyQueryBuilder.from_cli(date1="1906", date2="1907").build()
        assert result["date1"] == "01/01/1906"
        assert result["date2"] == "12/31/1907"

    def test_full_date_converted_to_mm_dd_yyyy(self):
        result = LegacyQueryBuilder.from_cli(
            date1="1906-04-18", date2="1906-04-19"
        ).build()
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

    def test_date2_defaults_to_current_year_when_only_date1_given(self):
        current_year = str(datetime.now().year)
        result = LegacyQueryBuilder.from_cli(date1="1906").build()
        assert result["date2"].endswith(current_year)

    def test_no_date_keys_when_neither_given(self):
        result = LegacyQueryBuilder(ChroniclingAmericaSearchParams()).build()
        assert "date1" not in result
        assert "date2" not in result

    def test_from_facet_produces_correct_legacy_dates(self):
        """test_search_pages_with_facet asserted dates=1906/1907."""
        facet = {"facet_type": "date_range", "facet_value": "1906/1907"}
        result = LegacyQueryBuilder.from_facet(facet).build()
        assert result["date1"] == "01/01/1906"
        assert result["date2"] == "12/31/1907"


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
        result = LegacyQueryBuilder.from_facet(date_range_facet).build()
        assert result["date1"] == "01/01/1906"
        assert result["date2"] == "12/31/1906"

    def test_date_range_facet_locgov(self, date_range_facet):
        result = LocGovQueryBuilder.from_facet(date_range_facet).build()
        assert result["start_date"] == "1906-01-01"
        assert result["end_date"] == "1906-12-31"

    def test_state_facet_legacy(self, state_facet):
        result = LegacyQueryBuilder.from_facet(state_facet).build()
        assert result["state"] == "California"

    def test_state_facet_locgov(self, state_facet):
        result = LocGovQueryBuilder.from_facet(state_facet).build()
        assert result["location_state"] == "california"

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
        facet = {"facet_type": "date_range", "facet_value": "1900/1910"}
        assert LegacyQueryBuilder.from_facet(facet).build()["date1"] == "01/01/1900"
        assert LegacyQueryBuilder.from_facet(facet).build()["date2"] == "12/31/1910"
        assert LocGovQueryBuilder.from_facet(facet).build()["start_date"] == "1900-01-01"
        assert LocGovQueryBuilder.from_facet(facet).build()["end_date"] == "1910-12-31"


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
            assert "start_date" in result and "end_date" in result


# ---------------------------------------------------------------------------
# 6. Multiple states and builder independence
# ---------------------------------------------------------------------------

class TestStatesAndBuilderIndependence:

    LEGACY_ONLY_KEYS = {"format", "andtext", "state", "date1", "date2"} # row # page
    LOCGOV_ONLY_KEYS = {"fo", "qs", "ops", "location_state", "start_date", "end_date", "dl"}

    def test_legacy_uses_first_state_only(self):
        params = ChroniclingAmericaSearchParams(states=["california", "oregon"])
        assert LegacyQueryBuilder(params).build()["state"] == "California"

    def test_locgov_uses_first_state_only(self):
        params = ChroniclingAmericaSearchParams(states=["california", "oregon"])
        assert LocGovQueryBuilder(params).build()["location_state"] == "california"

    def test_legacy_state_is_title_case(self):
        params = ChroniclingAmericaSearchParams(states=["new york"])
        assert LegacyQueryBuilder(params).build()["state"] == "New York"

    def test_locgov_state_is_lowercase(self):
        params = ChroniclingAmericaSearchParams(states=["California"])
        assert LocGovQueryBuilder(params).build()["location_state"] == "california"

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