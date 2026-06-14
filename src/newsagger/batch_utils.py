"""
Batch Utilities

Utility functions for mapping between batches, LCCNs, issues, and downloads.
Provides consistent batch analysis across all audit tools.
"""

import sqlite3
import time
from datetime import datetime
from typing import Dict, List, Set, Tuple, Optional
from pathlib import Path

from .rate_limited_client import LocApiClient


class BatchMapper:
    """Maps between batches, LCCNs, issues, and download status."""
    
    def __init__(self, storage, api_client: LocApiClient = None):
        """Initialize batch mapper."""
        self.storage = storage
        self.api_client = api_client or LocApiClient()
        self._batch_cache = {}
        self._lccn_to_batch_cache = {}
    
    def get_batch_metadata(self, batch_name: str) -> Optional[Dict]:
        """Get batch metadata from API with caching."""
        if batch_name in self._batch_cache:
            return self._batch_cache[batch_name]
        
        try:
            endpoint = f"batches/{batch_name}.json"
            batch_data = self.api_client._make_request(endpoint)
            
            metadata = {
                'name': batch_data.get('name', batch_name),
                'page_count': batch_data.get('page_count', 0),
                'issues': batch_data.get('issues', []),
                'issue_count': len(batch_data.get('issues', [])),
                'url': batch_data.get('url', ''),
                'created': batch_data.get('created', ''),
                'ingested': batch_data.get('ingested', ''),
                'lccns': self._extract_lccns_from_batch(batch_data)
            }
            
            self._batch_cache[batch_name] = metadata
            return metadata
            
        except Exception as e:
            return {'error': str(e), 'name': batch_name}
    
    def _extract_lccns_from_batch(self, batch_data: Dict) -> Set[str]:
        """Extract unique LCCNs from batch issues."""
        lccns = set()
        for issue in batch_data.get('issues', []):
            url = issue.get('url', '')
            # Extract LCCN from URL like https://chroniclingamerica.loc.gov/lccn/sn12345/1900-01-01/ed-1.json
            if '/lccn/' in url:
                # Find the LCCN part
                lccn_start = url.find('/lccn/') + 6
                lccn_end = url.find('/', lccn_start)
                if lccn_end > lccn_start:
                    lccn = url[lccn_start:lccn_end]
                    if lccn.startswith('sn') and len(lccn) >= 8:  # Basic LCCN format check
                        lccns.add(lccn)
        return lccns
    
    def get_lccn_to_batch_mapping(self, batch_names: List[str]) -> Dict[str, str]:
        """Create mapping from LCCN to batch name."""
        if not self._lccn_to_batch_cache:
            for batch_name in batch_names:
                metadata = self.get_batch_metadata(batch_name)
                if metadata and 'lccns' in metadata:
                    for lccn in metadata['lccns']:
                        self._lccn_to_batch_cache[lccn] = batch_name
        
        return self._lccn_to_batch_cache
    
    def get_batch_discovery_status(self, batch_name: str) -> Dict:
        """Get discovery status for a batch."""
        metadata = self.get_batch_metadata(batch_name)
        if not metadata or 'error' in metadata:
            return metadata
        
        # Get discovered pages for this batch's issues
        discovered_pages = []
        discovered_issues = set()
        
        with sqlite3.connect(self.storage.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            for issue in metadata['issues']:
                issue_url = issue.get('url', '').replace('https://chroniclingamerica.loc.gov', '')
                if not issue_url.endswith('.json'):
                    issue_url += '.json'
                
                # Convert issue URL to page URL pattern for searching
                page_url_base = issue_url.replace('.json', '')
                
                cursor.execute("""
                    SELECT item_id, page_url, downloaded, created_at
                    FROM pages
                    WHERE page_url LIKE ?
                    ORDER BY sequence
                """, (f"%{page_url_base}%",))
                
                issue_pages = cursor.fetchall()
                
                if issue_pages:
                    discovered_issues.add(issue_url)
                    discovered_pages.extend([dict(page) for page in issue_pages])
            
        
        # Calculate discovery percentages
        expected_issues = metadata['issue_count']
        expected_pages = metadata['page_count']
        found_issues = len(discovered_issues)
        found_pages = len(discovered_pages)
        
        return {
            'batch_name': batch_name,
            'expected_issues': expected_issues,
            'expected_pages': expected_pages,
            'discovered_issues': found_issues,
            'discovered_pages': found_pages,
            'discovery_issue_pct': (found_issues / expected_issues * 100) if expected_issues > 0 else 0,
            'discovery_page_pct': (found_pages / expected_pages * 100) if expected_pages > 0 else 0,
            'is_discovery_complete': found_pages >= expected_pages * 0.99,
            'pages_data': discovered_pages,
            'lccns': metadata.get('lccns', set())
        }
    
    def get_batch_download_status(self, batch_name: str, downloads_dir: str = "downloads") -> Dict:
        """Get download status for a batch by checking filesystem."""
        discovery_status = self.get_batch_discovery_status(batch_name)
        if 'error' in discovery_status:
            return discovery_status
        
        # Count downloaded pages from database
        discovered_pages = discovery_status['pages_data']
        downloaded_count = sum(1 for page in discovered_pages if page.get('downloaded', False))
        
        # Also check filesystem for additional verification
        downloads_path = Path(downloads_dir)
        filesystem_files = 0
        filesystem_size_mb = 0
        
        if downloads_path.exists():
            # Check each LCCN directory
            for lccn in discovery_status['lccns']:
                lccn_dir = downloads_path / lccn
                if lccn_dir.exists():
                    # Count files in this LCCN directory
                    for file_path in lccn_dir.rglob("*"):
                        if file_path.is_file():
                            filesystem_files += 1
                            filesystem_size_mb += file_path.stat().st_size / (1024 * 1024)
        
        discovered_count = discovery_status['discovered_pages']
        download_pct_of_discovered = (downloaded_count / discovered_count * 100) if discovered_count > 0 else 0
        download_pct_of_expected = (downloaded_count / discovery_status['expected_pages'] * 100) if discovery_status['expected_pages'] > 0 else 0
        
        discovery_status.update({
            'downloaded_pages': downloaded_count,
            'download_pct_of_discovered': download_pct_of_discovered,
            'download_pct_of_expected': download_pct_of_expected,
            'is_download_complete': download_pct_of_discovered >= 99.0,
            'filesystem_files': filesystem_files,
            'filesystem_size_mb': filesystem_size_mb
        })
        
        return discovery_status
    
    def get_session_batches(self) -> List[Dict]:
        """Get all batches from discovery sessions."""
        with sqlite3.connect(self.storage.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            cursor.execute("""
                SELECT DISTINCT 
                    current_batch_name,
                    session_name,
                    status,
                    current_batch_index,
                    total_batches,
                    updated_at
                FROM batch_discovery_sessions
                WHERE current_batch_name IS NOT NULL
                ORDER BY updated_at DESC
            """)
            
            return [dict(row) for row in cursor.fetchall()]
    
    def get_all_session_batch_names(self) -> List[str]:
        """Get unique batch names from all sessions."""
        session_batches = self.get_session_batches()
        session_batch_names = set(row['current_batch_name'] for row in session_batches if row['current_batch_name'])
        
        # Also try to infer completed batches from the pages table
        # by looking at creation time patterns and extracting batch info
        inferred_batches = self._infer_batches_from_pages()
        
        # Combine session batches with inferred batches
        all_batches = session_batch_names.union(set(inferred_batches))
        return list(all_batches)
    
    def _infer_batches_from_pages(self) -> List[str]:
        """Infer batch names by looking at LCCNs in the pages table and matching against known batches."""
        # Get unique LCCNs from pages
        with sqlite3.connect(self.storage.db_path) as conn:
            cursor = conn.cursor()
            
            cursor.execute("""
                SELECT DISTINCT 
                    substr(page_url, 
                        instr(page_url, '/lccn/') + 6, 
                        instr(substr(page_url, instr(page_url, '/lccn/') + 6), '/') - 1
                    ) as lccn,
                    COUNT(*) as page_count,
                    MIN(created_at) as first_seen,
                    MAX(created_at) as last_seen
                FROM pages
                WHERE page_url LIKE '%/lccn/%'
                GROUP BY lccn
                HAVING page_count > 10
                ORDER BY first_seen DESC
            """)
            
            page_lccns = [row[0] for row in cursor.fetchall() if row[0]]
            
        if not page_lccns:
            return []
        
        # Get recent batches from API and see which ones contain our LCCNs
        try:
            all_batches = list(self.api_client.get_all_batches())
            # Take recent batches (last 50)
            recent_batches = all_batches[-50:] if len(all_batches) > 50 else all_batches
            
            matching_batches = []
            for batch in recent_batches:
                batch_name = batch.get('name', '')
                if batch_name:
                    # Check if this batch contains any of our LCCNs
                    metadata = self.get_batch_metadata(batch_name)
                    if metadata and 'lccns' in metadata:
                        # If any LCCN from this batch appears in our pages, include the batch
                        if metadata['lccns'].intersection(set(page_lccns)):
                            matching_batches.append(batch_name)
            
            return matching_batches
            
        except Exception as e:
            # If API fails, return empty list
            return []
    
    def analyze_all_session_batches(self, downloads_dir: str = "downloads") -> Dict[str, Dict]:
        """Analyze discovery and download status for all session batches."""
        batch_names = self.get_all_session_batch_names()
        results = {}
        
        for batch_name in batch_names:
            results[batch_name] = self.get_batch_download_status(batch_name, downloads_dir)
            time.sleep(0.1)  # Rate limiting
        
        return results
    
    def get_download_summary(self, downloads_dir: str = "downloads") -> Dict:
        """Get overall download summary across all LCCNs."""
        downloads_path = Path(downloads_dir)
        
        if not downloads_path.exists():
            return {
                'total_files': 0,
                'total_size_mb': 0,
                'lccn_count': 0,
                'lccns': [],
                'lccn_details': {}
            }
        
        total_files = 0
        total_size_mb = 0
        lccns = []
        lccn_details = {}
        
        # Check each subdirectory (should be LCCNs)
        for item in downloads_path.iterdir():
            if item.is_dir():
                lccn_name = item.name
                lccns.append(lccn_name)
                lccn_files = 0
                lccn_size = 0
                
                for file_path in item.rglob("*"):
                    if file_path.is_file():
                        total_files += 1
                        lccn_files += 1
                        size = file_path.stat().st_size / (1024 * 1024)
                        total_size_mb += size
                        lccn_size += size
                
                lccn_details[lccn_name] = {
                    'files': lccn_files,
                    'size_mb': lccn_size
                }
        
        return {
            'total_files': total_files,
            'total_size_mb': total_size_mb,
            'lccn_count': len(lccns),
            'lccns': sorted(lccns),
            'lccn_details': lccn_details
        }


class BatchSessionTracker:
    """Track batch discovery session progress."""
    
    def __init__(self, storage):
        """Initialize session tracker."""
        self.storage = storage
    
    def get_active_sessions(self) -> List[Dict]:
        """Get currently active batch discovery sessions."""

        with sqlite3.connect(self.storage.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            cursor.execute("""
                SELECT * FROM batch_discovery_sessions
                WHERE status IN ('active', 'captcha_blocked')
                ORDER BY updated_at DESC
            """)
            
            return [dict(row) for row in cursor.fetchall()]
    
    def get_session_progress(self, session_name: str) -> Optional[Dict]:
        """Get detailed progress for a specific session."""

        with sqlite3.connect(self.storage.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            cursor.execute("""
                SELECT * FROM batch_discovery_sessions
                WHERE session_name = ?
            """, (session_name,))
            
            session = cursor.fetchone()
        
        if not session:
            return None
        
        session_dict = dict(session)
        
        # Calculate additional metrics
        started_at = datetime.fromisoformat(session_dict['started_at'])
        updated_at = datetime.fromisoformat(session_dict['updated_at'])
        duration = updated_at - started_at
        
        session_dict['duration_str'] = str(duration).split('.')[0]
        session_dict['duration_seconds'] = duration.total_seconds()
        
        # Calculate rates
        pages = session_dict.get('total_pages_discovered', 0)
        if duration.total_seconds() > 0 and pages > 0:
            session_dict['pages_per_hour'] = int(pages / (duration.total_seconds() / 3600))
            session_dict['pages_per_minute'] = pages / (duration.total_seconds() / 60)
        else:
            session_dict['pages_per_hour'] = 0
            session_dict['pages_per_minute'] = 0
        
        return session_dict