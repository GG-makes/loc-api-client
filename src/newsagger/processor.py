"""
Data Processing Module

Handles processing and validation of news archive data from the 
Library of Congress API responses.
"""

import json
import logging
from typing import Dict, List, Optional, Set
from dataclasses import dataclass
from datetime import datetime
import re

#TODO: MAjor migration

@dataclass
class NewspaperInfo:
    """Structured representation of newspaper metadata."""
    lccn: str
    title: str
    place_of_publication: List[str]
    start_year: Optional[int]
    end_year: Optional[int]
    frequency: Optional[str]
    subject: List[str]
    language: List[str]
    url: str
    
    @classmethod
    def from_api_response(cls, data: Dict) -> 'NewspaperInfo':
        """Create NewspaperInfo from API response (handles both list and detail formats)."""
        # Handle real LOC API format: {lccn, state, title, url}
        place_of_publication = []
        if 'state' in data:
            place_of_publication = [data['state']]
        elif 'place_of_publication' in data:
            place_of_publication = data['place_of_publication']
        
        return cls(
            lccn=data.get('lccn', ''),
            title=data.get('title', ''),
            place_of_publication=place_of_publication,
            start_year=cls._parse_year(data.get('start_year')),
            end_year=cls._parse_year(data.get('end_year')),
            frequency=data.get('frequency'),
            subject=data.get('subject', []),
            language=data.get('language', []),
            url=data.get('url', '')
        )
    
    @classmethod
    def from_detail_response(cls, basic_info: Dict, detail_data: Dict) -> 'NewspaperInfo':
        """Create NewspaperInfo from newspaper detail API response."""
        return cls(
            lccn=basic_info.get('lccn', ''),
            title=basic_info.get('title', ''),
            place_of_publication=[basic_info.get('state', '')] if basic_info.get('state') else [],
            start_year=cls._parse_year(detail_data.get('start_year')),
            end_year=cls._parse_year(detail_data.get('end_year')),
            frequency=detail_data.get('frequency'),
            subject=detail_data.get('subject', []),
            language=detail_data.get('language', []),
            url=basic_info.get('url', '')
        )
    
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
    """Structured representation of newspaper page metadata."""
    item_id: str
    lccn: str
    title: str
    date: str
    edition: int
    sequence: int
    page_url: str
    pdf_url: Optional[str]
    jp2_url: Optional[str]
    ocr_text: Optional[str]
    word_count: Optional[int]
    
    @classmethod
    def from_search_result(cls, data: Dict) -> 'PageInfo':
        """Create PageInfo from search result item (handles real LOC API format)."""
        # Real LOC API uses 'id' field like: '/lccn/sn83025581/1756-10-07/ed-1/seq-1/'
        item_id = data.get('id', '')
        if not item_id and data.get('url'):
            # Extract from URL if ID not available
            url_parts = data.get('url', '').strip('/').split('/')
            if len(url_parts) >= 2:
                item_id = url_parts[-1] or url_parts[-2]
        if not item_id:
            # Fallback: create from lccn + date + sequence
            date_val = data.get('date', 'unknown')
            seq_val = data.get('sequence', data.get('seq', 1))
            item_id = f"{data.get('lccn', 'unknown')}_{date_val}_{seq_val}"
        
        # Parse date from real API format (YYYYMMDD) to YYYY-MM-DD
        date_raw = data.get('date', '')
        formatted_date = cls._format_date(date_raw)
        
        # Extract edition from id or use default
        edition = data.get('edition')
        if edition is None and item_id:
            # Try to extract edition from id like '/lccn/sn83025581/1756-10-07/ed-1/seq-1/'
            parts = item_id.strip('/').split('/')
            for part in parts:
                if part.startswith('ed-'):
                    try:
                        edition = int(part.split('-')[1])
                        break
                    except (IndexError, ValueError):
                        pass
        if edition is None:
            edition = 1
            
        # Extract sequence from id or use default
        sequence = data.get('sequence', data.get('seq'))
        if sequence is None and item_id:
            # Try to extract sequence from id
            parts = item_id.strip('/').split('/')
            for part in parts:
                if part.startswith('seq-'):
                    try:
                        sequence = int(part.split('-')[1])
                        break
                    except (IndexError, ValueError):
                        pass
        if sequence is None:
            sequence = 1
        
        # Build URLs from the API data
        page_url = data.get('url', '')
        if not page_url and item_id:
            # Construct URL from ID (add slash if item_id doesn't start with one)
            if item_id.startswith('/'):
                page_url = f"https://chroniclingamerica.loc.gov{item_id}"
            else:
                page_url = f"https://chroniclingamerica.loc.gov/{item_id}"
            
        # PDF URL construction (replace .json with .pdf)
        pdf_url = None
        if page_url and page_url.endswith('.json'):
            pdf_url = page_url.replace('.json', '.pdf')
        elif page_url and not page_url.endswith('/'):
            pdf_url = page_url + '.pdf'
            
        # JP2 URL construction
        jp2_url = None
        if page_url and page_url.endswith('.json'):
            jp2_url = page_url.replace('.json', '.jp2')
        elif page_url and not page_url.endswith('/'):
            jp2_url = page_url + '.jp2'
        
        return cls(
            item_id=item_id,
            lccn=data.get('lccn', ''),
            title=data.get('title', ''),
            date=formatted_date,
            edition=edition,
            sequence=sequence,
            page_url=page_url,
            pdf_url=pdf_url,
            jp2_url=jp2_url,
            ocr_text=data.get('ocr_eng'),  # Real API field name
            word_count=None
        )
    
    @staticmethod
    def _format_date(date_str: str) -> str:
        """Format date from YYYYMMDD to YYYY-MM-DD."""
        if len(date_str) == 8 and date_str.isdigit():
            # YYYYMMDD format
            return f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
        elif len(date_str) == 10 and date_str.count('-') == 2:
            # Already in YYYY-MM-DD format
            return date_str
        else:
            # Return as-is for other formats
            return date_str


