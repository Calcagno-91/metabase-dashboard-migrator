# Metabase Dashboard Migrator

Utility Python per migrare una singola dashboard tra due istanze Metabase tramite API, copiando:

- dashboard e layout delle dashcard;
- card native SQL;
- card Query Builder/MBQL;
- parametri, filtri e impostazioni di visualizzazione compatibili;
- riferimenti tra saved questions del tipo `{{#123-nome-card}}`;
- riferimenti interni a collection/tabelle e campi Metabase, compresi diversi casi MongoDB.

Lo script è pensato per migrazioni tra ambienti, ad esempio **collaudo → produzione**.

## Funzioni principali

- autenticazione con API key oppure username/password;
- selezione del database target per ID o nome;
- salvataggio diretto in una collection Metabase target;
- creazione opzionale di una collection figlia;
- rimappatura di `source-table`, `table_id`, `field_id` e riferimenti `['field', ID, ...]`;
- resolver aggiuntivo tramite `/api/table/:id/query_metadata` e `/api/field/:id`, utile per MongoDB e Query Builder;
- tentativi progressivi con payload più conservativi quando una card non viene accettata;
- dry-run senza scritture;
- test finale delle query create;
- generazione di `failed_card_payload_<ID>.json` in caso di errore.

## Requisiti

- Python 3.9 o superiore;
- accesso API alle due istanze Metabase;
- permessi di lettura sul source;
- permessi di creazione/modifica di card e dashboard sul target;
- database e collection/tabelle dati già presenti e sincronizzati nei metadata del target.

> Lo script crea oggetti Metabase. Non crea collection MongoDB o tabelle fisiche nel database target.

## Installazione

```bash
git clone https://github.com/Calcagno-91/metabase-dashboard-migrator.git
cd metabase-dashboard-migrator

python -m venv .venv
```

Linux/macOS:

```bash
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Verifica:

```bash
python migrate_metabase_dashboard.py --help
```

## Uso raccomandato con API key

Non inserire API key o password nel repository. Esportale come variabili d'ambiente.

### Linux/macOS

```bash
export METABASE_SOURCE_URL="https://metabase-coll.example.com"
export METABASE_SOURCE_API_KEY="mb_src_xxx"
export METABASE_TARGET_URL="https://metabase-prod.example.com"
export METABASE_TARGET_API_KEY="mb_tgt_yyy"

python migrate_metabase_dashboard.py \
  --source-url "$METABASE_SOURCE_URL" \
  --source-api-key "$METABASE_SOURCE_API_KEY" \
  --target-url "$METABASE_TARGET_URL" \
  --target-api-key "$METABASE_TARGET_API_KEY" \
  --dashboard-id 12401 \
  --target-database-id 65 \
  --target-collection-id 191
```

### Windows PowerShell

```powershell
$env:METABASE_SOURCE_URL = "https://metabase-coll.example.com"
$env:METABASE_SOURCE_API_KEY = "mb_src_xxx"
$env:METABASE_TARGET_URL = "https://metabase-prod.example.com"
$env:METABASE_TARGET_API_KEY = "mb_tgt_yyy"

python .\migrate_metabase_dashboard.py `
  --source-url $env:METABASE_SOURCE_URL `
  --source-api-key $env:METABASE_SOURCE_API_KEY `
  --target-url $env:METABASE_TARGET_URL `
  --target-api-key $env:METABASE_TARGET_API_KEY `
  --dashboard-id 12401 `
  --target-database-id 65 `
  --target-collection-id 191
```

## Uso con username e password

```bash
python migrate_metabase_dashboard.py \
  --source-url "https://metabase-coll.example.com" \
  --source-user "admin-source@example.com" \
  --source-password "PASSWORD_SOURCE" \
  --target-url "https://metabase-prod.example.com" \
  --target-user "admin-target@example.com" \
  --target-password "PASSWORD_TARGET" \
  --dashboard-id 12401 \
  --target-database-id 65 \
  --target-collection-id 191
```

L'API key è preferibile quando disponibile.

## Collection target: differenza importante

Per salvare direttamente dashboard e card nella collection Metabase con ID `191`, usare:

```bash
--target-collection-id 191
```

`--target-parent-collection-id` **non indica la destinazione finale**. Serve soltanto quando si cerca o si crea una collection figlia per nome.

Esempio: crea o trova `Prescrizioni` sotto la collection padre `191`:

```bash
--target-collection-name "Prescrizioni" \
--target-parent-collection-id 191
```

Il comando seguente è volutamente rifiutato, perché altrimenti la dashboard finirebbe nella root:

```bash
--target-parent-collection-id 191
```

## Dry-run prima della migrazione

Il dry-run legge source e target, costruisce le mappe e stampa i payload, ma non crea oggetti:

```bash
python migrate_metabase_dashboard.py \
  --source-url "$METABASE_SOURCE_URL" \
  --source-api-key "$METABASE_SOURCE_API_KEY" \
  --target-url "$METABASE_TARGET_URL" \
  --target-api-key "$METABASE_TARGET_API_KEY" \
  --dashboard-id 12401 \
  --target-database-id 65 \
  --target-collection-id 191 \
  --dry-run
```

## Opzioni principali

