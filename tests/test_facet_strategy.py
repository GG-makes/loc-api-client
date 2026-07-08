"""
Unit tests for FacetQueryStrategy — the API-specific facet→query translation.

These exercise the strategy in isolation (mock storage, real builders), which
the discover_facet_content integration tests can't: those mock paginate_search
and never inspect the builder the strategy produces.
"""

import pytest
from unittest.mock import Mock
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))

from newsagger.facet_strategy import (
    FacetQueryStrategy,
    LegacyFacetQueryStrategy,
    LocGovFacetQueryStrategy,
)
from newsagger.api_params import LegacyQueryBuilder, LocGovQueryBuilder


class TestFacetQueryStrategyABC:
    def test_cannot_instantiate_abstract_base(self):
        with pytest.raises(TypeError):
            FacetQueryStrategy(Mock(), LegacyQueryBuilder)


class TestLegacyFacetQueryStrategy:
    def setup_method(self):
        self.storage = Mock()
        self.strategy = LegacyFacetQueryStrategy(self.storage, LegacyQueryBuilder)

    def test_state_facet_samples_lccns_and_keeps_state_filter(self):
        self.storage.get_periodicals.return_value = [{'lccn': 'sn1'}, {'lccn': 'sn2'}]
        facet = {'facet_type': 'state', 'facet_value': 'California'}

        builder = self.strategy.build_query(facet, rows=100)

        # from_facet keeps the state= filter; the strategy overlays the lccn andtext query
        assert builder.params.states == ['california']
        assert builder.params.search_text == 'lccn:(sn1 OR sn2)'
        self.storage.get_periodicals.assert_called_once_with(state='California')

    def test_state_facet_with_no_periodicals_returns_none(self):
        self.storage.get_periodicals.return_value = []
        facet = {'facet_type': 'state', 'facet_value': 'California'}
        assert self.strategy.build_query(facet, rows=100) is None

    def test_state_facet_caps_at_five_lccns(self):
        self.storage.get_periodicals.return_value = [{'lccn': f'sn{i}'} for i in range(7)]
        facet = {'facet_type': 'state', 'facet_value': 'California'}
        builder = self.strategy.build_query(facet, rows=100)
        assert builder.params.search_text == 'lccn:(sn0 OR sn1 OR sn2 OR sn3 OR sn4)'

    def test_query_facet_sets_search_text_without_touching_storage(self):
        facet = {'facet_type': 'query', 'facet_value': 'earthquake', 'query': 'earthquake'}
        builder = self.strategy.build_query(facet, rows=50)
        assert builder.params.search_text == 'earthquake'
        self.storage.get_periodicals.assert_not_called()

    def test_date_range_facet_sets_dates_and_no_query(self):
        facet = {'facet_type': 'date_range', 'facet_value': '1906/1906'}
        builder = self.strategy.build_query(facet, rows=50)
        assert builder.params.date1 == '1906'
        assert builder.params.date2 == '1906'
        assert not builder.params.search_text

    def test_combined_facet_sets_state_and_dates(self):
        facet = {'facet_type': 'combined', 'facet_value': 'state:California|date_range:1906/1906'}
        builder = self.strategy.build_query(facet, rows=50)
        assert builder.params.states == ['california']
        assert builder.params.date1 == '1906'
        assert builder.params.date2 == '1906'

    def test_rows_passed_to_builder(self):
        facet = {'facet_type': 'date_range', 'facet_value': '1906/1906'}
        builder = self.strategy.build_query(facet, rows=42)
        assert builder.params.rows == 42

    def test_returns_legacy_builder(self):
        facet = {'facet_type': 'date_range', 'facet_value': '1906/1906'}
        builder = self.strategy.build_query(facet, rows=10)
        assert isinstance(builder, LegacyQueryBuilder)


class TestLocGovFacetQueryStrategy:
    def setup_method(self):
        self.storage = Mock()
        self.strategy = LocGovFacetQueryStrategy(self.storage, LocGovQueryBuilder)

    def test_state_facet_uses_native_filter_without_storage(self):
        facet = {'facet_type': 'state', 'facet_value': 'California'}
        builder = self.strategy.build_query(facet, rows=100)
        assert builder.params.states == ['california']
        assert not builder.params.search_text
        # loc.gov filters by location_state natively — no periodicals precondition
        self.storage.get_periodicals.assert_not_called()

    def test_state_facet_never_returns_none(self):
        facet = {'facet_type': 'state', 'facet_value': 'California'}
        assert self.strategy.build_query(facet, rows=100) is not None

    def test_query_facet_sets_search_text(self):
        facet = {'facet_type': 'query', 'facet_value': 'earthquake', 'query': 'earthquake'}
        builder = self.strategy.build_query(facet, rows=50)
        assert builder.params.search_text == 'earthquake'

    def test_date_range_facet_sets_dates(self):
        facet = {'facet_type': 'date_range', 'facet_value': '1906/1906'}
        builder = self.strategy.build_query(facet, rows=50)
        assert builder.params.date1 == '1906'
        assert builder.params.date2 == '1906'

    def test_returns_locgov_builder(self):
        facet = {'facet_type': 'date_range', 'facet_value': '1906/1906'}
        builder = self.strategy.build_query(facet, rows=10)
        assert isinstance(builder, LocGovQueryBuilder)