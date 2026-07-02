"""
Command Line Interface - Modular Version

Provides interactive CLI for newspaper selection and download management.
"""

import click
import json
from typing import List, Dict
import time
from pathlib import Path
import shutil
from datetime import datetime
import os
import sqlite3
import re
import traceback
from tqdm import tqdm
from unittest.mock import patch

from .config import Config
from .rate_limited_client import LocApiClient, CaptchaHandlingException, GlobalCaptchaManager
from .processor import NewsDataProcessor
from .storage import NewsStorage
from .discovery_manager import DiscoveryManager
from .downloader import DownloadProcessor
from .api_params import LocGovQueryBuilder # From migration

# Import command modules
from .commands.newspaper import newspaper


@click.group()
@click.option('--verbose', '-v', is_flag=True, help='Enable verbose logging')
def cli(verbose):
    """Newsagger - Library of Congress News Archive Aggregator"""
    config = Config()
    if verbose:
        config.log_level = 'DEBUG'
    config.setup_logging()


# Register command groups - turned off until fully enabled. 
cli.add_command(newspaper)


@cli.command()
@click.argument('text')
@click.option('--date1', default='1836', help='Start date')
@click.option('--date2', help='End date')
@click.option('--limit', default=100, help='Max results per facet')
def search_text(text, date1, date2, limit):
    """Search for text across newspaper pages and report match counts.

    Queries the LOC Chronicling America archive for pages containing TEXT
    and prints a total result count. Results are not saved to the database
    and pages are not enqueued for download.

    This is an exploration tool for scoping a query before committing to
    bulk acquisition. For actual downloads, use discover-via-batches with
    --auto-enqueue followed by process-downloads.
        """
    config = Config()
    client = LocApiClient(**config.get_api_config())
    processor = NewsDataProcessor()
    
    builder = config.query_builder_class.from_cli(text=text, date1=date1, date2=date2, rows=limit)

    click.echo(f"🔍 Searching for '{text}' from {date1} to {date2 or 'present'}...")

    total_results = 0
    # TODO: search_text is half-implemented. Current behavior:
    #   - paginate_search walks ALL pages of results (one rate-limited API call
    #     per page) even though only a count is produced; use get_count() instead
    #     for a single-call count
    #   - --limit sets the page size (rows per API call), not a total result cap;
    #     there is no early-exit so --limit 100 still fetches the entire archive
    #   - results are parsed into PageInfo objects via process_search_response()
    #     then immediately discarded — nothing is stored or displayed
    #   - no output beyond a final count; no titles, dates, or OCR snippets shown
    #
    # Full implementation should choose one of two directions:
    #   a) Fast count: replace paginate_search loop with a single get_count() call
    #   b) Exploration tool: fetch first N results, display metadata + OCR snippet
    #      per match, optionally store PageInfo records to DB via storage.store_pages()
    #      so they can be enqueued for process-downloads separately
    #
    # Direction (b) is the primary intended use case and aligns with the
    # enrich_from_detail architecture being built in the migration.
    
    with tqdm(desc="Searching") as pbar:
        for result_batch in client.paginate_search(builder):
            pages = processor.process_search_response(result_batch)
            stored = len(pages)
            total_results += stored
            pbar.update(stored)


    click.echo(f"Found {total_results} results")


# ===== ENHANCED DISCOVERY AND TRACKING COMMANDS =====

@cli.command()
@click.option('--max-papers', default=None, type=int, help='Limit discovery to N newspapers')
@click.option('--states', help='Comma-separated list of states to prioritize')
def discover(max_papers, states):
    """Discover and catalog available periodicals from LOC."""
    config = Config()
    client = LocApiClient(**config.get_api_config())
    processor = NewsDataProcessor()
    storage = NewsStorage(**config.get_storage_config())
    discovery = DiscoveryManager(client, processor, storage, 
                                 **config.get_querybuilder_config())
    
    click.echo("🔍 Starting periodical discovery...")
    
    try:
        discovered_count = discovery.discover_all_periodicals(max_newspapers=max_papers)
        click.echo(f"✅ Discovered {discovered_count} periodicals")
        
        # Show summary
        periodicals = storage.get_periodicals()
        if periodicals:
            state_counts = {}
            for p in periodicals:
                state = p.get('state', 'Unknown')
                state_counts[state] = state_counts.get(state, 0) + 1
            
            click.echo(f"\n📊 Periodicals by state:")
            for state, count in sorted(state_counts.items(), key=lambda x: x[1], reverse=True)[:10]:
                click.echo(f"   {state}: {count}")
        
        # Create facets if states specified
        if states:
            priority_states = [s.strip() for s in states.split(',')]
            click.echo(f"\n🗺️ Creating facets for priority states: {', '.join(priority_states)}")
            facet_ids = discovery.create_state_facets(priority_states)
            click.echo(f"✅ Created {len(facet_ids)} state facets")
        
    except Exception as e:
        click.echo(f"❌ Discovery failed: {e}")


@cli.command()
@click.option('--start-year', default=1900, type=int, help='Start year for facets')
@click.option('--end-year', default=1920, type=int, help='End year for facets') 
@click.option('--facet-size', default=1, type=int, help='Years per facet')
@click.option('--estimate-items', is_flag=True, help='Estimate items per facet (makes API calls, may trigger rate limiting)')
@click.option('--rate-limit-delay', default=5.0, type=float, help='Extra delay between API calls (seconds)')
def create_facets(start_year, end_year, facet_size, estimate_items, rate_limit_delay):
    """Create date range facets for systematic downloading.
    
    Progress is saved automatically - you can safely interrupt (Ctrl+C) and resume later.
    Use 'check-facet-progress' to see current status for a date range.
    """
    config = Config()
    client = LocApiClient(**config.get_api_config())
    processor = NewsDataProcessor()
    storage = NewsStorage(**config.get_storage_config())
    discovery = DiscoveryManager(client, processor, storage, 
                                 **config.get_querybuilder_config())
    
    total_facets = len(range(start_year, end_year + 1, facet_size))
    
    if estimate_items:
        click.echo(f"⚠️  WARNING: Will make {total_facets} API calls for estimation")
        click.echo(f"   This may trigger rate limiting (1 hour timeout)")
        click.echo(f"   Using {rate_limit_delay}s delay between calls")
        if not click.confirm("Continue with estimation?"):
            click.echo("Cancelled. Use without --estimate-items to skip estimation.")
            return
    
    # Check for existing facets to enable resumption
    existing_facets = storage.get_search_facets(facet_type='date_range')
    existing_ranges = {f['facet_value'] for f in existing_facets}
    
    # Calculate which facets need to be created
    all_ranges = []
    for year in range(start_year, end_year + 1, facet_size):
        facet_end_year = min(year + facet_size - 1, end_year)
        facet_value = f"{year}/{facet_end_year}" if year != facet_end_year else f"{year}/{year}"
        all_ranges.append(facet_value)
    
    missing_ranges = [r for r in all_ranges if r not in existing_ranges]
    
    if existing_ranges:
        click.echo(f"🔄 Found {len(existing_ranges)} existing facets, {len(missing_ranges)} to create")
        if missing_ranges:
            click.echo(f"   Will create: {missing_ranges[0]} to {missing_ranges[-1]}")
        else:
            click.echo("✅ All facets already exist!")
            return
    
    click.echo(f"📅 Creating {len(missing_ranges)} date facets from {start_year} to {end_year} ({facet_size} year(s) each)...")
    click.echo("💡 Tip: Press Ctrl+C to safely interrupt - progress is saved automatically")
    
    try:
        facet_ids = discovery.create_date_range_facets(
            start_year, 
            end_year, 
            facet_size_years=facet_size,
            estimate_items=estimate_items,
            rate_limit_delay=rate_limit_delay if estimate_items else None
        )
        
        click.echo(f"✅ Created {len(facet_ids)} new date range facets")
        if len(facet_ids) > 0:
            click.echo(f"📊 Total facets now: {len(existing_ranges) + len(facet_ids)}")
        else:
            click.echo("📊 No new facets created (all already existed)")
            
    except KeyboardInterrupt:
        click.echo(f"\n⚠️  Interrupted by user")
        # Check how many were created before interruption
        current_facets = storage.get_search_facets(facet_type='date_range')
        current_ranges = {f['facet_value'] for f in current_facets}
        newly_created = len(current_ranges) - len(existing_ranges)
        
        if newly_created > 0:
            click.echo(f"✅ Saved {newly_created} facets before interruption")
            click.echo(f"📊 Progress: {len(current_ranges)}/{len(all_ranges)} facets created")
            click.echo("🔄 Run the same command again to resume from where you left off")
        else:
            click.echo("❌ No facets were created before interruption")
        return
        
    except Exception as e:
        click.echo(f"❌ Facet creation failed: {e}")
        return
    
    # Show sample of created facets
    if len(facet_ids) > 0:
        facets = storage.get_search_facets(facet_type='date_range')
        recent_facets = [f for f in facets if f['id'] in facet_ids]
        
        click.echo(f"\n📋 Sample of newly created facets:")
        for facet in recent_facets[:5]:  # Show first 5
            if estimate_items:
                click.echo(f"   📅 {facet['facet_value']}: ~{facet['estimated_items']:,} items")
            else:
                click.echo(f"   📅 {facet['facet_value']}: (estimation skipped)")
        
        if len(recent_facets) > 5:
            click.echo(f"   ... and {len(recent_facets)-5} more")
        
        if not estimate_items:
            click.echo(f"\n💡 Tip: Use 'newsagger estimate-facets' to get item estimates later")


@cli.command()
@click.option('--start-year', default=1900, type=int, help='Start year to check progress for')
@click.option('--end-year', default=1920, type=int, help='End year to check progress for')
@click.option('--facet-size', default=1, type=int, help='Years per facet')
def check_facet_progress(start_year, end_year, facet_size):
    """Check progress of facet creation for a date range."""
    config = Config()
    storage = NewsStorage(**config.get_storage_config())
    
    # Calculate expected facets
    all_ranges = []
    for year in range(start_year, end_year + 1, facet_size):
        facet_end_year = min(year + facet_size - 1, end_year)
        facet_value = f"{year}/{facet_end_year}" if year != facet_end_year else f"{year}/{year}"
        all_ranges.append(facet_value)
    
    # Check existing facets
    existing_facets = storage.get_search_facets(facet_type='date_range')
    existing_ranges = {f['facet_value'] for f in existing_facets}
    
    # Calculate progress
    completed_ranges = [r for r in all_ranges if r in existing_ranges]
    missing_ranges = [r for r in all_ranges if r not in existing_ranges]
    
    progress_percent = (len(completed_ranges) / len(all_ranges)) * 100
    
    click.echo(f"📊 Facet Creation Progress ({start_year}-{end_year})")
    click.echo(f"   Total expected: {len(all_ranges)} facets")
    click.echo(f"   Created: {len(completed_ranges)} facets ({progress_percent:.1f}%)")
    click.echo(f"   Missing: {len(missing_ranges)} facets")
    
    if missing_ranges:
        click.echo(f"\n❌ Missing ranges:")
        # Group consecutive ranges for cleaner display
        groups = []
        current_group = [missing_ranges[0]]
        
        for i in range(1, len(missing_ranges)):
            current_year = int(missing_ranges[i].split('/')[0])
            prev_year = int(missing_ranges[i-1].split('/')[0])
            
            if current_year == prev_year + facet_size:
                current_group.append(missing_ranges[i])
            else:
                groups.append(current_group)
                current_group = [missing_ranges[i]]
        groups.append(current_group)
        
        for group in groups:
            if len(group) == 1:
                click.echo(f"   📅 {group[0]}")
            else:
                click.echo(f"   📅 {group[0]} to {group[-1]} ({len(group)} facets)")
        
        click.echo(f"\n🔄 Run 'newsagger create-facets {start_year} {end_year}' to resume")
    else:
        click.echo(f"\n✅ All facets created for {start_year}-{end_year}")