| Opzione | Significato |
|---|---|
| `--dashboard-id` | ID della dashboard sorgente |
| `--source-database-id` | ID database source; viene inferito se la dashboard ne usa uno solo |
| `--target-database-id` | ID database target |
| `--target-database-name` | Alternativa all'ID: nome esatto del database target |
| `--target-collection-id` | Collection Metabase finale in cui creare dashboard e card |
| `--target-collection-name` | Collection da cercare o creare per nome |
| `--target-parent-collection-id` | Padre della collection indicata con `--target-collection-name` |
| `--no-create-target-collection` | Non creare la collection per nome se manca |
| `--name-suffix` | Suffisso per dashboard e card, ad esempio ` - TEST` |
| `--dry-run` | Nessuna scrittura sul target |
| `--skip-query-test` | Non eseguire il test delle card create |
| `--legacy-mbql` | Richiede il formato MBQL legacy durante la lettura delle card |
| `--no-remap-fields` | Disabilita la rimappatura table/field; usare solo per diagnosi |
| `--timeout` | Timeout HTTP in secondi, predefinito `60` |

## Flusso della migrazione

Lo script esegue otto fasi:

1. legge dashboard e dashcard sorgenti;
2. legge le card sorgenti;
3. risolve il database target e costruisce le mappe metadata;
4. prepara i payload e risolve on-demand gli ID MBQL;
5. crea le card target;
6. aggiorna i riferimenti tra saved questions;
7. crea la dashboard e ricostruisce il layout;
8. esegue le query delle card target come verifica.

## MongoDB e card Query Builder

Per una card Query Builder, Metabase salva ID interni come:

```json
{
  "source-table": 1524,
  "breakout": [
    ["field", 23619, {"temporal-unit": "day"}]
  ]
}
```

Gli ID sono specifici dell'istanza Metabase. Lo script prova a trasformarli negli ID target usando:

1. corrispondenza esatta `schema + collection/table + field`;
2. corrispondenza univoca per nome collection e campo;
3. metadata diretti della table e del field;
4. nomi normalizzati, anche per differenze tra nome tecnico e display name.

Se la collection Mongo esiste ma non compare nei metadata Metabase target, eseguire prima una sincronizzazione dello schema dal pannello amministrativo del target.

È normale vedere `source=65, target=65` quando source e target sono due istanze Metabase differenti: gli ID dei database possono coincidere, mentre gli ID di table e field possono essere diversi.

## Risoluzione problemi

### Dashboard migrata nella root

Usare `--target-collection-id 191`, non `--target-parent-collection-id 191` da solo.

### `HTTP 500` su `POST /api/card`

Controllare nel log che gli ID siano stati rimappati:

```text
Rimappata table 1524 -> 2524
Rimappato field 23609 -> 33609
Rimappato field 23619 -> 33619
```

Se compare:

```text
ATTENZIONE: table ID non presenti nel target dopo remap
ATTENZIONE: field ID non presenti nel target dopo remap
```

verificare:

- sync metadata completato sul target;
- stesso nome tecnico della collection/tabella;
- stessi campi richiesti dalla card;
- permessi dell'utente/API key sul database target;
- eventuali duplicati di nomi che rendono ambiguo il mapping.

In caso di fallimento viene scritto:

```text
failed_card_payload_<ID>.json
```

Il file è escluso da Git tramite `.gitignore`, ma può contenere SQL, nomi di campi o informazioni interne: non pubblicarlo.

### `visualization_settings: Il valore deve essere una mappa`

La versione corrente garantisce che il payload minimo usi:

```json
{
  "visualization_settings": {},
  "parameters": []
}
```

### Migrazione parziale

Non è presente un rollback automatico. Se il processo si interrompe dopo avere creato alcune card, queste rimangono sul target. Durante i test è utile usare:

```bash
--name-suffix " - MIGRATION TEST"
```

ed eseguire la migrazione in una collection dedicata.

## Limiti noti

- una dashboard che usa più database source richiede un'estensione del mapping;
- Query Builder molto complessi, azioni e visualizzazioni particolari possono richiedere adattamenti;
- una seconda esecuzione crea nuovi oggetti e non aggiorna automaticamente quelli creati in precedenza;
- gli endpoint Metabase per le dashcard possono variare tra versioni; lo script prova più payload compatibili;
- il tool non copia permessi di collection, subscription, alert, cronologia, link pubblici o configurazioni di embedding esterne.

## Test locali

```bash
python -m py_compile migrate_metabase_dashboard.py
python -m unittest discover -s tests -v
```

La pipeline GitHub Actions esegue gli stessi controlli su Python 3.9 e 3.12.

## Pubblicazione su GitHub

### Metodo rapido con GitHub CLI

Dalla cartella del progetto:

```bash
git init
git add .
git commit -m "feat: add Metabase dashboard migrator"
git branch -M main

gh auth login
gh repo create Calcagno-91/metabase-dashboard-migrator \
  --private \
  --source . \
  --remote origin \
  --push
```

Per renderlo pubblico, sostituire `--private` con `--public` dopo avere verificato che non siano presenti credenziali o payload di debug.

### Metodo senza GitHub CLI

Creare da GitHub un repository vuoto chiamato `metabase-dashboard-migrator`, poi:

```bash
git init
git add .
git commit -m "feat: add Metabase dashboard migrator"
git branch -M main
git remote add origin https://github.com/Calcagno-91/metabase-dashboard-migrator.git
git push -u origin main
```

## Sicurezza

- non salvare API key e password nel codice, nel README o nei file versionati;
- non aggiungere `.env` al repository;
- controllare sempre `git diff --cached` prima del push;
- preferire una API key dedicata con i soli permessi necessari;
- revocare o ruotare immediatamente una chiave pubblicata per errore.
