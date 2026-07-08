from abc import ABC, abstractmethod
from typing import Optional
from .api_params import QueryBuilder, ChroniclingAmericaSearchParams


class FacetQueryStrategy(ABC):
    """
    Translate a stored search facet (date_range / state / combined / query)
    into a query builder for discovery.

    The state case diverges by API and is storage-coupled, which is why this
    can't live in the stateless QueryBuilder.from_facet:
      - legacy samples LCCNs from previously-discovered periodicals and searches
        andtext 'lccn:(A OR B ...)' (the original author's workaround for
        unreliable legacy state search);
      - loc.gov filters by location_state natively, no periodicals required.
    """

    def __init__(self, storage, query_builder_class):
        self.storage = storage
        self.query_builder_class = query_builder_class

    @abstractmethod
    def build_query(self, facet: dict, rows: int) -> Optional[QueryBuilder]:
        """
        Return a query builder for the facet, or None if the facet has no
        searchable content — in which case the caller completes it with 0 items.
        None is a legacy-only outcome (a state with no discovered periodicals);
        loc.gov always returns a builder.
        """

class LegacyFacetQueryStrategy(FacetQueryStrategy):
    """Reproduces the pre-migration facet→query behaviour (ADR 0004/0005)."""

    def build_query(self, facet, rows):
        if facet['facet_type'] == 'state':
            periodicals = self.storage.get_periodicals(state=facet['facet_value'])
            if not periodicals:
                return None  # nothing discovered to search
            sample_lccns = [p['lccn'] for p in periodicals[:5]]
            # Faithful to the original: from_facet keeps the state= filter;
            # overlay the andtext lccn:(...) query the author added on top of it.
            builder = self.query_builder_class.from_facet(facet, rows=rows)
            builder.params.search_text = (
                f"lccn:({' OR '.join(sample_lccns)})" if sample_lccns
                else facet['facet_value']
            )
            return builder

        if facet.get('query'):
            return self.query_builder_class.from_facet(
                facet, rows=rows, search_text=facet['query'])

        # date_range / combined: handled natively by from_facet
        return self.query_builder_class.from_facet(facet, rows=rows)

class LocGovFacetQueryStrategy(FacetQueryStrategy):
    """loc.gov facet→query: native filters, no periodicals precondition."""

    def build_query(self, facet, rows):
        if facet.get('query'):
            return self.query_builder_class.from_facet(
                facet, rows=rows, search_text=facet['query']
            )
        # date_range / state / combined all native via from_facet
        # (state → fa=location_state; no periodicals precondition, so never None).
        return self.query_builder_class.from_facet(facet, rows=rows)