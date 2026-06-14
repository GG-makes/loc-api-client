#!/usr/bin/env python3
"""
Monitor Batch Discovery Progress

This script monitors and displays real-time progress of batch discovery operations,
showing detailed information about each batch being processed.
"""

import sys
import time
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional
import argparse

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / 'src'))

from newsagger.storage import NewsStorage
from rich.console import Console
from rich.table import Table
from rich.live import Live
from rich.panel import Panel
from rich.layout import Layout
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeRemainingColumn
from rich.text import Text


class BatchDiscoveryMonitor:
    """Monitor batch discovery progress in real-time."""
    
    def __init__(self, db_path: str):
        """Initialize the monitor with database path."""
        self.storage = NewsStorage(db_path)
        self.console = Console()
        
    def get_batch_sessions(self) -> List[Dict]:
        """Get all batch discovery sessions."""
        with sqlite3.connect(self.storage.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            cursor.execute("""
                SELECT * FROM batch_discovery_sessions
                ORDER BY started_at DESC
            """)
            
            sessions = [dict(row) for row in cursor.fetchall()]
        return sessions
    
    def get_session_details(self, session_name: str) -> Optional[Dict]:
        """Get detailed information about a specific session."""

        with sqlite3.connect(self.storage.db_path) as conn: 
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            cursor.execute("""
                SELECT * FROM batch_discovery_sessions
                WHERE session_name = ?
            """, (session_name,))
            
            row = cursor.fetchone()
        return dict(row) if row else None
    
    def create_session_table(self, sessions: List[Dict]) -> Table:
        """Create a table showing all batch discovery sessions."""
        table = Table(title="Batch Discovery Sessions", expand=True)
        
        table.add_column("Session", style="cyan", no_wrap=True)
        table.add_column("Status", style="yellow")
        table.add_column("Progress", justify="right")
        table.add_column("Batches", justify="right")
        table.add_column("Pages", justify="right", style="green")
        table.add_column("Duration", justify="right")
        table.add_column("Rate", justify="right", style="blue")
        
        for session in sessions:
            # Calculate progress
            total_batches = session.get('total_batches', 0)
            current_batch = session.get('current_batch_index', 0)
            progress_pct = (current_batch / total_batches * 100) if total_batches > 0 else 0
            
            # Calculate duration
            started_at = datetime.fromisoformat(session['started_at'])
            updated_at = datetime.fromisoformat(session['updated_at'])
            duration = updated_at - started_at
            
            # Calculate rate
            total_pages = session.get('total_pages_discovered', 0)
            if duration.total_seconds() > 0:
                pages_per_hour = int(total_pages / (duration.total_seconds() / 3600))
                rate = f"{pages_per_hour:,}/hr"
            else:
                rate = "N/A"
            
            # Status with color
            status = session['status']
            if status == 'active':
                status_text = Text(status, style="green bold")
            elif status == 'captcha_blocked':
                status_text = Text(status, style="red bold")
            elif status == 'completed':
                status_text = Text(status, style="blue")
            else:
                status_text = Text(status, style="yellow")
            
            table.add_row(
                session['session_name'],
                status_text,
                f"{progress_pct:.1f}%",
                f"{current_batch:,}/{total_batches:,}",
                f"{total_pages:,}",
                str(duration).split('.')[0],
                rate
            )
        
        return table
    
    def create_active_session_panel(self, session: Optional[Dict]) -> Panel:
        """Create a panel showing details of the active session."""
        if not session:
            return Panel("No active batch discovery session", title="Active Session")
        
        # Build content
        lines = []
        
        # Basic info
        lines.append(f"[cyan]Session:[/cyan] {session['session_name']}")
        lines.append(f"[cyan]Status:[/cyan] {session['status']}")
        lines.append("")
        
        # Current batch info
        if session.get('current_batch_name'):
            lines.append(f"[yellow]Current Batch:[/yellow] {session['current_batch_name']}")
            
            # Issue progress within batch
            total_issues = session.get('total_issues_in_batch', 0)
            current_issue = session.get('current_issue_index', 0)
            if total_issues > 0:
                issue_pct = (current_issue / total_issues * 100)
                lines.append(f"[yellow]Issue Progress:[/yellow] {current_issue}/{total_issues} ({issue_pct:.1f}%)")
        
        lines.append("")
        
        # Overall progress
        total_batches = session.get('total_batches', 0)
        current_batch = session.get('current_batch_index', 0)
        if total_batches > 0:
            batch_pct = (current_batch / total_batches * 100)
            lines.append(f"[green]Batch Progress:[/green] {current_batch}/{total_batches} ({batch_pct:.1f}%)")
        
        # Pages discovered
        pages = session.get('total_pages_discovered', 0)
        enqueued = session.get('total_pages_enqueued', 0)
        lines.append(f"[green]Pages Discovered:[/green] {pages:,}")
        if session.get('auto_enqueue'):
            lines.append(f"[green]Pages Enqueued:[/green] {enqueued:,}")
        
        # Timing
        lines.append("")
        started_at = datetime.fromisoformat(session['started_at'])
        updated_at = datetime.fromisoformat(session['updated_at'])
        duration = updated_at - started_at
        lines.append(f"[blue]Started:[/blue] {started_at.strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"[blue]Duration:[/blue] {str(duration).split('.')[0]}")
        
        # Rate calculation
        if duration.total_seconds() > 0 and pages > 0:
            pages_per_minute = pages / (duration.total_seconds() / 60)
            lines.append(f"[blue]Rate:[/blue] {pages_per_minute:.1f} pages/min")
            
            # ETA calculation
            if current_batch < total_batches:
                avg_pages_per_batch = pages / current_batch if current_batch > 0 else 0
                remaining_batches = total_batches - current_batch
                estimated_remaining_pages = remaining_batches * avg_pages_per_batch
                eta_seconds = estimated_remaining_pages / (pages_per_minute / 60)
                eta = datetime.now() + timedelta(seconds=eta_seconds)
                lines.append(f"[blue]ETA:[/blue] {eta.strftime('%Y-%m-%d %H:%M:%S')}")
        
        # CAPTCHA info if blocked
        if session['status'] == 'captcha_blocked':
            lines.append("")
            lines.append("[red bold]CAPTCHA COOLING-OFF ACTIVE[/red bold]")
            lines.append("Waiting for cooling-off period to complete...")
        
        content = "\n".join(lines)
        return Panel(content, title="Active Batch Discovery Session", expand=True)
    
    def create_batch_progress_table(self, session: Optional[Dict]) -> Table:
        """Create a table showing progress of recent batches."""
        table = Table(title="Recent Batch Progress", expand=True)
        
        table.add_column("Batch", style="cyan", no_wrap=True)
        table.add_column("Pages", justify="right", style="green")
        table.add_column("Issues", justify="right")
        table.add_column("Status", style="yellow")
        table.add_column("Time", justify="right")
        
        if not session:
            return table
        
        # Get batch history from the database
        with sqlite3.connect(self.storage.db_path) as conn:
            conn.row_factory = sqlite3.Row
            
            # This would require tracking individual batch completions in the database
            # For now, show current batch if available
            if session.get('current_batch_name'):
                table.add_row(
                    session['current_batch_name'][:30],
                    str(session.get('batch_pages_discovered', 0)),
                    f"{session.get('current_issue_index', 0)}/{session.get('total_issues_in_batch', 0)}",
                    "Processing",
                    "In Progress"
                )
            
        return table
    
    def monitor(self, refresh_interval: int = 5):
        """Monitor batch discovery progress with live updates."""
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="sessions", size=10),
            Layout(name="active", size=15),
            Layout(name="batches", size=10)
        )
        
        with Live(layout, refresh_per_second=1, console=self.console) as live:
            while True:
                try:
                    # Get all sessions
                    sessions = self.get_batch_sessions()
                    
                    # Find active session
                    active_session = None
                    for session in sessions:
                        if session['status'] in ['active', 'captcha_blocked']:
                            active_session = session
                            break
                    
                    # Update header
                    header_text = Text()
                    header_text.append("Batch Discovery Monitor", style="bold magenta")
                    header_text.append(f"\nLast Update: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                    layout["header"].update(Panel(header_text, expand=True))
                    
                    # Update sessions table
                    layout["sessions"].update(self.create_session_table(sessions))
                    
                    # Update active session details
                    layout["active"].update(self.create_active_session_panel(active_session))
                    
                    # Update batch progress
                    layout["batches"].update(self.create_batch_progress_table(active_session))
                    
                    # Wait before next update
                    time.sleep(refresh_interval)
                    
                except KeyboardInterrupt:
                    break
                except Exception as e:
                    self.console.print(f"[red]Error: {e}[/red]")
                    time.sleep(refresh_interval)
    
    def show_summary(self):
        """Show a one-time summary of batch discovery progress."""
        sessions = self.get_batch_sessions()
        
        if not sessions:
            self.console.print("[yellow]No batch discovery sessions found.[/yellow]")
            return
        
        # Overall statistics
        total_pages = sum(s.get('total_pages_discovered', 0) for s in sessions)
        total_enqueued = sum(s.get('total_pages_enqueued', 0) for s in sessions)
        active_sessions = [s for s in sessions if s['status'] == 'active']
        completed_sessions = [s for s in sessions if s['status'] == 'completed']
        
        # Create summary panel
        summary_lines = [
            f"[cyan]Total Sessions:[/cyan] {len(sessions)}",
            f"[green]Active:[/green] {len(active_sessions)}",
            f"[blue]Completed:[/blue] {len(completed_sessions)}",
            "",
            f"[cyan]Total Pages Discovered:[/cyan] {total_pages:,}",
            f"[cyan]Total Pages Enqueued:[/cyan] {total_enqueued:,}",
        ]
        
        self.console.print(Panel("\n".join(summary_lines), title="Batch Discovery Summary", expand=False))
        
        # Show sessions table
        self.console.print()
        self.console.print(self.create_session_table(sessions))
        
        # Show active session details if any
        if active_sessions:
            self.console.print()
            self.console.print(self.create_active_session_panel(active_sessions[0]))


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Monitor batch discovery progress")
    parser.add_argument(
        '--db-path',
        type=str,
        default='news_archive.db',
        help='Path to the database file (default: news_archive.db)'
    )
    parser.add_argument(
        '--interval',
        type=int,
        default=5,
        help='Refresh interval in seconds (default: 5)'
    )
    parser.add_argument(
        '--summary',
        action='store_true',
        help='Show one-time summary instead of live monitoring'
    )
    
    args = parser.parse_args()
    
    # Check if database exists
    if not Path(args.db_path).exists():
        print(f"Error: Database file '{args.db_path}' not found.")
        sys.exit(1)
    
    # Create monitor
    monitor = BatchDiscoveryMonitor(args.db_path)
    
    if args.summary:
        monitor.show_summary()
    else:
        print("Starting batch discovery monitor... Press Ctrl+C to exit.")
        monitor.monitor(refresh_interval=args.interval)


if __name__ == '__main__':
    main()