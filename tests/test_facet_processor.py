"""
Tests for the facet processor components.
"""
import time
import logging
import pytest
from unittest.mock import Mock

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))

from newsagger.discovery.facet_processor import (
    FacetStatusValidator,
    adjust_batch_size_for_facet,
    FacetDiscoveryContext
)


class TestFacetStatusValidator:
    """Test FacetStatusValidator class."""
    
    @pytest.fixture
    def mock_storage(self):
        """Create a mock storage object."""
        storage = Mock()
        storage.update_facet_discovery = Mock()
        return storage
    
    @pytest.fixture
    def validator(self, mock_storage):
        """Create a FacetStatusValidator instance."""
        logger = Mock()
        return FacetStatusValidator(mock_storage, logger)
    
    def test_validate_normal_facet(self, validator):
        """Test validation of a normal facet that doesn't need fixing."""
        facet = {
            'id': 1,
            'status': 'pending',
            'current_page': None,
            'error_message': None,
            'resume_from_page': None
        }
        
        result = validator.validate_and_fix_facet_status(facet)
        
        # Should return the same facet unchanged
        assert result == facet
        assert not validator.storage.update_facet_discovery.called
    
    def test_validate_completed_facet_with_indicators(self, validator):
        """Test validation of incorrectly completed facet with CAPTCHA indicators."""
        facet = {
            'id': 1,
            'status': 'completed',
            'current_page': 5,
            'error_message': '',
            'resume_from_page': 3
        }
        
        result = validator.validate_and_fix_facet_status(facet)
        
        # Should be marked as discovering
        assert result['status'] == 'discovering'
        assert result['current_page'] == 6  # current_page + 1
        assert result['resume_from_page'] == 6
        assert 'Auto-fixed incorrectly completed facet' in result['error_message']
        
        # Should call storage update
        validator.storage.update_facet_discovery.assert_called_once()
        call_args = validator.storage.update_facet_discovery.call_args
        assert call_args[0][0] == 1  # facet_id
        assert call_args[1]['status'] == 'discovering'
        assert call_args[1]['current_page'] == 6
    
    def test_validate_completed_facet_no_indicators(self, validator):
        """Test validation of properly completed facet."""
        facet = {
            'id': 1,
            'status': 'completed',
            'current_page': 1,
            'error_message': 'Completed successfully',
            'resume_from_page': None
        }
        
        result = validator.validate_and_fix_facet_status(facet)
        
        # Should return unchanged
        assert result == facet
        assert not validator.storage.update_facet_discovery.called

class TestAdjustBatchSizeForFacet:
    """adjust_batch_size_for_facet: state facets get a smaller batch."""

    def test_state_facet_capped_at_50(self):
        logger = Mock()
        adjusted = adjust_batch_size_for_facet({'facet_type': 'state'}, 100, logger)
        assert adjusted == 50
        logger.info.assert_called_once()

    def test_date_range_facet_unchanged(self):
        logger = Mock()
        adjusted = adjust_batch_size_for_facet({'facet_type': 'date_range'}, 100, logger)
        assert adjusted == 100
        assert not logger.info.called


class TestFacetDiscoveryContext:
    """Test FacetDiscoveryContext class."""
    
    def test_initialization(self):
        """Test context initialization."""
        facet = {
            'id': 1,
            'resume_from_page': 3,
            'resume_cursor': 'https://www.loc.gov/collections/chronicling-america/?fo=json&c=150&sp=3',
            'items_discovered': 150
        }

        context = FacetDiscoveryContext(facet, batch_size=50, max_items=1000)

        assert context.facet_id == 1
        assert context.batch_size == 50
        assert context.max_items == 1000
        assert context.resume_cursor == 'https://www.loc.gov/collections/chronicling-america/?fo=json&c=150&sp=3'
        assert context.resume_from_page == 3
        assert context.total_discovered == 150
        assert context.current_page == 3

    def test_initialization_new_facet(self):
        """Test context initialization for new facet."""
        facet = {
            'id': 2,
            'resume_from_page': None,
            'items_discovered': None
        }
        
        context = FacetDiscoveryContext(facet, batch_size=100)
        
        assert context.facet_id == 2
        assert context.resume_from_page == 1
        assert context.total_discovered == 0
        assert context.current_page == 1
        assert context.resume_cursor is None
    
    def test_should_continue_discovery_unlimited(self):
        """Test discovery continuation with no max_items limit."""
        facet = {'id': 1}
        context = FacetDiscoveryContext(facet, batch_size=50)
        
        assert context.should_continue_discovery()
        
        # Should always continue without limit
        context.total_discovered = 10000
        assert context.should_continue_discovery()
    
    def test_should_continue_discovery_with_limit(self):
        """Test discovery continuation with max_items limit."""
        facet = {'id': 1}
        context = FacetDiscoveryContext(facet, batch_size=50, max_items=100)
        
        assert context.should_continue_discovery()
        
        context.total_discovered = 50
        assert context.should_continue_discovery()
        
        context.total_discovered = 100
        assert not context.should_continue_discovery()
        
        context.total_discovered = 150
        assert not context.should_continue_discovery()
    
    def test_get_remaining_items(self):
        """Test getting remaining items count."""
        facet = {'id': 1}
        context = FacetDiscoveryContext(facet, batch_size=50, max_items=100)
        
        assert context.get_remaining_items() == 100
        
        context.total_discovered = 30
        assert context.get_remaining_items() == 70
        
        context.total_discovered = 100
        assert context.get_remaining_items() == 0
        
        # Test unlimited
        context_unlimited = FacetDiscoveryContext(facet, batch_size=50)
        assert context_unlimited.get_remaining_items() is None
    
    def test_update_progress(self):
        """Test progress updates."""
        facet = {'id': 1}
        context = FacetDiscoveryContext(facet, batch_size=50)
        
        assert context.total_discovered == 0
        
        context.update_progress(25)
        assert context.total_discovered == 25
        
        context.update_progress(10)
        assert context.total_discovered == 35