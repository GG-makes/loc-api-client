#!/usr/bin/env python3
"""
Find Active Database

Helps locate the database file being used by your active batch discovery process.
"""

import sys
from pathlib import Path
import sqlite3
from datetime import datetime

def check_database(db_path: Path) -> dict:
    """Check a database file for batch discovery activity."""
    if not db_path.exists():
        return {'exists': False}
    
    try:
        with sqlite3.connect(self.db_path, timeout=timeout) as conn:

            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            # Check if it has the batch discovery tables
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='batch_discovery_sessions'")
            has_sessions = cursor.fetchone() is not None
            
            if not has_sessions:
                conn.close()
                return {'exists': True, 'has_batch_tables': False}
            
            # Get session info
            cursor.execute("SELECT COUNT(*) as count FROM batch_discovery_sessions")
            session_count = cursor.fetchone()[0]
            
            # Get page count
            cursor.execute("SELECT COUNT(*) as count FROM pages")
            page_count = cursor.fetchone()[0]
            
            # Get recent activity
            cursor.execute("""
                SELECT MAX(updated_at) as last_update 
                FROM batch_discovery_sessions
            """)
            last_update = cursor.fetchone()[0]
            
            # Get active sessions
            cursor.execute("""
                SELECT session_name, current_batch_name, status, total_pages_discovered
                FROM batch_discovery_sessions
                WHERE status IN ('active', 'captcha_blocked')
            """)
            active_sessions = [dict(row) for row in cursor.fetchall()]
                
        return {
            'exists': True,
            'has_batch_tables': True,
            'session_count': session_count,
            'page_count': page_count,
            'last_update': last_update,
            'active_sessions': active_sessions,
            'size_mb': db_path.stat().st_size / (1024 * 1024)
        }
        
    except Exception as e:
        return {'exists': True, 'error': str(e)}

def main():
    """Find the active database."""
    print("🔍 Searching for database files...")
    
    # Common locations to check
    locations_to_check = [
        Path("data/newsagger.db"),
        Path("newsagger.db"),
        Path("../newsagger.db"),
        Path("loc/data/newsagger.db"),
        Path("loc/newsagger.db"),
        Path.home() / "loc" / "data" / "newsagger.db",
        Path.home() / "loc" / "newsagger.db"
    ]
    
    # Also search current directory and subdirectories
    current_dir = Path(".")
    for db_file in current_dir.rglob("*.db"):
        if db_file not in locations_to_check:
            locations_to_check.append(db_file)
    
    candidates = []
    
    print(f"\n📁 Checking {len(locations_to_check)} potential database locations...\n")
    
    for db_path in locations_to_check:
        print(f"Checking: {db_path}")
        result = check_database(db_path)
        
        if not result['exists']:
            print("  ❌ File does not exist")
        elif 'error' in result:
            print(f"  ⚠️  Error: {result['error']}")
        elif not result['has_batch_tables']:
            print("  ⚠️  No batch discovery tables")
        else:
            print(f"  ✅ Valid database:")
            print(f"     📊 Sessions: {result['session_count']}")
            print(f"     📄 Pages: {result['page_count']:,}")
            print(f"     💾 Size: {result['size_mb']:.1f} MB")
            print(f"     🕐 Last update: {result['last_update']}")
            
            if result['active_sessions']:
                print(f"     🔄 Active sessions: {len(result['active_sessions'])}")
                for session in result['active_sessions']:
                    pages = session['total_pages_discovered']
                    print(f"        - {session['session_name']}: {session['current_batch_name']} ({pages:,} pages)")
                
                candidates.append((db_path, result))
            else:
                print("     💤 No active sessions")
        
        print()
    
    # Summary
    if candidates:
        print("🎯 ACTIVE DATABASE CANDIDATES:")
        print("=" * 50)
        
        for db_path, result in candidates:
            total_pages = sum(s['total_pages_discovered'] for s in result['active_sessions'])
            print(f"📍 {db_path}")
            print(f"   Total pages discovered: {total_pages:,}")
            print(f"   Active sessions: {len(result['active_sessions'])}")
            print(f"   Size: {result['size_mb']:.1f} MB")
            print()
        
        if len(candidates) == 1:
            active_db = candidates[0][0]
            print(f"🎯 RECOMMENDED DATABASE: {active_db}")
            print(f"\nTo audit this database, run:")
            print(f"python batch_audit.py --db-path '{active_db}' --downloads-dir downloads")
        else:
            print("Multiple active databases found. Choose the one with the most recent activity.")
    
    else:
        print("❌ No active databases found.")
        print("Make sure your batch discovery process is running and has created a database.")

if __name__ == '__main__':
    main()