@cli.command()
def status():
    """Show overall progress status of all operations."""
    config = Config()
    storage = NewsStorage(**config.get_storage_config())
    
    click.echo("📊 Newsagger Status Overview")
    
    # Get basic storage stats
    storage_stats = storage.get_storage_stats()
    discovery_stats = storage.get_discovery_stats()
    
    click.echo(f"\n📚 Storage:")
    click.echo(f"   Newspapers: {storage_stats['total_newspapers']:,}")
    click.echo(f"   Pages discovered: {storage_stats['total_pages']:,}")
    click.echo(f"   Pages downloaded: {storage_stats['downloaded_pages']:,}")
    click.echo(f"   Database size: {storage_stats['db_size_mb']} MB")
    
    # Periodicals discovery
    click.echo(f"\n🏛️ Periodicals:")
    click.echo(f"   Total: {discovery_stats['total_periodicals']:,}")
    click.echo(f"   Discovery complete: {discovery_stats['discovered_periodicals']:,}")
    click.echo(f"   Download complete: {discovery_stats['downloaded_periodicals']:,}")
    
    # Facets status
    click.echo(f"\n📅 Search Facets:")
    click.echo(f"   Total: {discovery_stats['total_facets']:,}")
    click.echo(f"   Completed: {discovery_stats['completed_facets']:,}")
    click.echo(f"   Errors: {discovery_stats['error_facets']:,}")
    
    # Estimate accuracy
    if discovery_stats['total_facets'] > 0:
        estimated_items = discovery_stats.get('estimated_items', 0)
        actual_items = discovery_stats.get('actual_items', 0)
        if estimated_items > 0 and actual_items > 0:
            accuracy = (actual_items / estimated_items) * 100
            click.echo(f"   Estimate accuracy: {accuracy:.1f}% ({actual_items:,}/{estimated_items:,})")
    
    # Download queue
    click.echo(f"\n📥 Download Queue:")
    click.echo(f"   Total items: {discovery_stats['total_queue_items']:,}")
    click.echo(f"   Queued: {discovery_stats['queued_items']:,}")
    click.echo(f"   Active: {discovery_stats['active_items']:,}")
    click.echo(f"   Completed: {discovery_stats['completed_queue_items']:,}")
    
    # Quick recommendations
    click.echo(f"\n💡 Quick Actions:")
    
    if discovery_stats['total_facets'] == 0:
        click.echo("   • Run 'newsagger create-facets' to create date facets")
    elif discovery_stats['completed_facets'] == 0:
        click.echo("   • Run 'newsagger auto-discover-facets' to discover content")
    elif discovery_stats['queued_items'] == 0:
        click.echo("   • Run 'newsagger auto-enqueue' to queue content for download")
    elif discovery_stats['queued_items'] > 0:
        click.echo("   • Run 'newsagger process-downloads' to start downloading")
    
    # Show current year range of facets if any exist
    facets = storage.get_search_facets(facet_type='date_range')
    if facets:
        years = []
        for facet in facets:
            start_year = facet['facet_value'].split('/')[0]
            years.append(int(start_year))
        if years:
            min_year, max_year = min(years), max(years)
            click.echo(f"   • Current facet range: {min_year}-{max_year}")
            click.echo(f"   • Use 'newsagger check-facet-progress {min_year} {max_year}' for details")


@cli.command()
@click.option('--facet-type', default='date_range', help='Type of facets to estimate (date_range, state)')
@click.option('--rate-limit-delay', default=8.0, type=float, help='Delay between API calls (seconds)')
@click.option('--max-facets', default=None, type=int, help='Maximum number of facets to estimate')
@click.option('--force-reestimate', is_flag=True, help='Re-estimate facets that already have estimates')
def estimate_facets(facet_type, rate_limit_delay, max_facets, force_reestimate):
    """Estimate item counts for existing facets that don't have estimates."""
    config = Config()
    client = LocApiClient(**config.get_api_config())
    storage = NewsStorage(**config.get_storage_config())
    
    # Get facets without estimates or force re-estimation
    all_facets = storage.get_search_facets(facet_type=facet_type)
    if force_reestimate:
        facets_to_estimate = all_facets
    else:
        facets_to_estimate = [f for f in all_facets if f['estimated_items'] == 0]
    
    if not facets_to_estimate:
        if force_reestimate:
            click.echo(f"✅ No {facet_type} facets found")
        else:
            click.echo(f"✅ All {facet_type} facets already have estimates")
            click.echo("   Use --force-reestimate to update existing estimates")
        return
    
    if max_facets:
        facets_to_estimate = facets_to_estimate[:max_facets]
    
    total_calls = len(facets_to_estimate)
    estimated_time = (total_calls * rate_limit_delay) / 60
    
    if force_reestimate:
        click.echo(f"📊 Re-estimating {total_calls} {facet_type} facets")
    else:
        click.echo(f"📊 Found {total_calls} {facet_type} facets without estimates")
    click.echo(f"⚠️  This will make {total_calls} API calls over ~{estimated_time:.1f} minutes")
    click.echo(f"   Using {rate_limit_delay}s delay between calls to avoid rate limiting")
    
    if not click.confirm("Continue with estimation?"):
        click.echo("Cancelled.")
        return
    
    try:
        with tqdm(total=total_calls, desc="Estimating facets") as pbar:
            for i, facet in enumerate(facets_to_estimate):
                pbar.set_description(f"Estimating {facet['facet_value']}")
                
                try:
                    builder = config.query_builder_class.from_facet(facet)
                    estimated_items = client.get_count(builder)                    
                    # Update the facet with the new estimate
                    # We need to update the estimated_items field in the database directly
                    # since update_facet_discovery doesn't have an estimated_items parameter
                    with sqlite3.connect(storage.db_path) as conn:
                        conn.execute("""
                            UPDATE search_facets 
                            SET estimated_items = ?, updated_at = CURRENT_TIMESTAMP
                            WHERE id = ?
                        """, (estimated_items, facet['id']))
                        conn.commit()
                    
                    pbar.set_postfix(items=f"{estimated_items:,}")
                    
                    # Rate limiting delay
                    if i < total_calls - 1:  # Don't delay after last item
                        time.sleep(rate_limit_delay)
                
                except Exception as e:
                    click.echo(f"\n⚠️ Failed to estimate {facet['facet_value']}: {e}")
                
                pbar.update(1)
        
        click.echo(f"\n✅ Estimation complete for {total_calls} facets")
        
    except Exception as e:
        click.echo(f"❌ Estimation failed: {e}")


@cli.command()
def fix_wildly_inaccurate_estimates():
    """Fix facets with wildly inaccurate estimates (21M+ items) using improved estimation."""
    config = Config()
    client = LocApiClient(**config.get_api_config())
    storage = NewsStorage(**config.get_storage_config())
    
    # Find facets with obviously wrong estimates (anything over 1 million is likely wrong)
    all_facets = storage.get_search_facets()
    bad_estimates = [f for f in all_facets if f['estimated_items'] > 1000000]
    
    if not bad_estimates:
        click.echo("✅ No facets with wildly inaccurate estimates found")
        return
    
    click.echo(f"🔍 Found {len(bad_estimates)} facets with inaccurate estimates (>1M items)")
    click.echo("These likely have the old broken estimate of ~21M items")
    
    # Show examples
    for facet in bad_estimates[:5]:
        click.echo(f"   📅 {facet['facet_value']}: {facet['estimated_items']:,} items")
    if len(bad_estimates) > 5:
        click.echo(f"   ... and {len(bad_estimates)-5} more")
    
    estimated_time = (len(bad_estimates) * 8) / 60  # 8 seconds per estimate
    click.echo(f"\n⚠️  This will make {len(bad_estimates)} API calls over ~{estimated_time:.1f} minutes")
    click.echo("   Using 8s delay between calls to avoid rate limiting")
    
    if not click.confirm("Fix these estimates?"):
        click.echo("Cancelled.")
        return
    
    try:
        
        with tqdm(total=len(bad_estimates), desc="Fixing estimates") as pbar:
            for i, facet in enumerate(bad_estimates):
                pbar.set_description(f"Fixing {facet['facet_value']}")
                
                try:
                    builder = config.query_builder_class.from_facet(facet)
                    new_estimate = client.get_count(builder)
                    
                    # Update the estimate directly
                    with sqlite3.connect(storage.db_path) as conn:
                        conn.execute("""
                            UPDATE search_facets 
                            SET estimated_items = ?, updated_at = CURRENT_TIMESTAMP
                            WHERE id = ?
                        """, (new_estimate, facet['id']))
                        conn.commit()
                    
                    pbar.set_postfix(items=f"{new_estimate:,}")
                    
                    # Rate limiting delay  
                    if i < len(bad_estimates) - 1:
                        time.sleep(8.0)
                
                except Exception as e:
                    click.echo(f"\n⚠️ Failed to fix {facet['facet_value']}: {e}")
                
                pbar.update(1)
        
        click.echo(f"\n✅ Fixed estimates for {len(bad_estimates)} facets")
        click.echo("   Estimates should now be realistic (hundreds to low thousands)")
        
    except Exception as e:
        click.echo(f"❌ Fix failed: {e}")


@cli.command()
@click.option('--priority-states', help='Comma-separated priority states')
@click.option('--priority-dates', help='Comma-separated priority date ranges (e.g., "1906/1906,1929/1929")')
def populate_queue(priority_states, priority_dates):
    """Populate download queue with discovered content."""
    config = Config()
    client = LocApiClient(**config.get_api_config())
    processor = NewsDataProcessor()
    storage = NewsStorage(**config.get_storage_config())
    discovery = DiscoveryManager(client, processor, storage, 
                                **config.get_querybuilder_config())
    
    click.echo("⬇️ Populating download queue...")
    
    try:
        states_list = priority_states.split(',') if priority_states else None
        dates_list = priority_dates.split(',') if priority_dates else None
        
        queue_count = discovery.populate_download_queue(
            priority_states=states_list,
            priority_date_ranges=dates_list
        )
        
        click.echo(f"✅ Added {queue_count} items to download queue")
        
        # Show top queue items
        queue = storage.get_download_queue(status='queued', limit=10)
        if queue:
            click.echo(f"\n📋 Next downloads (top 10):")
            for i, item in enumerate(queue, 1):
                click.echo(f"   {i}. Priority {item['priority']}: {item['queue_type']} {item['reference_id']}")
                click.echo(f"      💾 {item['estimated_size_mb']} MB, ⏱️ {item['estimated_time_hours']:.1f} hours")
        
    except Exception as e:
        click.echo(f"❌ Queue population failed: {e}")