class NewsDataProcessor:
    """Processes and validates news archive data."""
    
    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self._seen_items: Set[str] = set()
    
    def process_newspapers_response(self, response: Dict) -> List[NewspaperInfo]:
        """Process newspapers API response into structured data."""
        newspapers = []
        
        for newspaper_data in response.get('newspapers', []):
            try:
                newspaper = NewspaperInfo.from_api_response(newspaper_data)
                newspapers.append(newspaper)
            except Exception as e:
                self.logger.warning(f"Failed to process newspaper data: {e}")
                continue
                
        return newspapers
    
    def process_search_response(self, response: Dict, deduplicate: bool = True) -> List[PageInfo]:
        """Process search results into structured page data with deduplication."""
        pages = []
        
        # The LOC API returns items under 'items' key, not 'results'
        for item in response.get('items', []):
            try:
                page = PageInfo.from_search_result(item)
                
                if deduplicate:
                    if page.item_id in self._seen_items:
                        continue
                    self._seen_items.add(page.item_id)
                
                pages.append(page)
            except Exception as e:
                self.logger.warning(f"Failed to process page data: {e}")
                continue
                
        return pages
    
    def reset_deduplication(self):
        """Reset the deduplication cache."""
        self._seen_items.clear()
    
    def process_page_details(self, page_details: Dict, page_url: str = '') -> Optional[PageInfo]:
        """Process page details from direct page endpoint into PageInfo object."""
        try:
            # Extract information from the page details structure
            sequence = page_details.get('sequence', 1)
            
            # Extract title info
            title_info = page_details.get('title', {})
            title = title_info.get('name', 'Unknown Title')
            title_url = title_info.get('url', '')
            
            # Extract LCCN from title URL (format: https://chroniclingamerica.loc.gov/lccn/sn83045201.json)
            lccn = ''
            if title_url:
                lccn_match = title_url.split('/lccn/')
                if len(lccn_match) > 1:
                    lccn = lccn_match[1].replace('.json', '')
            
            # Extract date from issue info
            issue_info = page_details.get('issue', {})
            date = issue_info.get('date_issued', '')
            
            # Extract edition from page URL or use default
            edition = 1
            if page_url:
                parts = page_url.strip('/').split('/')
                for part in parts:
                    if part.startswith('ed-'):
                        try:
                            edition = int(part.split('-')[1])
                            break
                        except (IndexError, ValueError):
                            pass
            
            # Create item_id from the page URL path
            item_id = ''
            if page_url:
                # Extract path from full URL
                if page_url.startswith('https://chroniclingamerica.loc.gov/'):
                    item_id = page_url.replace('https://chroniclingamerica.loc.gov/', '').replace('.json', '')
                else:
                    item_id = page_url.replace('.json', '')
            
            # Create PageInfo object with all available URLs
            page_info = PageInfo(
                item_id=item_id,
                lccn=lccn,
                title=title,
                date=date,
                edition=edition,
                sequence=sequence,
                page_url=page_url.replace('.json', '') if page_url else '',
                pdf_url=page_details.get('pdf', ''),
                jp2_url=page_details.get('jp2', ''),
                ocr_text=page_details.get('text', ''),  # OCR text URL
                word_count=None  # Not available in page details
            )
            
            return page_info
            
        except Exception as e:
            self.logger.error(f"Failed to process page details: {e}")
            return None
    
    def process_page_from_issue(self, page_data: Dict, issue_details: Dict) -> Optional[PageInfo]:
        """Process page data from issue without fetching individual page details (much faster)."""
        try:
            page_url = page_data.get('url', '')
            sequence = page_data.get('sequence', 1)
            
            if not page_url:
                return None
            
            # Extract information from issue details (avoid extra API call)
            title_info = issue_details.get('title', {})
            title = title_info.get('name', 'Unknown Title')
            date = issue_details.get('date_issued', '')
            
            # Extract LCCN from issue URL or title
            lccn = ''
            issue_url = issue_details.get('url', '')
            if issue_url and '/lccn/' in issue_url:
                parts = issue_url.split('/lccn/')
                if len(parts) > 1:
                    lccn = parts[1].split('/')[0]
            
            # Extract edition from page URL
            edition = 1
            if page_url:
                parts = page_url.strip('/').split('/')
                for part in parts:
                    if part.startswith('ed-'):
                        try:
                            edition = int(part.split('-')[1])
                            break
                        except (IndexError, ValueError):
                            pass
            
            # Create item_id from page URL
            item_id = ''
            if page_url:
                if page_url.startswith('https://chroniclingamerica.loc.gov/'):
                    item_id = page_url.replace('https://chroniclingamerica.loc.gov/', '').replace('.json', '')
                else:
                    item_id = page_url.replace('.json', '')
            
            # Construct URLs from the base page URL (without fetching page details)
            base_url = page_url.replace('.json', '')
            
            # Create PageInfo object with constructed URLs
            page_info = PageInfo(
                item_id=item_id,
                lccn=lccn,
                title=title,
                date=date,
                edition=edition,
                sequence=sequence,
                page_url=base_url,
                pdf_url=f"{base_url}.pdf",
                jp2_url=f"{base_url}.jp2", 
                ocr_text=f"{base_url}/ocr.txt",
                word_count=None  # Not available without fetching page details
            )
            
            return page_info
            
        except Exception as e:
            self.logger.error(f"Failed to process page from issue: {e}")
            return None
    
    def estimate_pages_from_batch_issue(self, issue_data: Dict, typical_pages_per_issue: int = 8) -> List[PageInfo]:
        """
        Estimate pages for an issue from batch data without fetching issue details.
        This is much faster but less accurate than fetching actual issue details.
        """
        try:
            issue_url = issue_data.get('url', '')
            date = issue_data.get('date_issued', '')
            title_info = issue_data.get('title', {})
            title = title_info.get('name', 'Unknown Title')
            
            # Extract LCCN from title URL or issue URL
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
            
            # Extract edition from issue URL
            edition = 1
            if issue_url:
                parts = issue_url.strip('/').split('/')
                for part in parts:
                    if part.startswith('ed-'):
                        try:
                            edition = int(part.split('-')[1])
                            break
                        except (IndexError, ValueError):
                            pass
            
            # Estimate pages (typically newspapers have 4-12 pages per issue)
            estimated_pages = []
            base_url = issue_url.replace('.json', '') if issue_url else ''
            
            for seq in range(1, typical_pages_per_issue + 1):
                # Construct page URL from issue URL
                page_url = f"{base_url}/seq-{seq}"
                item_id = page_url.replace('https://chroniclingamerica.loc.gov/', '') if page_url.startswith('https://') else page_url
                
                page_info = PageInfo(
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
                    word_count=None
                )
                estimated_pages.append(page_info)
            
            return estimated_pages
            
        except Exception as e:
            self.logger.error(f"Failed to estimate pages from batch issue: {e}")
            return []
    
    def filter_newspapers_by_criteria(self, newspapers: List[NewspaperInfo], 
                                    state: Optional[str] = None,
                                    language: Optional[str] = None,
                                    start_year: Optional[int] = None,
                                    end_year: Optional[int] = None) -> List[NewspaperInfo]:
        """Filter newspapers by various criteria."""
        filtered = newspapers
        
        if state:
            filtered = [n for n in filtered if any(state.lower() in place.lower() 
                       for place in n.place_of_publication)]
        
        if language:
            filtered = [n for n in filtered if any(language.lower() in lang.lower() 
                       for lang in n.language)]
        
        if start_year:
            # Include newspapers that end after start_year (overlap logic)
            filtered = [n for n in filtered if n.end_year and n.end_year >= start_year]
        
        if end_year:
            # Include newspapers that start before end_year (overlap logic)
            filtered = [n for n in filtered if n.start_year and n.start_year <= end_year]
        
        return filtered
    
    def get_newspaper_summary(self, newspapers: List[NewspaperInfo]) -> Dict:
        """Generate summary statistics for a list of newspapers."""
        if not newspapers:
            return {'total_newspapers': 0}
        
        states = {}
        languages = {}
        year_range = []
        
        for newspaper in newspapers:
            # Count states
            for place in newspaper.place_of_publication:
                state = place.split(',')[-1].strip() if ',' in place else place
                states[state] = states.get(state, 0) + 1
            
            # Count languages
            for lang in newspaper.language:
                languages[lang] = languages.get(lang, 0) + 1
            
            # Collect years
            if newspaper.start_year:
                year_range.append(newspaper.start_year)
            if newspaper.end_year:
                year_range.append(newspaper.end_year)
        
        return {
            'total_newspapers': len(newspapers),
            'states': dict(sorted(states.items(), key=lambda x: x[1], reverse=True)[:10]),
            'languages': dict(sorted(languages.items(), key=lambda x: x[1], reverse=True)[:10]),
            'year_range': (min(year_range), max(year_range)) if year_range else None,
            'sample_titles': [n.title for n in newspapers[:5]]
        }
    
    def validate_date_range(self, date1: str, date2: str) -> bool:
        """Validate that date range is reasonable for LOC data."""
        try:
            if len(date1) == 4:
                date1 += '-01-01'
            if len(date2) == 4:
                date2 += '-12-31'
                
            start = datetime.strptime(date1, '%Y-%m-%d')
            end = datetime.strptime(date2, '%Y-%m-%d')
            
            # LOC data ranges from 1836 to current year
            min_date = datetime(1836, 1, 1)
            max_date = datetime.now()
            
            return (start >= min_date and end <= max_date and start <= end)
            
        except ValueError:
            return False