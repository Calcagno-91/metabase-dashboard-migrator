# Metabase Dashboard Migrator

## Description

Metabase Dashboard Migrator is a Python command-line tool that copies a dashboard from one Metabase instance to another through the Metabase API.

It migrates the dashboard, its layout, and the associated cards/questions. It supports native SQL cards and Query Builder/MBQL cards, and attempts to remap database, table, MongoDB collection, and field references between the source and target environments.

Main features:

- API key or username/password authentication;
- target database selection by ID or name;
- migration to a specific Metabase collection;
- optional creation of a nested target collection;
- table and field ID remapping for MBQL/Query Builder cards;
- additional metadata resolution for MongoDB collections;
- remapping of references between saved questions;
- dashboard filter and layout migration;
- dry-run mode;
- post-migration query validation;
- diagnostic payload generation when a card cannot be created.

The tool creates Metabase objects only. It does not create physical database tables or MongoDB collections in the target data source.

## Installation

### Requirements

- Python 3.9 or later;
- Git;
- API access to both Metabase instances;
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

## Usage

The minimum required information is:

- source Metabase URL and credentials;
- target Metabase URL and credentials;
- source dashboard ID;
- target database ID or target database name.

A target collection is optional. If no target collection is specified, the dashboard and its cards are created in the Metabase root collection.

### 1. Run a dry run first

A dry run reads the source dashboard, builds the target payloads, and prints them without creating anything on the target instance.

Using API keys:

```bash
python migrate_metabase_dashboard.py \
  --source-url "https://metabase-source.example.com" \
  --source-api-key "mb_src_xxx" \
  --target-url "https://metabase-target.example.com" \
  --target-api-key "mb_tgt_xxx" \
  --dashboard-id 12401 \
  --target-database-id 65 \
  --target-collection-id 191 \
  --dry-run
```

Windows Command Prompt:

```cmd
python migrate_metabase_dashboard.py ^
  --source-url "https://metabase-source.example.com" ^
  --source-api-key "mb_src_xxx" ^
  --target-url "https://metabase-target.example.com" ^
  --target-api-key "mb_tgt_xxx" ^
  --dashboard-id 12401 ^
  --target-database-id 65 ^
  --target-collection-id 191 ^
  --dry-run
```

Windows PowerShell:

```powershell
python .\migrate_metabase_dashboard.py `
  --source-url "https://metabase-source.example.com" `
  --source-api-key "mb_src_xxx" `
  --target-url "https://metabase-target.example.com" `
  --target-api-key "mb_tgt_xxx" `
  --dashboard-id 12401 `
  --target-database-id 65 `
  --target-collection-id 191 `
  --dry-run
```

### 2. Run the migration

After checking the dry-run output, run the same command without `--dry-run`:

```bash
python migrate_metabase_dashboard.py \
  --source-url "https://metabase-source.example.com" \
  --source-api-key "mb_src_xxx" \
  --target-url "https://metabase-target.example.com" \
  --target-api-key "mb_tgt_xxx" \
  --dashboard-id 12401 \
  --target-database-id 65 \
  --target-collection-id 191
```

The tool will:

1. read the source dashboard;
2. read all associated cards;
3. resolve the target database;
4. build table and field mappings;
5. create the cards on the target instance;
6. remap references between saved questions;
7. create the dashboard and restore its layout;
8. test the migrated card queries.

At the end, it prints the new dashboard ID and URL.

### Authentication with username and password

```bash
python migrate_metabase_dashboard.py \
  --source-url "https://metabase-source.example.com" \
  --source-user "source-admin@example.com" \
  --source-password "SOURCE_PASSWORD" \
  --target-url "https://metabase-target.example.com" \
  --target-user "target-admin@example.com" \
  --target-password "TARGET_PASSWORD" \
  --dashboard-id 12401 \
  --target-database-id 65 \
  --target-collection-id 191
```

Do not commit real passwords or API keys to Git. Prefer environment variables, a local secrets manager, or interactive shell variables.

### Select the target database by name

Instead of `--target-database-id`, you can use the exact Metabase database name:

```bash
--target-database-name "MongoDB Production"
```

Example:

