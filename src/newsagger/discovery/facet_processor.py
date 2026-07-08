"""
Facet processing components for breaking down the massive discover_facet_content method.

This module contains extracted components that handle specific aspects of facet discovery.
"""
import time
import logging
from typing import Dict, List, Optional, Any


class FacetStatusValidator:
    """Handles validation and recovery of facet status inconsistencies."""
    
    def __init__(self, storage, logger: Optional[logging.Logger] = None):
        self.storage = storage
        self.logger = logger or logging.getLogger(__name__)
    
    def validate_and_fix_facet_status(self, facet: Dict[str, Any]) -> Dict[str, Any]:
        """
        Validate facet status and fix CAPTCHA interruption issues.
        
        Args:
            facet: Facet data dictionary
            
        Returns:
            Updated facet data dictionary
        """
        facet_id = facet['id']
        
        # Proactive CAPTCHA interruption detection and fix
        if facet.get('status') == 'completed':
            current_page = facet.get('current_page')
            error_message = facet.get('error_message', '')
            
            # Check if this facet was incorrectly marked as completed due to CAPTCHA interruption
            captcha_indicators = []
            
            # Check for mid-discovery interruption (current_page set but no error)
            if current_page and current_page > 1 and not error_message:
                captcha_indicators.append(f"interrupted at page {current_page} with no error message")
            
            # Check if resume_from_page is set
            resume_from_page_check = facet.get('resume_from_page')
            if resume_from_page_check and resume_from_page_check > 1:
                captcha_indicators.append(f"resume page set to {resume_from_page_check}")
            
            if captcha_indicators:
                self.logger.warning(f"Detected incorrectly completed facet {facet_id}: {'; '.join(captcha_indicators)}")
                self.logger.info(f"Auto-fixing facet {facet_id} and continuing discovery...")
                
                # Auto-fix the facet status
                retry_time = time.time() + 1800  # 30 minute cooling-off
                retry_message = f"Auto-fixed incorrectly completed facet - was interrupted (indicators: {'; '.join(captcha_indicators)}). Fixed at: {time.ctime()}"
                
                # Resume from the next page after interruption
                resume_page = current_page + 1 if current_page and current_page > 1 else 1
                
                self.storage.update_facet_discovery(
                    facet_id,
                    status='discovering',  # Set to discovering so we can continue
                    error_message=retry_message,
                    current_page=resume_page  # This will set resume_from_page automatically
                )
                
                self.logger.info(f"Auto-fixed facet {facet_id}: status changed to 'discovering', will resume from page {resume_page}")
                
                # Update facet data for continued processing
                facet['status'] = 'discovering'
                facet['current_page'] = resume_page
                facet['resume_from_page'] = resume_page
                facet['error_message'] = retry_message
        
        return facet

def adjust_batch_size_for_facet(facet: Dict[str, Any], batch_size: int,
                                logger: Optional[logging.Logger] = None) -> int:
    """
    Adjust batch size by facet type to avoid timeouts. State facets use a
    smaller batch; other types are unchanged.
    """
    logger = logger or logging.getLogger(__name__)
    if facet['facet_type'] == 'state':
        adjusted_size = min(batch_size, 50)
        if adjusted_size != batch_size:
            logger.info(f"Using smaller batch size ({adjusted_size}) for state facet to avoid timeouts")
        return adjusted_size
    return batch_size

class FacetDiscoveryContext:
    """Context object for managing facet discovery state."""
    
    def __init__(self, facet: Dict[str, Any], batch_size: int, max_items: Optional[int] = None):
        self.facet = facet
        self.facet_id = facet['id']
        self.batch_size = batch_size
        self.max_items = max_items
        
        # Resume capability
        self.resume_cursor = facet.get('resume_cursor')
        resuming = self.resume_cursor is not None
        self.total_discovered = facet.get('items_discovered', 0) if resuming else 0

        # Legacy page-number resume state (retained until piece 5 removes it)
        self.resume_from_page = facet.get('resume_from_page') or 1
        self.current_page = self.resume_from_page
                        
    def should_continue_discovery(self) -> bool:
        """Check if discovery should continue based on max_items limit."""
        if self.max_items is None:
            return True
        return self.total_discovered < self.max_items
    
    def get_remaining_items(self) -> Optional[int]:
        """Get number of remaining items to discover."""
        if self.max_items is None:
            return None
        return self.max_items - self.total_discovered
    
    def update_progress(self, items_count: int):
        """Update discovery progress."""
        self.total_discovered += items_count