@cli.command()
@click.option('--auto-enqueue', is_flag=True, help='Automatically enqueue discovered content')
@click.option('--batch-size', default=100, help='Items per discovery batch')
@click.option('--max-items', default=None, type=int, help='Maximum items to discover per facet')
@click.option('--skip-errors', is_flag=True, help='Skip facets that encounter errors and continue')
@click.option('--timeout-seconds', default=300, type=int, help='Timeout per facet in seconds')
@click.option('--override-captcha', is_flag=True, help='Override CAPTCHA cooling-off periods (risky)')
def auto_discover_facets(auto_enqueue, batch_size, max_items, skip_errors, timeout_seconds, override_captcha):
    """Systematically discover content for all pending facets.
    
    IMPORTANT: This makes many API calls and may hit rate limits (20 req/min).
    Use smaller --batch-size (20-50) to reduce API calls per facet.
    If rate limited, wait ~1 hour before retrying.
    """
    config = Config()
    client = LocApiClient(**config.get_api_config())
    processor = NewsDataProcessor()
    storage = NewsStorage(**config.get_storage_config())
    discovery = DiscoveryManager(client, processor, storage, 
                                **config.get_querybuilder_config())

    click.echo("🔍 Starting systematic facet discovery...")
    
    # Check global CAPTCHA status first
    global_captcha = GlobalCaptchaManager()
    captcha_status = global_captcha.get_status()
    
    if captcha_status['blocked']:
        click.echo(f"🛑 DISCOVERY BLOCKED: {captcha_status['reason']}")
        click.echo(f"   Consecutive CAPTCHAs: {captcha_status['consecutive_captchas']}")
        click.echo(f"   Cooling-off period: {captcha_status['cooling_off_hours']:.1f} hours")
        
        if override_captcha:
            click.echo(f"\n⚠️  OVERRIDE: Ignoring cooling-off period (this may trigger immediate CAPTCHA)")
            global_captcha.reset_state()
            click.echo("✅ CAPTCHA state reset - proceeding with discovery")
        elif captcha_status['last_captcha_time']:
            last_captcha = time.ctime(captcha_status['last_captcha_time'])
            resume_time = captcha_status['last_captcha_time'] + (captcha_status['cooling_off_hours'] * 3600)
            remaining_minutes = (resume_time - time.time()) / 60
            
            click.echo(f"   Last CAPTCHA: {last_captcha}")
            click.echo(f"   Resume after: {time.ctime(resume_time)}")
            click.echo(f"   Remaining: {remaining_minutes:.1f} minutes")
            
            # Automatically wait (no user prompt required)
            click.echo(f"\n⏳ Automatically waiting for cooling-off period to complete...")
            click.echo(f"   Discovery will start at: {time.ctime(resume_time)}")
            click.echo(f"   Press Ctrl+C to exit early (or use --override-captcha to skip wait)")
            
            try:
                cooling_off_seconds = remaining_minutes * 60
                start_wait = time.time()
                
                with tqdm(desc="Cooling-off", total=int(cooling_off_seconds), unit="s") as wait_pbar:
                    while time.time() < resume_time:
                        current_time = time.time()
                        elapsed = current_time - start_wait
                        remaining = resume_time - current_time
                        
                        wait_pbar.n = int(elapsed)
                        wait_pbar.set_postfix(remaining=f"{remaining/60:.1f}m")
                        wait_pbar.refresh()
                        
                        time.sleep(1)
                        
                        if remaining <= 0:
                            break
                
                # Reset CAPTCHA state and continue
                global_captcha.reset_state()
                click.echo(f"\n✅ Cooling-off completed - starting discovery!")
                
            except KeyboardInterrupt:
                click.echo(f"\n⚠️  Interrupted. Try again later or use: python main.py reset-captcha-state")
                return
        else:
            click.echo("\n💡 Try again later or use: python main.py reset-captcha-state")
            return
    else:
        click.echo(f"✅ Global CAPTCHA status: {captcha_status['reason']}")
    
    # Proactive CAPTCHA interruption detection and fix
    click.echo("🛠️  Checking for incorrectly completed facets...")
    try:
        fix_stats = discovery.fix_incorrectly_completed_facets()
        if fix_stats['facets_fixed'] > 0:
            click.echo(f"✅ Auto-fixed {fix_stats['facets_fixed']} incorrectly completed facets")
            click.echo(f"   These facets will now resume from where they were interrupted")
        else:
            click.echo("✅ No incorrectly completed facets found")
    except Exception as e:
        click.echo(f"⚠️  Error during facet checking: {e}")
        click.echo("   Continuing with discovery...")
    
    # Calculate estimated API calls and warn about rate limiting
    # Include captcha_retry facets that are ready for retry
    facets = storage.get_search_facets(status=['pending', 'discovering', 'captcha_retry'])
    if not facets:
        click.echo("✅ No pending facets found. Create facets first with 'create-facets' command.")
        return
    
    estimated_api_calls = len(facets) * (max_items // batch_size + 1) if max_items else len(facets) * 10
    estimated_minutes = estimated_api_calls / 20  # 20 requests per minute limit
    
    click.echo(f"📋 Found {len(facets)} pending facets to discover")
    click.echo(f"⚠️  Estimated {estimated_api_calls} API calls (~{estimated_minutes:.1f} minutes at 20 req/min)")
    click.echo(f"💡 Using batch size {batch_size} - smaller sizes reduce rate limiting risk")
    
    if estimated_minutes > 30:
        click.echo(f"⚠️  WARNING: This will take >30 minutes and may hit rate limits!")
        click.echo(f"   Consider using --max-items=100 to limit discovery per facet")
        if not click.confirm("Continue anyway?"):
            click.echo("Cancelled")
            return
    
    try:
        
        total_discovered = 0
        total_enqueued = 0
        
        errors_count = 0
        skipped_facets = []
        
        with tqdm(desc="Auto-discovering", total=len(facets)) as main_pbar:
            for i, facet in enumerate(facets):
                main_pbar.set_description(f"Discovering {facet['facet_type']}: {facet['facet_value']}")
                
                # Check if this is a captcha_retry facet that needs time checking
                if facet.get('status') == 'captcha_retry':
                    # Parse retry time from error message (simple approach for now)
                    error_message = facet.get('error_message', '')
                    
                    # Look for "Retry after: <timestamp>" pattern  
                    retry_match = re.search(r'Retry after: (.+?)(?:\.|\n|$)', error_message)
                    if retry_match:
                        try:
                            retry_time_str = retry_match.group(1).strip()
                            retry_time = time.mktime(time.strptime(retry_time_str, '%a %b %d %H:%M:%S %Y'))
                            current_time = time.time()
                            
                            if current_time < retry_time:
                                # Still in cooling-off period
                                remaining_minutes = (retry_time - current_time) / 60
                                click.echo(f"⏳ Skipping facet {facet['id']} ({facet['facet_value']}) - cooling-off period ({remaining_minutes:.1f} minutes remaining)")
                                main_pbar.update(1)
                                continue
                            else:
                                click.echo(f"🔄 Retrying facet {facet['id']} ({facet['facet_value']}) - cooling-off period completed")
                        except Exception as e:
                            click.echo(f"⚠️  Could not parse retry time for facet {facet['id']}, proceeding with retry")
                    else:
                        click.echo(f"🔄 Retrying facet {facet['id']} ({facet['facet_value']}) - no retry time specified")
                
                # Initialize nested progress bar for this facet
                batch_pbar = None
                current_page = 0
                
                def update_batch_progress(progress_info):
                    nonlocal batch_pbar, current_page
                    
                    # Create or update batch progress bar
                    if batch_pbar is None:
                        # Estimate batches based on estimated items (if available)
                        estimated_items = facet.get('estimated_items', 1000)
                        estimated_batches = max(1, estimated_items // batch_size)
                        batch_pbar = tqdm(
                            desc=f"  Batches for {facet['facet_value'][:20]}",
                            total=estimated_batches,
                            leave=False,
                            position=1
                        )
                    
                    # Update progress
                    if progress_info['page'] > current_page:
                        batch_pbar.update(progress_info['page'] - current_page)
                        current_page = progress_info['page']
                    
                    # Update postfix with real-time stats
                    batch_pbar.set_postfix(
                        page=progress_info['page'],
                        items=progress_info['total_discovered']
                    )
                
                try:
                    # Add timeout handling using signal (for Unix systems)
                    import signal
                    
                    def timeout_handler(signum, frame):
                        raise TimeoutError(f"Facet {facet['id']} discovery timed out after {timeout_seconds} seconds")
                    
                    # Set up timeout for this facet
                    if hasattr(signal, 'SIGALRM'):  # Unix systems only
                        signal.signal(signal.SIGALRM, timeout_handler)
                        signal.alarm(timeout_seconds)
                    
                    # Discover content for this facet with progress callback
                    discovered_count = discovery.discover_facet_content(
                        facet['id'], 
                        batch_size=batch_size,
                        max_items=max_items,
                        progress_callback=update_batch_progress
                    )
                    
                    # Cancel the timeout
                    if hasattr(signal, 'SIGALRM'):
                        signal.alarm(0)
                    
                    # Close batch progress bar
                    if batch_pbar:
                        batch_pbar.close()
                    
                    total_discovered += discovered_count
                    
                    # Auto-enqueue if requested
                    if auto_enqueue and discovered_count > 0:
                        enqueued = discovery.enqueue_facet_content(facet['id'])
                        total_enqueued += enqueued
                        main_pbar.set_postfix(discovered=total_discovered, enqueued=total_enqueued, errors=errors_count)
                    else:
                        main_pbar.set_postfix(discovered=total_discovered, errors=errors_count)
                
                except CaptchaHandlingException as e:
                    # Handle global CAPTCHA - automatically wait and resume
                    if hasattr(signal, 'SIGALRM'):
                        signal.alarm(0)
                    if batch_pbar:
                        batch_pbar.close()
                    
                    click.echo(f"\n🛑 GLOBAL CAPTCHA DETECTED - Initiating automatic wait-and-resume")
                    click.echo(f"   Facet {facet['id']} ({facet['facet_value']}) triggered CAPTCHA protection")
                    
                    captcha_status = global_captcha.get_status()
                    cooling_off_seconds = captcha_status['cooling_off_hours'] * 3600
                    
                    click.echo(f"   Global cooling-off: {captcha_status['cooling_off_hours']:.1f} hours")
                    click.echo(f"   Consecutive CAPTCHAs: {captcha_status['consecutive_captchas']}")
                    
                    if captcha_status['last_captcha_time']:
                        import time
                        resume_time = captcha_status['last_captcha_time'] + cooling_off_seconds
                        click.echo(f"   Resume after: {time.ctime(resume_time)}")
                    
                    click.echo(f"\n📊 Progress before CAPTCHA:")
                    click.echo(f"   Facets processed: {i}")
                    click.echo(f"   Items discovered: {total_discovered:,}")
                    if auto_enqueue:
                        click.echo(f"   Items enqueued: {total_enqueued:,}")
                    
                    # Automatically wait and resume (no user prompt required)
                    click.echo(f"\n⏳ Automatically waiting for cooling-off period...")
                    click.echo(f"   Discovery will resume at: {time.ctime(resume_time)}")
                    click.echo(f"   Press Ctrl+C if you want to exit early")
                    
                    try:
                        # Wait with progress updates
                        import time
                        start_wait = time.time()
                        
                        with tqdm(desc="Cooling-off", total=int(cooling_off_seconds), unit="s") as wait_pbar:
                            while time.time() < resume_time:
                                current_time = time.time()
                                elapsed = current_time - start_wait
                                remaining = resume_time - current_time
                                
                                wait_pbar.n = int(elapsed)
                                wait_pbar.set_postfix(remaining=f"{remaining/60:.1f}m")
                                wait_pbar.refresh()
                                
                                time.sleep(1)  # Update every second
                                
                                # Check for user interrupt
                                if remaining <= 0:
                                    break
                        
                        # Reset global CAPTCHA state
                        global_captcha.reset_state()
                        click.echo(f"\n✅ Cooling-off period completed - resuming discovery!")
                        click.echo(f"   Continuing from facet {i + 1} of {len(facets)}")
                        
                        # Continue the discovery loop (don't break)
                        main_pbar.set_description("Resuming after CAPTCHA")
                        continue
                        
                    except KeyboardInterrupt:
                        click.echo(f"\n\n⚠️  Discovery interrupted by user during cooling-off")
                        click.echo(f"   Progress saved. Resume later with: python main.py auto-discover-facets")
                        break
                
                except (TimeoutError, Exception) as e:
                    # Cancel the timeout
                    if hasattr(signal, 'SIGALRM'):
                        signal.alarm(0)
                    
                    # Close batch progress bar if it exists
                    if batch_pbar:
                        batch_pbar.close()
                    
                    errors_count += 1
                    error_msg = str(e)
                    skipped_facets.append(f"{facet['facet_value']}: {error_msg}")
                    
                    # Detailed error logging
                    click.echo(f"\n❌ ERROR on facet {facet['id']} ({facet['facet_type']} = {facet['facet_value']}):")
                    click.echo(f"   Error: {error_msg}")
                    if hasattr(e, '__traceback__'):
                        import traceback
                        click.echo(f"   Traceback: {traceback.format_exc()}")
                    
                    # Check for rate limiting specifically
                    if "Rate limited" in error_msg or "429" in error_msg:
                        click.echo(f"\n🛑 RATE LIMITED by LoC API!")
                        click.echo(f"   The API allows only 20 requests per minute")
                        click.echo(f"   You must wait ~1 hour before trying again")
                        click.echo(f"   Progress saved: {total_discovered:,} items discovered so far")
                        click.echo(f"\n💡 Next time, try:")
                        click.echo(f"   • Use smaller --batch-size (20 instead of {batch_size})")
                        click.echo(f"   • Use --max-items=50 to limit items per facet")
                        click.echo(f"   • Process fewer facets at once")
                        break
                    
                    # Mark facet as error in database
                    storage.update_facet_discovery(
                        facet['id'], 
                        status='error', 
                        error_message=error_msg[:500]  # Limit error message length
                    )
                    
                    if skip_errors:
                        click.echo(f"\n⚠️ Skipping facet {facet['facet_value']}: {error_msg}")
                        main_pbar.set_postfix(discovered=total_discovered, errors=errors_count)
                    else:
                        click.echo(f"\n❌ Failed on facet {facet['facet_value']}: {error_msg}")
                        click.echo("Use --skip-errors to continue past failed facets")
                        break
                
                main_pbar.update(1)
        
        click.echo(f"\n✅ Discovery complete!")
        click.echo(f"   📄 Total items discovered: {total_discovered:,}")
        if auto_enqueue:
            click.echo(f"   ⬇️ Total items enqueued: {total_enqueued:,}")
        if errors_count > 0:
            click.echo(f"   ❌ Facets with errors: {errors_count}")
        
        # Show updated stats
        stats = storage.get_discovery_stats()
        click.echo(f"\n📊 Updated Stats:")
        click.echo(f"   Completed facets: {stats['completed_facets']}/{stats['total_facets']}")
        click.echo(f"   Error facets: {stats['error_facets']}")
        click.echo(f"   Total discovered items: {stats['discovered_items']:,}")
        
        # Show problematic facets if any
        if skipped_facets:
            click.echo(f"\n⚠️  Problematic facets (use 'list-facets' to see details):")
            for error in skipped_facets[:5]:  # Show first 5
                click.echo(f"   • {error}")
            if len(skipped_facets) > 5:
                click.echo(f"   ... and {len(skipped_facets)-5} more")
            click.echo(f"\n💡 You can retry failed facets later or use different batch sizes")
        
        if not auto_enqueue and total_discovered > 0:
            click.echo(f"\n💡 Run 'newsagger auto-enqueue' to queue discovered content for download.")
            
    except Exception as e:
        click.echo(f"❌ Auto-discovery failed: {e}")


@cli.command()
@click.option('--max-batches', default=None, type=int, help='Maximum number of batches to process')
@click.option('--auto-enqueue', is_flag=True, help='Automatically enqueue discovered content')
@click.option('--rate-limit-delay', default=3.0, type=float, help='Delay between batch requests (seconds)')
def discover_via_batches(max_batches, auto_enqueue, rate_limit_delay):
    """Discover content via digitization batches (CAPTCHA-friendly alternative).
    
    This method uses the batches.json endpoint which is designed for bulk access
    and should be much less likely to trigger CAPTCHA protection compared to
    the search API used by auto-discover-facets.
    """
    config = Config()
    
    # Initialize components
    api_client = LocApiClient()
    processor = NewsDataProcessor()
    storage = NewsStorage(**config.get_storage_config())
    discovery = DiscoveryManager(client, processor, storage, 
                                **config.get_querybuilder_config())

    click.echo("🔍 Starting batch-based content discovery...")
    click.echo(f"   📦 Max batches: {max_batches or 'unlimited'}")
    click.echo(f"   ⬇️ Auto-enqueue: {'Yes' if auto_enqueue else 'No'}")
    click.echo(f"   ⏱️ Rate limit delay: {rate_limit_delay}s")
    
    # Progress tracking
    def progress_callback(processed, discovered, enqueued):
        click.echo(f"   📦 Batches: {processed}, 📄 Pages: {discovered}, ⬇️ Enqueued: {enqueued}")
    
    try:
        # Run batch discovery
        stats = discovery_manager.discover_content_via_batches(
            max_batches=max_batches,
            auto_enqueue=auto_enqueue,
            rate_limit_delay=rate_limit_delay,
            progress_callback=progress_callback
        )
        
        # Display results
        click.echo(f"\n✅ Batch discovery complete!")
        click.echo(f"   📦 Processed batches: {stats['processed_batches']}")
        click.echo(f"   📄 Discovered pages: {stats['discovered_pages']}")
        if auto_enqueue:
            click.echo(f"   ⬇️ Enqueued pages: {stats['enqueued_pages']}")
        if stats['errors'] > 0:
            click.echo(f"   ❌ Errors: {stats['errors']}")
        
        # Show queue status
        if auto_enqueue:
            queue_stats = storage.get_download_queue_stats()
            click.echo(f"\n📊 Queue Status:")
            click.echo(f"   Queued: {queue_stats['queued']}")
            click.echo(f"   Completed: {queue_stats['completed']}")
        
        if not auto_enqueue and stats['discovered_pages'] > 0:
            click.echo(f"\n💡 Run with --auto-enqueue to queue discovered content for download.")
            
    except Exception as e:
        click.echo(f"❌ Batch discovery failed: {e}")
        click.echo(f"💡 This method should be CAPTCHA-friendly. If it fails, check your network connection.")


@cli.command()
@click.option('--year', type=int, help='Specific year to test (e.g. 1906)')
@click.option('--state', help='Specific state to test (e.g. California)')
@click.option('--max-items', default=20, type=int, help='Maximum items to discover')
def test_discovery(year, state, max_items):
    """Test discovery with a small, focused dataset."""
    config = Config()
    client = LocApiClient(**config.get_api_config())
    processor = NewsDataProcessor()
    storage = NewsStorage(**config.get_storage_config())
    discovery = DiscoveryManager(client, processor, storage, **config.get_querybuilder_config())
    
    if year:
        click.echo(f"🔍 Testing discovery for year {year} (max {max_items} items)...")
        
        # Create a temporary facet for testing
        facet_id = storage.create_search_facet(
            'date_range', f'{year}/{year}', '', max_items
        )
        
        try:
            discovered = discovery.discover_facet_content(facet_id, max_items=max_items, batch_size=20)
            click.echo(f"✅ Successfully discovered {discovered} items for {year}")
            
            # Show sample of what was found
            if discovered > 0:
                pages = storage.get_pages_for_facet(facet_id)[:5]
                click.echo(f"\n📄 Sample pages found:")
                for page in pages:
                    click.echo(f"   • {page['title']} - {page['date']}")
                    
        except Exception as e:
            click.echo(f"❌ Test discovery failed: {e}")
    
    elif state:
        click.echo(f"🔍 Testing discovery for {state} newspapers (max {max_items} items)...")
        
        # First, check how many periodicals we have for this state
        periodicals = storage.get_periodicals(state=state)
        if not periodicals:
            click.echo(f"⚠️ No periodicals found for state '{state}'")
            return
        
        click.echo(f"📰 Found {len(periodicals)} periodicals in {state}")
        
        # Create a temporary facet for testing
        facet_id = storage.create_search_facet(
            'state', state, '', max_items
        )
        
        try:
            discovered = discovery.discover_facet_content(facet_id, max_items=max_items, batch_size=10)
            click.echo(f"✅ Successfully discovered {discovered} items for {state}")
            
        except Exception as e:
            click.echo(f"❌ Test discovery failed: {e}")
    
    else:
        click.echo("❌ Please specify either --year or --state for testing")


@cli.command()
@click.option('--priority-facets', help='Only enqueue specific facet IDs (comma-separated)')
@click.option('--max-size-gb', default=None, type=float, help='Maximum total queue size in GB')
@click.option('--dry-run', is_flag=True, help='Show what would be enqueued without actually doing it')
def auto_enqueue(priority_facets, max_size_gb, dry_run):
    """Automatically enqueue all discovered content for download."""
    config = Config()
    storage = NewsStorage(**config.get_storage_config())
    discovery = DiscoveryManager(None, None, storage, **config.get_querybuilder_config())
    
    action = "Would enqueue" if dry_run else "Enqueuing"
    click.echo(f"⬇️ {action} discovered content...")
    
    try:
        # Get facets with discovered content
        if priority_facets:
            facet_ids = [int(fid.strip()) for fid in priority_facets.split(',')]
            facets = [storage.get_search_facet(fid) for fid in facet_ids]
            facets = [f for f in facets if f]  # Remove None values
        else:
            facets = storage.get_search_facets(status=['completed', 'discovering'])
            facets = [f for f in facets if f['items_discovered'] > 0]
        
        if not facets:
            click.echo("✅ No facets with discovered content found.")
            return
        
        click.echo(f"📋 Found {len(facets)} facets with discovered content")
        
        total_items = 0
        total_size_mb = 0
        items_by_facet = {}
        
        # Calculate what would be enqueued
        for facet in facets:
            if max_size_gb and (total_size_mb / 1024) >= max_size_gb:
                click.echo(f"⚠️ Reached size limit of {max_size_gb} GB")
                break
                
            discovered = facet['items_discovered'] - facet.get('items_downloaded', 0)
            if discovered > 0:
                # Estimate size (rough estimate: 1MB per item average)
                estimated_size = discovered * 1.0  # MB
                if max_size_gb and (total_size_mb + estimated_size) / 1024 > max_size_gb:
                    # Partial enqueue to stay under limit
                    remaining_mb = (max_size_gb * 1024) - total_size_mb
                    discovered = int(remaining_mb / 1.0)
                    estimated_size = remaining_mb
                
                items_by_facet[facet['id']] = {
                    'items': discovered,
                    'size_mb': estimated_size,
                    'facet': facet
                }
                total_items += discovered
                total_size_mb += estimated_size
        
        if dry_run:
            click.echo(f"\n📊 Would enqueue {total_items:,} items ({total_size_mb/1024:.1f} GB):")
            for facet_id, info in items_by_facet.items():
                facet = info['facet']
                click.echo(f"   📄 {facet['facet_type']}: {facet['facet_value']}")
                click.echo(f"      {info['items']:,} items, {info['size_mb']/1024:.1f} GB")
            return
        
        # Actually enqueue the content
        enqueued_total = 0
        with tqdm(desc="Auto-enqueuing", total=len(items_by_facet)) as main_pbar:
            for facet_id, info in items_by_facet.items():
                facet = info['facet']
                main_pbar.set_description(f"Enqueuing {facet['facet_type']}: {facet['facet_value']}")
                
                # Initialize nested progress bar for large enqueuing operations
                item_pbar = None
                
                def update_enqueue_progress(progress_info):
                    nonlocal item_pbar
                    
                    # Create item progress bar for large operations (>1000 items)
                    if item_pbar is None and progress_info['total_pages'] > 1000:
                        item_pbar = tqdm(
                            desc=f"  Items for {facet['facet_value'][:20]}",
                            total=progress_info['total_pages'],
                            leave=False,
                            position=1,
                            unit="items"
                        )
                    
                    # Update item progress
                    if item_pbar:
                        item_pbar.n = progress_info['current_item']
                        item_pbar.refresh()
                
                enqueued = discovery.enqueue_facet_content(
                    facet_id,
                    max_items=info['items'],
                    progress_callback=update_enqueue_progress
                )
                
                # Close item progress bar if it exists
                if item_pbar:
                    item_pbar.close()
                
                enqueued_total += enqueued
                main_pbar.set_postfix(enqueued=f"{enqueued_total:,}")
                main_pbar.update(1)
        
        click.echo(f"\n✅ Enqueuing complete!")
        click.echo(f"   ⬇️ Total items enqueued: {enqueued_total:,}")
        click.echo(f"   💾 Estimated total size: {total_size_mb/1024:.1f} GB")
        
        # Show queue stats
        queue_stats = storage.get_download_queue_stats()
        click.echo(f"\n📊 Download Queue:")
        click.echo(f"   Queued items: {queue_stats.get('queued', 0):,}")
        click.echo(f"   Total estimated size: {queue_stats.get('total_size_mb', 0)/1024:.1f} GB")
        
        click.echo(f"\n💡 Run 'newsagger show-queue' to see queued downloads.")
        
    except Exception as e:
        click.echo(f"❌ Auto-enqueue failed: {e}")


@cli.command()
@click.option('--interval', default=5, type=int, help='Update interval in seconds')
@click.option('--count', default=0, type=int, help='Number of updates (0 for infinite)')
def watch_progress(interval, count):
    """Real-time monitoring of discovery and download progress."""
    config = Config()
    storage = NewsStorage(**config.get_storage_config())
    
    try:
        iterations = 0
        while count == 0 or iterations < count:
            # Clear screen
            click.clear()
            
            # Show current timestamp
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            click.echo(f"🕐 Live Progress Monitor - {now} (refreshing every {interval}s)")
            click.echo("=" * 60)
            
            # Discovery stats
            stats = storage.get_discovery_stats()
            click.echo(f"\n📊 Discovery Progress:")
            click.echo(f"   Total facets: {stats['total_facets']}")
            click.echo(f"   Completed: {stats['completed_facets']} ({100*stats['completed_facets']/max(1,stats['total_facets']):.1f}%)")
            click.echo(f"   Discovering: {stats['discovering_facets']}")
            click.echo(f"   Error facets: {stats['error_facets']}")
            click.echo(f"   Items discovered: {stats['discovered_items']:,}")
            
            # Queue stats
            queue_stats = storage.get_download_queue_stats()
            click.echo(f"\n⬇️ Download Queue:")
            click.echo(f"   Queued: {queue_stats.get('queued', 0):,}")
            click.echo(f"   Active: {queue_stats.get('active', 0):,}")
            click.echo(f"   Completed: {queue_stats.get('completed', 0):,}")
            click.echo(f"   Failed: {queue_stats.get('failed', 0):,}")
            click.echo(f"   Total size: {queue_stats.get('total_size_mb', 0)/1024:.1f} GB")
            
            # Recent activity (show facets currently being discovered)
            active_facets = storage.get_search_facets(status=['discovering'])
            if active_facets:
                click.echo(f"\n🔍 Currently Discovering:")
                for facet in active_facets[:3]:  # Show top 3
                    discovered = facet.get('items_discovered', 0)
                    click.echo(f"   • {facet['facet_type']}: {facet['facet_value']} ({discovered:,} items)")
            
            # Recent downloads
            try:
                recent_downloads = storage.get_recent_downloads(limit=3)
                if recent_downloads:
                    click.echo(f"\n📥 Recent Downloads:")
                    for item in recent_downloads:
                        click.echo(f"   • {item.get('title', 'Unknown')[:50]}...")
            except:
                pass  # Skip if method doesn't exist
            
            click.echo(f"\n💡 Press Ctrl+C to stop monitoring")
            
            if count > 0:
                click.echo(f"   Updates remaining: {count - iterations - 1}")
            
            iterations += 1
            if count == 0 or iterations < count:
                time.sleep(interval)
                
    except KeyboardInterrupt:
        click.echo(f"\n👋 Monitoring stopped.")
    except Exception as e:
        click.echo(f"\n❌ Monitoring error: {e}")


@cli.command()
@click.option('--facet-id', type=int, help='Reset specific facet by ID')
@click.option('--all-stuck', is_flag=True, help='Reset all facets stuck in discovering status')
def reset_stuck_facets(facet_id, all_stuck):
    """Reset facets that are stuck in discovering status for resume."""
    config = Config()
    storage = NewsStorage(**config.get_storage_config())
    
    if facet_id:
        # Reset specific facet
        facet = storage.get_search_facet(facet_id)
        if not facet:
            click.echo(f"❌ Facet {facet_id} not found")
            return
        
        if facet['status'] == 'discovering':
            # Reset to pending but preserve discovered items
            storage.update_facet_discovery(
                facet_id,
                status='pending',
                current_page=facet.get('resume_from_page', 1)
            )
            click.echo(f"✅ Reset facet {facet_id} ({facet['facet_value']}) to resume from page {facet.get('resume_from_page', 1)}")
        else:
            click.echo(f"⚠️ Facet {facet_id} is not stuck (status: {facet['status']})")
    
    elif all_stuck:
        # Find all stuck facets
        stuck_facets = storage.get_search_facets(status=['discovering'])
        if not stuck_facets:
            click.echo("✅ No stuck facets found")
            return
        
        click.echo(f"🔧 Found {len(stuck_facets)} stuck facets:")
        for facet in stuck_facets:
            resume_page = facet.get('resume_from_page', 1)
            storage.update_facet_discovery(
                facet['id'],
                status='pending',
                current_page=resume_page
            )
            click.echo(f"   • Reset {facet['facet_value']} (will resume from page {resume_page})")
        
        click.echo(f"\n✅ Reset {len(stuck_facets)} facets for resume")
    
    else:
        click.echo("❌ Please specify either --facet-id or --all-stuck")


@cli.command()
@click.option('--num-workers', default=4, type=int, help='Number of worker databases to create')
@click.option('--output-dir', default='./distributed', help='Directory to store worker databases')
@click.option('--include-completed', is_flag=True, help='Include completed facets for redistribution')
def split_database(num_workers, output_dir, include_completed):
    """Split remaining work into multiple databases for distributed processing."""
    config = Config()
    storage = NewsStorage(**config.get_storage_config())
        
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    
    # Get facets to distribute
    if include_completed:
        facets = storage.get_search_facets()
    else:
        facets = storage.get_search_facets(status=['pending', 'discovering', 'error'])
    
    if not facets:
        click.echo("❌ No facets found to distribute")
        return
    
    click.echo(f"🔄 Splitting {len(facets)} facets across {num_workers} worker databases...")
    
    # Calculate facets per worker
    facets_per_worker = len(facets) // num_workers
    remainder = len(facets) % num_workers
    
    worker_assignments = []
    start_idx = 0
    
    for i in range(num_workers):
        # Add one extra facet to first 'remainder' workers
        worker_size = facets_per_worker + (1 if i < remainder else 0)
        end_idx = start_idx + worker_size
        worker_facets = facets[start_idx:end_idx]
        worker_assignments.append(worker_facets)
        start_idx = end_idx
    
    # Create worker databases
    for worker_id, worker_facets in enumerate(worker_assignments):
        worker_db_path = output_path / f"worker_{worker_id}.db"
        
        # Copy main database structure
        shutil.copy2(storage.db_path, worker_db_path)
        
        # Create worker storage instance
        worker_storage = NewsStorage(str(worker_db_path))
        
        # Clear all facets and keep only assigned ones
        with worker_storage._get_connection() as conn:
            # Clear existing facets
            conn.execute("DELETE FROM search_facets")
            
            # Insert assigned facets
            for facet in worker_facets:
                conn.execute("""
                    INSERT INTO search_facets 
                    (id, facet_type, facet_value, facet_query, estimated_items,
                     actual_items, items_discovered, items_downloaded,
                     discovery_started, discovery_completed, status, error_message,
                     current_page, last_batch_size, resume_from_page)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    facet['id'], facet['facet_type'], facet['facet_value'], 
                    facet.get('query'), facet.get('estimated_items', 0),
                    facet.get('actual_items', 0), facet.get('items_discovered', 0),
                    facet.get('items_downloaded', 0), facet.get('discovery_started'),
                    facet.get('discovery_completed'), facet.get('status', 'pending'),
                    facet.get('error_message'), facet.get('current_page', 1),
                    facet.get('last_batch_size', 100), facet.get('resume_from_page', 1)
                ))
            
            conn.commit()
        
        # Create worker configuration
        worker_config_path = output_path / f"worker_{worker_id}_config.txt"
        with open(worker_config_path, 'w') as f:
            f.write(f"# Worker {worker_id} Configuration\n")
            f.write(f"DATABASE_PATH={worker_db_path.absolute()}\n")
            f.write(f"WORKER_ID={worker_id}\n")
            f.write(f"ASSIGNED_FACETS={len(worker_facets)}\n")
            f.write(f"FACET_IDS={','.join(str(f['id']) for f in worker_facets)}\n")
        
        click.echo(f"   ✅ Worker {worker_id}: {len(worker_facets)} facets → {worker_db_path}")
    
    # Create master coordination file
    master_config_path = output_path / "master_config.json"
    master_config = {
        "num_workers": num_workers,
        "source_database": str(storage.db_path),
        "worker_databases": [f"worker_{i}.db" for i in range(num_workers)],
        "created_at": datetime.now().isoformat(),
        "total_facets": len(facets),
        "facets_per_worker": [len(assignments) for assignments in worker_assignments]
    }
    
    with open(master_config_path, 'w') as f:
        json.dump(master_config, f, indent=2)
    
    click.echo(f"\n🎉 Database splitting complete!")
    click.echo(f"   📁 Output directory: {output_path}")
    click.echo(f"   🔢 Workers created: {num_workers}")
    click.echo(f"   📊 Facets distributed: {len(facets)}")
    click.echo(f"\n💡 Next steps:")
    click.echo(f"   1. Deploy worker databases to different machines/proxies")
    click.echo(f"   2. Run discovery on each worker: python main.py auto-discover-facets --database worker_X.db")
    click.echo(f"   3. Merge results back: python main.py merge-databases {output_dir}")


@cli.command()
@click.argument('distributed_dir', type=click.Path(exists=True))
@click.option('--dry-run', is_flag=True, help='Show what would be merged without doing it')
def merge_databases(distributed_dir, dry_run):
    """Merge completed work from distributed worker databases back into master."""
    config = Config()
    master_storage = NewsStorage(**config.get_storage_config())
        
    dist_path = Path(distributed_dir)
    master_config_path = dist_path / "master_config.json"
    
    if not master_config_path.exists():
        click.echo(f"❌ Master config not found at {master_config_path}")
        return
    
    with open(master_config_path) as f:
        master_config = json.load(f)
    
    click.echo(f"🔄 Merging results from {master_config['num_workers']} worker databases...")
    
    total_merged_facets = 0
    total_merged_pages = 0
    
    for worker_id in range(master_config['num_workers']):
        worker_db_path = dist_path / f"worker_{worker_id}.db"
        
        if not worker_db_path.exists():
            click.echo(f"⚠️ Worker database not found: {worker_db_path}")
            continue
        
        click.echo(f"\n📥 Processing worker {worker_id}...")
        
        # Connect to worker database
        with sqlite3.connect(worker_db_path) as worker_conn:
            worker_conn.row_factory = sqlite3.Row
            
            # Get completed facets from worker
            cursor = worker_conn.execute("""
                SELECT * FROM search_facets 
                WHERE status = 'completed' AND items_discovered > 0
            """)
            completed_facets = cursor.fetchall()
            
            if not completed_facets:
                click.echo(f"   ℹ️ No completed work found")
                continue
            
            # Get discovered pages for completed facets
            facet_ids = [str(f['id']) for f in completed_facets]
            cursor = worker_conn.execute(f"""
                SELECT * FROM pages 
                WHERE item_id IN (
                    SELECT reference_id FROM download_queue 
                    WHERE queue_type = 'page'
                )
            """)
            discovered_pages = cursor.fetchall()
            
            if dry_run:
                click.echo(f"   📊 Would merge: {len(completed_facets)} facets, {len(discovered_pages)} pages")
                total_merged_facets += len(completed_facets)
                total_merged_pages += len(discovered_pages)
                continue
            
            # Merge into master database
            with master_storage._get_connection() as master_conn:
                # Update facet progress
                for facet in completed_facets:
                    master_conn.execute("""
                        UPDATE search_facets 
                        SET actual_items = ?, items_discovered = ?, status = ?,
                            discovery_completed = ?, current_page = ?, 
                            last_batch_size = ?, resume_from_page = ?,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                    """, (
                        facet['actual_items'], facet['items_discovered'], facet['status'],
                        facet['discovery_completed'], facet['current_page'],
                        facet['last_batch_size'], facet['resume_from_page'], facet['id']
                    ))
                
                # Merge discovered pages (INSERT OR REPLACE to handle duplicates)
                for page in discovered_pages:
                    master_conn.execute("""
                        INSERT OR REPLACE INTO pages 
                        (item_id, lccn, title, date, edition, sequence, page_url,
                         pdf_url, jp2_url, ocr_url, thumbnail_url, metadata_json,
                         downloaded, file_size_bytes, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, tuple(page))
                
                master_conn.commit()
            
            click.echo(f"   ✅ Merged: {len(completed_facets)} facets, {len(discovered_pages)} pages")
            total_merged_facets += len(completed_facets)
            total_merged_pages += len(discovered_pages)
    
    if dry_run:
        click.echo(f"\n📊 Dry run summary:")
        click.echo(f"   Would merge {total_merged_facets} completed facets")
        click.echo(f"   Would merge {total_merged_pages} discovered pages")
    else:
        click.echo(f"\n🎉 Merge complete!")
        click.echo(f"   📊 Merged {total_merged_facets} completed facets")
        click.echo(f"   📄 Merged {total_merged_pages} discovered pages")
        
        # Show updated stats
        stats = master_storage.get_discovery_stats()
        click.echo(f"\n📈 Updated master database stats:")
        click.echo(f"   Completed facets: {stats['completed_facets']}/{stats['total_facets']}")
        click.echo(f"   Discovered items: {stats['discovered_items']:,}")


@cli.command()
@click.option('--start-year', default=1900, type=int, help='Start year')
@click.option('--end-year', default=1920, type=int, help='End year')
@click.option('--states', help='Comma-separated states to focus on')
@click.option('--auto-discover', is_flag=True, help='Automatically discover content after creating facets')
@click.option('--auto-enqueue', is_flag=True, help='Automatically enqueue discovered content')
@click.option('--max-size-gb', default=10.0, type=float, help='Maximum download queue size in GB')
@click.option('--unlimited-discovery', is_flag=True, help='Discover ALL content (no per-facet limits)')
def setup_download_workflow(start_year, end_year, states, auto_discover, auto_enqueue, max_size_gb, unlimited_discovery):
    """Set up complete automated download workflow from scratch.
    
    Use --unlimited-discovery to discover ALL content (no per-facet limits).
    Default behavior uses conservative limits to avoid rate limiting.
    
    All progress is saved automatically - you can safely interrupt and resume.
    """
    config = Config()
    client = LocApiClient(**config.get_api_config())
    processor = NewsDataProcessor()
    storage = NewsStorage(**config.get_storage_config())
    discovery = DiscoveryManager(client, processor, storage, 
                                **config.get_querybuilder_config())

    click.echo("🚀 Setting up automated download workflow...")
    click.echo(f"   📅 Years: {start_year} - {end_year}")
    if states:
        click.echo(f"   🗺️ States: {states}")
    click.echo(f"   💾 Max queue size: {max_size_gb} GB")
    click.echo("💡 Tip: Progress is saved automatically - you can safely interrupt (Ctrl+C) and resume")
    
    try:
        # Step 1: Discover periodicals if needed
        periodicals = storage.get_periodicals()
        if not periodicals:
            click.echo("\n🔍 Step 1: Discovering periodicals...")
            discovered_count = discovery.discover_all_periodicals()
            click.echo(f"✅ Discovered {discovered_count} periodicals")
        else:
            click.echo(f"\n✅ Step 1: Using existing {len(periodicals)} periodicals")
        
        # Step 2: Create facets (without estimation to avoid rate limiting)
        click.echo(f"\n📅 Step 2: Creating date facets...")
        facet_ids = discovery.create_date_range_facets(
            start_year, end_year, facet_size_years=1, estimate_items=False
        )
        click.echo(f"✅ Created {len(facet_ids)} date facets (estimation skipped to avoid rate limiting)")
        
        if states:
            click.echo(f"\n🗺️ Step 2b: Creating state facets...")
            state_list = [s.strip() for s in states.split(',')]
            state_facet_ids = discovery.create_state_facets(state_list)
            click.echo(f"✅ Created {len(state_facet_ids)} state facets")
            facet_ids.extend(state_facet_ids)
        
        if auto_discover:
            # Step 3: Auto-discover content with rate-limiting safe settings
            click.echo(f"\n🔍 Step 3: Auto-discovering content...")
            facets = storage.get_search_facets(status=['pending', 'discovering', 'captcha_retry'])
            
            # Estimate API calls and warn if too many
            estimated_api_calls = len(facets) * 5  # Conservative estimate
            estimated_minutes = estimated_api_calls / 20  # 20 requests per minute
            
            click.echo(f"   📋 Found {len(facets)} facets to discover")
            if unlimited_discovery:
                click.echo(f"   🚀 Using UNLIMITED discovery (all content per facet)")
            else:
                click.echo(f"   ⚠️  Using conservative settings to avoid rate limiting")
            click.echo(f"   📊 Estimated {estimated_api_calls} API calls (~{estimated_minutes:.1f} minutes)")
            
            if estimated_minutes > 30:
                click.echo(f"   ⚠️  This will take >{estimated_minutes:.1f} minutes due to rate limits")
                if not click.confirm("   Continue with auto-discovery?"):
                    click.echo("   Skipping auto-discovery. Run 'newsagger auto-discover-facets' later")
                    auto_discover = False

            if auto_discover:
                start_time = time.time()
                total_discovered = 0
                errors = 0
                
                # Proactive CAPTCHA interruption detection and fix
                click.echo(f"   🛠️  Checking for incorrectly completed facets...")
                try:
                    fix_stats = discovery.fix_incorrectly_completed_facets()
                    if fix_stats['facets_fixed'] > 0:
                        click.echo(f"   ✅ Auto-fixed {fix_stats['facets_fixed']} incorrectly completed facets")
                    else:
                        click.echo(f"   ✅ No incorrectly completed facets found")
                except Exception as e:
                    click.echo(f"   ⚠️  Error during facet checking: {e}")
                
                click.echo(f"   🚀 Starting discovery of {len(facets)} facets...")
                click.echo(f"   ⏱️  Started at: {time.strftime('%H:%M:%S')}")
                
                with tqdm(desc="Auto-discovering", total=len(facets)) as pbar:
                    for i, facet in enumerate(facets):
                        # Update description to show current facet
                        pbar.set_description(f"Discovering {facet['facet_type']}: {facet['facet_value']}")
                        
                        # Nested progress for this facet
                        current_facet_items = 0
                        current_page = 0
                        
                        def progress_callback(progress_info):
                            nonlocal current_facet_items, current_page
                            current_facet_items = progress_info['total_discovered']
                            current_page = progress_info['page']
                            
                            # Calculate elapsed time and estimated remaining
                            elapsed = time.time() - start_time
                            elapsed_str = f"{elapsed/60:.1f}m" if elapsed > 60 else f"{elapsed:.0f}s"
                            
                            # Update main progress bar with detailed stats
                            pbar.set_postfix(
                                facet=f"{i+1}/{len(facets)}",
                                page=current_page,
                                items=current_facet_items,
                                total=total_discovered + current_facet_items,
                                elapsed=elapsed_str,
                                errors=errors
                            )
                        
                        try:
                            if unlimited_discovery:
                                # Unlimited discovery: discover ALL content for each facet
                                discovered = discovery.discover_facet_content(
                                    facet['id'], 
                                    batch_size=50,  # Reasonable batch size
                                    max_items=None,  # No limit - discover everything
                                    progress_callback=progress_callback
                                )
                            else:
                                # Use conservative settings to avoid rate limiting:
                                # - Small batch_size (20) to reduce API calls per facet
                                # - Limit max_items (100) per facet to keep it manageable
                                discovered = discovery.discover_facet_content(
                                    facet['id'], 
                                    batch_size=20,  # Conservative batch size
                                    max_items=100,   # Limit per facet
                                    progress_callback=progress_callback
                                )
                            total_discovered += discovered
                            
                            # Show completion message for this facet with timing
                            elapsed = time.time() - start_time
                            elapsed_str = f"{elapsed/60:.1f}m" if elapsed > 60 else f"{elapsed:.0f}s"
                            avg_per_facet = elapsed / (i + 1)
                            remaining_estimate = (len(facets) - i - 1) * avg_per_facet
                            remaining_str = f"{remaining_estimate/60:.1f}m" if remaining_estimate > 60 else f"{remaining_estimate:.0f}s"
                            
                            click.echo(f"\n✅ Completed {facet['facet_type']} {facet['facet_value']}: {discovered:,} items")
                            click.echo(f"   ⏱️  Elapsed: {elapsed_str} | Est. remaining: {remaining_str} | Total discovered: {total_discovered:,}")
                            
                            pbar.set_postfix(discovered=total_discovered, errors=errors)
                            
                        except Exception as e:
                            errors += 1
                            error_msg = str(e)
                            
                            # Detailed error logging
                            click.echo(f"\n❌ ERROR on facet {facet['id']} ({facet['facet_type']} = {facet['facet_value']}):")
                            click.echo(f"   Error: {error_msg}")
                            if hasattr(e, '__traceback__'):
                                click.echo(f"   Traceback: {traceback.format_exc()}")
                            
                            # Check for rate limiting
                            if "Rate limited" in error_msg or "429" in error_msg:
                                click.echo(f"\n🛑 RATE LIMITED by LoC API during auto-discovery!")
                                click.echo(f"   Progress saved: {total_discovered:,} items discovered")
                                click.echo(f"   You can resume later with 'newsagger auto-discover-facets'")
                                break
                            else:
                                # Mark as error and continue
                                storage.update_facet_discovery(facet['id'], status='error', error_message=error_msg[:500])
                                pbar.set_postfix(discovered=total_discovered, errors=errors)
                        
                        pbar.update(1)
                
                if errors > 0:
                    click.echo(f"✅ Auto-discovered {total_discovered:,} items ({errors} errors)")
                    click.echo(f"   💡 Use 'newsagger retry-failed-facets' to retry failed ones")
                else:
                    click.echo(f"✅ Auto-discovered {total_discovered:,} items")
            
        if auto_enqueue:
            # Step 4: Auto-enqueue content
            click.echo(f"\n⬇️ Step 4: Auto-enqueuing content...")
            facets = storage.get_search_facets(status=['completed', 'discovering'])
            facets = [f for f in facets if f['items_discovered'] > 0]
            total_enqueued = 0
            with tqdm(desc="Auto-enqueuing", total=len(facets)) as pbar:
                for facet in facets:
                    enqueued = discovery.enqueue_facet_content(facet['id'])
                    total_enqueued += enqueued
                    pbar.update(1)
            click.echo(f"✅ Auto-enqueued {total_enqueued:,} items")
        
        click.echo(f"\n🎉 Workflow setup complete!")
        click.echo(f"💡 Next steps:")
        if not auto_discover:
            click.echo(f"   - Run 'newsagger auto-discover-facets --auto-enqueue' to discover and queue content")
        elif not auto_enqueue:
            click.echo(f"   - Run 'newsagger auto-enqueue' to queue discovered content")
        else:
            click.echo(f"   - Run 'newsagger show-queue' to see your download queue")
            click.echo(f"   - Implement actual download logic to process the queue")
        
    except Exception as e:
        click.echo(f"❌ Workflow setup failed: {e}")


@cli.command()
def discovery_status():
    """Show comprehensive discovery and download progress."""
    config = Config()
    storage = NewsStorage(**config.get_storage_config())
    
    try:
        stats = storage.get_discovery_stats()
        
        click.echo("🔍 Discovery & Download Status:")
        click.echo(f"\n📰 Periodicals:")
        click.echo(f"   Total: {stats['total_periodicals']:,}")
        click.echo(f"   Discovered: {stats['discovered_periodicals']:,}")
        click.echo(f"   Downloaded: {stats['downloaded_periodicals']:,}")
        
        if stats['total_periodicals'] > 0:
            discovery_pct = (stats['discovered_periodicals'] / stats['total_periodicals']) * 100
            download_pct = (stats['downloaded_periodicals'] / stats['total_periodicals']) * 100
            click.echo(f"   Discovery progress: {discovery_pct:.1f}%")
            click.echo(f"   Download progress: {download_pct:.1f}%")
        
        click.echo(f"\n🔍 Search Facets:")
        click.echo(f"   Total: {stats['total_facets']:,}")
        click.echo(f"   Completed: {stats['completed_facets']:,}")
        click.echo(f"   Errors: {stats['error_facets']:,}")
        
        click.echo(f"\n📊 Estimated Content:")
        click.echo(f"   Estimated items: {stats['estimated_items']:,}")
        click.echo(f"   Actual items: {stats['actual_items']:,}")
        click.echo(f"   Discovered: {stats['discovered_items']:,}")
        click.echo(f"   Downloaded: {stats['downloaded_items']:,}")
        
        if stats['actual_items'] > 0:
            discovery_item_pct = (stats['discovered_items'] / stats['actual_items']) * 100
            download_item_pct = (stats['downloaded_items'] / stats['actual_items']) * 100
            click.echo(f"   Discovery progress: {discovery_item_pct:.1f}%")
            click.echo(f"   Download progress: {download_item_pct:.1f}%")
        
        click.echo(f"\n⬇️ Download Queue:")
        click.echo(f"   Total items: {stats['total_queue_items']:,}")
        click.echo(f"   Queued: {stats['queued_items']:,}")
        click.echo(f"   Active: {stats['active_items']:,}")
        click.echo(f"   Completed: {stats['completed_queue_items']:,}")
        click.echo(f"   Average progress: {stats['avg_queue_progress']:.1f}%")
        
        # Show undiscovered periodicals
        undiscovered = storage.get_periodicals(discovery_complete=False)
        if undiscovered:
            click.echo(f"\n🔍 Next to discover ({len(undiscovered)} periodicals):")
            for p in undiscovered[:5]:
                click.echo(f"   📄 {p['title']} ({p['state']})")
            if len(undiscovered) > 5:
                click.echo(f"   ... and {len(undiscovered) - 5} more")
        
        # Show ready facets
        ready_facets = storage.get_search_facets(status='completed')
        if ready_facets:
            click.echo(f"\n✅ Ready for download ({len(ready_facets)} facets):")
            for facet in ready_facets[:5]:
                click.echo(f"   📅 {facet['facet_type']}: {facet['facet_value']} ({facet['actual_items']:,} items)")
            if len(ready_facets) > 5:
                click.echo(f"   ... and {len(ready_facets) - 5} more")
        
    except Exception as e:
        click.echo(f"❌ Failed to get discovery status: {e}")


@cli.command()
@click.option('--facet-type', help='Filter by facet type')
@click.option('--status', help='Filter by status')
def list_facets(facet_type, status):
    """List search facets and their status."""
    config = Config()
    storage = NewsStorage(**config.get_storage_config())
    
    try:
        facets = storage.get_search_facets(facet_type=facet_type, status=status)
        
        if not facets:
            click.echo("No facets found matching criteria.")
            return
        
        click.echo(f"📋 Found {len(facets)} facets:")
        
        for facet in facets:
            status_icon = {
                'pending': '⏳',
                'discovering': '🔍',
                'downloading': '⬇️',
                'completed': '✅',
                'error': '❌'
            }.get(facet['status'], '❓')
            
            click.echo(f"\n{status_icon} {facet['facet_type']}: {facet['facet_value']}")
            click.echo(f"   Status: {facet['status']}")
            click.echo(f"   Estimated: {facet['estimated_items']:,} items")
            if facet['actual_items']:
                click.echo(f"   Actual: {facet['actual_items']:,} items")
            if facet['items_discovered']:
                click.echo(f"   Discovered: {facet['items_discovered']:,} items")
            if facet['items_downloaded']:
                click.echo(f"   Downloaded: {facet['items_downloaded']:,} items")
            if facet['error_message']:
                click.echo(f"   Error: {facet['error_message']}")
        
    except Exception as e:
        click.echo(f"❌ Failed to list facets: {e}")


@cli.command()
@click.option('--status', help='Filter by status')
@click.option('--limit', default=20, help='Number of items to show')
def show_queue(status, limit):
    """Show download queue items."""
    config = Config()
    storage = NewsStorage(**config.get_storage_config())
    
    try:
        queue = storage.get_download_queue(status=status, limit=limit)
        
        if not queue:
            status_msg = f" with status '{status}'" if status else ""
            click.echo(f"No queue items found{status_msg}.")
            return
        
        status_msg = f" ({status})" if status else ""
        click.echo(f"📋 Download Queue{status_msg} - Top {len(queue)} items:")
        
        for i, item in enumerate(queue, 1):
            status_icon = {
                'queued': '⏳',
                'active': '🔄',
                'paused': '⏸️',
                'completed': '✅',
                'failed': '❌'
            }.get(item['status'], '❓')
            
            click.echo(f"\n{i}. {status_icon} Priority {item['priority']}: {item['queue_type']} {item['reference_id']}")
            click.echo(f"   Status: {item['status']}")
            click.echo(f"   Size: {item['estimated_size_mb']} MB")
            click.echo(f"   Time: {item['estimated_time_hours']:.1f} hours")
            if item['progress_percent'] > 0:
                click.echo(f"   Progress: {item['progress_percent']:.1f}%")
            if item['error_message']:
                click.echo(f"   Error: {item['error_message']}")
        
    except Exception as e:
        click.echo(f"❌ Failed to show queue: {e}")


# ===== DOWNLOAD PROCESSING COMMANDS =====

@cli.command()
@click.option('--max-items', default=None, type=int, help='Maximum items to download (per batch if continuous)')
@click.option('--max-size-mb', default=None, type=float, help='Maximum total download size in MB')
@click.option('--download-dir', default=None, help='Directory to store downloaded files (default: ./data/downloads)')
@click.option('--file-types', default='pdf,jp2,ocr,metadata', help='Comma-separated file types to download (pdf,jp2,ocr,metadata)')
@click.option('--parallel-workers', default=None, type=int, help='Number of parallel workers (default: CPU core count)')
@click.option('--file-concurrency', default=None, type=int, help='Number of concurrent file downloads per item (default: 6)')
@click.option('--dry-run', is_flag=True, help='Show what would be downloaded without actually doing it')
@click.option('--continuous', is_flag=True, help='Continuously process new items as they become available')
@click.option('--max-idle-minutes', default=10, type=int, help='In continuous mode, stop after this many minutes without new items')
def process_downloads(max_items, max_size_mb, download_dir, file_types, parallel_workers, file_concurrency, dry_run, continuous, max_idle_minutes):
    """Process the download queue and download files."""
    config = Config()
    storage = NewsStorage(**config.get_storage_config())
    client = LocApiClient(**config.get_api_config())
    
    # Use config default if not specified
    download_dir = download_dir or config.download_dir
    
    # Parse file types
    file_types_list = [ft.strip().lower() for ft in file_types.split(',')]
    valid_types = {'pdf', 'jp2', 'ocr', 'metadata'}
    invalid_types = set(file_types_list) - valid_types
    if invalid_types:
        click.echo(f"❌ Invalid file types: {', '.join(invalid_types)}")
        click.echo(f"Valid types: {', '.join(sorted(valid_types))}")
        return
    
    downloader = DownloadProcessor(storage, client, download_dir, file_types_list, parallel_workers, file_concurrency)
    
    mode = "continuous" if continuous else "single batch"
    action = "Would process" if dry_run else "Processing"
    click.echo(f"📥 {action} download queue ({mode} mode)...")
    if max_items:
        if continuous:
            click.echo(f"   📊 Max items per batch: {max_items}")
        else:
            click.echo(f"   📊 Max items: {max_items}")
    if max_size_mb:
        click.echo(f"   💾 Max size: {max_size_mb} MB")
    if continuous:
        click.echo(f"   ⏱️ Idle timeout: {max_idle_minutes} minutes")
    click.echo(f"   📁 Download directory: {download_dir}")
    click.echo(f"   📄 File types: {', '.join(file_types_list)}")
    
    try:
        stats = downloader.process_queue(
            max_items=max_items,
            max_size_mb=max_size_mb,
            dry_run=dry_run,
            continuous=continuous,
            max_idle_minutes=max_idle_minutes
        )
        
        if dry_run:
            click.echo(f"\n📊 Dry Run Results:")
            click.echo(f"   Would download: {stats.get('would_download', 0)} items")
            click.echo(f"   Estimated size: {stats.get('estimated_size_mb', 0):.1f} MB")
            if continuous and 'batches_processed' in stats:
                click.echo(f"   Batches processed: {stats['batches_processed']}")
        else:
            click.echo(f"\n✅ Download processing complete!")
            click.echo(f"   📥 Downloaded: {stats['downloaded']} items")
            click.echo(f"   ❌ Errors: {stats['errors']}")
            click.echo(f"   ⏭️ Skipped: {stats['skipped']}")
            click.echo(f"   💾 Total size: {stats['total_size_mb']:.1f} MB")
            if 'duration_minutes' in stats:
                click.echo(f"   ⏱️ Duration: {stats['duration_minutes']:.1f} minutes")
            if continuous and 'batches_processed' in stats:
                click.echo(f"   📦 Batches processed: {stats['batches_processed']}")
            
            # Show updated queue stats
            queue_stats = storage.get_download_queue_stats()
            click.echo(f"\n📊 Queue Status:")
            click.echo(f"   Queued: {queue_stats.get('queued', 0)}")
            click.echo(f"   Completed: {queue_stats.get('completed', 0)}")
            click.echo(f"   Failed: {queue_stats.get('failed', 0)}")
        
    except Exception as e:
        click.echo(f"❌ Download processing failed: {e}")


@cli.command()
@click.argument('item_id')
@click.option('--download-dir', default=None, help='Directory to store downloaded files (default: ./data/downloads)')
@click.option('--file-types', default='pdf,jp2,ocr,metadata', help='Comma-separated file types to download (pdf,jp2,ocr,metadata)')
@click.option('--parallel-workers', default=None, type=int, help='Number of parallel workers (default: CPU core count)')
@click.option('--file-concurrency', default=None, type=int, help='Number of concurrent file downloads per item (default: 6)')
def download_page(item_id, download_dir, file_types, parallel_workers, file_concurrency):
    """Download a specific page by item ID."""
    config = Config()
    storage = NewsStorage(**config.get_storage_config())
    client = LocApiClient(**config.get_api_config())
    
    # Parse file types
    file_types_list = [ft.strip().lower() for ft in file_types.split(',')]
    valid_types = {'pdf', 'jp2', 'ocr', 'metadata'}
    invalid_types = set(file_types_list) - valid_types
    if invalid_types:
        click.echo(f"❌ Invalid file types: {', '.join(invalid_types)}")
        click.echo(f"Valid types: {', '.join(sorted(valid_types))}")
        return
    
    downloader = DownloadProcessor(storage, client, download_dir, file_types_list, parallel_workers, file_concurrency)
    
    click.echo(f"📥 Downloading page {item_id}...")
    
    try:
        result = downloader._download_page(item_id)
        
        if result['success']:
            if result.get('skipped'):
                click.echo(f"⏭️ Page already downloaded")
            else:
                click.echo(f"✅ Downloaded {len(result.get('files', []))} files")
                click.echo(f"   💾 Size: {result.get('size_mb', 0):.1f} MB")
                click.echo(f"   📁 Files: {', '.join(result.get('files', []))}")
        else:
            click.echo(f"❌ Download failed: {result.get('error', 'Unknown error')}")
    
    except Exception as e:
        click.echo(f"❌ Download failed: {e}")


@cli.command()
def resume_downloads():
    """Resume failed downloads by resetting them to queued status."""
    config = Config()
    storage = NewsStorage(**config.get_storage_config())
    client = LocApiClient(**config.get_api_config())
    downloader = DownloadProcessor(storage, client)
    
    click.echo("🔄 Resuming failed downloads...")
    
    try:
        result = downloader.resume_failed_downloads()
        
        if result['resumed'] > 0:
            click.echo(f"✅ Reset {result['resumed']} failed downloads to queued status")
            click.echo(f"💡 Run 'newsagger process-downloads' to retry them")
        else:
            click.echo("✅ No failed downloads to resume")
    
    except Exception as e:
        click.echo(f"❌ Failed to resume downloads: {e}")


@cli.command()
def reset_stuck_downloads():
    """Reset stuck active downloads back to queued status."""
    config = Config()
    storage = NewsStorage(**config.get_storage_config())
    client = LocApiClient(**config.get_api_config())
    downloader = DownloadProcessor(storage, client)
    
    click.echo("🔧 Resetting stuck downloads...")
    
    try:
        result = downloader.reset_stuck_downloads()
        
        if result['reset'] > 0:
            click.echo(f"✅ Reset {result['reset']} stuck downloads to queued status")
            click.echo(f"💡 Run 'newsagger process-downloads' to retry them")
        else:
            click.echo("✅ No stuck downloads to reset")
    
    except Exception as e:
        click.echo(f"❌ Failed to reset stuck downloads: {e}")


@cli.command()
def reset_stuck_facets():
    """Reset facets stuck in 'discovering' status back to pending."""
    config = Config()
    storage = NewsStorage(**config.get_storage_config())
    
    # Find facets stuck in discovering status
    stuck_facets = storage.get_search_facets(status='discovering')
    
    if not stuck_facets:
        click.echo("✅ No stuck facets found")
        return
    
    click.echo(f"🔧 Found {len(stuck_facets)} facets stuck in 'discovering' status")
    
    for facet in stuck_facets:
        click.echo(f"   📅 {facet['facet_value']} (ID: {facet['id']})")
    
    if click.confirm("Reset these facets to pending status?"):
        with sqlite3.connect(storage.db_path) as conn:
            for facet in stuck_facets:
                conn.execute("""
                    UPDATE search_facets 
                    SET status = 'pending', error_message = NULL, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                """, (facet['id'],))
            conn.commit()
        
        click.echo(f"✅ Reset {len(stuck_facets)} facets to pending status")
        click.echo("💡 Run 'newsagger auto-discover-facets --skip-errors' to retry them")
    else:
        click.echo("Cancelled")


@cli.command()
@click.option('--batch-size', default=50, help='Smaller batch size for retry')
@click.option('--max-items', default=500, type=int, help='Limit items per facet for problematic ones')
def retry_failed_facets(batch_size, max_items):
    """Retry facets that failed during discovery with smaller batch sizes."""
    config = Config()
    client = LocApiClient(**config.get_api_config())
    processor = NewsDataProcessor()
    storage = NewsStorage(**config.get_storage_config())
    discovery = DiscoveryManager(client, processor, storage, 
                                **config.get_querybuilder_config())
    
    # Find failed facets
    failed_facets = storage.get_search_facets(status='error')
    
    if not failed_facets:
        click.echo("✅ No failed facets to retry")
        return
    
    click.echo(f"🔄 Found {len(failed_facets)} failed facets to retry")
    click.echo(f"   Using smaller batch size ({batch_size}) and item limit ({max_items})")
    
    for facet in failed_facets[:5]:  # Show first 5
        error_msg = facet.get('error_message', 'Unknown error')[:100]
        click.echo(f"   📅 {facet['facet_value']}: {error_msg}")
    
    if len(failed_facets) > 5:
        click.echo(f"   ... and {len(failed_facets)-5} more")
    
    if not click.confirm("Retry these facets with conservative settings?"):
        click.echo("Cancelled")
        return
    
    total_discovered = 0
    success_count = 0
    
    with tqdm(desc="Retrying failed facets", total=len(failed_facets)) as pbar:
        for facet in failed_facets:
            pbar.set_description(f"Retrying {facet['facet_value']}")
            
            try:
                # Reset facet to pending first
                storage.update_facet_discovery(facet['id'], status='pending', error_message=None)
                
                # Try discovery with conservative settings
                discovered_count = discovery.discover_facet_content(
                    facet['id'], 
                    batch_size=batch_size,
                    max_items=max_items
                )
                
                total_discovered += discovered_count
                success_count += 1
                pbar.set_postfix(success=success_count, discovered=total_discovered)
                
            except Exception as e:
                # Mark as failed again
                storage.update_facet_discovery(
                    facet['id'], 
                    status='error', 
                    error_message=f"Retry failed: {str(e)[:400]}"
                )
                pbar.set_postfix(success=success_count, discovered=total_discovered)
            
            pbar.update(1)
    
    click.echo(f"\n✅ Retry complete!")
    click.echo(f"   🎯 Successfully retried: {success_count}/{len(failed_facets)} facets")
    click.echo(f"   📄 Items discovered: {total_discovered:,}")
    
    remaining_failed = len(failed_facets) - success_count
    if remaining_failed > 0:
        click.echo(f"   ❌ Still failing: {remaining_failed} facets")
        click.echo("💡 These facets may have data issues or need manual investigation")


@cli.command()
@click.option('--download-dir', default=None, help='Directory to check (default: ./data/downloads)')
def download_stats(download_dir):
    """Show comprehensive download statistics."""
    config = Config()
    storage = NewsStorage(**config.get_storage_config())
    client = LocApiClient(**config.get_api_config())
    
    # Use config default if not specified  
    download_dir = download_dir or config.download_dir
    downloader = DownloadProcessor(storage, client, download_dir=download_dir)
    
    try:
        stats = downloader.get_download_stats()
        
        click.echo("📊 Download Statistics:")
        
        # Queue stats
        queue_stats = stats['queue_stats']
        click.echo(f"\n⬇️ Download Queue:")
        click.echo(f"   Total items: {queue_stats.get('total_items', 0)}")
        click.echo(f"   Queued: {queue_stats.get('queued', 0)}")
        click.echo(f"   Active: {queue_stats.get('active', 0)}")
        click.echo(f"   Completed: {queue_stats.get('completed', 0)}")
        click.echo(f"   Failed: {queue_stats.get('failed', 0)}")
        click.echo(f"   Total estimated size: {queue_stats.get('total_size_mb', 0)/1024:.1f} GB")
        
        # Storage stats
        storage_stats = stats['storage_stats']
        click.echo(f"\n💾 Database:")
        click.echo(f"   Total pages: {storage_stats.get('total_pages', 0):,}")
        click.echo(f"   Downloaded pages: {storage_stats.get('downloaded_pages', 0):,}")
        if storage_stats.get('total_pages', 0) > 0:
            download_pct = (storage_stats.get('downloaded_pages', 0) / storage_stats.get('total_pages', 1)) * 100
            click.echo(f"   Download progress: {download_pct:.1f}%")
        
        # Disk usage
        click.echo(f"\n💿 Local Storage:")
        click.echo(f"   Download directory: {stats['download_directory']}")
        click.echo(f"   Files on disk: {stats['files_on_disk']:,}")
        click.echo(f"   Disk usage: {stats['disk_usage_mb']:.1f} MB ({stats['disk_usage_mb']/1024:.2f} GB)")
        
    except Exception as e:
        click.echo(f"❌ Failed to get download stats: {e}")


@cli.command()
@click.option('--download-dir', default=None, help='Directory to clean (default: ./data/downloads)')
def cleanup_downloads(download_dir):
    """Clean up incomplete or corrupted download files."""
    config = Config()
    storage = NewsStorage(**config.get_storage_config())
    
    # Use config default if not specified
    download_dir = download_dir or config.download_dir
    client = LocApiClient(**config.get_api_config())
    downloader = DownloadProcessor(storage, client, download_dir=download_dir)
    
    click.echo("🧹 Cleaning up incomplete downloads...")
    
    try:
        result = downloader.cleanup_incomplete_downloads()
        
        if result['cleaned_files'] > 0:
            click.echo(f"✅ Cleaned up {result['cleaned_files']} files")
            click.echo(f"   💾 Freed space: {result['freed_space_mb']:.1f} MB")
        else:
            click.echo("✅ No cleanup needed - all files appear complete")
    
    except Exception as e:
        click.echo(f"❌ Cleanup failed: {e}")


@cli.command()
@click.option('--priority', default=None, type=int, help='Only download items with this priority')
@click.option('--queue-type', help='Only download items of this type (page, facet, periodical)')
@click.option('--max-items', default=10, type=int, help='Maximum items to download')
@click.option('--download-dir', default=None, help='Directory to store files (default: ./data/downloads)')
@click.option('--file-types', default='pdf,jp2,ocr,metadata', help='Comma-separated file types to download (pdf,jp2,ocr,metadata)')
@click.option('--parallel-workers', default=None, type=int, help='Number of parallel workers (default: CPU core count)')
@click.option('--file-concurrency', default=None, type=int, help='Number of concurrent file downloads per item (default: 6)')
def download_priority(priority, queue_type, max_items, download_dir, file_types, parallel_workers, file_concurrency):
    """Download items from queue with specific priority or type."""
    config = Config()
    
    # Use config default if not specified
    download_dir = download_dir or config.download_dir
    storage = NewsStorage(**config.get_storage_config())
    client = LocApiClient(**config.get_api_config())
    
    # Parse file types
    file_types_list = [ft.strip().lower() for ft in file_types.split(',')]
    valid_types = {'pdf', 'jp2', 'ocr', 'metadata'}
    invalid_types = set(file_types_list) - valid_types
    if invalid_types:
        click.echo(f"❌ Invalid file types: {', '.join(invalid_types)}")
        click.echo(f"Valid types: {', '.join(sorted(valid_types))}")
        return
    
    downloader = DownloadProcessor(storage, client, download_dir, file_types_list, parallel_workers, file_concurrency)
    
    # Filter queue items
    all_queue = storage.get_download_queue(status='queued')
    filtered_queue = []
    
    for item in all_queue:
        if priority is not None and item['priority'] != priority:
            continue
        if queue_type and item['queue_type'] != queue_type:
            continue
        filtered_queue.append(item)
    
    if not filtered_queue:
        click.echo("No matching items found in queue")
        return
    
    # Limit items
    filtered_queue = filtered_queue[:max_items]
    
    filter_desc = []
    if priority is not None:
        filter_desc.append(f"priority {priority}")
    if queue_type:
        filter_desc.append(f"type {queue_type}")
    
    filter_str = " and ".join(filter_desc) if filter_desc else "all criteria"
    click.echo(f"📥 Downloading {len(filtered_queue)} items matching {filter_str}...")
    
    try:
        # Temporarily modify queue to only include filtered items
        # Mark others as paused temporarily
        paused_items = []
        for item in all_queue:
            if item not in filtered_queue and item['status'] == 'queued':
                storage.update_queue_item(item['id'], status='paused')
                paused_items.append(item['id'])
        
        # Process the filtered downloads
        stats = downloader.process_queue(max_items=len(filtered_queue))
        
        # Restore paused items
        for item_id in paused_items:
            storage.update_queue_item(item_id, status='queued')
        
        click.echo(f"\n✅ Priority download complete!")
        click.echo(f"   📥 Downloaded: {stats['downloaded']} items")
        click.echo(f"   ❌ Errors: {stats['errors']}")
        click.echo(f"   💾 Total size: {stats['total_size_mb']:.1f} MB")
        
    except Exception as e:
        click.echo(f"❌ Priority download failed: {e}")


@cli.command()  
def reset_captcha_state():
    """Reset global CAPTCHA protection state (use with caution)."""    
    global_captcha = GlobalCaptchaManager()
    captcha_status = global_captcha.get_status()
    
    if not captcha_status['blocked']:
        click.echo("✅ Global CAPTCHA state is not currently blocked")
        click.echo(f"   Status: {captcha_status['reason']}")
        return
    
    click.echo(f"🛑 Current CAPTCHA state:")
    click.echo(f"   Status: {captcha_status['reason']}")
    click.echo(f"   Consecutive CAPTCHAs: {captcha_status['consecutive_captchas']}")
    click.echo(f"   Cooling-off period: {captcha_status['cooling_off_hours']:.1f} hours")
    
    if captcha_status['last_captcha_time']:
        last_captcha = time.ctime(captcha_status['last_captcha_time'])
        click.echo(f"   Last CAPTCHA: {last_captcha}")
    
    click.echo(f"\n⚠️  WARNING: Resetting CAPTCHA state may trigger immediate CAPTCHA again!")
    click.echo(f"   Only reset if you're confident the API cooling-off period has passed.")
    
    if click.confirm("\nReset global CAPTCHA state?"):
        global_captcha.reset_state()
        click.echo("✅ Global CAPTCHA state has been reset")
        click.echo("💡 You can now resume discovery operations")
    else:
        click.echo("Cancelled")


@cli.command()
@click.option('--ultra-conservative', is_flag=True, help='Use ultra-conservative settings (1 item per request)')
@click.option('--small-batches', is_flag=True, help='Use small batch sizes (5-10 items)')
@click.option('--micro-batches', is_flag=True, help='Use micro batch sizes (1-3 items)')
def set_conservative_mode(ultra_conservative, small_batches, micro_batches):
    """Set conservative processing modes to avoid CAPTCHA triggers.
    
    These modes adjust batch sizes globally to reduce API load:
    - Ultra-conservative: 1 item per request (slowest, safest)
    - Small batches: 5-10 items per request (balanced)
    - Micro batches: 1-3 items per request (very safe)
    """
    config = Config()
    
    if ultra_conservative:
        batch_size = 1
        mode_name = "ultra-conservative"
    elif small_batches:
        batch_size = 5
        mode_name = "small batches"
    elif micro_batches:
        batch_size = 2
        mode_name = "micro batches"
    else:
        click.echo("Please specify a conservative mode:")
        click.echo("  --ultra-conservative: 1 item per request")
        click.echo("  --small-batches: 5 items per request")
        click.echo("  --micro-batches: 2 items per request")
        return
    
    click.echo(f"🐌 Setting {mode_name} mode (batch size: {batch_size})")
    click.echo(f"   This will be much slower but reduce CAPTCHA risk")
    
    # Store the setting in a config file for other commands to use    
    config_file = Path("newsagger_conservative.json")
    conservative_config = {
        'mode': mode_name,
        'batch_size': batch_size,
        'set_at': str(datetime.now()),
        'description': f'Conservative mode to avoid CAPTCHA triggers'
    }
    
    config_file.write_text(json.dumps(conservative_config, indent=2))
    
    click.echo(f"✅ Conservative mode saved to {config_file}")
    click.echo(f"💡 Other commands will now use batch size {batch_size} by default")
    click.echo(f"   Remove {config_file} to return to normal batch sizes")


@cli.command()
def show_conservative_mode():
    """Show current conservative mode settings."""
    
    config_file = Path("newsagger_conservative.json")
    
    if not config_file.exists():
        click.echo("🟢 Normal mode: No conservative settings active")
        click.echo("💡 Use 'set-conservative-mode' to activate CAPTCHA-safe processing")
        return
    
    try:
        conservative_config = json.loads(config_file.read_text())
        click.echo(f"🐌 Conservative mode active: {conservative_config['mode']}")
        click.echo(f"   Batch size: {conservative_config['batch_size']}")
        click.echo(f"   Set at: {conservative_config['set_at']}")
        click.echo(f"   Description: {conservative_config['description']}")
        click.echo(f"\n💡 Remove {config_file} to return to normal processing")
    except Exception as e:
        click.echo(f"❌ Error reading conservative config: {e}")


@cli.command()
def pause_operations():
    """Create a pause file to stop all long-running operations gracefully."""
    
    pause_file = Path("newsagger_pause.json")
    pause_config = {
        'paused_at': str(datetime.now()),
        'reason': 'Manual pause requested',
        'message': 'All operations paused via CLI command'
    }
    
    pause_file.write_text(json.dumps(pause_config, indent=2))
    
    click.echo("⏸️  Operations paused!")
    click.echo(f"   Pause file created: {pause_file}")
    click.echo("   Long-running operations will stop at next checkpoint")
    click.echo("💡 Use 'resume-operations' to continue")


@cli.command()
def resume_operations():
    """Remove pause file to resume operations."""    
    pause_file = Path("newsagger_pause.json")
    conservative_file = Path("newsagger_conservative.json")
    
    if pause_file.exists():
        pause_file.unlink()
        click.echo("▶️  Operations resumed!")
        click.echo("   Pause file removed")
    else:
        click.echo("✅ Operations not paused")
    
    # Show conservative mode status
    if conservative_file.exists():
        click.echo("\n🐌 Conservative mode is still active")
        click.echo("   Use 'show-conservative-mode' for details")


@cli.command()
@click.option('--facet-id', type=int, help='Split a specific facet by ID')
@click.option('--facet-value', help='Split facets by value pattern (e.g., "1906")')
def split_facet(facet_id, facet_value):
    """Split large facets into smaller date ranges to avoid CAPTCHA.
    
    This is useful when a facet has too many items and keeps triggering CAPTCHAs.
    """
    config = Config()
    storage = NewsStorage(**config.get_storage_config())
    
    if facet_id:
        facets = [storage.get_search_facet(facet_id)]
        if not facets[0]:
            click.echo(f"❌ Facet {facet_id} not found")
            return
    elif facet_value:
        facets = storage.get_search_facets()
        facets = [f for f in facets if facet_value in f['facet_value']]
        if not facets:
            click.echo(f"❌ No facets found matching '{facet_value}'")
            return
    else:
        click.echo("Please specify --facet-id or --facet-value")
        return
    
    for facet in facets:
        click.echo(f"\n📅 Splitting facet: {facet['facet_value']}")
        click.echo(f"   Current status: {facet['status']}")
        click.echo(f"   Items discovered: {facet.get('items_discovered', 0)}")
        
        # For date range facets, split into smaller periods
        if facet['facet_type'] == 'date_range' and '/' in facet['facet_value']:
            start_year, end_year = facet['facet_value'].split('/')
            try:
                start_year = int(start_year)
                end_year = int(end_year)
                
                if end_year - start_year > 0:
                    # Split into individual years
                    click.echo(f"   Splitting {start_year}-{end_year} into individual years...")
                    
                    for year in range(start_year, end_year + 1):
                        new_facet_id = storage.create_search_facet(
                            'date_range', 
                            f'{year}/{year}',
                            f'Split from {facet["facet_value"]}',
                            facet.get('estimated_items', 0) // (end_year - start_year + 1)
                        )
                        click.echo(f"   ✅ Created facet for {year} (ID: {new_facet_id})")
                    
                    # Mark original facet as split
                    storage.update_facet_discovery(
                        facet['id'], 
                        status='split',
                        error_message=f'Split into years {start_year}-{end_year}'
                    )
                    click.echo(f"   📋 Original facet marked as 'split'")
                else:
                    click.echo(f"   ⚠️  Facet is already a single year, cannot split further")
            except ValueError:
                click.echo(f"   ❌ Cannot parse year range: {facet['facet_value']}")
        else:
            click.echo(f"   ⚠️  Facet type '{facet['facet_type']}' not suitable for splitting")

@patch('newsagger.cli.Config')
@patch('newsagger.cli.LocApiClient')
@patch('newsagger.cli.NewsStorage')
def test_estimate_facets_command(self, mock_storage, mock_client, mock_config):
    """
    Covers the from_facet-based fix: previously, non-date_range facets
    were estimated via andtext=facet['facet_value'] — searching the state
    name as free text instead of filtering by state. Now from_facet
    dispatches state/date_range/combined facets correctly.
    """
    mock_config_instance = Mock()
    mock_config_instance.get_api_config.return_value = {'base_url': 'test'}
    mock_config_instance.get_storage_config.return_value = {'db_path': ':memory:'}
    mock_config_instance.query_builder_class = LegacyQueryBuilder
    mock_config.return_value = mock_config_instance

    mock_client_instance = Mock()
    mock_client_instance.get_count.return_value = 42
    mock_client.return_value = mock_client_instance

    mock_storage_instance = Mock()
    mock_storage_instance.get_search_facets.return_value = [
        {'id': 1, 'facet_type': 'state', 'facet_value': 'California', 'estimated_items': 0}
    ]
    mock_storage.return_value = mock_storage_instance

    result = self.runner.invoke(cli, ['estimate-facets', '--facet-type', 'state'])

    assert result.exit_code == 0
    mock_client_instance.get_count.assert_called_once()
    builder_passed = mock_client_instance.get_count.call_args[0][0]
    # Regression guard: state routed to states, not smuggled into search_text
    assert builder_passed.params.states == ['california']
    assert builder_passed.params.search_text is None

if __name__ == '__main__':
    cli()