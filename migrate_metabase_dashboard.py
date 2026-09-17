#!/usr/bin/env python3
"""
Migra una singola dashboard Metabase da un ambiente sorgente a uno target usando le API.

Funziona bene per:
- dashboard con card SQL native
- dashboard con filtri/template-tags SQL
- dashboard con Field Filter semplici, se lo schema sorgente e target hanno stessi schema/table/field name

Prova anche a rimappare alcuni ID MBQL/Query Builder, ma per card Query Builder molto complesse
è consigliato fare sempre un dry-run e testare le card in Metabase.

Dipendenze:
    pip install requests

Esempio con username/password:
    python migrate_metabase_dashboard.py \
      --source-url "https://metabase-coll.example.com" \
      --source-user "admin@example.com" \
      --source-password "xxx" \
      --target-url "https://metabase-prod.example.com" \
      --target-user "admin@example.com" \
      --target-password "yyy" \
      --dashboard-id 123 \
      --target-database-id 8 \
      --target-collection-id 15

Esempio con API key:
    python migrate_metabase_dashboard.py \
      --source-url "https://metabase-coll.example.com" \
      --source-api-key "mb_src_xxx" \
      --target-url "https://metabase-prod.example.com" \
      --target-api-key "mb_tgt_yyy" \
      --dashboard-id 123 \
      --target-database-name "DB Produzione" \
      --target-collection-id 15
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import requests


Json = Dict[str, Any]


class MetabaseApiError(RuntimeError):
    pass


@dataclass
class MetadataMaps:
    source_table_id_to_key: Dict[int, Tuple[Optional[str], str]]
    target_table_key_to_id: Dict[Tuple[Optional[str], str], int]
    source_field_id_to_key: Dict[int, Tuple[Optional[str], str, str]]
    target_field_key_to_id: Dict[Tuple[Optional[str], str, str], int]

    # Fallback utili soprattutto per MongoDB: tra ambienti diversi lo schema/database
    # logico può cambiare, ma collection e campi possono avere lo stesso nome.
    target_table_name_to_id: Dict[str, int]
    target_field_loose_key_to_id: Dict[Tuple[str, str], int]
    target_table_ids: Set[int]
    target_field_ids: Set[int]

    # Mappe dirette create on-demand per Query Builder/MBQL Mongo.
    # Servono quando /api/database/:id/metadata non contiene la table/field sorgente
    # oppure quando lo schema Mongo cambia tra ambienti ma la collection è la stessa.
    source_table_id_to_target_id: Dict[int, int]
    source_field_id_to_target_id: Dict[int, int]

    def map_table_id(self, old_id: Any) -> Any:
        if not isinstance(old_id, int):
            return old_id

        direct = self.source_table_id_to_target_id.get(old_id)
        if direct is not None:
            return direct

        key = self.source_table_id_to_key.get(old_id)
        if not key:
            return old_id

        # 1) Match esatto: (schema, table_name)
        mapped = self.target_table_key_to_id.get(key)
        if mapped is not None:
            return mapped

        # 2) Fallback Mongo/cross-env: solo table/collection name, se univoco.
        _, table_name = key
        mapped = self.target_table_name_to_id.get(normalize_name(table_name))
        return mapped if mapped is not None else old_id

    def map_field_id(self, old_id: Any) -> Any:
        if not isinstance(old_id, int):
            return old_id

        direct = self.source_field_id_to_target_id.get(old_id)
        if direct is not None:
            return direct

        key = self.source_field_id_to_key.get(old_id)
        if not key:
            return old_id

        # 1) Match esatto: (schema, table_name, field_name)
        mapped = self.target_field_key_to_id.get(key)
        if mapped is not None:
            return mapped

        # 2) Fallback Mongo/cross-env: (table/collection name, field name), se univoco.
        _, table_name, field_name = key
        mapped = self.target_field_loose_key_to_id.get(
            (normalize_name(table_name), normalize_name(field_name))
        )
        return mapped if mapped is not None else old_id


class MetabaseClient:
    def __init__(
        self,
        base_url: str,
        username: Optional[str] = None,
        password: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: int = 60,
    ):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.api_key = api_key
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})

        if api_key:
            self.session.headers.update({"X-API-Key": api_key})
        else:
            if not username or not password:
                raise ValueError("Servono username/password oppure api_key")
            self.login()

    def login(self) -> None:
        response = self.session.post(
            f"{self.base_url}/api/session",
            json={"username": self.username, "password": self.password},
            timeout=self.timeout,
        )
        self._raise_for_status(response, "POST /api/session")
        session_id = response.json()["id"]
        self.session.headers.update({"X-Metabase-Session": session_id})

    def _raise_for_status(self, response: requests.Response, label: str) -> None:
        if 200 <= response.status_code < 300:
            return
        try:
            details = response.json()
        except Exception:
            details = response.text
        raise MetabaseApiError(
            f"Errore Metabase su {label}: HTTP {response.status_code}\n{details}"
        )

    def get(self, path: str, params: Optional[Json] = None) -> Any:
        response = self.session.get(
            f"{self.base_url}{path}", params=params, timeout=self.timeout
        )
        self._raise_for_status(response, f"GET {path}")
        return response.json()

    def post(self, path: str, payload: Json) -> Any:
        response = self.session.post(
            f"{self.base_url}{path}", json=payload, timeout=self.timeout
        )
        self._raise_for_status(response, f"POST {path}")
        return response.json()

    def put(self, path: str, payload: Any) -> Any:
        response = self.session.put(
            f"{self.base_url}{path}", json=payload, timeout=self.timeout
        )
        self._raise_for_status(response, f"PUT {path}")
        if response.text.strip():
            return response.json()
        return None

    def try_put(self, path: str, payload: Any) -> Tuple[bool, Any, Optional[str]]:
        response = self.session.put(
            f"{self.base_url}{path}", json=payload, timeout=self.timeout
        )
        if 200 <= response.status_code < 300:
            if response.text.strip():
                try:
                    return True, response.json(), None
                except Exception:
                    return True, response.text, None
            return True, None, None
        try:
            details = response.json()
        except Exception:
            details = response.text
        return False, None, f"HTTP {response.status_code}: {details}"


READ_ONLY_CARD_KEYS = {
    "id",
    "entity_id",
    "created_at",
    "updated_at",
    "creator",
    "creator_id",
    "database_id",
    "table_id",
    "result_metadata",
    "metadata_checksum",
    "last_query_start",
    "average_query_time",
    "query_type",
    "can_write",
    "can_restore",
    "can_delete",
    "dashboard_count",
    "dashboard_id",
    "dashboards",
    "collection",
    "collection_preview",
    "moderated_status",
    "last-edit-info",
    "last_used_at",
    "view_count",
    "initially_published_at",
    "made_public_by_id",
    "public_uuid",
    "embedding_params",
}

CARD_PAYLOAD_KEYS = {
    "name",
    "description",
    "display",
    "dataset_query",
    "visualization_settings",
    "parameters",
    "type",
    "collection_id",
    "cache_ttl",
    "archived",
    "enable_embedding",
}

DASHBOARD_PAYLOAD_KEYS = {
    "name",
    "description",
    "parameters",
    "collection_id",
    "cache_ttl",
    "archived",
    "enable_embedding",
    "embedding_params",
    "auto_apply_filters",
}

DASHCARD_KEYS = {
    "id",
    "card_id",
    "row",
    "col",
    "size_x",
    "size_y",
    "parameter_mappings",
    "visualization_settings",
    "series",
    "dashboard_tab_id",
    "inline_parameters",
    "action_id",
}

TABLE_ID_KEYS = {"source-table", "table_id", "table-id"}
FIELD_ID_KEYS = {"field_id", "field-id", "source-field", "source_field"}


METABASE_CARD_REF_RE = re.compile(r"\{\{#(\d+)([^}]*)\}\}")


def pretty_json(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False, sort_keys=False)


def dashboard_cards(dashboard: Json) -> List[Json]:
    cards = dashboard.get("dashcards")
    if cards is None:
        cards = dashboard.get("ordered_cards")
    if cards is None:
        cards = dashboard.get("cards")
    return cards or []


def extract_dataset_database_ids(cards: Iterable[Json]) -> List[int]:
    ids = set()
    for card in cards:
        dq = card.get("dataset_query") or {}
        database_id = dq.get("database")
        if isinstance(database_id, int):
            ids.add(database_id)
    return sorted(ids)


def resolve_database_id(client: MetabaseClient, database_id: Optional[int], database_name: Optional[str]) -> int:
    if database_id:
        return database_id
    if not database_name:
        raise ValueError("Specifica --target-database-id oppure --target-database-name")

    data = client.get("/api/database")
    databases = data.get("data") if isinstance(data, dict) else data
    databases = databases or []

    matches = [db for db in databases if db.get("name") == database_name]
    if not matches:
        available = ", ".join(db.get("name", "?") for db in databases)
        raise ValueError(
            f"Database target non trovato: {database_name!r}. Disponibili: {available}"
        )
    if len(matches) > 1:
        raise ValueError(f"Trovati più database target con nome {database_name!r}; usa --target-database-id")
    return int(matches[0]["id"])


def collection_items(payload: Any) -> List[Json]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        if isinstance(payload.get("data"), list):
            return payload["data"]
        if isinstance(payload.get("collections"), list):
            return payload["collections"]
    return []


def resolve_collection_id(
    client: MetabaseClient,
    collection_id: Optional[int],
    collection_name: Optional[str],
    parent_collection_id: Optional[int],
    create_if_missing: bool,
) -> Optional[int]:
    """
    Determina la collection target.
    - Se passi --target-collection-id, usa direttamente quella.
    - Se passi --target-collection-name, la cerca per nome nel target.
    - Se non la trova e create_if_missing=True, la crea.
    - Se non passi nulla, salva nella collection root/default.
    """
    if collection_id is not None:
        return collection_id

    if not collection_name:
        return None

    payload = client.get("/api/collection")
    collections = collection_items(payload)

    matches = []
    for collection in collections:
        if collection.get("name") != collection_name:
            continue
        if parent_collection_id is not None and collection.get("parent_id") != parent_collection_id:
            continue
        matches.append(collection)

    if len(matches) == 1:
        resolved_id = int(matches[0]["id"])
        print(f"      Collection target trovata: {collection_name!r} -> ID {resolved_id}")
        return resolved_id

    if len(matches) > 1:
        raise ValueError(
            f"Trovate più collection target con nome {collection_name!r}. Usa --target-collection-id."
        )

    if not create_if_missing:
        raise ValueError(
            f"Collection target {collection_name!r} non trovata. Rimuovi --no-create-target-collection oppure usa --target-collection-id."
        )

    created = client.post(
        "/api/collection",
        {
            "name": collection_name,
            "parent_id": parent_collection_id,
        },
    )
    resolved_id = int(created["id"])
    print(f"      Collection target creata: {collection_name!r} -> ID {resolved_id}")
    return resolved_id


def metadata_tables(metadata: Any) -> List[Json]:
    if isinstance(metadata, dict):
        if isinstance(metadata.get("tables"), list):
            return metadata["tables"]
        if isinstance(metadata.get("data"), dict) and isinstance(metadata["data"].get("tables"), list):
            return metadata["data"]["tables"]
    return []


def normalize_schema(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    return str(value)


def normalize_name(value: Any) -> str:
    return str(value or "").strip().lower()


def add_unique(mapping: Dict[Any, Any], duplicate_keys: Set[Any], key: Any, value: Any) -> None:
    """Mantiene il valore solo se la chiave è univoca; se duplicata la disabilita."""
    if key in duplicate_keys:
        return
    if key in mapping and mapping[key] != value:
        mapping.pop(key, None)
        duplicate_keys.add(key)
        return
    mapping[key] = value


def compact_norm(value: Any) -> str:
    """Normalizzazione più aggressiva per confrontare nomi Mongo/Metabase."""
    return re.sub(r"[^a-z0-9]+", "", normalize_name(value))


def table_name_candidates(table: Json) -> List[str]:
    candidates: List[str] = []
    for key in ("name", "display_name", "schema_name"):
        value = table.get(key)
        if isinstance(value, str) and value.strip():
            candidates.append(value.strip())
    # Dedup preservando ordine
    seen: Set[str] = set()
    out: List[str] = []
    for value in candidates:
        norm = normalize_name(value)
        if norm not in seen:
            out.append(value)
            seen.add(norm)
    return out


def field_name_candidates(field: Json) -> List[str]:
    candidates: List[str] = []
    for key in ("name", "display_name"):
        value = field.get(key)
        if isinstance(value, str) and value.strip():
            candidates.append(value.strip())
    seen: Set[str] = set()
    out: List[str] = []
    for value in candidates:
        # inserisco sia normalizzazione semplice sia compatta, ma ritorno il valore originale
        norm = normalize_name(value)
        if norm not in seen:
            out.append(value)
            seen.add(norm)
    return out


def flatten_fields(fields: Any) -> List[Json]:
    """Appiattisce eventuali nested fields ritornati da query_metadata."""
    out: List[Json] = []
    if not isinstance(fields, list):
        return out
    for field in fields:
        if not isinstance(field, dict):
            continue
        out.append(field)
        for nested_key in ("fields", "nested_fields", "fk_target_field"):
            nested = field.get(nested_key)
            if isinstance(nested, list):
                out.extend(flatten_fields(nested))
            elif isinstance(nested, dict):
                out.extend(flatten_fields([nested]))
    return out


def get_table_query_metadata(client: MetabaseClient, table_id: int) -> Json:
    """Legge la table direttamente: più affidabile di /api/database/:id/metadata per alcune Mongo collection."""
    try:
        data = client.get(f"/api/table/{table_id}/query_metadata")
        return data if isinstance(data, dict) else {}
    except Exception:
        data = client.get(f"/api/table/{table_id}")
        return data if isinstance(data, dict) else {}


def get_field_metadata(client: MetabaseClient, field_id: int) -> Json:
    data = client.get(f"/api/field/{field_id}")
    return data if isinstance(data, dict) else {}


def build_field_index_for_table(table_meta: Json) -> Tuple[Dict[str, int], Dict[str, int]]:
    """Ritorna due indici univoci: normalize_name -> field_id e compact_norm -> field_id."""
    by_norm: Dict[str, int] = {}
    dup_norm: Set[str] = set()
    by_compact: Dict[str, int] = {}
    dup_compact: Set[str] = set()

    for field in flatten_fields(table_meta.get("fields") or []):
        field_id = field.get("id")
        if not isinstance(field_id, int):
            continue
        for candidate in field_name_candidates(field):
            add_unique(by_norm, dup_norm, normalize_name(candidate), field_id)
            add_unique(by_compact, dup_compact, compact_norm(candidate), field_id)
    return by_norm, by_compact


def find_target_table_id_for_source_table(maps: MetadataMaps, source_table_meta: Json) -> Optional[int]:
    schema = normalize_schema(source_table_meta.get("schema"))

    # 1) Match esatto per schema + nome/display_name
    for candidate in table_name_candidates(source_table_meta):
        for key in ((schema, candidate), (schema, str(candidate))):
            mapped = maps.target_table_key_to_id.get(key)
            if mapped is not None:
                return mapped

    # 2) Match per nome o display_name, se univoco nel DB target
    for candidate in table_name_candidates(source_table_meta):
        mapped = maps.target_table_name_to_id.get(normalize_name(candidate))
        if mapped is not None:
            return mapped

    # 3) Match compatto: utile per differenze tipo DATA_PRESCRIZIONE vs Data Prescrizione
    source_compacts = {compact_norm(candidate) for candidate in table_name_candidates(source_table_meta)}
    matches = []
    for target_name_norm, table_id in maps.target_table_name_to_id.items():
        if compact_norm(target_name_norm) in source_compacts:
            matches.append(table_id)
    matches = sorted(set(matches))
    if len(matches) == 1:
        return matches[0]

    return None


def index_target_table_details(maps: MetadataMaps, target_table_meta: Json) -> None:
    target_table_id = target_table_meta.get("id")
    if not isinstance(target_table_id, int):
        return

    schema = normalize_schema(target_table_meta.get("schema"))
    for table_candidate in table_name_candidates(target_table_meta):
        maps.target_table_key_to_id[(schema, table_candidate)] = target_table_id
        # qui non uso add_unique: se siamo arrivati qui, la tabella è già stata scelta come target.
        maps.target_table_name_to_id.setdefault(normalize_name(table_candidate), target_table_id)

    maps.target_table_ids.add(target_table_id)

    for field in flatten_fields(target_table_meta.get("fields") or []):
        field_id = field.get("id")
        if not isinstance(field_id, int):
            continue
        maps.target_field_ids.add(field_id)
        for table_candidate in table_name_candidates(target_table_meta):
            for field_candidate in field_name_candidates(field):
                maps.target_field_loose_key_to_id.setdefault(
                    (normalize_name(table_candidate), normalize_name(field_candidate)),
                    field_id,
                )


def prime_metadata_maps_for_refs(
    source: MetabaseClient,
    target: MetabaseClient,
    maps: MetadataMaps,
    refs: Dict[str, Set[int]],
) -> None:
    """
    Espande le mappe leggendo direttamente table/field API.
    Questo risolve il caso Query Builder Mongo in cui il DB metadata globale non contiene
    gli ID sorgente usati nel payload MBQL.
    """
    source_table_cache: Dict[int, Json] = {}
    target_table_cache: Dict[int, Json] = {}

    def ensure_table(source_table_id: int) -> Optional[int]:
        existing = maps.source_table_id_to_target_id.get(source_table_id)
        if existing is not None:
            return existing

        # Se la vecchia funzione sa già mapparla, usala.
        mapped_existing = maps.map_table_id(source_table_id)
        if isinstance(mapped_existing, int) and mapped_existing != source_table_id:
            maps.source_table_id_to_target_id[source_table_id] = mapped_existing
            return mapped_existing

        try:
            source_table_meta = source_table_cache.get(source_table_id)
            if source_table_meta is None:
                source_table_meta = get_table_query_metadata(source, source_table_id)
                source_table_cache[source_table_id] = source_table_meta
        except Exception as exc:
            print(f"        Non riesco a leggere table sorgente {source_table_id}: {exc}")
            return None

        source_names = table_name_candidates(source_table_meta)
        if not source_names:
            print(f"        Table sorgente {source_table_id}: nome non trovato nei metadata")
            return None

        source_schema = normalize_schema(source_table_meta.get("schema"))
        source_table_name = source_names[0]
        maps.source_table_id_to_key[source_table_id] = (source_schema, source_table_name)

        target_table_id = find_target_table_id_for_source_table(maps, source_table_meta)
        if target_table_id is None:
            print(
                f"        Table sorgente {source_table_id} ({source_names}) non trovata nel target "
                "per nome/display_name."
            )
            return None

        maps.source_table_id_to_target_id[source_table_id] = target_table_id
        print(f"        Rimappata table {source_table_id} -> {target_table_id} ({source_table_name})")

        try:
            target_table_meta = target_table_cache.get(target_table_id)
            if target_table_meta is None:
                target_table_meta = get_table_query_metadata(target, target_table_id)
                target_table_cache[target_table_id] = target_table_meta
            index_target_table_details(maps, target_table_meta)
        except Exception as exc:
            print(f"        Non riesco a leggere table target {target_table_id}: {exc}")
            target_table_meta = {}

        target_by_norm, target_by_compact = build_field_index_for_table(target_table_meta)

        # Mappa direttamente i campi della table sorgente verso quelli della table target scelta.
        for source_field in flatten_fields(source_table_meta.get("fields") or []):
            source_field_id = source_field.get("id")
            if not isinstance(source_field_id, int):
                continue
            maps.source_field_id_to_key[source_field_id] = (
                source_schema,
                source_table_name,
                str(source_field.get("name") or source_field.get("display_name") or source_field_id),
            )
            target_field_id = None
            for candidate in field_name_candidates(source_field):
                target_field_id = target_by_norm.get(normalize_name(candidate))
                if target_field_id is None:
                    target_field_id = target_by_compact.get(compact_norm(candidate))
                if target_field_id is not None:
                    break
            if target_field_id is not None:
                maps.source_field_id_to_target_id[source_field_id] = target_field_id

        return target_table_id

    # Prima mappo le table dichiarate nel MBQL.
    for source_table_id in sorted(refs.get("tables") or []):
        ensure_table(source_table_id)

    # Poi mappo eventuali field rimasti non risolti leggendo /api/field/:id.
    for source_field_id in sorted(refs.get("fields") or []):
        mapped_field = maps.map_field_id(source_field_id)
        if isinstance(mapped_field, int) and mapped_field != source_field_id:
            maps.source_field_id_to_target_id[source_field_id] = mapped_field
            continue

        try:
            source_field_meta = get_field_metadata(source, source_field_id)
        except Exception as exc:
            print(f"        Non riesco a leggere field sorgente {source_field_id}: {exc}")
            continue

        source_table_id = source_field_meta.get("table_id")
        target_table_id = ensure_table(source_table_id) if isinstance(source_table_id, int) else None
        if target_table_id is None:
            continue

        target_table_meta = target_table_cache.get(target_table_id)
        if target_table_meta is None:
            try:
                target_table_meta = get_table_query_metadata(target, target_table_id)
                target_table_cache[target_table_id] = target_table_meta
                index_target_table_details(maps, target_table_meta)
            except Exception as exc:
                print(f"        Non riesco a leggere table target {target_table_id}: {exc}")
                continue

        target_by_norm, target_by_compact = build_field_index_for_table(target_table_meta)
        target_field_id = None
        for candidate in field_name_candidates(source_field_meta):
            target_field_id = target_by_norm.get(normalize_name(candidate))
            if target_field_id is None:
                target_field_id = target_by_compact.get(compact_norm(candidate))
            if target_field_id is not None:
                break

        if target_field_id is not None:
            maps.source_field_id_to_target_id[source_field_id] = target_field_id
            print(f"        Rimappato field {source_field_id} -> {target_field_id} ({source_field_meta.get('name')})")
        else:
            print(
                f"        Field sorgente {source_field_id} ({field_name_candidates(source_field_meta)}) "
                f"non trovato nella table target {target_table_id}."
            )


def build_metadata_maps(
    source: MetabaseClient,
    target: MetabaseClient,
    source_database_id: int,
    target_database_id: int,
) -> MetadataMaps:
    src_meta = source.get(f"/api/database/{source_database_id}/metadata")
    tgt_meta = target.get(f"/api/database/{target_database_id}/metadata")

    src_table_id_to_key: Dict[int, Tuple[Optional[str], str]] = {}
    tgt_table_key_to_id: Dict[Tuple[Optional[str], str], int] = {}
    src_field_id_to_key: Dict[int, Tuple[Optional[str], str, str]] = {}
    tgt_field_key_to_id: Dict[Tuple[Optional[str], str, str], int] = {}

    target_table_name_to_id: Dict[str, int] = {}
    target_table_name_duplicates: Set[str] = set()
    target_field_loose_key_to_id: Dict[Tuple[str, str], int] = {}
    target_field_loose_duplicates: Set[Tuple[str, str]] = set()
    target_table_ids: Set[int] = set()
    target_field_ids: Set[int] = set()

    for table in metadata_tables(src_meta):
        table_id = table.get("id")
        table_name = table.get("name")
        schema = normalize_schema(table.get("schema"))
        if isinstance(table_id, int) and table_name:
            table_key = (schema, str(table_name))
            src_table_id_to_key[table_id] = table_key
            for field in table.get("fields") or []:
                field_id = field.get("id")
                field_name = field.get("name")
                if isinstance(field_id, int) and field_name:
                    src_field_id_to_key[field_id] = (schema, str(table_name), str(field_name))

    for table in metadata_tables(tgt_meta):
        table_id = table.get("id")
        table_name = table.get("name")
        schema = normalize_schema(table.get("schema"))
        if isinstance(table_id, int) and table_name:
            target_table_ids.add(table_id)
            table_key = (schema, str(table_name))
            tgt_table_key_to_id[table_key] = table_id

            for candidate_name in table_name_candidates(table):
                add_unique(
                    target_table_name_to_id,
                    target_table_name_duplicates,
                    normalize_name(candidate_name),
                    table_id,
                )

            for field in table.get("fields") or []:
                field_id = field.get("id")
                field_name = field.get("name")
                if isinstance(field_id, int) and field_name:
                    target_field_ids.add(field_id)
                    tgt_field_key_to_id[(schema, str(table_name), str(field_name))] = field_id
                    for table_candidate in table_name_candidates(table):
                        for field_candidate in field_name_candidates(field):
                            add_unique(
                                target_field_loose_key_to_id,
                                target_field_loose_duplicates,
                                (normalize_name(table_candidate), normalize_name(field_candidate)),
                                field_id,
                            )

    return MetadataMaps(
        source_table_id_to_key=src_table_id_to_key,
        target_table_key_to_id=tgt_table_key_to_id,
        source_field_id_to_key=src_field_id_to_key,
        target_field_key_to_id=tgt_field_key_to_id,
        target_table_name_to_id=target_table_name_to_id,
        target_field_loose_key_to_id=target_field_loose_key_to_id,
        target_table_ids=target_table_ids,
        target_field_ids=target_field_ids,
        source_table_id_to_target_id={},
        source_field_id_to_target_id={},
    )


def remap_mbql_like_object(value: Any, maps: Optional[MetadataMaps]) -> Any:
    """
    Rimappa in modo prudente riferimenti MBQL noti:
    - ["field", 123, ...]
    - chiavi source-table/table_id
    - chiavi source-field/field_id

    In più gestisce alcune stringhe JSON presenti nei visualization_settings,
    ad esempio le chiavi di column_settings del tipo:
    "[\"ref\",[\"field\",123,null]]".
    """
    if maps is None:
        return value

    if isinstance(value, list):
        if len(value) >= 2 and value[0] == "field" and isinstance(value[1], int):
            new_list = [value[0], maps.map_field_id(value[1])]
            new_list.extend(remap_mbql_like_object(item, maps) for item in value[2:])
            return new_list
        return [remap_mbql_like_object(item, maps) for item in value]

    if isinstance(value, dict):
        out: Json = {}
        for key, item in value.items():
            new_key = key

            # Alcuni settings Metabase usano chiavi stringa che sono JSON MBQL.
            if isinstance(key, str) and key.startswith("["):
                try:
                    decoded_key = json.loads(key)
                    remapped_key = remap_mbql_like_object(decoded_key, maps)
                    new_key = json.dumps(remapped_key, ensure_ascii=False, separators=(",", ":"))
                except Exception:
                    new_key = key

            if key in TABLE_ID_KEYS and isinstance(item, int):
                out[new_key] = maps.map_table_id(item)
            elif key in FIELD_ID_KEYS and isinstance(item, int):
                out[new_key] = maps.map_field_id(item)
            else:
                out[new_key] = remap_mbql_like_object(item, maps)
        return out

    return value


def remap_dataset_query(
    dataset_query: Json,
    source_database_id: Optional[int],
    target_database_id: int,
    maps: Optional[MetadataMaps],
) -> Json:
    dq = copy.deepcopy(dataset_query or {})

    if "database" in dq:
        # Se source_database_id non è esplicito, sostituisce comunque il database della card.
        if source_database_id is None or dq.get("database") == source_database_id:
            dq["database"] = target_database_id

    dq = remap_mbql_like_object(dq, maps)
    return dq


def remap_metabase_card_refs_in_sql(query: str, old_to_new_card_id: Dict[int, int]) -> str:
    def replace(match: re.Match[str]) -> str:
        old_id = int(match.group(1))
        suffix = match.group(2) or ""
        new_id = old_to_new_card_id.get(old_id)
        if not new_id:
            return match.group(0)
        return "{{#" + str(new_id) + suffix + "}}"

    return METABASE_CARD_REF_RE.sub(replace, query)


def remap_card_references_after_creation(card_payload: Json, old_to_new_card_id: Dict[int, int]) -> Json:
    """
    Rimappa riferimenti a saved questions usati dentro SQL nativo, es. {{#123-nome-card}}.
    Funziona solo se la card referenziata è tra quelle già migrate.
    """
    payload = copy.deepcopy(card_payload)
    dq = payload.get("dataset_query") or {}
    native = dq.get("native") or {}
    query = native.get("query")
    if isinstance(query, str):
        new_query = remap_metabase_card_refs_in_sql(query, old_to_new_card_id)
        if new_query != query:
            native["query"] = new_query
            dq["native"] = native
            payload["dataset_query"] = dq
    return payload


def sanitize_card_payload(
    card: Json,
    target_database_id: int,
    target_collection_id: Optional[int],
    source_database_id: Optional[int],
    maps: Optional[MetadataMaps],
    name_suffix: str = "",
) -> Json:
    payload: Json = {}

    for key, value in card.items():
        if key in READ_ONLY_CARD_KEYS:
            continue
        if key in CARD_PAYLOAD_KEYS:
            payload[key] = copy.deepcopy(value)

    payload["name"] = str(payload.get("name") or card.get("name") or "Card migrata") + name_suffix
    payload["display"] = payload.get("display") or "table"
    payload["type"] = payload.get("type") or "question"
    payload["description"] = payload.get("description") or None
    payload["visualization_settings"] = payload.get("visualization_settings") or {}
    payload["parameters"] = payload.get("parameters") or []
    payload["collection_id"] = target_collection_id

    payload["dataset_query"] = remap_dataset_query(
        payload.get("dataset_query") or {},
        source_database_id=source_database_id,
        target_database_id=target_database_id,
        maps=maps,
    )

    # Anche parameters/template-tags possono contenere riferimenti field/table.
    payload["parameters"] = remap_mbql_like_object(payload.get("parameters") or [], maps)
    payload["visualization_settings"] = remap_mbql_like_object(
        payload.get("visualization_settings") or {}, maps
    )

    return payload


def sanitize_dashboard_payload(
    dashboard: Json,
    target_collection_id: Optional[int],
    name_suffix: str = "",
) -> Json:
    payload: Json = {}
    for key, value in dashboard.items():
        if key in DASHBOARD_PAYLOAD_KEYS:
            payload[key] = copy.deepcopy(value)

    payload["name"] = str(payload.get("name") or dashboard.get("name") or "Dashboard migrata") + name_suffix
    payload["description"] = payload.get("description") or None
    payload["parameters"] = payload.get("parameters") or []
    payload["collection_id"] = target_collection_id
    return payload


def sanitize_dashcard(
    dashcard: Json,
    temp_id: int,
    old_to_new_card_id: Dict[int, int],
    maps: Optional[MetadataMaps],
    dashboard_tab_id_map: Optional[Dict[int, int]] = None,
) -> Json:
    out: Json = {}

    for key in DASHCARD_KEYS:
        if key in dashcard:
            out[key] = copy.deepcopy(dashcard[key])

    # Per endpoint bulk: id negativo = nuova dashcard.
    out["id"] = temp_id

    old_card_id = dashcard.get("card_id")
    if old_card_id is not None:
        out["card_id"] = old_to_new_card_id.get(old_card_id)
    else:
        # Text/header/link/iframe cards possono non avere card_id.
        out["card_id"] = None

    for required_key in ["row", "col", "size_x", "size_y"]:
        if required_key not in out and required_key in dashcard:
            out[required_key] = dashcard[required_key]

    out["parameter_mappings"] = remap_mbql_like_object(
        out.get("parameter_mappings") or [], maps
    )
    for mapping in out["parameter_mappings"]:
        if isinstance(mapping, dict):
            mapped_card_id = mapping.get("card_id")
            if mapped_card_id in old_to_new_card_id:
                mapping["card_id"] = old_to_new_card_id[mapped_card_id]

    out["visualization_settings"] = remap_mbql_like_object(
        out.get("visualization_settings") or {}, maps
    )

    if "series" in out and isinstance(out["series"], list):
        for series_item in out["series"]:
            if isinstance(series_item, dict):
                sid = series_item.get("card_id") or series_item.get("id")
                if sid in old_to_new_card_id:
                    if "card_id" in series_item:
                        series_item["card_id"] = old_to_new_card_id[sid]
                    if "id" in series_item:
                        series_item["id"] = old_to_new_card_id[sid]

    if dashboard_tab_id_map and isinstance(out.get("dashboard_tab_id"), int):
        out["dashboard_tab_id"] = dashboard_tab_id_map.get(out["dashboard_tab_id"], out["dashboard_tab_id"])

    return out


def collect_mbql_refs(value: Any, refs: Optional[Dict[str, Set[int]]] = None) -> Dict[str, Set[int]]:
    if refs is None:
        refs = {"tables": set(), "fields": set()}

    if isinstance(value, dict):
        for key, item in value.items():
            if key in TABLE_ID_KEYS and isinstance(item, int):
                refs["tables"].add(item)
            elif key in FIELD_ID_KEYS and isinstance(item, int):
                refs["fields"].add(item)
            else:
                collect_mbql_refs(item, refs)
    elif isinstance(value, list):
        if len(value) >= 2 and value[0] == "field" and isinstance(value[1], int):
            refs["fields"].add(value[1])
        for item in value:
            collect_mbql_refs(item, refs)

    return refs


def print_payload_refs(payload: Json, metadata_maps: Optional[MetadataMaps]) -> None:
    refs = collect_mbql_refs(payload.get("dataset_query") or {})
    tables = sorted(refs["tables"])
    fields = sorted(refs["fields"])
    print(f"        MBQL source-table/table_id: {tables}")
    print(f"        MBQL field_id: {fields[:40]}" + (" ..." if len(fields) > 40 else ""))

    if metadata_maps is not None:
        missing_tables = [table_id for table_id in tables if table_id not in metadata_maps.target_table_ids]
        missing_fields = [field_id for field_id in fields if field_id not in metadata_maps.target_field_ids]
        if missing_tables:
            print(f"        ATTENZIONE: table ID non presenti nel target dopo remap: {missing_tables}")
        if missing_fields:
            print(f"        ATTENZIONE: field ID non presenti nel target dopo remap: {missing_fields[:40]}" + (" ..." if len(missing_fields) > 40 else ""))


def compact_card_payload(payload: Json) -> Json:
    """Payload minimo/sicuro per POST /api/card."""
    compact: Json = {
        "name": payload.get("name") or "Card migrata",
        "display": payload.get("display") or "table",
        "dataset_query": payload.get("dataset_query") or {},
        "type": payload.get("type") or "question",
        "collection_id": payload.get("collection_id"),
        "visualization_settings": {},
        "parameters": [],
    }
    if payload.get("description") is not None:
        compact["description"] = payload.get("description")
    return compact


def create_card_with_retries(
    target: MetabaseClient,
    old_id: int,
    payload: Json,
    metadata_maps: Optional[MetadataMaps],
) -> Json:
    """
    Crea una card provando payload progressivamente più sicuri.
    Serve per Metabase che a volte risponde 500 su settings copiati da un'altra istanza.
    """
    attempts: List[Tuple[str, Json]] = []
    attempts.append(("payload completo", payload))

    no_viz = copy.deepcopy(payload)
    no_viz["visualization_settings"] = {}
    attempts.append(("senza visualization_settings", no_viz))

    no_params = copy.deepcopy(no_viz)
    no_params["parameters"] = []
    attempts.append(("senza visualization_settings e parameters", no_params))

    attempts.append(("payload minimo", compact_card_payload(payload)))

    errors: List[str] = []
    for label, attempt_payload in attempts:
        print(f"      Creo card sorgente {old_id}: {payload.get('name')!r} ({label})")
        print_payload_refs(attempt_payload, metadata_maps)
        try:
            return target.post("/api/card", attempt_payload)
        except Exception as exc:
            errors.append(f"{label}: {exc}")

    debug_path = f"failed_card_payload_{old_id}.json"
    with open(debug_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)

    raise MetabaseApiError(
        f"Impossibile creare la card sorgente {old_id}: {payload.get('name')!r}.\n"
        f"Payload salvato in: {debug_path}\n"
        + "\n\n".join(errors)
    )


def test_card_query(client: MetabaseClient, card_id: int) -> Tuple[bool, Optional[str]]:
    try:
        client.post(f"/api/card/{card_id}/query/json", {})
        return True, None
    except Exception as exc:
        return False, str(exc)


def update_dashboard_cards(
    target: MetabaseClient,
    dashboard_id: int,
    dashboard_payload: Json,
    dashcards: List[Json],
) -> None:
    """
    Metabase ha cambiato più volte gli endpoint per le dashcard.
    Proviamo vari payload comuni, fermandoci al primo che funziona.
    """
    attempts: List[Tuple[str, Any]] = [
        (f"/api/dashboard/{dashboard_id}/cards", {"cards": dashcards}),
        (f"/api/dashboard/{dashboard_id}/cards", dashcards),
        (f"/api/dashboard/{dashboard_id}", {**dashboard_payload, "dashcards": dashcards}),
        (f"/api/dashboard/{dashboard_id}", {**dashboard_payload, "ordered_cards": dashcards}),
    ]

    errors = []
    for path, payload in attempts:
        ok, _, error = target.try_put(path, payload)
        if ok:
            return
        errors.append(f"PUT {path}: {error}")

    raise MetabaseApiError(
        "Non sono riuscito ad aggiornare le card della dashboard target.\n"
        "Controlla /api/docs nella tua istanza Metabase e adatta update_dashboard_cards().\n\n"
        + "\n".join(errors)
    )


def migrate_dashboard(args: argparse.Namespace) -> None:
    source = MetabaseClient(
        args.source_url,
        username=args.source_user,
        password=args.source_password,
        api_key=args.source_api_key,
        timeout=args.timeout,
    )
    target = MetabaseClient(
        args.target_url,
        username=args.target_user,
        password=args.target_password,
        api_key=args.target_api_key,
        timeout=args.timeout,
    )

    target_collection_id = resolve_collection_id(
        target,
        collection_id=args.target_collection_id,
        collection_name=args.target_collection_name,
        parent_collection_id=args.target_parent_collection_id,
        create_if_missing=args.create_target_collection,
    )
    if target_collection_id is None:
        print("      Collection target: root/default")
    else:
        print(f"      Collection target ID: {target_collection_id}")

    print(f"[1/8] Leggo dashboard sorgente ID {args.dashboard_id}...")
    source_dashboard = source.get(f"/api/dashboard/{args.dashboard_id}")
    source_dashcards = dashboard_cards(source_dashboard)

    card_ids = []
    for dc in source_dashcards:
        card_id = dc.get("card_id")
        if isinstance(card_id, int) and card_id not in card_ids:
            card_ids.append(card_id)

    print(f"      Dashboard: {source_dashboard.get('name')!r}")
    print(f"      Dashcard trovate: {len(source_dashcards)}")
    print(f"      Card/question dati trovate: {len(card_ids)}")

    print("[2/8] Leggo card sorgenti...")
    source_cards: Dict[int, Json] = {}
    for card_id in card_ids:
        # legacy-mbql=true aiuta le istanze >=0.57 quando vuoi avere MBQL vecchio/compatibile.
        params = {"legacy-mbql": "true"} if args.legacy_mbql else None
        source_cards[card_id] = source.get(f"/api/card/{card_id}", params=params)

    target_database_id = resolve_database_id(
        target,
        database_id=args.target_database_id,
        database_name=args.target_database_name,
    )

    source_database_ids = extract_dataset_database_ids(source_cards.values())
    source_database_id: Optional[int] = args.source_database_id
    if source_database_id is None:
        if len(source_database_ids) == 1:
            source_database_id = source_database_ids[0]
        elif len(source_database_ids) > 1:
            raise ValueError(
                "La dashboard usa più database sorgente: "
                f"{source_database_ids}. Specifica --source-database-id e adatta lo script per mapping multiplo."
            )

    print(f"[3/8] Mapping DB: source={source_database_id}, target={target_database_id}")

    metadata_maps: Optional[MetadataMaps] = None
    if args.remap_fields and source_database_id is not None:
        print("      Costruisco mappa metadata table/field per Field Filter e MBQL...")
        metadata_maps = build_metadata_maps(source, target, source_database_id, target_database_id)
        print(f"      Tabelle sorgente mappabili: {len(metadata_maps.source_table_id_to_key)}")
        print(f"      Field sorgente mappabili: {len(metadata_maps.source_field_id_to_key)}")
    else:
        print("      Rimappatura field/table disabilitata.")

    dashboard_payload = sanitize_dashboard_payload(
        source_dashboard,
        target_collection_id=target_collection_id,
        name_suffix=args.name_suffix,
    )
    dashboard_payload["parameters"] = remap_mbql_like_object(
        dashboard_payload.get("parameters") or [], metadata_maps
    )

    print("[4/8] Preparo payload card...")
    prepared_card_payloads: Dict[int, Json] = {}
    for old_id, card in source_cards.items():
        if metadata_maps is not None:
            refs = collect_mbql_refs(card.get("dataset_query") or {})
            if refs["tables"] or refs["fields"]:
                print(f"      Risolvo riferimenti MBQL card sorgente {old_id}: tables={sorted(refs['tables'])}, fields={sorted(refs['fields'])[:20]}")
                prime_metadata_maps_for_refs(source, target, metadata_maps, refs)

        prepared_card_payloads[old_id] = sanitize_card_payload(
            card,
            target_database_id=target_database_id,
            target_collection_id=target_collection_id,
            source_database_id=source_database_id,
            maps=metadata_maps,
            name_suffix=args.name_suffix,
        )

    if args.dry_run:
        print("\n=== DRY RUN: dashboard payload ===")
        print(pretty_json(dashboard_payload))
        print("\n=== DRY RUN: prime 3 card payload ===")
        for index, (old_id, payload) in enumerate(prepared_card_payloads.items()):
            if index >= 3:
                break
            print(f"\n--- old card {old_id} ---")
            print(pretty_json(payload))
        print("\nDry-run completato: non ho scritto nulla su target.")
        return

    print("[5/8] Creo card su target...")
    old_to_new_card_id: Dict[int, int] = {}
    new_card_payloads_by_old_id: Dict[int, Json] = {}

    for old_id, payload in prepared_card_payloads.items():
        created = create_card_with_retries(target, old_id, payload, metadata_maps)
        new_id = int(created["id"])
        old_to_new_card_id[old_id] = new_id
        new_card_payloads_by_old_id[old_id] = payload
        print(f"      Card {old_id} -> {new_id}: {payload.get('name')}")

    print("[6/8] Rimappo eventuali riferimenti SQL tra saved questions e aggiorno card...")
    for old_id, original_payload in new_card_payloads_by_old_id.items():
        remapped_payload = remap_card_references_after_creation(original_payload, old_to_new_card_id)
        if remapped_payload != original_payload:
            new_id = old_to_new_card_id[old_id]
            target.put(f"/api/card/{new_id}", remapped_payload)
            print(f"      Aggiornata card {new_id} per riferimenti {{#card_id}} nel SQL")

    print("[7/8] Creo dashboard target...")
    created_dashboard = target.post("/api/dashboard", dashboard_payload)
    new_dashboard_id = int(created_dashboard["id"])
    print(f"      Dashboard {args.dashboard_id} -> {new_dashboard_id}: {dashboard_payload.get('name')}")

    print("      Ricostruisco layout dashcards...")
    new_dashcards: List[Json] = []
    for index, source_dc in enumerate(source_dashcards):
        temp_id = -1 - index
        new_dashcards.append(
            sanitize_dashcard(
                source_dc,
                temp_id=temp_id,
                old_to_new_card_id=old_to_new_card_id,
                maps=metadata_maps,
            )
        )

    print("      Aggiorno dashboard target con dashcards...")
    update_dashboard_cards(target, new_dashboard_id, dashboard_payload, new_dashcards)

    if not args.skip_query_test:
        print("[8/8] Test query card su target...")
        failures = []
        for old_id, new_id in old_to_new_card_id.items():
            ok, error = test_card_query(target, new_id)
            if ok:
                print(f"      OK card {new_id}")
            else:
                print(f"      KO card {new_id}: {error}")
                failures.append((new_id, error))
        if failures:
            print("\nATTENZIONE: alcune card sono state create ma non eseguono correttamente la query.")
            print("Cause comuni: Field Filter non rimappato, Query Builder complesso, SQL non compatibile, permessi target.")
    else:
        print("[8/8] Test query saltato.")

    print("\nMigrazione completata.")
    print(f"Nuova dashboard ID: {new_dashboard_id}")
    print(f"URL: {args.target_url.rstrip('/')}/dashboard/{new_dashboard_id}")


def add_auth_args(parser: argparse.ArgumentParser, prefix: str) -> None:
    parser.add_argument(f"--{prefix}-url", required=True)
    parser.add_argument(f"--{prefix}-user")
    parser.add_argument(f"--{prefix}-password")
    parser.add_argument(f"--{prefix}-api-key")


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Migra una singola dashboard Metabase da un ambiente a un altro."
    )
    add_auth_args(parser, "source")
    add_auth_args(parser, "target")

    parser.add_argument("--dashboard-id", type=int, required=True, help="ID dashboard sorgente da migrare")
    parser.add_argument("--source-database-id", type=int, help="DB ID sorgente. Se omesso, viene inferito se unico.")
    parser.add_argument("--target-database-id", type=int, help="DB ID target/prod")
    parser.add_argument("--target-database-name", help="Nome DB target/prod, alternativa a --target-database-id")
    parser.add_argument("--target-collection-id", type=int, default=None, help="Collection ID target. Ometti per root/default.")
    parser.add_argument("--target-collection-name", help="Nome collection target. Se non esiste viene creata, salvo --no-create-target-collection.")
    parser.add_argument("--target-parent-collection-id", type=int, default=None, help="Parent collection ID target se vuoi creare/cercare una collection annidata.")
    parser.add_argument("--no-create-target-collection", dest="create_target_collection", action="store_false", help="Non creare automaticamente la collection target se non esiste.")
    parser.add_argument("--name-suffix", default="", help="Suffisso da aggiungere a dashboard e card, es. ' - PROD'")
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--dry-run", action="store_true", help="Mostra payload senza creare nulla")
    parser.add_argument("--skip-query-test", action="store_true", help="Non eseguire POST /api/card/:id/query/json")
    parser.add_argument("--legacy-mbql", action="store_true", help="Legge card con ?legacy-mbql=true, utile da Metabase >=0.57")
    parser.add_argument("--no-remap-fields", dest="remap_fields", action="store_false", help="Disabilita rimappatura field/table ID")
    parser.set_defaults(remap_fields=True, create_target_collection=True)

    args = parser.parse_args(argv)

    for prefix in ("source", "target"):
        api_key = getattr(args, f"{prefix}_api_key")
        user = getattr(args, f"{prefix}_user")
        password = getattr(args, f"{prefix}_password")
        if not api_key and not (user and password):
            parser.error(
                f"Per {prefix} devi passare --{prefix}-api-key oppure --{prefix}-user e --{prefix}-password"
            )

    if not args.target_database_id and not args.target_database_name:
        parser.error("Specifica --target-database-id oppure --target-database-name")

    if (
        args.target_parent_collection_id is not None
        and args.target_collection_id is None
        and not args.target_collection_name
    ):
        parser.error(
            "--target-parent-collection-id non indica la collection finale: "
            "serve solo insieme a --target-collection-name. "
            "Se vuoi migrare direttamente nella collection 191 usa --target-collection-id 191."
        )

    return args


if __name__ == "__main__":
    try:
        migrate_dashboard(parse_args())
    except KeyboardInterrupt:
        print("\nInterrotto.", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        print(f"\nERRORE: {exc}", file=sys.stderr)
        sys.exit(1)
