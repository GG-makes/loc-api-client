"""
Newspaper management commands.

Commands for discovering, searching, and downloading newspapers.
"""

import click
import json
from typing import List, Dict
from tqdm import tqdm
from ..config import Config
from ..rate_limited_client import LocApiClient, CaptchaHandlingException, GlobalCaptchaManager
from ..processor import NewsDataProcessor
from ..storage import NewsStorage
from ..api_params import LocGovQueryBuilder


@click.group()
def newspaper():
    """Newspaper discovery and management commands."""
    pass


@newspaper.command()
def list_newspapers():
    """List available newspapers with filtering options."""
    config = Config()
    client = LocApiClient(**config.get_api_config())
    processor = NewsDataProcessor()
    
    click.echo("Fetching newspaper list from Library of Congress...")
    
    # Process newspapers in batches for better memory efficiency and progress tracking
    newspapers = []
    batch_size = 100
    
    with tqdm(desc="Loading newspapers") as pbar:
        batch = []
        for newspaper in client.get_all_newspapers():
            batch.append(newspaper)
            pbar.update(1)
            
            # Process in batches to avoid memory issues with large datasets
            if len(batch) >= batch_size:
                newspapers.extend(batch)
                batch = []
        
        # Process remaining newspapers
        if batch:
            newspapers.extend(batch)
    
    processed = processor.process_newspapers_response({'newspapers': newspapers})
    
    # Display summary
    summary = processor.get_newspaper_summary(processed)
    
    click.echo(f"\n📰 Found {summary['total_newspapers']} newspapers")
    
    if summary.get('states'):
        click.echo("\n🗺️  Top states:")
        for state, count in list(summary['states'].items())[:10]:
            click.echo(f"   {state}: {count}")
    
    if summary.get('languages'):
        click.echo("\n🌐 Languages:")
        for lang, count in summary['languages'].items():
            click.echo(f"   {lang}: {count}")
    
    if summary.get('year_range'):
        start, end = summary['year_range']
        click.echo(f"\n📅 Date range: {start} - {end}")


@newspaper.command()
@click.option('--state', help='Filter by state')
@click.option('--language', help='Filter by language')
@click.option('--limit', default=10, help='Number of results to show')
def search_newspapers(state, language, limit):
    """Search newspapers with filters."""
    config = Config()
    storage = NewsStorage(**config.get_storage_config())
    
    newspapers = storage.get_newspapers(state=state, language=language)
    
    if not newspapers:
        click.echo("No newspapers found matching criteria.")
        return
    
    click.echo(f"Found {len(newspapers)} newspapers:\n")
    
    for i, newspaper in enumerate(newspapers[:limit]):
        place = json.loads(newspaper['place_of_publication'])
        langs = json.loads(newspaper['language'])
        
        click.echo(f"{i+1}. {newspaper['title']}")
        click.echo(f"   LCCN: {newspaper['lccn']}")
        click.echo(f"   Location: {', '.join(place)}")
        click.echo(f"   Languages: {', '.join(langs)}")
        click.echo(f"   Years: {newspaper['start_year']}-{newspaper['end_year']}")
        click.echo()


@newspaper.command()
@click.argument('lccn')
@click.option('--date1', default='1836', help='Start date (YYYY or YYYY-MM-DD)')
@click.option('--date2', help='End date (YYYY or YYYY-MM-DD)')
@click.option('--estimate-only', is_flag=True, help='Only show download estimate')
def download_newspaper(lccn, date1, date2, estimate_only):
    """Download pages for a specific newspaper."""
    config = Config()
    client = LocApiClient(**config.get_api_config())
    processor = NewsDataProcessor()
    storage = NewsStorage(**config.get_storage_config())

    # Validate date range
    if not processor.validate_date_range(date1, date2 or str(config.current_year)):
        click.echo("❌ Invalid date range")
        return

    # Get estimate
    
    
    builder = config.query_builder_class.from_cli(date1=date1, date2=date2, lccn=lccn)
    total_pages = client.get_count(builder)
    estimate = {
        'total_pages': total_pages,
        'estimated_size_mb': total_pages * 2,  # same rough estimate estimate_download_size used
        'date_range': f"{date1}-{date2}",
    }
    estimated_size_gb = estimate['estimated_size_mb'] / 1024
    estimated_time_hours = estimate['total_pages'] / 1000  # rough: ~1000 pages/hr at rate limit

    click.echo(f"📊 Download Estimate for {lccn}:")
    click.echo(f"   Total pages: {estimate['total_pages']:,}")
    click.echo(f"   Estimated size: {estimated_size_gb:.2f} GB")
    click.echo(f"   Estimated time: {estimated_time_hours:.1f} hours")

    if estimate_only:
        return

    if estimate['total_pages'] > 10000:
        if not click.confirm(f"This will download {estimate['total_pages']:,} pages. Continue?"):
            return

    # Create download session
    session_id = storage.create_download_session(
        f"{lccn}_{date1}_{date2}",
        {'lccn': lccn, 'date1': date1, 'date2': date2},
        estimate['total_pages']
    )

    # Start download with faceted search
    builder = LocGovQueryBuilder.from_cli(date1=date1, date2=date2, lccn=lccn)

    total_downloaded = 0

    with tqdm(total=estimate['total_pages'], desc="Downloading pages") as pbar:
        for result_batch in client.paginate_search(builder):
            pages = processor.process_search_response(result_batch)
            stored = storage.store_pages(pages)

            total_downloaded += stored
            pbar.update(stored)

            # Update session progress
            storage.update_session_progress(session_id, total_downloaded)

    # Complete session
    storage.complete_session(session_id)
    click.echo(f"✅ Downloaded {total_downloaded:,} pages")

# The existing tests is against the function 'status' in cli.py, so turning this
# off for now. Note this is part of a separate cli update by the author
# and not part of the migration, so keeping it here and commented out. 

# @newspaper.command()
# def status():
#     """Show download status and statistics."""
#     config = Config()
#     storage = NewsStorage(**config.get_storage_config())
#     stats = storage.get_storage_stats()
    
#     click.echo("📊 Newsagger Status:")
#     click.echo(f"   Database size: {stats['db_size_mb']} MB")
#     click.echo(f"   Total newspapers: {stats['total_newspapers']:,}")
#     click.echo(f"   Total pages: {stats['total_pages']:,}")
#     click.echo(f"   Downloaded pages: {stats['downloaded_pages']:,}")
#     click.echo(f"   Active sessions: {stats['active_sessions']}")
    
#     if stats['total_pages'] > 0:
#         progress = (stats['downloaded_pages'] / stats['total_pages']) * 100
#         click.echo(f"   Download progress: {progress:.1f}%")