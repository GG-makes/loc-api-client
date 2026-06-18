# LoC Chronicling America Client

A comprehensive Python client for the Library of Congress Chronicling America API, designed for systematic discovery, queuing, and downloading of historical newspaper archives.

## Overview

The **LoC Chronicling America Client** provides automated workflows for large-scale newspaper archive collection from the Library of Congress's Chronicling America digital collection. Built specifically for the LoC API, it handles the complexities of rate limiting, faceted search pagination, and systematic content discovery.

## Fork Maintenance Notes
![CI](https://github.com/GG-makes/loc-api-client/actions/workflows/ci.yml/badge.svg?branch=migration-hardening)

This fork contains maintenance and migration work to keep the project operational as external dependencies evolve.

### Chronicling America API Migration

In August 2025, the Library of Congress retired the legacy Chronicling America API endpoints used by this project and migrated search functionality to the `loc.gov` API platform.

This fork is updating the discovery and ingestion workflows to support the new API:

- replacing retired API endpoints
- adapting query generation to the new API model
- updating response parsing
- adding migration tests and validation
- improving maintainability through CI and documentation

Detailed technical notes are available in [MIGRATION.md](MIGRATION.md).

### Status

🚧 Migration in progress

Completed:
- Identified legacy API dependencies
- Investigated the impact of the API retirement on discovery workflows
- Added Windows compatibility fixes
- Began documenting migration decisions
- Investigated changed API behaviour and response formats


In progress:
- Centralising query construction logic to isolate API-specific concerns
- Expanding regression tests around discovery and ingestion workflows
- Integrating the new query construction approach
- Integrating the new query response ingestion approach

Planned:
- Remove legacy API-specific CLI commands and obsolete code paths
- Complete migration to the updated API
- Validate end-to-end ingestion workflows

Changes are being developed with the intention of contributing improvements upstream where appropriate.

## Key Features

### 🔍 **Automated Discovery & Queuing**
- **Systematic periodical discovery** - Automatically catalog all available newspapers
- **Intelligent facet creation** - Generate date ranges and state-based search facets
- **Priority-based queuing** - Smart download queue management with size limits
- **Complete workflow automation** - End-to-end setup from discovery to download

### 📥 **Robust Download Processing**
- **Queue-based downloads** - Process thousands of items systematically
- **Multiple file formats** - PDF, JP2 images, OCR text, and metadata
- **Organized file structure** - Hierarchical storage by LCCN/year/month
- **Resume capability** - Automatic retry of failed downloads
- **Progress tracking** - Real-time progress bars and statistics

### 🛡️ **API Compliance & Reliability**
- **Rate limiting compliance** - Respects LoC's 20 requests/minute limit
- **CAPTCHA/429 handling** - Automatic 1-hour pause on rate limit detection
- **Faceted search optimization** - Avoids deep paging limits (100,000+ results)
- **Error recovery** - Graceful handling of timeouts and network issues

### 📊 **Advanced Management**
- **Comprehensive statistics** - Track discovery progress and download metrics
- **Flexible filtering** - Download by priority, type, date range, or state
- **Database tracking** - SQLite-based metadata and progress storage
- **Interactive CLI** - Modular command-line interface with grouped commands
- **Memory efficiency** - Batch processing for handling large newspaper collections

## 🖥️ TUI Monitor - Real-Time Operations Dashboard

The **TUI Monitor** (`tui_monitor.py`) provides a comprehensive, real-time dashboard for monitoring and managing automated batch discovery and download processes.

### Key Features
- **🔄 Automated Process Management** - Automatically starts and manages batch discovery and download processes
- **📊 Live Progress Tracking** - Real-time updates for discovery progress, download completion, and queue status
- **⏱️ Intelligent Estimates** - Calculates realistic completion times based on current processing rates
- **🔧 Process Health Monitoring** - Tracks PIDs, uptime, restart counts, and error status
- **📈 Real-Time Statistics** - Live updates of discovered items, downloaded content, and total data size
- **🎯 Clean Interface** - Organized panels for Discovery, Downloads, Processes, Statistics, and Estimates

### Usage
```bash
# Launch TUI monitor with default paths
python tui_monitor.py

# Launch with custom database and download paths
python tui_monitor.py --db-path /custom/path/newsagger.db --downloads-dir /custom/downloads

# Exit anytime with Ctrl+C (gracefully stops all processes)
```

The TUI monitor automatically:
1. **Starts batch discovery** - Runs `discover-via-batches --auto-enqueue` in the background
2. **Starts download processor** - Runs `process-downloads --continuous` to handle the queue
3. **Monitors progress** - Updates every second with fresh database statistics
4. **Handles failures** - Automatically restarts failed processes with exponential backoff
5. **Provides ETAs** - Calculates realistic completion times based on current processing rates

### Display Panels
- **Discovery** - Batch progress (5/25), current batch status, and processing rate
- **Downloads** - Queue status, completion progress, and download rate  
- **Processes** - PID tracking, uptime, restart counts, and process health
- **Statistics** - Total batches, discovered items, queue size, downloaded count, and total data size
- **Estimates** - Realistic ETAs for discovery completion and download completion

**Perfect for long-running operations** - The TUI monitor is designed for multi-hour batch discovery and download operations, providing confidence that processes are running correctly and giving accurate progress updates.

## Quick Start

### 1. Setup Environment
```bash
# Clone and setup
git clone <repository-url>
cd newsagger
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Launch TUI Monitor (Recommended)
```bash
# Launch the comprehensive TUI monitor for automated operations
python tui_monitor.py

# Optional: specify custom paths
python tui_monitor.py --db-path /path/to/db --downloads-dir /path/to/downloads
```

The **TUI Monitor** provides real-time monitoring with:
- ✅ **Live progress tracking** for discovery and downloads
- ✅ **Process management** - automatically starts and manages background processes
- ✅ **Real-time statistics** - queue status, completion rates, and ETAs
- ✅ **Error monitoring** - process health and restart tracking
- ✅ **Estimate calculations** - completion time estimates for long-running operations

### 3. Alternative: Manual Command-Line Workflow
```bash
# Complete automated workflow for California newspapers (1906)
python main.py setup-download-workflow \
  --start-year 1906 --end-year 1906 \
  --states "California" \
  --auto-discover --auto-enqueue \
  --max-size-gb 1.0

# Process the download queue
python main.py process-downloads --max-items 10

# Monitor progress manually
python main.py discovery-status
python main.py show-queue --limit 10
python main.py download-stats
```

## Complete Workflow Examples

### 🏛️ **Large-Scale Historical Collection**
```bash
# 1. List all available newspapers with statistics
python main.py newspaper list-newspapers

# 2. Discover all available periodicals
python main.py discover --max-papers 1000

# 2. Create systematic date facets
python main.py create-facets --start-year 1900 --end-year 1920 --facet-size 1

# 3. Auto-discover content for all facets
python main.py auto-discover-facets --auto-enqueue --max-items 1000

# 4. Process high-priority downloads first
python main.py download-priority --priority 1 --max-items 50

# 5. Monitor progress
python main.py discovery-status

# 6. Check overall system status
python main.py newspaper status
```

### 📰 **Focused State/Event Collection**
```bash
# San Francisco earthquake coverage (1906)
python main.py setup-download-workflow \
  --start-year 1906 --end-year 1906 \
  --states "California" \
  --auto-discover --auto-enqueue \
  --max-size-gb 5.0

# WWI era coverage (1917-1919)  
python main.py setup-download-workflow \
  --start-year 1917 --end-year 1919 \
  --states "New York,Illinois,California" \
  --auto-discover --auto-enqueue \
  --max-size-gb 10.0
```

### 🔍 **Research-Focused Discovery**
```bash
# Test discovery with small dataset
python main.py test-discovery --year 1906 --max-items 20

# Search for specific newspapers
python main.py newspaper search-newspapers --state "California" --limit 10

# Search for specific content
python main.py search-text "earthquake" --date1 1906 --date2 1907 --limit 100

# Download specific newspaper with size estimation
python main.py newspaper download-newspaper sn84038012 --date1 1906 --date2 1906 --estimate-only

# Download specific newspaper after confirming size
python main.py newspaper download-newspaper sn84038012 --date1 1906 --date2 1906
```

## Advanced Commands

### 📰 **Newspaper Commands**
The CLI now features a modular command structure with dedicated newspaper operations:

- `newspaper list-newspapers` - List all available newspapers with statistics
  - Batch processing for memory efficiency with large datasets
  - Shows top states, languages, and date coverage
  ```bash
  python main.py newspaper list-newspapers
  ```

- `newspaper search-newspapers` - Search newspapers with filters
  - Filter by state, language, or limit results
  ```bash
  python main.py newspaper search-newspapers --state "California" --limit 20
  python main.py newspaper search-newspapers --language "Spanish" --limit 10
  ```

- `newspaper download-newspaper` - Download specific newspaper by LCCN
  - Size estimation and confirmation for large downloads
  - Progress tracking with session management
  ```bash
  python main.py newspaper download-newspaper sn84038012 --date1 1906 --date2 1906
  python main.py newspaper download-newspaper sn84038012 --estimate-only
  ```

- `newspaper status` - Comprehensive system status and statistics
  ```bash
  python main.py newspaper status
  ```

### Discovery & Planning
- `discover` - Catalog available periodicals from LoC
- `create-facets` - Create systematic date/state facets
- `auto-discover-facets` - Systematically discover content for all facets
- `test-discovery` - Test discovery with focused datasets
- `discovery-status` - Comprehensive discovery progress

### Queue Management
- `auto-enqueue` - Automatically queue discovered content
- `populate-queue` - Populate queue with priority settings
- `show-queue` - View queued items with filtering
- `list-facets` - View facet status and progress

### Download Processing
- `process-downloads` - Main download queue processor
- `download-page` - Download individual pages
- `download-priority` - Download by priority/type
- `resume-downloads` - Resume failed downloads
- `download-stats` - Comprehensive download statistics
- `cleanup-downloads` - Clean incomplete files

### Workflow Automation
- `setup-download-workflow` - Complete automated setup
- `status` - Overall system status
- `search-text` - Search content across archives

## Configuration

Environment variables (`.env` file):
```bash
# API Settings
REQUEST_DELAY=3.0          # Seconds between requests (min 3.0)
MAX_RETRIES=3              # API retry attempts

# Storage Settings  
DATABASE_PATH=./data/newsagger.db
DOWNLOAD_DIR=./downloads

# Logging
LOG_LEVEL=INFO             # DEBUG, INFO, WARNING, ERROR
```

## Project Structure

```
newsagger/
├── src/newsagger/
│   ├── api_client.py      # LoC API client with rate limiting
│   ├── discovery.py       # Automated discovery and queuing
│   ├── downloader.py      # Download queue processor
│   ├── processor.py       # API response processing
│   ├── storage.py         # SQLite database operations
│   ├── config.py          # Configuration management
│   ├── cli.py             # Command-line interface (legacy)
│   ├── cli_new.py         # Modular CLI with command groups
│   └── commands/          # Modular command implementations
│       ├── __init__.py
│       ├── newspaper.py   # Newspaper-specific commands
│       └── remaining.py   # Commands being refactored
├── tests/                 # Comprehensive test suite
├── downloads/             # Downloaded content (auto-created)
├── data/                  # Database storage (auto-created)
├── logs/                  # Process logs (auto-created)
├── main.py               # CLI entry point
└── tui_monitor.py        # Real-time TUI dashboard (RECOMMENDED)
```

## API Compliance

### Library of Congress Guidelines
- **Rate Limiting**: 20 requests/minute maximum
- **Deep Paging**: Limited to 100,000 items per query
- **Timeout Handling**: 1-hour pause on 429/CAPTCHA responses
- **Faceted Search**: Uses date/state facets to manage large result sets

### Best Practices
- Always use `--estimate-only` for large downloads
- Start with small test datasets (`test-discovery`)
- Monitor progress with `discovery-status` and `download-stats`
- Use priority downloads for time-sensitive research
- Set reasonable size limits with `--max-size-gb`

## Data Organization

### File Structure
```
downloads/
└── {lccn}/           # Library of Congress Control Number
    └── {year}/       # Publication year
        └── {month}/  # Publication month
            ├── {item_id}.pdf           # Page PDF
            ├── {item_id}.jp2           # Page image
            ├── {item_id}_ocr.txt       # OCR text
            └── {item_id}_metadata.json # Complete metadata
```

### Database Schema
- **Periodicals** - Newspaper metadata and discovery tracking
- **Search Facets** - Date/state facets with progress tracking  
- **Pages** - Individual page metadata and download status
- **Download Queue** - Priority-based download management
- **Sessions** - Progress tracking for bulk operations

## Testing

```bash
# Install test dependencies
pip install pytest pytest-mock

# Run comprehensive test suite
python -m pytest tests/ -v

# Test specific components
python -m pytest tests/test_downloader.py -v
python -m pytest tests/test_discovery_automation.py -v

# Run with coverage
pip install pytest-cov
python -m pytest --cov=src/newsagger tests/
```

## Contributing

This project focuses specifically on the Library of Congress Chronicling America API. Contributions should maintain compatibility with LoC's API structure and rate limiting requirements.

## License

Educational and research use. Respects Library of Congress terms of service and API guidelines.
