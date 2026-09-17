# Metabase Dashboard Migrator

## Description

Metabase Dashboard Migrator is a Python command-line tool for copying a dashboard from one Metabase instance to another through the Metabase API.

It migrates the dashboard structure and layout together with its associated questions, including native SQL cards and Query Builder/MBQL cards. The tool can also preserve compatible parameters, filters, visualization settings, and references between saved questions.

Because Metabase table and field IDs are specific to each instance, the migrator attempts to remap database, table, collection, and field references between the source and target environments. This includes additional metadata resolution for MongoDB collections and Query Builder cards.

The tool supports:

- authentication with API keys or username and password;
- target database selection by ID or name;
- migration to a specific Metabase collection;
- optional creation of a nested target collection;
- MBQL table and field ID remapping;
- saved-question reference remapping;
- dry-run execution without writing to the target instance;
- validation of migrated card queries;
- diagnostic payload generation when a card cannot be created.

The migrator creates Metabase objects only. It does not create physical database tables or MongoDB collections in the target data source.

## Installation

### Requirements

- Python 3.9 or later;
- Git;
- API access to both the source and target Metabase instances;
- read permissions on the source instance;
- permission to create and update cards and dashboards on the target instance;
- the required target database tables or MongoDB collections already available and synchronized in Metabase.

Clone the repository:

```bash
git clone https://github.com/Calcagno-91/metabase-dashboard-migrator.git
cd metabase-dashboard-migrator
```

Create a virtual environment:

```bash
python -m venv .venv
```

Activate it on Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

Activate it on Windows Command Prompt:

```cmd
.venv\Scripts\activate.bat
```

Activate it on Linux or macOS:

```bash
source .venv/bin/activate
```

Install the dependencies:

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Verify the installation:

```bash
python migrate_metabase_dashboard.py --help
```