```bash
python migrate_metabase_dashboard.py \
  --source-url "https://metabase-source.example.com" \
  --source-api-key "mb_src_xxx" \
  --target-url "https://metabase-target.example.com" \
  --target-api-key "mb_tgt_xxx" \
  --dashboard-id 12401 \
  --target-database-name "MongoDB Production" \
  --target-collection-id 191
```

### Migrate directly into an existing collection

Use `--target-collection-id` when the destination collection already exists:

```bash
--target-collection-id 191
```

This places both the migrated dashboard and its cards directly in collection `191`.

`--target-parent-collection-id` is not the destination collection. It is used only when creating or locating a nested collection by name.

### Create or reuse a nested collection

To create or reuse a collection named `Migrated Dashboards` under parent collection `191`:

```bash
--target-collection-name "Migrated Dashboards" \
--target-parent-collection-id 191
```

Full example:

```bash
python migrate_metabase_dashboard.py \
  --source-url "https://metabase-source.example.com" \
  --source-api-key "mb_src_xxx" \
  --target-url "https://metabase-target.example.com" \
  --target-api-key "mb_tgt_xxx" \
  --dashboard-id 12401 \
  --target-database-id 65 \
  --target-collection-name "Migrated Dashboards" \
  --target-parent-collection-id 191
```

By default, the nested collection is created if it does not exist. Add `--no-create-target-collection` to fail instead of creating it.

### Add a suffix to migrated object names

```bash
--name-suffix " - PROD"
```

For example, `Prescriptions` becomes `Prescriptions - PROD`.

### Skip the post-migration query test

```bash
--skip-query-test
```

This skips calls to:

```text
POST /api/card/:id/query/json
```

The cards and dashboard are still created.

### Read legacy MBQL

For Metabase versions where legacy MBQL output is required:

```bash
--legacy-mbql
```

This reads source cards using `?legacy-mbql=true`.

### Disable table and field remapping

```bash
--no-remap-fields
```

Use this only when source and target Metabase IDs are intentionally identical. In normal cross-environment migrations, remapping should remain enabled.

### Specify the source database explicitly

The source database ID is normally inferred when all migrated cards use the same database. To force it:

```bash
--source-database-id 65
```

This is useful when inference is ambiguous or when a dashboard contains unusual card definitions.

### Main command-line options

| Option | Description |
|---|---|
| `--source-url` | Source Metabase base URL. |
| `--source-api-key` | Source API key. |
| `--source-user` | Source username, used with `--source-password`. |
| `--source-password` | Source password. |
| `--target-url` | Target Metabase base URL. |
| `--target-api-key` | Target API key. |
| `--target-user` | Target username, used with `--target-password`. |
| `--target-password` | Target password. |
| `--dashboard-id` | Source dashboard ID to migrate. |
| `--source-database-id` | Optional explicit source database ID. |
| `--target-database-id` | Target database ID. |
| `--target-database-name` | Target database name, alternative to the target database ID. |
| `--target-collection-id` | Existing destination collection ID. |
| `--target-collection-name` | Destination collection name to find or create. |
| `--target-parent-collection-id` | Parent ID used only with `--target-collection-name`. |
| `--no-create-target-collection` | Do not create a missing named collection. |
| `--name-suffix` | Suffix added to migrated dashboard and card names. |
| `--timeout` | API request timeout in seconds. Default: `60`. |
| `--dry-run` | Print payloads without creating target objects. |
| `--skip-query-test` | Skip the final card query tests. |
| `--legacy-mbql` | Request legacy MBQL from the source instance. |
| `--no-remap-fields` | Disable table and field ID remapping. |

### Important notes

- Tables and MongoDB collections must already exist in the target data source.
- The target Metabase database must be synchronized before migrating Query Builder/MBQL cards.
- Source and target collection/table/field names should match whenever possible.
- Complex Query Builder cards, Field Filters, and version differences between Metabase instances may require manual validation.
- If card creation fails, the tool writes a diagnostic file named `failed_card_payload_<source_card_id>.json` in the current directory.
- Diagnostic payload files can contain internal schema, table, field, or query information. Review them before sharing or committing them.
