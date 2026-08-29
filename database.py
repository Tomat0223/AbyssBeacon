import sqlite3
import json
import hashlib
import os
import re
from datetime import datetime, timezone, timedelta
from config import DATABASE


_INITIALIZED = False


# Built-in safety exclusions are intentionally code-defined rather than stored
# in user settings. They are source-scoped so an unrelated creator using the
# same display name on another provider is not affected.
HARD_BLOCKED_CREATORS = {
    # Built-in, non-removable safety exclusions. Keep these source-scoped so
    # unrelated accounts on another provider are never affected.
    "tensorhub": {"e7g3", "kunjung"},
}

# TensorHub display names are not stable/unique enough for every hard block.
# Owner IDs let AbyssBeacon exclude the exact accounts even if a nickname changes
# (and avoid globally blocking a generic nickname such as "R").
HARD_BLOCKED_SOURCE_CREATOR_IDS = {
    "tensorhub": {
        "838872246360732333",  # Kunjung
        "893963469739538903",  # R
    },
}


def is_hard_blocked_creator(source, creator):
    source = str(source or "").strip().lower()
    creator = str(creator or "").strip().casefold()
    if not source or not creator:
        return False
    if creator in HARD_BLOCKED_CREATORS.get(source, set()):
        return True

    # For exact-ID hard blocks (notably TensorHub's ambiguous one-letter
    # nickname), resolve the stored creator identity instead of blocking every
    # account that happens to share the same display name.
    creator_ids = HARD_BLOCKED_SOURCE_CREATOR_IDS.get(source, set())
    if not creator_ids:
        return False
    try:
        conn = connect()
        marks = ",".join("?" for _ in creator_ids)
        row = conn.execute(
            f"SELECT 1 FROM creator_sources WHERE lower(source)=? AND lower(creator_name)=? "
            f"AND source_creator_id IN ({marks}) LIMIT 1",
            [source, creator, *creator_ids],
        ).fetchone()
        conn.close()
        return row is not None
    except sqlite3.OperationalError:
        return False


def connect():

    conn = sqlite3.connect(
        DATABASE,
        timeout=5
    )

    conn.row_factory = sqlite3.Row

    # AbyssBeacon's Flask server is threaded. A short busy timeout handles
    # ordinary write collisions without making a browser request hang for
    # thirty seconds.
    conn.execute("PRAGMA busy_timeout = 5000")

    return conn



def initialize():

    global _INITIALIZED
    if _INITIALIZED:
        return

    conn = connect()

    # WAL is a much better fit for AbyssBeacon's threaded read-heavy workload:
    # feed/search/detail reads do not need to wait behind routine writers.
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    except sqlite3.OperationalError as exc:
        # Do not make startup fatal if another process briefly owns the DB.
        print(f"SQLite WAL setup skipped: {exc}")

    c = conn.cursor()

    c.execute("""
    CREATE TABLE IF NOT EXISTS models (
        id INTEGER PRIMARY KEY,
        name TEXT,
        display_name TEXT,
        author TEXT,
        source TEXT,
        url TEXT UNIQUE,
        model_key TEXT,
        card_data TEXT,
        library TEXT,
        sensitive INTEGER DEFAULT 0,
        parameters TEXT,
        quantization TEXT,
        format TEXT,
        parent_model TEXT,
        sha TEXT,
        image TEXT,
        description TEXT,
        base_model TEXT,
        architecture TEXT,
        model_type TEXT,
        pipeline TEXT,
        tags TEXT,
        created TEXT,
        updated TEXT,
        downloads INTEGER,
        likes INTEGER,
        viewed INTEGER DEFAULT 0,
        favorite INTEGER DEFAULT 0,
        first_seen TEXT,
        last_seen TEXT,
        metadata_hash TEXT,
        last_changed TEXT,
        retention_mode TEXT DEFAULT 'source',
        creator_discovered_at TEXT,
        license TEXT,
        has_media INTEGER DEFAULT 0,
        has_video INTEGER DEFAULT 0,
        preview_count INTEGER DEFAULT 0,
        gated INTEGER DEFAULT 0
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS model_media (
        id INTEGER PRIMARY KEY,
        model_id INTEGER,
        source TEXT,
        type TEXT,
        url TEXT,
        thumbnail TEXT,
        filename TEXT,
        path TEXT,
        metadata TEXT,
        position INTEGER DEFAULT 0,
        FOREIGN KEY(model_id) REFERENCES models(id)
    )
    """)

    c.execute("""
    CREATE INDEX IF NOT EXISTS idx_model_media_model_id
    ON model_media(model_id)
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS scan_runs (
        id INTEGER PRIMARY KEY,
        started TEXT,
        finished TEXT,
        duration REAL,
        total_processed INTEGER DEFAULT 0,
        total_added INTEGER DEFAULT 0,
        total_updated INTEGER DEFAULT 0,
        total_media INTEGER DEFAULT 0,
        total_images INTEGER DEFAULT 0,
        total_videos INTEGER DEFAULT 0
    )
    """)


    c.execute("""
    CREATE TABLE IF NOT EXISTS scan_model_changes (
        id INTEGER PRIMARY KEY,
        scan_id INTEGER NOT NULL,
        model_id INTEGER NOT NULL,
        change_type TEXT NOT NULL,
        UNIQUE(scan_id, model_id, change_type),
        FOREIGN KEY(scan_id) REFERENCES scan_runs(id),
        FOREIGN KEY(model_id) REFERENCES models(id)
    )
    """)

    c.execute("""
    CREATE INDEX IF NOT EXISTS idx_scan_model_changes_scan_type
    ON scan_model_changes(scan_id, change_type, model_id)
    """)


    c.execute("""
    CREATE TABLE IF NOT EXISTS scan_results (
        id INTEGER PRIMARY KEY,
        scan_id INTEGER,
        source TEXT,
        processed INTEGER DEFAULT 0,
        added INTEGER DEFAULT 0,
        updated INTEGER DEFAULT 0,
        media INTEGER DEFAULT 0,
        images INTEGER DEFAULT 0,
        videos INTEGER DEFAULT 0,
        FOREIGN KEY(scan_id) REFERENCES scan_runs(id)
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS creators (
        id INTEGER PRIMARY KEY,
        name TEXT COLLATE NOCASE UNIQUE,
        favorite INTEGER DEFAULT 0,
        first_seen TEXT,
        last_seen TEXT
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS creator_sources (
        id INTEGER PRIMARY KEY,
        creator_name TEXT NOT NULL COLLATE NOCASE,
        source TEXT NOT NULL,
        source_creator_id TEXT NOT NULL,
        profile_url TEXT,
        discovered_via TEXT,
        first_seen TEXT,
        last_seen TEXT,
        UNIQUE(source, source_creator_id)
    )
    """)


    c.execute("""
    CREATE TABLE IF NOT EXISTS retention_tombstones (
        id INTEGER PRIMARY KEY,
        source TEXT NOT NULL,
        model_key TEXT NOT NULL,
        metadata_hash TEXT,
        activity_at TEXT,
        deleted_at TEXT,
        UNIQUE(source, model_key)
    )
    """)


    c.execute("""
    CREATE TABLE IF NOT EXISTS download_history (
        id INTEGER PRIMARY KEY,
        model_id INTEGER,
        source TEXT NOT NULL,
        model_key TEXT NOT NULL,
        source_file_id TEXT,
        file_key TEXT,
        filename TEXT,
        sha TEXT,
        source_updated TEXT,
        file_fingerprint TEXT NOT NULL,
        downloaded_at TEXT NOT NULL
    )
    """)

    c.execute("""
    CREATE INDEX IF NOT EXISTS idx_download_history_model
    ON download_history(source, model_key, downloaded_at DESC)
    """)


    download_history_columns = {
        row["name"]
        for row in c.execute("PRAGMA table_info(download_history)").fetchall()
    }
    for name, definition in [
        ("version_id", "TEXT DEFAULT ''"),
        ("version_name", "TEXT DEFAULT ''"),
    ]:
        if name not in download_history_columns:
            c.execute(f"ALTER TABLE download_history ADD COLUMN {name} {definition}")

    c.execute("""
    CREATE TABLE IF NOT EXISTS download_queue (
        id INTEGER PRIMARY KEY,
        model_id INTEGER NOT NULL,
        source TEXT NOT NULL,
        model_key TEXT NOT NULL,
        version_id TEXT DEFAULT '',
        version_name TEXT DEFAULT '',
        model_name TEXT DEFAULT '',
        source_url TEXT DEFAULT '',
        release_at TEXT DEFAULT '',
        status TEXT NOT NULL DEFAULT 'waiting',
        last_checked TEXT DEFAULT '',
        last_error TEXT DEFAULT '',
        created_at TEXT NOT NULL,
        UNIQUE(source, model_key, version_id, version_name)
    )
    """)

    c.execute("""
    CREATE INDEX IF NOT EXISTS idx_download_queue_status
    ON download_queue(status, created_at)
    """)


    c.execute("""
    CREATE TABLE IF NOT EXISTS download_watchlist (
        id INTEGER PRIMARY KEY,
        model_id INTEGER NOT NULL,
        source TEXT NOT NULL,
        model_key TEXT NOT NULL,
        version_id TEXT DEFAULT '',
        version_name TEXT DEFAULT '',
        model_name TEXT DEFAULT '',
        source_url TEXT DEFAULT '',
        file_id TEXT DEFAULT '',
        file_name TEXT NOT NULL,
        file_fingerprint TEXT DEFAULT '',
        file_index INTEGER DEFAULT -1,
        file_size_display TEXT DEFAULT '',
        status TEXT NOT NULL DEFAULT 'waiting',
        last_checked TEXT DEFAULT '',
        last_error TEXT DEFAULT '',
        available_at TEXT DEFAULT '',
        dismissed_at TEXT DEFAULT '',
        created_at TEXT NOT NULL,
        UNIQUE(source, model_key, version_id, version_name, file_id, file_name)
    )
    """)

    c.execute("""
    CREATE INDEX IF NOT EXISTS idx_download_watchlist_status
    ON download_watchlist(status, created_at)
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS blocked_creators (
        id INTEGER PRIMARY KEY,
        source TEXT NOT NULL,
        creator TEXT NOT NULL COLLATE NOCASE,
        blocked_at TEXT,
        UNIQUE(source, creator)
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS universal_blocked_creators (
        creator TEXT PRIMARY KEY COLLATE NOCASE,
        blocked_at TEXT
    )
    """)

    conn.commit()
    conn.close()

    migrate()
    purge_hard_blocked_creators()
    _INITIALIZED = True


def add_column_if_missing(column, datatype):

    conn = connect()

    c = conn.cursor()

    c.execute(
        "PRAGMA table_info(models)"
    )

    columns = [
        row["name"]
        for row in c.fetchall()
    ]


    if column not in columns:

        c.execute(
            f"""
            ALTER TABLE models
            ADD COLUMN {column} {datatype}
            """
        )


    conn.commit()
    conn.close()


_SOURCE_PRIORITY = {
    "civitaired": 100,
    "civitai": 90,
    "tensorhub": 80,
    "seaart": 70,
    "huggingface": 60,
    "modelscope": 50,
}


def _model_mapping(model):
    """Return a dict-like scanner model without requiring an actual dict.

    Scanner results are Model objects with .get()/as_dict().  Older source
    snapshot code only accepted dict, which silently discarded alternate-source
    files/card_data during SHA/source merges.
    """
    if isinstance(model, dict):
        return model
    if hasattr(model, "as_dict"):
        try:
            value = model.as_dict()
            if isinstance(value, dict):
                return value
        except Exception:
            pass
    if hasattr(model, "get"):
        keys = (
            "author", "name", "display_name", "tags", "display_tags", "files",
            "card_data", "sha", "updated", "created", "gated", "description",
            "base_model", "architecture", "model_type", "format", "quantization",
            "parameters", "license", "pipeline",
        )
        return {key: model.get(key) for key in keys}
    return {}


def _source_snapshot(model):
    """Keep source-specific metadata when several sites merge into one card."""
    model = _model_mapping(model)
    if not model:
        return {}
    return {
        # Keep source attribution as well as download metadata.  Once a card is
        # SHA-merged the canonical models row can only hold one uploader/name,
        # so alternate-source creator identities must live in model_sources.
        "author": model.get("author") or "",
        "name": model.get("name") or "",
        "display_name": model.get("display_name") or "",
        # Preserve a remote recovery URL separately from models.image, which
        # normally becomes a local /static/cache/previews/... path.
        "image": model.get("_remote_preview_url") or (
            model.get("image")
            if str(model.get("image") or "").startswith(("http://", "https://"))
            else ""
        ),
        # Preserve source-specific tags so SHA-merged cards can expose the
        # union of every mirror's metadata rather than only the canonical row.
        "tags": model.get("tags") or "",
        "display_tags": model.get("display_tags") or [],
        "files": model.get("files") or [],
        "card_data": model.get("card_data") or {},
        "sha": model.get("sha") or "",
        "metadata_hash": model.get("metadata_hash") or "",
        "updated": model.get("updated") or "",
        "created": model.get("created") or "",
        "gated": int(bool(model.get("gated", 0))),
        "description": model.get("description") or "",
        "base_model": model.get("base_model") or "",
        "architecture": model.get("architecture") or "",
        "model_type": model.get("model_type") or "",
        "format": model.get("format") or "",
        "quantization": model.get("quantization") or "",
        "parameters": model.get("parameters") or "",
        "license": model.get("license") or "",
        "pipeline": model.get("pipeline") or "",
    }


def _register_model_source(cursor, model_id, source, url, model_key, model=None):
    if not model_id or not source:
        return
    snapshot = _source_snapshot(model)
    cursor.execute(
        """
        INSERT INTO model_sources (model_id, source, url, model_key, source_data)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(source, model_key) DO UPDATE SET
            model_id=excluded.model_id,
            url=excluded.url,
            source_data=CASE WHEN excluded.source_data NOT IN ('', '{}') THEN excluded.source_data ELSE model_sources.source_data END
        """,
        (model_id, source, url or "", model_key or "", json.dumps(snapshot, ensure_ascii=False) if snapshot else "")
    )
    if _model_mapping(model):
        _register_model_hashes(cursor, model_id, source, model_key, model)


def get_model_sources(model_id):
    conn = connect()
    rows = conn.execute(
        "SELECT source, url, model_key, source_data FROM model_sources WHERE model_id=? ORDER BY source",
        (model_id,)
    ).fetchall()
    conn.close()
    return rows


def get_model_source_snapshot(source, model_key):
    """Return one provider's preserved source snapshot, even when that source
    is not the canonical row in models.

    SHA/URL-merged cards keep only one canonical models row. Source-specific
    unchanged checks must therefore read model_sources rather than assuming
    every provider still has its own row in models.
    """
    source = str(source or "").strip().lower()
    model_key = str(model_key or "").strip()
    if not source or not model_key:
        return None

    conn = connect()
    row = conn.execute(
        """
        SELECT model_id, source, url, model_key, source_data
        FROM model_sources
        WHERE lower(source)=? AND model_key=?
        LIMIT 1
        """,
        (source, model_key),
    ).fetchone()
    conn.close()

    if not row:
        return None

    try:
        snapshot = json.loads(row["source_data"] or "{}")
    except Exception:
        snapshot = {}

    if not isinstance(snapshot, dict):
        snapshot = {}

    snapshot = dict(snapshot)
    snapshot["_model_id"] = int(row["model_id"])
    snapshot["_source"] = str(row["source"] or source)
    snapshot["_url"] = str(row["url"] or "")
    snapshot["_model_key"] = str(row["model_key"] or model_key)
    return snapshot





def refresh_model_source_snapshot(model_id, source, model_key, snapshot, url=""):
    """Replace one source's preserved metadata without touching unrelated mirrors."""
    source = str(source or "").strip().lower()
    model_key = str(model_key or "").strip()
    if not model_id or not source or not model_key or not isinstance(snapshot, dict):
        return False

    conn = connect()
    c = conn.cursor()

    c.execute(
        """
        UPDATE model_sources
        SET source_data=?, url=CASE WHEN ?<>'' THEN ? ELSE url END
        WHERE model_id=? AND lower(source)=? AND model_key=?
        """,
        (
            json.dumps(snapshot, ensure_ascii=False),
            str(url or ""),
            str(url or ""),
            int(model_id),
            source,
            model_key,
        ),
    )
    changed = c.rowcount > 0

    # If this source is the canonical row, keep its download-facing fields in
    # sync as well. Merged cards whose canonical source is different continue
    # using model_sources for this provider.
    canonical = c.execute(
        "SELECT source,model_key FROM models WHERE id=?",
        (int(model_id),),
    ).fetchone()
    if canonical and str(canonical["source"] or "").casefold() == source and str(canonical["model_key"] or "") == model_key:
        c.execute(
            """
            UPDATE models
            SET files=?, card_data=?, gated=?,
                description=CASE WHEN ?<>'' THEN ? ELSE description END,
                updated=CASE WHEN ?<>'' THEN ? ELSE updated END
            WHERE id=?
            """,
            (
                json.dumps(snapshot.get("files") or [], ensure_ascii=False),
                json.dumps(snapshot.get("card_data") or {}, ensure_ascii=False),
                int(bool(snapshot.get("gated", 0))),
                str(snapshot.get("description") or ""),
                str(snapshot.get("description") or ""),
                str(snapshot.get("updated") or ""),
                str(snapshot.get("updated") or ""),
                int(model_id),
            ),
        )

    # Rebuild only this source identity hash so a newly exposed primary artifact
    # can participate in the conservative dedupe system.
    c.execute(
        "DELETE FROM model_file_hashes WHERE model_id=? AND lower(source)=? AND model_key=?",
        (int(model_id), source, model_key),
    )
    _register_model_hashes(c, int(model_id), source, model_key, snapshot)

    conn.commit()
    conn.close()
    return changed

def refresh_canonical_model_media(model_id, source, media_items, fallback_image=""):
    """Refresh gallery media only when this source owns the canonical card.

    Alternate mirror reloads must never replace a stronger canonical source's
    gallery. CivitAI Red/CivitAI explicit reloads can safely repair stale media
    when the reloaded source is the card currently representing the model.
    """
    source=str(source or "").strip().lower()
    conn=connect()
    c=conn.cursor()
    canonical=c.execute("SELECT source FROM models WHERE id=?",(int(model_id),)).fetchone()
    if not canonical or str(canonical["source"] or "").strip().lower()!=source:
        conn.close()
        return False

    cleaned=[item for item in (media_items or []) if isinstance(item,dict) and str(item.get("url") or "").strip()]
    changed=_replace_media_rows(c,int(model_id),source,cleaned)

    total=len(cleaned)
    videos=sum(1 for item in cleaned if str(item.get("type") or "").lower()=="video")
    images=[item for item in cleaned if str(item.get("type") or "").lower()!="video"]
    image=str((images[0].get("url") if images else "") or fallback_image or "").strip()
    c.execute(
        """
        UPDATE models
        SET has_media=?, has_video=?, preview_count=?, image=?
        WHERE id=?
        """,
        (int(total>0),int(videos>0),len(images),image,int(model_id)),
    )
    conn.commit(); conn.close()
    return changed


def get_download_history_open_path(history_id):
    """Return a trusted installed-file path associated with one history row."""
    conn=connect()
    row=conn.execute(
        """
        SELECT dh.model_id, dh.source, dh.model_key,
               (
                   SELECT i.local_path
                   FROM installed_files i
                   WHERE i.model_id=dh.model_id
                     AND lower(i.source)=lower(dh.source)
                     AND (i.model_key=dh.model_key OR dh.model_key='')
                   ORDER BY i.installed_at DESC, i.id DESC
                   LIMIT 1
               ) AS local_path
        FROM download_history dh
        WHERE dh.id=?
        """,
        (int(history_id),),
    ).fetchone()
    conn.close()
    return str(row["local_path"] or "") if row else ""


def _infer_source_author_from_key(source, model_key="", url=""):
    """Recover an uploader only when the source key itself encodes it."""
    source = str(source or "").strip().lower()
    model_key = str(model_key or "").strip()
    url = str(url or "").strip()
    if source in {"huggingface", "modelscope"}:
        if "/" in model_key:
            owner = model_key.split("/", 1)[0].strip()
            if owner:
                return owner
        try:
            from urllib.parse import urlparse
            parts = [part for part in urlparse(url).path.split("/") if part]
            if source == "huggingface" and len(parts) >= 2:
                return parts[0]
            if source == "modelscope" and "models" in parts:
                idx = parts.index("models")
                if len(parts) > idx + 1:
                    return parts[idx + 1]
        except Exception:
            pass
    return ""


def _normalize_sha256(value):
    """Return a canonical SHA256 only; reject repo commits, version IDs and short hashes."""
    value = str(value or "").strip().lower()
    if len(value) == 64 and all(ch in "0123456789abcdef" for ch in value):
        return value
    return ""


def _hash_from_hash_container(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).replace("-", "").replace("_", "").lower() == "sha256":
                found = _normalize_sha256(item)
                if found:
                    return found
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                kind = str(item.get("type") or item.get("name") or item.get("algorithm") or "").replace("-", "").lower()
                if kind == "sha256":
                    found = _normalize_sha256(item.get("hash") or item.get("value") or item.get("digest"))
                    if found:
                        return found
    return ""


def _file_sha256(file_data):
    if not isinstance(file_data, dict):
        return ""
    for key in ("sha256", "sha", "hash"):
        value = _normalize_sha256(file_data.get(key))
        if value:
            return value
    for key in ("hashes", "hashs"):
        value = _hash_from_hash_container(file_data.get(key))
        if value:
            return value
    return ""


_PRIMARY_IDENTITY_EXTENSIONS = {
    ".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf", ".onnx"
}


def _primary_identity_sha256s(model):
    """Hashes safe enough to identify an entire model across sources.

    Shared repository components must not merge cards. We only trust one exact
    hash-bearing model artifact. Multiple artifact hashes are considered
    ambiguous and are deliberately not auto-merged.
    """
    model = _model_mapping(model)
    if not model:
        return []

    files = model.get("files") or []
    if isinstance(files, str):
        try:
            files = json.loads(files)
        except Exception:
            files = []
    if isinstance(files, dict):
        files = list(files.values())

    artifact_hashes = []
    for file_data in files if isinstance(files, list) else []:
        if not isinstance(file_data, dict):
            continue

        path = str(
            file_data.get("path")
            or file_data.get("name")
            or file_data.get("filename")
            or ""
        ).replace("\\", "/")
        basename = path.rsplit("/", 1)[-1]
        extension = ""
        if "." in basename and not basename.startswith("."):
            extension = "." + basename.rsplit(".", 1)[-1].casefold()

        if not (
            file_data.get("primary") is True
            or extension in _PRIMARY_IDENTITY_EXTENSIONS
        ):
            continue

        sha256 = _file_sha256(file_data)
        if sha256 and sha256 not in artifact_hashes:
            artifact_hashes.append(sha256)

    if len(artifact_hashes) == 1:
        return artifact_hashes
    if len(artifact_hashes) > 1:
        return []

    direct = _normalize_sha256(model.get("sha"))
    return [direct] if direct else []


def _model_sha256s(model):
    return _primary_identity_sha256s(model)



def _register_model_hashes(cursor, model_id, source, model_key, model):
    if not model_id or not source:
        return
    for sha256 in _model_sha256s(model):
        cursor.execute(
            """
            INSERT INTO model_file_hashes (model_id, source, model_key, sha256)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(source, model_key, sha256) DO UPDATE SET model_id=excluded.model_id
            """,
            (model_id, str(source), str(model_key or ""), sha256),
        )


def get_model_sha256s(model_id):
    conn = connect()
    rows = conn.execute(
        "SELECT DISTINCT sha256 FROM model_file_hashes WHERE model_id=? ORDER BY sha256",
        (model_id,),
    ).fetchall()
    conn.close()
    return [row["sha256"] for row in rows]


def get_model_sha256_lookup(model_ids):
    ids = [int(x) for x in (model_ids or []) if str(x).isdigit()]
    if not ids:
        return {}
    lookup = {model_id: [] for model_id in ids}
    conn = connect()
    for offset in range(0, len(ids), 800):
        chunk = ids[offset:offset + 800]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"SELECT model_id, sha256 FROM model_file_hashes WHERE model_id IN ({placeholders}) ORDER BY model_id, sha256",
            chunk,
        ).fetchall()
        for row in rows:
            lookup.setdefault(row["model_id"], []).append(row["sha256"])
    conn.close()
    return lookup


def _find_cross_source_duplicate(cursor, model):
    source = str(model.get("source", "") or "")
    model_key = str(model.get("model_key", "") or "")
    if source in {"civitai", "civitaired"} and model_key:
        other = "civitaired" if source == "civitai" else "civitai"
        row = cursor.execute(
            "SELECT * FROM models WHERE source=? AND model_key=? LIMIT 1",
            (other, model_key)
        ).fetchone()
        if row:
            return row

    # SHA256 is the authoritative cross-source file identity. Only exact
    # 64-hex SHA256 values qualify; repo commit SHAs and source version IDs do not.
    for sha256 in _model_sha256s(model):
        # A SHA can legitimately appear in many repositories when it belongs to
        # a shared auxiliary file (VAE, text encoder, config bundle, etc.).
        # Only auto-merge when this exact hash points to one unambiguous existing
        # canonical card outside the incoming source.
        rows = cursor.execute(
            """
            SELECT DISTINCT m.*
            FROM model_file_hashes h
            JOIN models m ON m.id=h.model_id
            WHERE h.sha256=? AND lower(h.source)<>lower(?)
            ORDER BY m.id
            """,
            (sha256, source),
        ).fetchall()
        ids = {int(row["id"]) for row in rows}
        if len(ids) == 1:
            candidate = rows[0]
            # A legitimate mirror card can have only one source model identity
            # per source.  If this canonical card already contains a different
            # key for the incoming source, merging would create the kind of
            # chained/monster card produced by shared repository components.
            existing_same_source = cursor.execute(
                """
                SELECT 1 FROM model_sources
                WHERE model_id=? AND lower(source)=lower(?)
                  AND COALESCE(model_key,'')<>COALESCE(?, '')
                LIMIT 1
                """,
                (candidate["id"], source, model_key),
            ).fetchone()
            if existing_same_source:
                continue
            if str(candidate["source"] or "").casefold() == source.casefold() and str(candidate["model_key"] or "") != model_key:
                continue
            return candidate
    return None



def _canonical_rank(row):
    """Choose which source remains the visible/canonical card after an exact-file merge."""
    source = str(row["source"] or "").lower()
    priority = _SOURCE_PRIORITY.get(source, 0)
    richness = 0
    for key in ("description", "image", "files", "card_data", "tags", "sha"):
        try:
            if row[key]:
                richness += 1
        except Exception:
            pass
    return (priority, richness, -int(row["id"] or 0))


def _merge_existing_model_rows(cursor, winner_id, loser_id):
    """Fold an old duplicate card into a canonical card without losing its source."""
    if not winner_id or not loser_id or int(winner_id) == int(loser_id):
        return False
    winner = cursor.execute("SELECT * FROM models WHERE id=?", (winner_id,)).fetchone()
    loser = cursor.execute("SELECT * FROM models WHERE id=?", (loser_id,)).fetchone()
    if not winner or not loser:
        return False

    favorite = max(int(winner["favorite"] or 0), int(loser["favorite"] or 0))
    viewed = max(int(winner["viewed"] or 0), int(loser["viewed"] or 0))
    first_values = [str(v or "").strip() for v in (winner["first_seen"], loser["first_seen"]) if str(v or "").strip()]
    first_seen = min(first_values) if first_values else ""
    cursor.execute(
        "UPDATE models SET favorite=?, viewed=?, first_seen=? WHERE id=?",
        (favorite, viewed, first_seen, winner_id),
    )

    # Preserve the losing canonical row as a fully usable alternate source.
    _register_model_source(
        cursor, winner_id, loser["source"], loser["url"], loser["model_key"], dict(loser)
    )
    links = cursor.execute(
        "SELECT source,url,model_key,source_data FROM model_sources WHERE model_id=?",
        (loser_id,),
    ).fetchall()
    for link in links:
        cursor.execute(
            """
            INSERT INTO model_sources(model_id,source,url,model_key,source_data)
            VALUES(?,?,?,?,?)
            ON CONFLICT(source,model_key) DO UPDATE SET
                model_id=excluded.model_id,
                url=CASE WHEN excluded.url<>'' THEN excluded.url ELSE model_sources.url END,
                source_data=CASE WHEN excluded.source_data NOT IN ('','{}') THEN excluded.source_data ELSE model_sources.source_data END
            """,
            (winner_id, link["source"], link["url"] or "", link["model_key"] or "", link["source_data"] or ""),
        )
    cursor.execute("DELETE FROM model_sources WHERE model_id=?", (loser_id,))

    cursor.execute("UPDATE model_file_hashes SET model_id=? WHERE model_id=?", (winner_id, loser_id))

    # Retain both galleries; append the losing source's previews after the
    # canonical gallery rather than deleting useful source-specific media.
    max_row = cursor.execute(
        "SELECT COALESCE(MAX(position),-1) p FROM model_media WHERE model_id=?", (winner_id,)
    ).fetchone()
    next_pos = int(max_row["p"] if max_row else -1) + 1
    media_rows = cursor.execute(
        "SELECT id FROM model_media WHERE model_id=? ORDER BY position,id", (loser_id,)
    ).fetchall()
    for index, media in enumerate(media_rows):
        cursor.execute(
            "UPDATE model_media SET model_id=?, position=? WHERE id=?",
            (winner_id, next_pos + index, media["id"]),
        )

    cursor.execute("UPDATE download_history SET model_id=? WHERE model_id=?", (winner_id, loser_id))
    cursor.execute("DELETE FROM models WHERE id=?", (loser_id,))
    return True


def _snapshot_dict(value):
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _snapshot_list(value):
    if isinstance(value, list):
        return list(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []
    return []


def _link_identity_hashes(link):
    return set(_primary_identity_sha256s(_snapshot_dict(link["source_data"])))


def _cluster_corrupted_links(links):
    """Join only mirrors we can prove while splitting an impossible card."""
    links = [dict(link) for link in links]
    parent = list(range(len(links)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def source_set(root):
        return {
            str(links[i]["source"] or "").casefold()
            for i in range(len(links))
            if find(i) == root
        }

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra == rb:
            return
        if source_set(ra) & source_set(rb):
            return
        parent[rb] = ra

    # CivitAI Red is a mirror of CivitAI only when its native model key matches.
    for i, left in enumerate(links):
        for j in range(i + 1, len(links)):
            right = links[j]
            pair = {
                str(left["source"] or "").casefold(),
                str(right["source"] or "").casefold(),
            }
            if pair == {"civitai", "civitaired"} and (
                str(left["model_key"] or "").strip()
                and str(left["model_key"] or "").strip()
                == str(right["model_key"] or "").strip()
            ):
                union(i, j)

    hashes = [_link_identity_hashes(link) for link in links]
    for i in range(len(links)):
        if len(hashes[i]) != 1:
            continue
        for j in range(i + 1, len(links)):
            if len(hashes[j]) == 1 and hashes[i] == hashes[j]:
                union(i, j)

    clusters = {}
    for i, link in enumerate(links):
        clusters.setdefault(find(i), []).append(link)
    return list(clusters.values())


def _build_source_row_from_link(original, link):
    snapshot = _snapshot_dict(link["source_data"])
    source = str(link["source"] or "").strip()
    model_key = str(link["model_key"] or "").strip()
    url = str(link["url"] or "").strip()

    display_tags = snapshot.get("display_tags")
    if not isinstance(display_tags, list):
        display_tags = _snapshot_list(display_tags)

    files = snapshot.get("files")
    if not isinstance(files, list):
        files = _snapshot_list(files)

    card_data = snapshot.get("card_data")
    if not isinstance(card_data, dict):
        card_data = _snapshot_dict(card_data)

    name = str(
        snapshot.get("name")
        or snapshot.get("display_name")
        or model_key
        or original["name"]
        or ""
    ).strip()
    display_name = str(
        snapshot.get("display_name")
        or snapshot.get("name")
        or name
    ).strip()
    author = str(
        snapshot.get("author")
        or _infer_source_author_from_key(source, model_key, url)
        or ""
    ).strip()

    same_identity = (
        str(original["source"] or "").casefold() == source.casefold()
        and str(original["model_key"] or "") == model_key
    )

    return {
        "name": name,
        "display_name": display_name,
        "author": author,
        "sha": str(snapshot.get("sha") or ""),
        "source": source,
        "url": url,
        "model_key": model_key,
        "image": str(original["image"] or "") if same_identity else "",
        "description": str(snapshot.get("description") or ""),
        "base_model": str(snapshot.get("base_model") or ""),
        "architecture": str(snapshot.get("architecture") or original["architecture"] or ""),
        "model_type": str(snapshot.get("model_type") or ""),
        "tags": snapshot.get("tags") or "",
        "display_tags": display_tags,
        "created": str(snapshot.get("created") or original["created"] or ""),
        "updated": str(snapshot.get("updated") or original["updated"] or ""),
        "downloads": 0,
        "likes": 0,
        "license": str(snapshot.get("license") or ""),
        "pipeline": str(snapshot.get("pipeline") or ""),
        "files": files,
        "card_data": card_data,
        "library": "",
        "sensitive": int(original["sensitive"] or 0),
        "parameters": str(snapshot.get("parameters") or ""),
        "quantization": str(snapshot.get("quantization") or ""),
        "format": str(snapshot.get("format") or ""),
        "parent_model": "",
        "has_media": 0,
        "has_video": 0,
        "preview_count": 0,
        "gated": int(bool(snapshot.get("gated", 0))),
    }


def _insert_detangled_model(cursor, row, original):
    now = datetime.now(timezone.utc).isoformat()
    cursor.execute(
        """
        INSERT INTO models
        (
            name,display_name,author,sha,source,url,model_key,image,description,
            base_model,architecture,model_type,tags,display_tags,created,updated,
            downloads,likes,viewed,favorite,first_seen,last_seen,metadata_hash,
            last_changed,retention_mode,creator_discovered_at,license,pipeline,
            files,card_data,library,sensitive,parameters,quantization,format,
            parent_model,has_media,has_video,preview_count,gated
        )
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            row["name"],row["display_name"],row["author"],row["sha"],row["source"],
            row["url"],row["model_key"],row["image"],row["description"],
            row["base_model"],row["architecture"],row["model_type"],row["tags"],
            json.dumps(row["display_tags"],ensure_ascii=False),row["created"],
            row["updated"],row["downloads"],row["likes"],int(original["viewed"] or 0),
            0,original["first_seen"] or row["created"] or now,
            original["last_seen"] or now,_stable_model_metadata_hash(row),
            original["last_changed"] or now,original["retention_mode"] or "source",
            original["creator_discovered_at"] or "",row["license"],row["pipeline"],
            json.dumps(row["files"],ensure_ascii=False),
            json.dumps(row["card_data"],ensure_ascii=False),row["library"],
            row["sensitive"],row["parameters"],row["quantization"],row["format"],
            row["parent_model"],0,0,0,row["gated"],
        ),
    )
    return int(cursor.lastrowid)


def _update_detangled_model(cursor, model_id, row, original):
    cursor.execute(
        """
        UPDATE models SET
            name=?,display_name=?,author=?,sha=?,source=?,url=?,model_key=?,
            image=?,description=?,base_model=?,architecture=?,model_type=?,tags=?,
            display_tags=?,created=?,updated=?,downloads=?,likes=?,metadata_hash=?,
            license=?,pipeline=?,files=?,card_data=?,library=?,sensitive=?,
            parameters=?,quantization=?,format=?,parent_model=?,has_media=0,
            has_video=0,preview_count=0,gated=?
        WHERE id=?
        """,
        (
            row["name"],row["display_name"],row["author"],row["sha"],row["source"],
            row["url"],row["model_key"],row["image"],row["description"],
            row["base_model"],row["architecture"],row["model_type"],row["tags"],
            json.dumps(row["display_tags"],ensure_ascii=False),row["created"],
            row["updated"],row["downloads"],row["likes"],
            _stable_model_metadata_hash(row),row["license"],row["pipeline"],
            json.dumps(row["files"],ensure_ascii=False),
            json.dumps(row["card_data"],ensure_ascii=False),row["library"],
            row["sensitive"],row["parameters"],row["quantization"],row["format"],
            row["parent_model"],row["gated"],int(model_id),
        ),
    )


def _backup_before_detangle(connection):
    try:
        from pathlib import Path
        source_path = Path(DATABASE)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = source_path.with_name(
            f"{source_path.stem}.pre-detangle-{stamp}{source_path.suffix or '.db'}"
        )
        backup = sqlite3.connect(str(backup_path))
        try:
            connection.backup(backup)
        finally:
            backup.close()
        return str(backup_path)
    except Exception as exc:
        print(
            "AbyssBeacon detangle backup failed; repair cancelled:",
            type(exc).__name__,
            exc,
        )
        return ""


def repair_impossible_merged_cards(connection, cursor):
    """Split only cards proven corrupt by multiple keys from the same source."""
    candidates = cursor.execute(
        """
        SELECT DISTINCT model_id
        FROM model_sources
        WHERE model_id IN (
            SELECT model_id
            FROM model_sources
            GROUP BY model_id,lower(source)
            HAVING COUNT(DISTINCT model_key)>1
        )
        ORDER BY model_id
        """
    ).fetchall()
    candidate_ids = [int(row["model_id"]) for row in candidates]
    if not candidate_ids:
        return {"cards":0,"created":0,"backup":""}

    # IMPORTANT: Do not call connection.backup() here. migrate() has already
    # written to this same SQLite connection, so an in-process backup can wait
    # indefinitely on its own active transaction. The user should make an
    # ordinary filesystem copy of models.db before first running this repair.
    backup_path = "manual models.db backup"
    repaired = 0
    created = 0

    for model_id in candidate_ids:
        original = cursor.execute(
            "SELECT * FROM models WHERE id=?",(model_id,)
        ).fetchone()
        links = cursor.execute(
            """
            SELECT id,model_id,source,url,model_key,source_data
            FROM model_sources WHERE model_id=? ORDER BY id
            """,(model_id,)
        ).fetchall()
        if not original or len(links)<2:
            continue

        clusters = _cluster_corrupted_links(links)
        if len(clusters)<2:
            continue

        original_identity = (
            str(original["source"] or "").casefold(),
            str(original["model_key"] or ""),
        )
        reuse_index = 0
        for i,cluster in enumerate(clusters):
            if any(
                (
                    str(link["source"] or "").casefold(),
                    str(link["model_key"] or ""),
                ) == original_identity
                for link in cluster
            ):
                reuse_index=i
                break

        cluster_ids={}
        for i,cluster in enumerate(clusters):
            canonical=max(
                cluster,
                key=lambda link:(
                    _SOURCE_PRIORITY.get(str(link["source"] or "").casefold(),0),
                    bool(str(link["source_data"] or "").strip()),
                    -int(link["id"]),
                ),
            )
            row=_build_source_row_from_link(original,canonical)

            if i==reuse_index:
                target_id=model_id
                _update_detangled_model(cursor,target_id,row,original)
            else:
                target_id=_insert_detangled_model(cursor,row,original)
                created+=1

            for link in cluster:
                key=(
                    str(link["source"] or "").casefold(),
                    str(link["model_key"] or ""),
                )
                cluster_ids[key]=target_id
                cursor.execute(
                    "UPDATE model_sources SET model_id=? WHERE id=?",
                    (target_id,link["id"]),
                )

        # Move explicit source/model-key state to the correct repaired card.
        for table in ("model_file_hashes","download_history","installed_files"):
            # All three tables have an explicit INTEGER PRIMARY KEY named id.
            # Selecting SQLite's rowid is unreliable here because an INTEGER
            # PRIMARY KEY aliases rowid and sqlite3.Row may expose it as "id".
            rows=cursor.execute(
                f"SELECT id,source,model_key FROM {table} WHERE model_id=?",
                (model_id,),
            ).fetchall()
            for state in rows:
                target=cluster_ids.get((
                    str(state["source"] or "").casefold(),
                    str(state["model_key"] or ""),
                ))
                if target:
                    cursor.execute(
                        f"UPDATE {table} SET model_id=? WHERE id=?",
                        (target,state["id"]),
                    )

        # Gallery rows know source but not source model_key. Keep media when the
        # source has one link; discard only genuinely ambiguous repeated-source
        # galleries. A normal scan repopulates those cleanly.
        by_source={}
        for link in links:
            by_source.setdefault(str(link["source"] or "").casefold(),[]).append(link)

        media=cursor.execute(
            "SELECT id,source FROM model_media WHERE model_id=?",(model_id,)
        ).fetchall()
        for item in media:
            source=str(item["source"] or "").casefold()
            source_links=by_source.get(source,[])
            if len(source_links)==1:
                link=source_links[0]
                target=cluster_ids.get((source,str(link["model_key"] or "")))
                if target:
                    cursor.execute(
                        "UPDATE model_media SET model_id=? WHERE id=?",
                        (target,item["id"]),
                    )
            elif len(source_links)>1:
                cursor.execute("DELETE FROM model_media WHERE id=?",(item["id"],))

        # Recompute presentation media flags after the split.
        for target in sorted(set(cluster_ids.values())):
            stats=cursor.execute(
                """
                SELECT COUNT(*) total,
                       SUM(CASE WHEN type='video' THEN 1 ELSE 0 END) videos
                FROM model_media WHERE model_id=?
                """,(target,)
            ).fetchone()
            total=int(stats["total"] or 0)
            videos=int(stats["videos"] or 0)
            image_row=cursor.execute(
                """
                SELECT COALESCE(NULLIF(thumbnail,''),url) image
                FROM model_media
                WHERE model_id=? AND type='image'
                ORDER BY position,id LIMIT 1
                """,(target,)
            ).fetchone()
            image=str(image_row["image"] or "") if image_row else ""
            cursor.execute(
                """
                UPDATE models
                SET has_media=?,has_video=?,preview_count=?,
                    image=CASE WHEN COALESCE(image,'')='' AND ?<>'' THEN ? ELSE image END
                WHERE id=?
                """,(int(total>0),int(videos>0),total,image,image,target)
            )

        repaired+=1

    return {"cards":repaired,"created":created,"backup":backup_path}


def reconcile_cross_source_sha256_duplicates(cursor):
    """Repair all *unambiguous* existing cross-source SHA256 duplicates.

    Exact SHA256 remains the strongest identity signal, but a repository can
    contain shared support files used by many different models. To avoid false
    merges, a hash is auto-reconciled only when it maps to at most one canonical
    model per source. Same-source duplicate hashes are left alone for review.
    """
    groups = cursor.execute(
        """
        SELECT h.sha256
        FROM model_file_hashes h
        JOIN models m ON m.id=h.model_id
        GROUP BY h.sha256
        HAVING COUNT(DISTINCT h.model_id)>1
           AND COUNT(DISTINCT lower(m.source))>1
        ORDER BY h.sha256
        """
    ).fetchall()

    merged = 0
    merged_groups = 0
    skipped_ambiguous = 0
    for group in groups:
        sha256 = group["sha256"]
        per_source = cursor.execute(
            """
            SELECT lower(h.source) source, COUNT(DISTINCT h.model_id) n
            FROM model_file_hashes h
            WHERE h.sha256=?
            GROUP BY lower(h.source)
            """,
            (sha256,),
        ).fetchall()
        if any(int(row["n"] or 0) > 1 for row in per_source):
            skipped_ambiguous += 1
            continue

        rows = cursor.execute(
            """
            SELECT DISTINCT m.*
            FROM model_file_hashes h
            JOIN models m ON m.id=h.model_id
            WHERE h.sha256=?
            ORDER BY m.id
            """,
            (sha256,),
        ).fetchall()
        if len(rows) < 2:
            continue
        winner = max(rows, key=_canonical_rank)
        winner_id = int(winner["id"])
        changed = 0
        for row in rows:
            loser_id = int(row["id"])
            if loser_id == winner_id:
                continue

            loser_links = cursor.execute(
                "SELECT source,model_key FROM model_sources WHERE model_id=?",
                (loser_id,),
            ).fetchall()
            conflict = False
            for link in loser_links:
                existing = cursor.execute(
                    """
                    SELECT 1 FROM model_sources
                    WHERE model_id=? AND lower(source)=lower(?)
                      AND COALESCE(model_key,'')<>COALESCE(?, '')
                    LIMIT 1
                    """,
                    (winner_id,link["source"],link["model_key"]),
                ).fetchone()
                if existing:
                    conflict=True
                    break
            if conflict:
                skipped_ambiguous += 1
                continue

            if _merge_existing_model_rows(cursor,winner_id,loser_id):
                merged += 1
                changed += 1
        if changed:
            merged_groups += 1

    return {
        "groups": merged_groups,
        "merged": merged,
        "skipped_ambiguous": skipped_ambiguous,
    }


def _stable_model_metadata_hash(model):
    """Stable metadata fingerprint for meaningful model-definition changes."""
    supplied = str(model.get("metadata_hash", "") or "").strip()
    if supplied:
        return supplied

    stable = {
        "name": model.get("name", ""),
        "display_name": model.get("display_name", ""),
        "author": model.get("author", ""),
        "source": model.get("source", ""),
        "url": model.get("url", ""),
        "model_key": model.get("model_key", ""),
        "sha": model.get("sha", ""),
        "image": model.get("image", ""),
        "description": model.get("description", ""),
        "base_model": model.get("base_model", ""),
        "architecture": model.get("architecture", ""),
        "model_type": model.get("model_type", ""),
        "pipeline": model.get("pipeline", ""),
        "tags": model.get("tags", ""),
        "display_tags": model.get("display_tags", []),
        "license": model.get("license", ""),
        "files": model.get("files", []),
        "library": model.get("library", ""),
        "sensitive": int(bool(model.get("sensitive", 0))),
        "parameters": model.get("parameters", ""),
        "quantization": model.get("quantization", ""),
        "format": model.get("format", ""),
        "parent_model": model.get("parent_model", ""),
        "gated": int(bool(model.get("gated", 0))),
    }
    payload = json.dumps(
        stable, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _media_identity_from_items(media_items):
    rows = []
    for index, item in enumerate(media_items or []):
        rows.append((
            str(item.get("type", "image") or "image"),
            str(item.get("url", "") or ""),
            str(item.get("thumbnail", "") or ""),
            str(item.get("filename", "") or ""),
            str(item.get("path", "") or ""),
            int(item.get("position", index) or 0),
        ))
    return rows


def _media_identity_from_db(cursor, model_id):
    rows = cursor.execute(
        """
        SELECT type, url, thumbnail, filename, path, position
        FROM model_media
        WHERE model_id=?
        ORDER BY position, id
        """,
        (model_id,)
    ).fetchall()
    return [
        (
            str(row["type"] or "image"),
            str(row["url"] or ""),
            str(row["thumbnail"] or ""),
            str(row["filename"] or ""),
            str(row["path"] or ""),
            int(row["position"] or 0),
        )
        for row in rows
    ]


def _replace_media_rows(cursor, model_id, source, media_items):
    """Replace media only when the incoming media identity actually changed."""
    incoming = _media_identity_from_items(media_items)
    existing = _media_identity_from_db(cursor, model_id)

    if incoming == existing:
        return False

    cursor.execute("DELETE FROM model_media WHERE model_id = ?", (model_id,))

    rows = []
    for item in media_items or []:
        metadata = item.get("metadata", {})
        if metadata is None:
            metadata = {}
        if not isinstance(metadata, str):
            metadata = json.dumps(metadata, ensure_ascii=False)

        rows.append((
            model_id,
            source,
            item.get("type", "image"),
            item.get("url", ""),
            item.get("thumbnail", ""),
            item.get("filename", ""),
            item.get("path", ""),
            metadata,
            item.get("position", 0),
        ))

    if rows:
        cursor.executemany(
            """
            INSERT INTO model_media
            (model_id, source, type, url, thumbnail, filename, path, metadata, position)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            rows
        )

    return True


def add_model(model):

    conn = connect()

    c = conn.cursor()

    # Cross-source canonicalization. CivitAI Red and CivitAI share the same
    # underlying model IDs. Keep one feed card while retaining every source
    # URL as a fallback. Exact cryptographic hashes can also identify mirrors.
    cross = _find_cross_source_duplicate(c, model)
    if cross:
        incoming_source = str(model.get("source", "") or "")
        existing_source = str(cross["source"] or "")
        _register_model_source(c, cross["id"], existing_source, cross["url"], cross["model_key"], dict(cross))
        _register_model_source(c, cross["id"], incoming_source, model.get("url", ""), model.get("model_key", ""), model)

        incoming_priority = _SOURCE_PRIORITY.get(incoming_source, 0)
        existing_priority = _SOURCE_PRIORITY.get(existing_source, 0)
        incoming_existing = c.execute(
            "SELECT * FROM models WHERE source=? AND (model_key=? OR url=?) AND id<>? LIMIT 1",
            (incoming_source, model.get("model_key", ""), model.get("url", ""), cross["id"]),
        ).fetchone()
        if incoming_priority <= existing_priority:
            # If both cards already existed before SHA matching was learned,
            # collapse the incoming-source card into the stronger canonical row.
            if incoming_existing:
                c.execute("UPDATE models SET favorite=?, viewed=? WHERE id=?", (
                    max(int(cross["favorite"] or 0), int(incoming_existing["favorite"] or 0)),
                    max(int(cross["viewed"] or 0), int(incoming_existing["viewed"] or 0)),
                    cross["id"],
                ))
                c.execute("UPDATE model_sources SET model_id=? WHERE model_id=?", (cross["id"], incoming_existing["id"]))
                c.execute("UPDATE model_file_hashes SET model_id=? WHERE model_id=?", (cross["id"], incoming_existing["id"]))
                c.execute("DELETE FROM model_media WHERE model_id=?", (incoming_existing["id"],))
                c.execute("DELETE FROM models WHERE id=?", (incoming_existing["id"],))
            now_seen = datetime.now(timezone.utc).isoformat()
            c.execute("UPDATE models SET last_seen=? WHERE id=?", (now_seen, cross["id"]))
            conn.commit()
            conn.close()
            return {
                "model_id": cross["id"],
                "state": "unchanged",
                "media_changed": False,
                "media_count": 0,
            }

        # A richer source (currently Red over regular CivitAI) replaces the
        # canonical row while carrying over local user state and source links.
        preserved_favorite = int(cross["favorite"] or 0)
        preserved_viewed = int(cross["viewed"] or 0)
        preserved_first_seen = cross["first_seen"] or model.get("created", "")
        preserved_retention_mode = cross["retention_mode"] or "source"
        preserved_creator_discovered_at = cross["creator_discovered_at"] or ""
        old_id = cross["id"]
        links = c.execute("SELECT source,url,model_key FROM model_sources WHERE model_id=?", (old_id,)).fetchall()
        preserved_hashes = c.execute("SELECT source,model_key,sha256 FROM model_file_hashes WHERE model_id=?", (old_id,)).fetchall()
        c.execute("DELETE FROM model_media WHERE model_id=?", (old_id,))
        c.execute("DELETE FROM model_sources WHERE model_id=?", (old_id,))
        c.execute("DELETE FROM model_file_hashes WHERE model_id=?", (old_id,))
        c.execute("DELETE FROM models WHERE id=?", (old_id,))
        conn.commit()
        # Continue into the normal insert path; local state is restored below.
        setattr(model, "_preserved_favorite", preserved_favorite)
        setattr(model, "_preserved_viewed", preserved_viewed)
        setattr(model, "_preserved_first_seen", preserved_first_seen)
        setattr(model, "_preserved_retention_mode", preserved_retention_mode)
        setattr(model, "_preserved_creator_discovered_at", preserved_creator_discovered_at)
        setattr(model, "_preserved_links", [dict(row) for row in links])
        setattr(model, "_preserved_hashes", [dict(row) for row in preserved_hashes])
        if incoming_existing:
            c.execute("UPDATE models SET favorite=?, viewed=?, first_seen=COALESCE(NULLIF(first_seen,''), ?) WHERE id=?", (
                max(int(incoming_existing["favorite"] or 0), preserved_favorite),
                max(int(incoming_existing["viewed"] or 0), preserved_viewed),
                preserved_first_seen,
                incoming_existing["id"],
            ))


    # Check if model already exists.
    #
    # URL alone is not sufficient because a source can correct/change its public
    # route while retaining the same stable source model key. TensorHub is one
    # concrete example: early AbyssBeacon builds stored the outer project ID in
    # the URL, while TensorHub actually routes /models/<nested model ID>.
    c.execute(
        """
        SELECT id, metadata_hash FROM models
        WHERE url = ?
           OR (source = ? AND model_key = ?)
        ORDER BY CASE WHEN url = ? THEN 0 ELSE 1 END
        LIMIT 1
        """,
        (
            model.get("url", ""),
            model.get("source", ""),
            model.get("model_key", ""),
            model.get("url", ""),
        )
    )

    existing = c.fetchone()


    model_id = existing["id"] if existing else None
    now_seen = datetime.now(timezone.utc).isoformat()
    new_metadata_hash = _stable_model_metadata_hash(model)
    old_metadata_hash = str(existing["metadata_hash"] or "") if existing else ""
    metadata_changed = bool(existing and old_metadata_hash and old_metadata_hash != new_metadata_hash)
    change_state = "new" if not existing else ("changed" if metadata_changed else "unchanged")


    if existing:

        c.execute(
            """
            UPDATE models SET

                name=?,
                display_name=?,
                author=?,
                sha=?,
                source=?,
                image=?,
                has_media=?,
                has_video=?,
                preview_count=?,
                gated=?,
                description=?,
                base_model=?,
                architecture=?,
                model_type=?,
                tags=?,
                display_tags=?,
                updated=?,
                downloads=?,
                likes=?,
                license=?,
                pipeline=?,
                files=?,
                card_data=?,
                library=?,
                sensitive=?,
                parameters=?,
                quantization=?,
                format=?,
                parent_model=?,
                model_key=?,
                last_seen=?,
                metadata_hash=?,
                last_changed=CASE WHEN ? THEN ? ELSE last_changed END,
                url=?

                WHERE id=?

            """,
            (
                model.get("name", ""),
                model.get("display_name", ""),
                model.get("author", ""),
                model.get("sha", ""),
                model.get("source", ""),
                model.get("image", ""),
                model.get("has_media", 0),
                model.get("has_video", 0),
                model.get("preview_count", 0),
                model.get("gated", 0),
                model.get("description", ""),
                model.get("base_model", ""),
                model.get("architecture", ""),
                model.get("model_type", ""),
                model.get("tags", ""),
                json.dumps(
                    model.get("display_tags", [])
                ),
                model.get("updated", ""),
                model.get("downloads", 0),
                model.get("likes", 0),

                model.get("license", ""),

                model.get("pipeline", ""),

                json.dumps(
                    model.get("files", [])
                ),

                json.dumps(
                    model.get("card_data", {})
                ),

                model.get("library", ""),

                model.get("sensitive", 0),

                model.get("parameters", ""),

                model.get("quantization", ""),

                model.get("format", ""),

                model.get("parent_model", ""),

                model.get("model_key", ""),
                now_seen,
                new_metadata_hash,
                1 if metadata_changed else 0,
                now_seen,
                model.get("url", ""),

                model_id
            )
        )


    else:

        c.execute(
            """
            INSERT INTO models
            (
                name,
                display_name,
                author,
                sha,
                source,
                url,
                model_key,
                image,
                description,
                base_model,
                architecture,
                model_type,
                tags,
                display_tags,
                created,
                updated,
                downloads,
                likes,
                viewed,
                favorite,
                first_seen,
                last_seen,
                metadata_hash,
                last_changed,
                retention_mode,
                creator_discovered_at,
                license,
                pipeline,
                files,
                card_data,
                library,
                sensitive,
                parameters,
                quantization,
                format,
                parent_model,
                has_media,
                has_video,
                preview_count,
                gated
            )

            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)

            """,
            (
                model.get("name", ""),
                model.get("display_name", ""),
                model.get("author", ""),
                model.get("sha", ""),
                model.get("source", ""),
                model.get("url", ""),
                model.get("model_key",""),
                model.get("image", ""),
                model.get("description", ""),
                model.get("base_model", ""),
                model.get("architecture", ""),
                model.get("model_type", ""),
                model.get("tags", ""),
                json.dumps(
                    model.get("display_tags", [])
                ),
                model.get("created", ""),
                model.get("updated", ""),
                model.get("downloads", 0),
                model.get("likes", 0),

                model.get("_preserved_viewed", 0),  # viewed
                model.get("_preserved_favorite", 0),  # favorite

                model.get("_preserved_first_seen", model.get("created", "") or now_seen),  # first_seen
                now_seen,  # last_seen
                new_metadata_hash,
                now_seen,  # last_changed baseline for a newly discovered model
                model.get("_preserved_retention_mode", model.get("retention_mode", "source") or "source"),
                model.get("_preserved_creator_discovered_at", model.get("creator_discovered_at", "")),

                model.get("license", ""),
                model.get("pipeline", ""),

                json.dumps(
                    model.get("files", [])
                ),

                json.dumps(
                    model.get("card_data", {})
                ),

                model.get("library", ""),

                model.get("sensitive", 0),

                model.get("parameters", ""),

                model.get("quantization", ""),

                model.get("format", ""),

                model.get("parent_model", ""),

                model.get("has_media", 0),

                model.get("has_video", 0),

                model.get("preview_count", 0),

                model.get("gated", 0)
            )
        )

        model_id = c.lastrowid

    _register_model_source(c, model_id, model.get("source", ""), model.get("url", ""), model.get("model_key", ""), model)
    for link in model.get("_preserved_links", []):
        _register_model_source(c, model_id, link.get("source", ""), link.get("url", ""), link.get("model_key", ""))
    for hash_row in model.get("_preserved_hashes", []):
        c.execute(
            "INSERT OR REPLACE INTO model_file_hashes(model_id,source,model_key,sha256) VALUES(?,?,?,?)",
            (model_id, hash_row.get("source", ""), hash_row.get("model_key", ""), hash_row.get("sha256", "")),
        )

    # Replace all media using the same connection and a single transaction.
    # This keeps repositories with thousands of previews fast.
    media_changed = _replace_media_rows(
        c,
        model_id,
        model.get("source", ""),
        model.get("media", [])
    )

    conn.commit()
    conn.close()

    return {
        "model_id": model_id,
        "state": change_state,
        "media_changed": bool(media_changed),
        "media_count": len(model.get("media", []) or []) if media_changed else 0,
    }



def repair_canonical_architectures():
    """Repair legacy Other/generic architecture labels using cached metadata only."""
    from scanners.common import processors
    from utils.loader import load_architectures

    valid = set(load_architectures().keys())
    broad_legacy = {"", "other", "flux", "ltx", "scail"}

    conn = connect()
    conn.row_factory = sqlite3.Row
    repaired_sources = 0
    repaired_models = 0

    try:
        source_rows = conn.execute(
            "SELECT id, model_id, source_data FROM model_sources"
        ).fetchall()

        memberships = {}

        for row in source_rows:
            try:
                data = json.loads(row["source_data"] or "{}")
                if not isinstance(data, dict):
                    data = {}
            except Exception:
                data = {}

            classified = processors.classify_architecture(
                data.get("base_model"),
                data.get("architecture"),
                data.get("name"),
                data.get("display_name"),
                data.get("tags"),
                data.get("display_tags"),
                data.get("parent_model"),
                data.get("files"),
                data.get("card_data"),
            )
            existing = str(data.get("architecture") or "").strip()

            if classified in valid:
                bucket = memberships.setdefault(int(row["model_id"]), [])
                if classified not in bucket:
                    bucket.append(classified)

                if existing != classified and existing.casefold() in broad_legacy:
                    data["architecture"] = classified
                    conn.execute(
                        "UPDATE model_sources SET source_data=? WHERE id=?",
                        (json.dumps(data, ensure_ascii=False), row["id"]),
                    )
                    repaired_sources += 1

        model_rows = conn.execute(
            """SELECT id,name,display_name,base_model,architecture,tags,
                      display_tags,parent_model,files,card_data,description,url
               FROM models"""
        ).fetchall()

        for row in model_rows:
            existing = str(row["architecture"] or "").strip()

            classified = processors.classify_architecture(
                row["base_model"],
                row["name"],
                row["display_name"],
                row["tags"],
                row["display_tags"],
                row["parent_model"],
                row["files"],
                row["card_data"],
                row["description"],
                row["url"],
            )

            source_memberships = memberships.get(int(row["id"]), [])
            candidate = classified if classified in valid else (
                source_memberships[0] if source_memberships else "Other"
            )

            if existing in valid:
                continue
            if existing.casefold() not in broad_legacy:
                continue

            if candidate != existing:
                conn.execute(
                    "UPDATE models SET architecture=? WHERE id=?",
                    (candidate, row["id"]),
                )
                repaired_models += 1

        conn.commit()

    finally:
        conn.close()

    return {"models": repaired_models, "sources": repaired_sources}



def update_model(model):

    conn = connect()

    c = conn.cursor()


    c.execute(
        """
        UPDATE models SET

            name=?,
            display_name=?,
            author=?,
            sha=?,
            source=?,
            image=?,
            has_media=?,
            has_video=?,
            preview_count=?,
            gated=?,
            description=?,
            base_model=?,
            architecture=?,
            model_type=?,
            tags=?,
            display_tags=?,
            updated=?,
            downloads=?,
            likes=?,
            license=?,
            pipeline=?,
            files=?,
            card_data=?,
            library=?,
            sensitive=?,
            parameters=?,
            quantization=?,
            format=?,
            parent_model=?,
            model_key=?

        WHERE model_key=?

        """,
        (

            model.get("name", ""),

            model.get("display_name", ""),

            model.get("author", ""),

            model.get("sha", ""),

            model.get("source", ""),

            model.get("image", ""),

            model.get("has_media", 0),

            model.get("has_video", 0),

            model.get("preview_count", 0),

            model.get("gated", 0),

            model.get("description", ""),

            model.get("base_model", ""),

            model.get("architecture", ""),

            model.get("model_type", ""),

            model.get("tags", ""),

            json.dumps(
                model.get("display_tags", [])
            ),

            model.get("updated", ""),

            model.get("downloads", 0),

            model.get("likes", 0),

            model.get("license", ""),

            model.get("pipeline", ""),

            json.dumps(
                model.get("files", [])
            ),

            json.dumps(
                model.get("card_data", {})
            ),

            model.get(
                "library",
                ""
            ),

            model.get(
                "sensitive",
                0
            ),

            model.get(
                "parameters",
                ""
            ),

            model.get(
                "quantization",
                ""
            ),

            model.get(
                "format",
                ""
            ),

            model.get(
                "parent_model",
                ""
            ),

            model.get("model_key",""),

            model.get("model_key", "")

        )

    )


    c.execute(
        """
        SELECT id FROM models
        WHERE model_key = ? AND source = ?
        LIMIT 1
        """,
        (
            model.get("model_key", ""),
            model.get("source", "")
        )
    )

    existing = c.fetchone()

    if existing:
        _replace_media_rows(
            c,
            existing["id"],
            model.get("source", ""),
            model.get("media", [])
        )

    conn.commit()
    conn.close()


def model_exists(model_key, source=None):

    conn = connect()
    c = conn.cursor()

    if source:
        if source in {"civitai", "civitaired"}:
            c.execute(
                """
                SELECT id FROM models
                WHERE model_key = ?
                  AND source IN ('civitai','civitaired')
                LIMIT 1
                """,
                (model_key,)
            )
        else:
            c.execute(
                """
                SELECT id FROM models
                WHERE model_key = ?
                  AND source = ?
                LIMIT 1
                """,
                (model_key, source)
            )
    else:
        c.execute(
            """
            SELECT id FROM models
            WHERE model_key = ?
            LIMIT 1
            """,
            (model_key,)
        )

    result = c.fetchone()
    conn.close()

    return result is not None


def get_model(model_key, source=None):

    conn = connect()
    c = conn.cursor()

    if source:
        c.execute(
            """
            SELECT *
            FROM models
            WHERE model_key = ?
              AND source = ?
            LIMIT 1
            """,
            (model_key, source)
        )
    else:
        c.execute(
            """
            SELECT *
            FROM models
            WHERE model_key = ?
            LIMIT 1
            """,
            (model_key,)
        )

    model = c.fetchone()
    conn.close()

    return model



def update_gated_status(model_key, source, gated):

    conn = connect()
    c = conn.cursor()

    c.execute(
        """
        UPDATE models
        SET gated = ?
        WHERE model_key = ?
          AND source = ?
        """,
        (1 if gated else 0, model_key, source)
    )

    conn.commit()
    changed = c.rowcount
    conn.close()

    return changed > 0

def get_media(model_id):

    conn = connect()

    c = conn.cursor()

    c.execute(
        """
        SELECT *
        FROM model_media
        WHERE model_id = ?
        ORDER BY position
        """,
        (model_id,)
    )

    media = c.fetchall()

    conn.close()

    return media



def update_media_metadata(media_id, model_id, metadata):
    """Persist enriched metadata for one existing media row."""
    conn = connect()
    c = conn.cursor()
    payload = metadata if isinstance(metadata, str) else json.dumps(metadata or {}, ensure_ascii=False)
    c.execute(
        "UPDATE model_media SET metadata=? WHERE id=? AND model_id=?",
        (payload, int(media_id), int(model_id)),
    )
    changed = c.rowcount
    conn.commit()
    conn.close()
    return bool(changed)


def clear_media(model_id):

    conn = connect()

    c = conn.cursor()

    c.execute(
        """
        DELETE FROM model_media
        WHERE model_id = ?
        """,
        (model_id,)
    )

    conn.commit()

    conn.close()



def update_description(model_id, description):
    if not description:
        return
    conn = connect()
    conn.execute("UPDATE models SET description = ? WHERE id = ?", (description, model_id))
    conn.commit()
    conn.close()


def mark_viewed(model_id):
    """Mark one model Seen without allowing a transient SQLite lock to break
    the model-detail request.

    Returns True when persisted immediately, False when another writer kept the
    database busy through the short retry window.
    """
    import time

    delays = (0.0, 0.05, 0.15, 0.35, 0.75)

    for attempt, delay in enumerate(delays):
        if delay:
            time.sleep(delay)

        conn = None
        try:
            conn = connect()
            conn.execute(
                """
                UPDATE models
                SET viewed = 1
                WHERE id = ?
                """,
                (model_id,)
            )
            conn.commit()
            return True

        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                raise

            if attempt == len(delays) - 1:
                print(
                    f"MARK VIEWED: database busy; deferred model_id={model_id}"
                )
                return False

        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    return False




def mark_all_viewed():
    conn = connect()
    c = conn.cursor()
    c.execute("UPDATE models SET viewed = 1 WHERE viewed = 0")
    changed = c.rowcount
    conn.commit()
    conn.close()
    return changed


def mark_models_viewed(model_ids):
    ids = [int(model_id) for model_id in model_ids if str(model_id).isdigit()]
    if not ids:
        return 0
    conn = connect()
    c = conn.cursor()
    placeholders = ",".join("?" for _ in ids)
    c.execute(f"UPDATE models SET viewed = 1 WHERE viewed = 0 AND id IN ({placeholders})", ids)
    changed = c.rowcount
    conn.commit()
    conn.close()
    return changed

def add_media(
    model_id,
    source,
    media_type,
    url,
    thumbnail="",
    position=0,
    filename="",
    path="",
    metadata=None
):

    conn = connect()
    c = conn.cursor()

    if metadata is None:
        metadata = {}
    if not isinstance(metadata, str):
        metadata = json.dumps(metadata, ensure_ascii=False)

    c.execute(
        """
        INSERT INTO model_media
        (model_id, source, type, url, thumbnail, filename, path, metadata, position)
        VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (model_id, source, media_type, url, thumbnail, filename, path, metadata, position)
    )

    conn.commit()
    conn.close()


def get_media_count(model_id):

    conn = connect()

    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT COUNT(*)
        FROM model_media
        WHERE model_id = ?
        """,
        (model_id,)
    )

    count = cursor.fetchone()[0]

    conn.close()

    return count



def set_model_favorite(model_id, favorite):
    conn = connect()
    cur = conn.cursor()
    cur.execute("UPDATE models SET favorite = ? WHERE id = ?", (1 if favorite else 0, model_id))
    changed = cur.rowcount
    conn.commit()
    conn.close()
    return bool(changed)


def ensure_creator(name):
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc).isoformat()
    conn = connect()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO creators (name, favorite, first_seen, last_seen) VALUES (?, 0, ?, ?)",
        (name, now, now)
    )
    cur.execute("UPDATE creators SET last_seen = ? WHERE name = ? COLLATE NOCASE", (now, name))
    conn.commit()
    row = cur.execute("SELECT * FROM creators WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
    conn.close()
    return dict(row) if row else None


def remember_creator_source_identity(creator_name, source, source_creator_id, profile_url="", discovered_via="observed"):
    """Persist a provider-specific creator identity independently from model rows."""
    creator_name = str(creator_name or "").strip()
    source = str(source or "").strip().lower()
    source_creator_id = str(source_creator_id or "").strip()
    if not creator_name or not source or not source_creator_id:
        return False

    ensure_creator(creator_name)
    now = datetime.now(timezone.utc).isoformat()
    conn = connect()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO creator_sources
            (creator_name, source, source_creator_id, profile_url, discovered_via, first_seen, last_seen)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source, source_creator_id) DO UPDATE SET
            creator_name=excluded.creator_name,
            profile_url=CASE WHEN excluded.profile_url <> '' THEN excluded.profile_url ELSE creator_sources.profile_url END,
            discovered_via=CASE
                WHEN creator_sources.discovered_via IN ('explicit','discovery') THEN creator_sources.discovered_via
                ELSE excluded.discovered_via
            END,
            last_seen=excluded.last_seen
        """,
        (creator_name, source, source_creator_id, str(profile_url or ""), str(discovered_via or "observed"), now, now),
    )
    conn.commit()
    conn.close()
    return True


def get_creator_source_identities(source=None, creator_name=None):
    conn = connect()
    sql = "SELECT creator_name, source, source_creator_id, profile_url, discovered_via, first_seen, last_seen FROM creator_sources"
    where = []
    params = []
    if source:
        where.append("source=?")
        params.append(str(source).strip().lower())
    if creator_name:
        where.append("lower(creator_name)=lower(?)")
        params.append(str(creator_name).strip())
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY lower(creator_name), source_creator_id"
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def resolve_source_creator(source, model_key="", url="", source_data=None):
    """
    Resolve the creator attached to one source link.

    Resolution order:
      1. Author preserved in the source-specific snapshot.
      2. Deterministic creator encoded by the source model key / URL.
      3. Persistent creator_sources identity for opaque provider IDs.

    This is source-count agnostic: future providers can participate without
    changing the merged-card rendering code.
    """
    source = str(source or "").strip().lower()
    model_key = str(model_key or "").strip()

    snapshot = source_data or {}

    if isinstance(snapshot, str):
        try:
            snapshot = json.loads(snapshot or "{}")
        except Exception:
            snapshot = {}

    if not isinstance(snapshot, dict):
        snapshot = {}

    # Best source of truth: the exact uploader recorded when this source
    # discovered the model.
    author = str(snapshot.get("author") or "").strip()
    if author:
        return author

    # Hugging Face / ModelScope repository keys encode their owner directly.
    author = _infer_source_author_from_key(source, model_key, url)
    if author:
        return author

    # Opaque providers such as TensorHub cannot recover the creator from the
    # model URL/key. Their source metadata may contain a stable owner ID.
    source_creator_id = ""

    card_data = snapshot.get("card_data") or {}
    if isinstance(card_data, str):
        try:
            card_data = json.loads(card_data or "{}")
        except Exception:
            card_data = {}

    if isinstance(card_data, dict):
        if source == "tensorhub":
            source_creator_id = str(
                ((card_data.get("tensorhub") or {}).get("owner_id")) or ""
            ).strip()

    if not source_creator_id:
        return ""

    conn = connect()

    row = conn.execute(
        """
        SELECT creator_name
        FROM creator_sources
        WHERE lower(source)=lower(?)
          AND source_creator_id=?
        LIMIT 1
        """,
        (source, source_creator_id),
    ).fetchone()

    conn.close()

    return str(row["creator_name"] or "").strip() if row else ""


def set_creator_favorite(name, favorite):
    ensure_creator(name)
    conn = connect()
    cur = conn.cursor()
    cur.execute(
        "UPDATE creators SET favorite = ? WHERE name = ? COLLATE NOCASE",
        (1 if favorite else 0, name)
    )
    conn.commit()
    conn.close()
    return True



def get_blocked_creators(source=None):
    conn = connect()
    if source:
        rows = conn.execute(
            "SELECT source, creator, blocked_at FROM blocked_creators WHERE source=? ORDER BY lower(creator)",
            (source,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT source, creator, blocked_at FROM blocked_creators ORDER BY lower(creator), source"
        ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_universal_blocked_creators():
    conn = connect()
    rows = conn.execute(
        "SELECT creator, blocked_at FROM universal_blocked_creators ORDER BY lower(creator)"
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def is_universal_creator_blocked(creator):
    creator = str(creator or "").strip()
    if not creator:
        return False
    conn = connect()
    row = conn.execute(
        "SELECT 1 FROM universal_blocked_creators WHERE lower(creator)=lower(?) LIMIT 1",
        (creator,),
    ).fetchone()
    conn.close()
    return row is not None


def set_universal_creator_blocked(creator, blocked):
    creator = str(creator or "").strip()
    if not creator:
        return False
    conn = connect()
    if blocked:
        conn.execute(
            "INSERT OR IGNORE INTO universal_blocked_creators(creator, blocked_at) VALUES(?, ?)",
            (creator, datetime.now(timezone.utc).isoformat()),
        )
    else:
        conn.execute(
            "DELETE FROM universal_blocked_creators WHERE lower(creator)=lower(?)",
            (creator,),
        )
    conn.commit()
    conn.close()
    return True


def get_blocked_creator_set(source):
    source_key = str(source or "").strip().lower()
    user_blocked = {
        str(row["creator"] or "").strip().casefold()
        for row in get_blocked_creators(source_key)
    }
    universal_blocked = {
        str(row["creator"] or "").strip().casefold()
        for row in get_universal_blocked_creators()
    }
    return user_blocked | universal_blocked | set(HARD_BLOCKED_CREATORS.get(source_key, set()))


def purge_hard_blocked_creators():
    """Remove built-in safety-blocked source creators from an existing library.

    These exclusions are deliberately not written to blocked_creators, so they
    never appear as a user-toggleable preference. Existing cards are purged on
    startup and future scans receive the same exclusion through
    get_blocked_creator_set().
    """
    conn = connect()
    conn.row_factory = sqlite3.Row
    removed = 0
    try:
        # model_sources/source_data exists after migrate(). Canonical rows are
        # also checked because some older source links predate source snapshots.
        for source, creators in HARD_BLOCKED_CREATORS.items():
            for creator_key in creators:
                ids = {
                    int(row["id"])
                    for row in conn.execute(
                        "SELECT id FROM models WHERE lower(source)=? AND lower(author)=?",
                        (source, creator_key),
                    ).fetchall()
                }

                try:
                    links = conn.execute(
                        "SELECT model_id, source_data FROM model_sources WHERE lower(source)=?",
                        (source,),
                    ).fetchall()
                    for link in links:
                        try:
                            snapshot = json.loads(link["source_data"] or "{}")
                        except Exception:
                            snapshot = {}
                        author = str((snapshot or {}).get("author") or "").strip().casefold()
                        if author == creator_key:
                            ids.add(int(link["model_id"]))
                except sqlite3.OperationalError:
                    pass

                if ids:
                    marks = ",".join("?" for _ in ids)
                    params = list(ids)
                    # Remove the complete card when it is known to originate
                    # from a hard-blocked source creator. This prevents cached
                    # previews or merged source metadata from keeping the card
                    # visible after the source link itself is removed.
                    for table in ("model_media", "model_sources", "model_file_hashes"):
                        try:
                            conn.execute(f"DELETE FROM {table} WHERE model_id IN ({marks})", params)
                        except sqlite3.OperationalError:
                            pass
                    try:
                        conn.execute(
                            f"UPDATE download_history SET model_id=NULL WHERE model_id IN ({marks})",
                            params,
                        )
                    except sqlite3.OperationalError:
                        pass
                    cur = conn.execute(f"DELETE FROM models WHERE id IN ({marks})", params)
                    removed += max(0, int(cur.rowcount or 0))

                try:
                    conn.execute(
                        "DELETE FROM blocked_creators WHERE lower(source)=? AND lower(creator)=?",
                        (source, creator_key),
                    )
                except sqlite3.OperationalError:
                    pass

                try:
                    conn.execute(
                        "DELETE FROM creator_sources WHERE lower(source)=? AND lower(creator_name)=?",
                        (source, creator_key),
                    )
                except sqlite3.OperationalError:
                    pass

        # Some providers expose stable opaque creator IDs. Use those for exact
        # hard blocks when a display name is ambiguous or can change.
        for source, creator_ids in HARD_BLOCKED_SOURCE_CREATOR_IDS.items():
            creator_ids = {str(value or "").strip() for value in creator_ids if str(value or "").strip()}
            if not creator_ids:
                continue
            ids = set()

            if source == "tensorhub":
                try:
                    rows = conn.execute(
                        "SELECT id, card_data FROM models WHERE lower(source)=?",
                        (source,),
                    ).fetchall()
                    for row in rows:
                        try:
                            card = json.loads(row["card_data"] or "{}")
                        except Exception:
                            card = {}
                        owner_id = str(((card.get("tensorhub") or {}).get("owner_id")) or "").strip()
                        if owner_id in creator_ids:
                            ids.add(int(row["id"]))
                except sqlite3.OperationalError:
                    pass

                try:
                    links = conn.execute(
                        "SELECT model_id, source_data FROM model_sources WHERE lower(source)=?",
                        (source,),
                    ).fetchall()
                    for link in links:
                        try:
                            snapshot = json.loads(link["source_data"] or "{}")
                        except Exception:
                            snapshot = {}
                        card = snapshot.get("card_data") or {}
                        if isinstance(card, str):
                            try:
                                card = json.loads(card or "{}")
                            except Exception:
                                card = {}
                        owner_id = str(((card.get("tensorhub") or {}).get("owner_id")) or "").strip() if isinstance(card, dict) else ""
                        if owner_id in creator_ids:
                            ids.add(int(link["model_id"]))
                except sqlite3.OperationalError:
                    pass

            if ids:
                marks = ",".join("?" for _ in ids)
                params = list(ids)
                for table in ("model_media", "model_sources", "model_file_hashes"):
                    try:
                        conn.execute(f"DELETE FROM {table} WHERE model_id IN ({marks})", params)
                    except sqlite3.OperationalError:
                        pass
                try:
                    conn.execute(
                        f"UPDATE download_history SET model_id=NULL WHERE model_id IN ({marks})",
                        params,
                    )
                except sqlite3.OperationalError:
                    pass
                cur = conn.execute(f"DELETE FROM models WHERE id IN ({marks})", params)
                removed += max(0, int(cur.rowcount or 0))

            try:
                marks = ",".join("?" for _ in creator_ids)
                hard_names = [
                    str(row["creator_name"] or "").strip()
                    for row in conn.execute(
                        f"SELECT creator_name FROM creator_sources WHERE lower(source)=? AND source_creator_id IN ({marks})",
                        [source, *creator_ids],
                    ).fetchall()
                    if str(row["creator_name"] or "").strip()
                ]
                for hard_name in hard_names:
                    conn.execute(
                        "DELETE FROM blocked_creators WHERE lower(source)=? AND lower(creator)=lower(?)",
                        (source, hard_name),
                    )
                conn.execute(
                    f"DELETE FROM creator_sources WHERE lower(source)=? AND source_creator_id IN ({marks})",
                    [source, *creator_ids],
                )
            except sqlite3.OperationalError:
                pass

        conn.commit()
    finally:
        conn.close()

    if removed:
        print(f"Built-in safety exclusions: removed {removed} blocked model(s)")
    return removed


def is_creator_blocked(source, creator):
    if not source or not creator:
        return False
    if is_hard_blocked_creator(source, creator) or is_universal_creator_blocked(creator):
        return True
    conn = connect()
    row = conn.execute(
        "SELECT 1 FROM blocked_creators WHERE source=? AND lower(creator)=lower(?) LIMIT 1",
        (source, creator)
    ).fetchone()
    conn.close()
    return row is not None


def block_creator(source, creator):
    from datetime import datetime, timezone, timedelta
    source = str(source or "").strip().lower()
    creator = str(creator or "").strip()
    if not source or not creator:
        return False
    conn = connect()
    conn.execute(
        "INSERT OR IGNORE INTO blocked_creators(source,creator,blocked_at) VALUES(?,?,?)",
        (source, creator, datetime.now(timezone.utc).isoformat())
    )
    conn.commit(); conn.close(); return True


def unblock_creator(source, creator):
    if is_hard_blocked_creator(source, creator):
        return False
    conn = connect()
    cur = conn.execute(
        "DELETE FROM blocked_creators WHERE source=? AND lower(creator)=lower(?)",
        (str(source or "").strip().lower(), str(creator or "").strip())
    )
    changed = cur.rowcount
    conn.commit(); conn.close(); return bool(changed)


def blocked_creator_model_count(source, creator):
    conn = connect()
    row = conn.execute(
        "SELECT COUNT(*) FROM models WHERE source=? AND lower(author)=lower(?)",
        (source, creator)
    ).fetchone()
    conn.close(); return int(row[0] or 0)



def record_download(
    model_id, source, model_key, source_file_id, file_key, filename, sha,
    source_updated, file_fingerprint, version_id="", version_name=""
):
    """Record a AbyssBeacon-initiated download without depending on the model row surviving retention."""
    if not source or not model_key or not file_fingerprint:
        return False
    conn = connect()
    conn.execute(
        """
        INSERT INTO download_history
            (model_id, source, model_key, source_file_id, file_key, filename, sha,
             source_updated, file_fingerprint, version_id, version_name, downloaded_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            model_id,
            str(source),
            str(model_key),
            str(source_file_id or ""),
            str(file_key or ""),
            str(filename or ""),
            str(sha or ""),
            str(source_updated or ""),
            str(file_fingerprint),
            str(version_id or ""),
            str(version_name or ""),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit(); conn.close()
    return True


def record_installed_file(
    model_id, source, model_key, source_file_id, file_fingerprint,
    local_path, filename, version_id="", version_name=""
):
    if not source or not local_path:
        return False
    conn = connect()
    conn.execute(
        """
        INSERT INTO installed_files
            (model_id, source, model_key, source_file_id, file_fingerprint,
             local_path, filename, version_id, version_name, installed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(local_path) DO UPDATE SET
            model_id=excluded.model_id,
            source=excluded.source,
            model_key=excluded.model_key,
            source_file_id=excluded.source_file_id,
            file_fingerprint=excluded.file_fingerprint,
            filename=excluded.filename,
            version_id=excluded.version_id,
            version_name=excluded.version_name,
            installed_at=excluded.installed_at
        """,
        (
            model_id, str(source), str(model_key or ""), str(source_file_id or ""),
            str(file_fingerprint or ""), str(local_path), str(filename or ""),
            str(version_id or ""), str(version_name or ""),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit(); conn.close(); return True


def get_download_history_lookup():
    """Return history grouped by (source, model_key) for fast card annotation."""
    conn = connect()
    rows = conn.execute(
        """SELECT source, model_key, source_file_id, file_key, filename, sha,
                  source_updated, file_fingerprint, version_id, version_name, downloaded_at
           FROM download_history ORDER BY downloaded_at DESC"""
    ).fetchall()
    conn.close()
    result = {}
    for row in rows:
        key = (str(row["source"] or "").lower(), str(row["model_key"] or ""))
        result.setdefault(key, []).append(dict(row))
    return result


def add_download_queue_item(model_id, source, model_key, version_id="", version_name="", model_name="", source_url="", release_at=""):
    source = str(source or "").strip().lower()
    if source not in {"civitai", "civitaired"} or not str(model_key or "").strip():
        return False
    now = datetime.now(timezone.utc).isoformat()
    conn = connect()
    conn.execute(
        """
        INSERT INTO download_queue
            (model_id, source, model_key, version_id, version_name, model_name, source_url, release_at, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'waiting', ?)
        ON CONFLICT(source, model_key, version_id, version_name) DO UPDATE SET
            model_id=excluded.model_id,
            model_name=excluded.model_name,
            source_url=excluded.source_url,
            release_at=CASE WHEN excluded.release_at<>'' THEN excluded.release_at ELSE download_queue.release_at END,
            status=CASE WHEN download_queue.status='completed' THEN 'waiting' ELSE download_queue.status END,
            last_error=''
        """,
        (
            int(model_id), source, str(model_key), str(version_id or ""), str(version_name or ""),
            str(model_name or ""), str(source_url or ""), str(release_at or ""), now,
        ),
    )
    conn.commit(); conn.close()
    return True


def remove_download_queue_item(queue_id):
    conn = connect()
    cur = conn.execute("DELETE FROM download_queue WHERE id=?", (int(queue_id),))
    changed = int(cur.rowcount or 0)
    conn.commit(); conn.close()
    return changed


def get_download_queue(include_completed=False):
    conn = connect()
    where = "" if include_completed else "WHERE status <> 'completed'"
    rows = conn.execute(
        f"""
        SELECT id, model_id, source, model_key, version_id, version_name, model_name,
               source_url, release_at, status, last_checked, last_error, created_at
        FROM download_queue
        {where}
        ORDER BY
            CASE status WHEN 'ready' THEN 0 WHEN 'waiting' THEN 1 WHEN 'error' THEN 2 ELSE 3 END,
            created_at
        """
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_download_queue_keys(model_id=None):
    conn = connect()
    if model_id is None:
        rows = conn.execute(
            "SELECT source, model_key, version_id, version_name FROM download_queue WHERE status <> 'completed'"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT source, model_key, version_id, version_name FROM download_queue WHERE model_id=? AND status <> 'completed'",
            (int(model_id),),
        ).fetchall()
    conn.close()
    return {
        (
            str(row["source"] or "").lower(),
            str(row["model_key"] or ""),
            str(row["version_id"] or ""),
            str(row["version_name"] or "").casefold(),
        )
        for row in rows
    }


def update_download_queue_item(queue_id, *, status=None, last_checked=None, last_error=None, release_at=None):
    sets=[]; values=[]
    for field,value in (
        ("status",status), ("last_checked",last_checked), ("last_error",last_error), ("release_at",release_at)
    ):
        if value is not None:
            sets.append(f"{field}=?"); values.append(str(value))
    if not sets:
        return False
    values.append(int(queue_id))
    conn=connect()
    conn.execute(f"UPDATE download_queue SET {', '.join(sets)} WHERE id=?", values)
    conn.commit(); conn.close()
    return True


def add_download_watch_item(
    model_id, source, model_key, version_id="", version_name="", model_name="",
    source_url="", file_id="", file_name="", file_fingerprint="", file_index=-1,
    file_size_display=""
):
    source = str(source or "").strip().lower()
    file_name = str(file_name or "").strip()
    if source not in {"civitai", "civitaired"} or not str(model_key or "").strip() or not file_name:
        return False
    now = datetime.now(timezone.utc).isoformat()
    conn = connect()
    conn.execute(
        """
        INSERT INTO download_watchlist
            (model_id, source, model_key, version_id, version_name, model_name, source_url,
             file_id, file_name, file_fingerprint, file_index, file_size_display, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'waiting', ?)
        ON CONFLICT(source, model_key, version_id, version_name, file_id, file_name) DO UPDATE SET
            model_id=excluded.model_id,
            model_name=excluded.model_name,
            source_url=excluded.source_url,
            file_fingerprint=CASE WHEN excluded.file_fingerprint<>'' THEN excluded.file_fingerprint ELSE download_watchlist.file_fingerprint END,
            file_index=CASE WHEN excluded.file_index>=0 THEN excluded.file_index ELSE download_watchlist.file_index END,
            file_size_display=CASE WHEN excluded.file_size_display<>'' THEN excluded.file_size_display ELSE download_watchlist.file_size_display END,
            status=CASE WHEN download_watchlist.status='available' THEN 'available' ELSE 'waiting' END,
            last_error=''
        """,
        (
            int(model_id), source, str(model_key), str(version_id or ""), str(version_name or ""),
            str(model_name or ""), str(source_url or ""), str(file_id or ""), file_name,
            str(file_fingerprint or ""), int(file_index if file_index is not None else -1),
            str(file_size_display or ""), now,
        ),
    )
    conn.commit(); conn.close()
    return True


def remove_download_watch_item(watch_id):
    conn = connect()
    cur = conn.execute("DELETE FROM download_watchlist WHERE id=?", (int(watch_id),))
    changed = int(cur.rowcount or 0)
    conn.commit(); conn.close()
    return changed


def get_download_watchlist(include_dismissed=True):
    conn = connect()
    where = "" if include_dismissed else "WHERE status='available' AND COALESCE(dismissed_at,'')=''"
    rows = conn.execute(
        f"""
        SELECT id, model_id, source, model_key, version_id, version_name, model_name,
               source_url, file_id, file_name, file_fingerprint, file_index, file_size_display,
               status, last_checked, last_error, available_at, dismissed_at, created_at
        FROM download_watchlist
        {where}
        ORDER BY
            CASE status WHEN 'available' THEN 0 WHEN 'waiting' THEN 1 WHEN 'error' THEN 2 ELSE 3 END,
            created_at
        """
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_download_watch_keys(model_id=None):
    conn = connect()
    if model_id is None:
        rows = conn.execute(
            "SELECT source, model_key, version_id, version_name, file_id, file_name FROM download_watchlist"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT source, model_key, version_id, version_name, file_id, file_name FROM download_watchlist WHERE model_id=?",
            (int(model_id),),
        ).fetchall()
    conn.close()
    return {
        (
            str(row["source"] or "").lower(),
            str(row["model_key"] or ""),
            str(row["version_id"] or ""),
            str(row["version_name"] or "").casefold(),
            str(row["file_id"] or ""),
            str(row["file_name"] or "").casefold(),
        )
        for row in rows
    }


def update_download_watch_item(
    watch_id, *, status=None, last_checked=None, last_error=None, available_at=None,
    dismissed_at=None, file_id=None, file_name=None, file_fingerprint=None,
    file_index=None, file_size_display=None
):
    sets=[]; values=[]
    for field, value in (
        ("status", status), ("last_checked", last_checked), ("last_error", last_error),
        ("available_at", available_at), ("dismissed_at", dismissed_at),
        ("file_id", file_id), ("file_name", file_name), ("file_fingerprint", file_fingerprint),
        ("file_index", file_index), ("file_size_display", file_size_display),
    ):
        if value is not None:
            sets.append(f"{field}=?")
            values.append(int(value) if field == "file_index" else str(value))
    if not sets:
        return False
    values.append(int(watch_id))
    conn=connect()
    conn.execute(f"UPDATE download_watchlist SET {', '.join(sets)} WHERE id=?", values)
    conn.commit(); conn.close()
    return True


def downloaded_model_keys():
    conn = connect()
    rows = conn.execute("SELECT DISTINCT lower(source), model_key FROM download_history").fetchall()
    conn.close()
    return {(str(row[0] or ""), str(row[1] or "")) for row in rows}


def downloaded_model_ids():
    """Return current AbyssBeacon card IDs known to have downloaded/installed files."""
    conn = connect()
    rows = conn.execute(
        """
        SELECT DISTINCT model_id FROM download_history WHERE model_id IS NOT NULL
        UNION
        SELECT DISTINCT model_id FROM installed_files WHERE model_id IS NOT NULL
        """
    ).fetchall()
    conn.close()
    return {int(row[0]) for row in rows if row[0] is not None}



def get_installed_files_for_model(model_id):
    """Return exact local files AbyssBeacon recorded for one card."""
    conn = connect()
    rows = conn.execute(
        """SELECT id, model_id, source, model_key, local_path, filename, version_id, version_name, installed_at
           FROM installed_files WHERE model_id=? ORDER BY installed_at DESC, id DESC""",
        (int(model_id),),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_download_history_for_model(model_id):
    """Return download-history rows for one card, newest first."""
    conn = connect()
    rows = conn.execute(
        """SELECT id, model_id, source, model_key, source_file_id, file_key,
                  filename, file_fingerprint, version_id, version_name, downloaded_at
           FROM download_history WHERE model_id=? ORDER BY downloaded_at DESC, id DESC""",
        (int(model_id),),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def forget_download_history_records(model_id, history_ids):
    """Forget selected download-history identities without losing local-file ownership.

    installed_files is deliberately preserved. It is the safety inventory used by
    Delete Local Files even after the user chooses to remove download history.
    """
    ids = sorted({int(value) for value in (history_ids or []) if str(value).isdigit()})
    if not ids:
        return {"history": 0, "installed": 0}
    conn = connect()
    marks = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT * FROM download_history WHERE model_id=? AND id IN ({marks})",
        [int(model_id), *ids],
    ).fetchall()
    if not rows:
        conn.close()
        return {"history": 0, "installed": 0}

    clauses = []
    params = [int(model_id)]
    for row in rows:
        fp = str(row["file_fingerprint"] or "").strip()
        fid = str(row["source_file_id"] or "").strip()
        source = str(row["source"] or "").strip().lower()
        filename = str(row["filename"] or "").strip()
        if fp:
            clauses.append("file_fingerprint=?")
            params.append(fp)
        elif fid:
            clauses.append("(lower(source)=? AND source_file_id=?)")
            params.extend([source, fid])
        elif filename:
            clauses.append("(lower(source)=? AND filename=?)")
            params.extend([source, filename])

    if clauses:
        where = " OR ".join(f"({clause})" for clause in clauses)
        cur = conn.execute(
            f"DELETE FROM download_history WHERE model_id=? AND ({where})", params
        )
    else:
        cur = conn.execute(
            f"DELETE FROM download_history WHERE model_id=? AND id IN ({marks})",
            [int(model_id), *ids],
        )
    history_deleted = int(cur.rowcount or 0)
    conn.commit()
    conn.close()
    return {"history": history_deleted, "installed": 0}


def clear_tracking_for_installed_records(model_id, installed_ids):
    """Remove DB tracking for selected installed files after physical deletion."""
    ids = sorted({int(value) for value in (installed_ids or []) if str(value).isdigit()})
    if not ids:
        return {"history": 0, "installed": 0}
    conn = connect()
    marks = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT * FROM installed_files WHERE model_id=? AND id IN ({marks})",
        [int(model_id), *ids],
    ).fetchall()
    if not rows:
        conn.close()
        return {"history": 0, "installed": 0}

    clauses = []
    params = [int(model_id)]
    for row in rows:
        fp = str(row["file_fingerprint"] or "").strip()
        fid = str(row["source_file_id"] or "").strip()
        source = str(row["source"] or "").strip().lower()
        filename = str(row["filename"] or "").strip()
        if fp:
            clauses.append("file_fingerprint=?")
            params.append(fp)
        elif fid:
            clauses.append("(lower(source)=? AND source_file_id=?)")
            params.extend([source, fid])
        elif filename:
            clauses.append("(lower(source)=? AND filename=?)")
            params.extend([source, filename])

    history_deleted = 0
    if clauses:
        where = " OR ".join(f"({clause})" for clause in clauses)
        cur = conn.execute(
            f"DELETE FROM download_history WHERE model_id=? AND ({where})", params
        )
        history_deleted = int(cur.rowcount or 0)
    cur = conn.execute(
        f"DELETE FROM installed_files WHERE model_id=? AND id IN ({marks})",
        [int(model_id), *ids],
    )
    installed_deleted = int(cur.rowcount or 0)
    conn.commit()
    conn.close()
    return {"history": history_deleted, "installed": installed_deleted}


def clear_installed_files_for_model(model_id):
    conn = connect()
    cur = conn.execute("DELETE FROM installed_files WHERE model_id=?", (int(model_id),))
    changed = cur.rowcount
    conn.commit(); conn.close()
    return int(changed or 0)


def clear_download_history_for_model(model_id):
    """Forget download/update tracking for one current AbyssBeacon card."""
    conn = connect()
    cur = conn.execute("DELETE FROM download_history WHERE model_id=?", (int(model_id),))
    changed = cur.rowcount
    conn.commit()
    conn.close()
    return int(changed or 0)


def get_recent_download_history(limit=30):
    """Recent AbyssBeacon download records for the navbar history viewer."""
    limit = max(1, min(200, int(limit or 30)))
    conn = connect()
    rows = conn.execute(
        """
        SELECT
            dh.id,
            dh.model_id,
            dh.source,
            dh.model_key,
            dh.filename,
            dh.downloaded_at,
            COALESCE(NULLIF(m.display_name,''), NULLIF(m.name,''), dh.filename, dh.model_key) AS model_name,
            (
                SELECT i.local_path
                FROM installed_files i
                WHERE i.model_id=dh.model_id
                  AND lower(i.source)=lower(dh.source)
                  AND (i.model_key=dh.model_key OR dh.model_key='')
                ORDER BY i.installed_at DESC, i.id DESC
                LIMIT 1
            ) AS local_path
        FROM download_history dh
        LEFT JOIN models m ON m.id = dh.model_id
        ORDER BY dh.downloaded_at DESC, dh.id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]

def preview_download_history_cleanup(mode="all", days=0):
    conn = connect()
    now = datetime.now(timezone.utc)
    mode = str(mode or "all")
    days = max(0, int(days or 0))
    if mode == "recent_hour":
        cutoff = (now - timedelta(hours=1)).isoformat()
        count = conn.execute("SELECT COUNT(*) FROM download_history WHERE downloaded_at >= ?", (cutoff,)).fetchone()[0]
    elif mode == "recent_1":
        cutoff = (now - timedelta(days=1)).isoformat()
        count = conn.execute("SELECT COUNT(*) FROM download_history WHERE downloaded_at >= ?", (cutoff,)).fetchone()[0]
    elif mode == "recent_7":
        cutoff = (now - timedelta(days=7)).isoformat()
        count = conn.execute("SELECT COUNT(*) FROM download_history WHERE downloaded_at >= ?", (cutoff,)).fetchone()[0]
    elif mode == "older_than":
        cutoff = (now - timedelta(days=days)).isoformat()
        count = conn.execute("SELECT COUNT(*) FROM download_history WHERE downloaded_at < ?", (cutoff,)).fetchone()[0]
    else:
        count = conn.execute("SELECT COUNT(*) FROM download_history").fetchone()[0]
    conn.close()
    return int(count)


def clear_download_history(mode="all", days=0):
    conn = connect()
    now = datetime.now(timezone.utc)
    mode = str(mode or "all")
    days = max(0, int(days or 0))
    if mode == "recent_hour":
        cutoff = (now - timedelta(hours=1)).isoformat()
        cur = conn.execute("DELETE FROM download_history WHERE downloaded_at >= ?", (cutoff,))
    elif mode == "recent_1":
        cutoff = (now - timedelta(days=1)).isoformat()
        cur = conn.execute("DELETE FROM download_history WHERE downloaded_at >= ?", (cutoff,))
    elif mode == "recent_7":
        cutoff = (now - timedelta(days=7)).isoformat()
        cur = conn.execute("DELETE FROM download_history WHERE downloaded_at >= ?", (cutoff,))
    elif mode == "older_than":
        cutoff = (now - timedelta(days=days)).isoformat()
        cur = conn.execute("DELETE FROM download_history WHERE downloaded_at < ?", (cutoff,))
    else:
        cur = conn.execute("DELETE FROM download_history")
    changed = cur.rowcount
    conn.commit(); conn.close()
    return int(changed or 0)

def _abyssbeacon_renamed_local_path(value):
    """Return the post-rename equivalent of one legacy local install path."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    renamed = re.sub(
        r"(?i)(^|[\\/])ModelRadar-Other(?=([\\/]|$))",
        lambda match: match.group(1) + "AbyssBeacon-Other",
        raw,
    )
    renamed = re.sub(
        r"(?i)(^|[\\/])ModelRadar(?=([\\/]|$))",
        lambda match: match.group(1) + "AbyssBeacon",
        renamed,
    )
    return renamed


def _migrate_abyssbeacon_installed_paths(c):
    """Repair exact installed-file paths after a manual ModelRadar folder rename.

    Nothing is rewritten until the new AbyssBeacon path exists and the old path
    no longer exists. That lets users apply the code patch first and rename the
    handful of folders whenever convenient without risking their tracking data.
    """
    migrated = 0
    deduped = 0
    sidecars = 0
    try:
        rows = c.execute(
            "SELECT id, local_path FROM installed_files "
            "WHERE local_path LIKE '%ModelRadar%'"
        ).fetchall()
    except sqlite3.OperationalError:
        return {"migrated": 0, "deduped": 0, "sidecars": 0}

    touched_folders = set()
    for row in rows:
        old_path = str(row["local_path"] or "").strip()
        new_path = _abyssbeacon_renamed_local_path(old_path)
        if not new_path or new_path == old_path:
            continue
        # Only repair a path after the user's manual folder rename is visible
        # on disk. If both names exist, leave the record alone rather than guess.
        if os.path.exists(old_path) or not os.path.exists(new_path):
            continue
        existing = c.execute(
            "SELECT id FROM installed_files WHERE local_path=? AND id<>? LIMIT 1",
            (new_path, int(row["id"])),
        ).fetchone()
        if existing:
            c.execute("DELETE FROM installed_files WHERE id=?", (int(row["id"]),))
            deduped += 1
        else:
            c.execute(
                "UPDATE installed_files SET local_path=? WHERE id=?",
                (new_path, int(row["id"])),
            )
            migrated += 1
        touched_folders.add(os.path.dirname(new_path))

    # Folder renames move the old generated sidecar with the model. Rename that
    # file too so the local library is fully AbyssBeacon-branded and future
    # downloads update one sidecar instead of creating a second copy.
    for folder in touched_folders:
        if not folder:
            continue
        legacy_info = os.path.join(folder, "ModelRadar Info.txt")
        current_info = os.path.join(folder, "AbyssBeacon Info.txt")
        try:
            if os.path.isfile(legacy_info) and not os.path.exists(current_info):
                os.replace(legacy_info, current_info)
                sidecars += 1
            elif os.path.isfile(legacy_info) and os.path.isfile(current_info):
                os.remove(legacy_info)
                sidecars += 1
        except OSError:
            pass

    return {"migrated": migrated, "deduped": deduped, "sidecars": sidecars}


def migrate():

    conn = connect()
    c = conn.cursor()


    columns = [
        ("display_name", "TEXT"),
        ("display_tags", "TEXT"),
        ("viewed", "INTEGER DEFAULT 0"),
        ("favorite", "INTEGER DEFAULT 0"),
        ("first_seen", "TEXT"),
        ("last_seen", "TEXT"),
        ("metadata_hash", "TEXT"),
        ("last_changed", "TEXT"),
        ("retention_mode", "TEXT DEFAULT 'source'"),
        ("creator_discovered_at", "TEXT"),
        ("has_media", "INTEGER DEFAULT 0"),
        ("has_video", "INTEGER DEFAULT 0"),
        ("preview_count", "INTEGER DEFAULT 0"),
        ("gated", "INTEGER DEFAULT 0"),
        ("files", "TEXT"),
        ("card_data", "TEXT"),
        ("library", "TEXT"),
        ("sensitive", "INTEGER DEFAULT 0"),
        ("parameters", "TEXT"),
        ("quantization", "TEXT"),
        ("format", "TEXT"),
        ("parent_model", "TEXT"),
        ("sha", "TEXT"),
        ("model_key", "TEXT"),
    ]


    existing = [
        row["name"]
        for row in c.execute(
            "PRAGMA table_info(models)"
        )
    ]


    for name, definition in columns:

        if name not in existing:

            print(f"Adding database column: {name}")

            c.execute(
                f"""
                ALTER TABLE models
                ADD COLUMN {name} {definition}
                """
            )

    media_existing = [
        row["name"]
        for row in c.execute("PRAGMA table_info(model_media)")
    ]

    for name, definition in [
        ("filename", "TEXT"),
        ("path", "TEXT"),
        ("metadata", "TEXT"),
    ]:
        if name not in media_existing:
            print(f"Adding media column: {name}")
            c.execute(f"ALTER TABLE model_media ADD COLUMN {name} {definition}")


    # SeaArt v1.3 initially treated a logged-out browser session as if every
    # downloadable model were gated.  v1.4 fixed new scans, but existing rows
    # can still carry gated=1 until they are scanned again.  Repair those rows
    # from the source capability already stored in card_data so users do not
    # need to rescan/backfill the SeaArt library.
    try:
        seaart_rows = c.execute(
            "SELECT id, card_data FROM models WHERE lower(source)='seaart' AND COALESCE(gated,0)=1"
        ).fetchall()
        repaired = 0
        for row in seaart_rows:
            try:
                payload = json.loads(row["card_data"] or "{}")
                source_meta = payload.get("seaart") if isinstance(payload, dict) else None
                if isinstance(source_meta, dict) and source_meta.get("downloadable") is True:
                    c.execute("UPDATE models SET gated=0 WHERE id=?", (row["id"],))
                    repaired += 1
            except Exception:
                continue
        if repaired:
            print(f"SeaArt access repair: cleared stale gated flag on {repaired} model(s)")
    except Exception:
        pass


    c.execute("""
    CREATE TABLE IF NOT EXISTS download_history (
        id INTEGER PRIMARY KEY,
        model_id INTEGER,
        source TEXT NOT NULL,
        model_key TEXT NOT NULL,
        source_file_id TEXT,
        file_key TEXT,
        filename TEXT,
        sha TEXT,
        source_updated TEXT,
        file_fingerprint TEXT NOT NULL,
        downloaded_at TEXT NOT NULL
    )
    """)

    c.execute("""
    CREATE INDEX IF NOT EXISTS idx_download_history_model
    ON download_history(source, model_key, downloaded_at DESC)
    """)


    download_history_columns = {
        row["name"]
        for row in c.execute("PRAGMA table_info(download_history)").fetchall()
    }
    for name, definition in [
        ("version_id", "TEXT DEFAULT ''"),
        ("version_name", "TEXT DEFAULT ''"),
    ]:
        if name not in download_history_columns:
            c.execute(f"ALTER TABLE download_history ADD COLUMN {name} {definition}")

    c.execute("""
    CREATE TABLE IF NOT EXISTS download_queue (
        id INTEGER PRIMARY KEY,
        model_id INTEGER NOT NULL,
        source TEXT NOT NULL,
        model_key TEXT NOT NULL,
        version_id TEXT DEFAULT '',
        version_name TEXT DEFAULT '',
        model_name TEXT DEFAULT '',
        source_url TEXT DEFAULT '',
        release_at TEXT DEFAULT '',
        status TEXT NOT NULL DEFAULT 'waiting',
        last_checked TEXT DEFAULT '',
        last_error TEXT DEFAULT '',
        created_at TEXT NOT NULL,
        UNIQUE(source, model_key, version_id, version_name)
    )
    """)

    c.execute("""
    CREATE INDEX IF NOT EXISTS idx_download_queue_status
    ON download_queue(status, created_at)
    """)


    c.execute("""
    CREATE TABLE IF NOT EXISTS download_watchlist (
        id INTEGER PRIMARY KEY,
        model_id INTEGER NOT NULL,
        source TEXT NOT NULL,
        model_key TEXT NOT NULL,
        version_id TEXT DEFAULT '',
        version_name TEXT DEFAULT '',
        model_name TEXT DEFAULT '',
        source_url TEXT DEFAULT '',
        file_id TEXT DEFAULT '',
        file_name TEXT NOT NULL,
        file_fingerprint TEXT DEFAULT '',
        file_index INTEGER DEFAULT -1,
        file_size_display TEXT DEFAULT '',
        status TEXT NOT NULL DEFAULT 'waiting',
        last_checked TEXT DEFAULT '',
        last_error TEXT DEFAULT '',
        available_at TEXT DEFAULT '',
        dismissed_at TEXT DEFAULT '',
        created_at TEXT NOT NULL,
        UNIQUE(source, model_key, version_id, version_name, file_id, file_name)
    )
    """)

    c.execute("""
    CREATE INDEX IF NOT EXISTS idx_download_watchlist_status
    ON download_watchlist(status, created_at)
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS installed_files (
        id INTEGER PRIMARY KEY,
        model_id INTEGER,
        source TEXT NOT NULL,
        model_key TEXT,
        source_file_id TEXT,
        file_fingerprint TEXT,
        local_path TEXT NOT NULL,
        filename TEXT,
        installed_at TEXT NOT NULL,
        UNIQUE(local_path)
    )
    """)

    c.execute("""
    CREATE INDEX IF NOT EXISTS idx_installed_files_model
    ON installed_files(source, model_key, installed_at DESC)
    """)


    installed_file_columns = {
        row["name"]
        for row in c.execute("PRAGMA table_info(installed_files)").fetchall()
    }
    for name, definition in [
        ("version_id", "TEXT DEFAULT ''"),
        ("version_name", "TEXT DEFAULT ''"),
    ]:
        if name not in installed_file_columns:
            c.execute(f"ALTER TABLE installed_files ADD COLUMN {name} {definition}")

    abyss_path_migration = _migrate_abyssbeacon_installed_paths(c)
    if any(abyss_path_migration.values()):
        parts = []
        if abyss_path_migration["migrated"]:
            parts.append(f"{abyss_path_migration['migrated']} installed path(s)")
        if abyss_path_migration["deduped"]:
            parts.append(f"{abyss_path_migration['deduped']} duplicate tracking row(s)")
        if abyss_path_migration["sidecars"]:
            parts.append(f"{abyss_path_migration['sidecars']} info sidecar(s)")
        print("AbyssBeacon rename migration: repaired " + ", ".join(parts))

    c.execute("""
        CREATE TABLE IF NOT EXISTS blocked_creators (
            id INTEGER PRIMARY KEY,
            source TEXT NOT NULL,
            creator TEXT NOT NULL COLLATE NOCASE,
            blocked_at TEXT,
            UNIQUE(source, creator)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS universal_blocked_creators (
            creator TEXT PRIMARY KEY COLLATE NOCASE,
            blocked_at TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS creator_sources (
            id INTEGER PRIMARY KEY,
            creator_name TEXT NOT NULL COLLATE NOCASE,
            source TEXT NOT NULL,
            source_creator_id TEXT NOT NULL,
            profile_url TEXT,
            discovered_via TEXT,
            first_seen TEXT,
            last_seen TEXT,
            UNIQUE(source, source_creator_id)
        )
    """)

    # Seed persistent TensorHub creator identities from existing model rows.
    # This lets model cleanup remove every card without erasing the owner ID
    # required for future Expanded Creator Search / explicit Creator Scan.
    now_creator_seed = datetime.now(timezone.utc).isoformat()
    for row in c.execute(
        "SELECT author, card_data FROM models WHERE source='tensorhub' AND card_data IS NOT NULL AND card_data <> ''"
    ).fetchall():
        try:
            card = json.loads(row["card_data"] or "{}")
            th = card.get("tensorhub") or {}
            owner_id = str(th.get("owner_id") or "").strip()
            nickname = str(th.get("owner_nickname") or row["author"] or "").strip()
            if owner_id and nickname:
                c.execute(
                    """
                    INSERT INTO creator_sources
                        (creator_name, source, source_creator_id, profile_url, discovered_via, first_seen, last_seen)
                    VALUES (?, 'tensorhub', ?, ?, 'observed', ?, ?)
                    ON CONFLICT(source, source_creator_id) DO UPDATE SET
                        creator_name=excluded.creator_name,
                        last_seen=excluded.last_seen
                    """,
                    (nickname, owner_id, "", now_creator_seed, now_creator_seed),
                )
        except Exception:
            continue

    c.execute("""
        CREATE TABLE IF NOT EXISTS model_file_hashes (
            id INTEGER PRIMARY KEY,
            model_id INTEGER NOT NULL,
            source TEXT NOT NULL,
            model_key TEXT NOT NULL DEFAULT '',
            sha256 TEXT NOT NULL,
            UNIQUE(source, model_key, sha256),
            FOREIGN KEY(model_id) REFERENCES models(id)
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_model_file_hashes_sha256 ON model_file_hashes(sha256)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_model_file_hashes_model_id ON model_file_hashes(model_id)")

    c.execute("""
        CREATE TABLE IF NOT EXISTS model_sources (
            id INTEGER PRIMARY KEY,
            model_id INTEGER NOT NULL,
            source TEXT NOT NULL,
            url TEXT,
            model_key TEXT NOT NULL,
            source_data TEXT DEFAULT '',
            UNIQUE(source, model_key),
            FOREIGN KEY(model_id) REFERENCES models(id)
        )
    """)

    # Feed architecture filters frequently ask whether a merged card has an
    # alternate source snapshot with the requested architecture.  Without an
    # index on model_id SQLite can rescan the entire model_sources table for
    # every model row, making filtered lazy-load chunks dramatically slower
    # than the unfiltered home feed.
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_model_sources_model_id "
        "ON model_sources(model_id)"
    )

    # Source-specific snapshots power the multi-source download chooser.
    source_columns = {row["name"] for row in c.execute("PRAGMA table_info(model_sources)").fetchall()}
    if "source_data" not in source_columns:
        c.execute("ALTER TABLE model_sources ADD COLUMN source_data TEXT DEFAULT ''")

    # Older ModelScope scans stored Unix timestamps as bare numbers. SQLite's
    # datetime() cannot sort those consistently beside ISO timestamps, so
    # normalize existing canonical rows and ModelScope source snapshots once.
    def _modelscope_iso(value):
        value = str(value or "").strip()
        if not value:
            return ""
        try:
            number = float(value)
        except ValueError:
            return value
        try:
            if abs(number) > 100000000000:
                number /= 1000.0
            return datetime.fromtimestamp(number, timezone.utc).isoformat().replace("+00:00", "Z")
        except Exception:
            return value

    try:
        for row in c.execute(
            "SELECT id,updated FROM models WHERE lower(source)='modelscope' AND updated IS NOT NULL AND updated<>''"
        ).fetchall():
            normalized = _modelscope_iso(row["updated"])
            if normalized and normalized != str(row["updated"]):
                c.execute("UPDATE models SET updated=? WHERE id=?", (normalized, row["id"]))

        for row in c.execute(
            "SELECT id,source_data FROM model_sources WHERE lower(source)='modelscope' AND source_data IS NOT NULL AND source_data<>''"
        ).fetchall():
            try:
                payload = json.loads(row["source_data"] or "{}")
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            changed = False
            for key in ("updated", "listing_updated"):
                if payload.get(key):
                    normalized = _modelscope_iso(payload.get(key))
                    if normalized != str(payload.get(key)):
                        payload[key] = normalized
                        changed = True
            ms_meta = payload.get("modelscope")
            if isinstance(ms_meta, dict) and ms_meta.get("listing_updated"):
                normalized = _modelscope_iso(ms_meta.get("listing_updated"))
                if normalized != str(ms_meta.get("listing_updated")):
                    ms_meta["listing_updated"] = normalized
                    changed = True
            if changed:
                c.execute(
                    "UPDATE model_sources SET source_data=? WHERE id=?",
                    (json.dumps(payload, ensure_ascii=False), row["id"]),
                )
    except Exception:
        pass

    # Recover creator attribution for older merged Hugging Face / ModelScope
    # source links. Their stable repository key is owner/repo, so this is a
    # deterministic recovery rather than fuzzy creator guessing.
    repaired_source_authors = 0
    try:
        legacy_links = c.execute(
            "SELECT id,source,model_key,url,source_data FROM model_sources "
            "WHERE lower(source) IN ('huggingface','modelscope')"
        ).fetchall()
        for link in legacy_links:
            try:
                snapshot = json.loads(link["source_data"] or "{}")
                if not isinstance(snapshot, dict):
                    snapshot = {}
            except Exception:
                snapshot = {}
            if str(snapshot.get("author") or "").strip():
                continue
            author = _infer_source_author_from_key(link["source"], link["model_key"], link["url"])
            if not author:
                continue
            snapshot["author"] = author
            c.execute(
                "UPDATE model_sources SET source_data=? WHERE id=?",
                (json.dumps(snapshot, ensure_ascii=False), link["id"]),
            )
            repaired_source_authors += 1
        if repaired_source_authors:
            print(f"Source attribution repair: recovered {repaired_source_authors} creator name(s)")
    except Exception:
        pass

    # Seed source links for existing rows.
    c.execute("""
        INSERT OR IGNORE INTO model_sources (model_id, source, url, model_key)
        SELECT id, source, url, COALESCE(model_key, '') FROM models
        WHERE source IS NOT NULL AND source <> '' AND model_key IS NOT NULL AND model_key <> ''
    """)

    # Existing canonical rows can be snapshotted immediately; alternate links
    # acquire their own snapshots on the next scan from that source.
    canonical_rows = c.execute("SELECT * FROM models WHERE source IS NOT NULL AND source<>'' AND model_key IS NOT NULL AND model_key<>''").fetchall()
    for row in canonical_rows:
        data = dict(row)
        snapshot = _source_snapshot(data)
        c.execute("UPDATE model_sources SET source_data=? WHERE source=? AND model_key=? AND (source_data IS NULL OR source_data='')",
                  (json.dumps(snapshot, ensure_ascii=False), data.get("source"), data.get("model_key")))

    # v1.6.2: discard the old broad hash index and rebuild it from conservative
    # primary-model artifact identities only.
    c.execute("DELETE FROM model_file_hashes")
    for row in canonical_rows:
        data = dict(row)
        _register_model_hashes(
            c,data.get("id"),data.get("source",""),data.get("model_key",""),data
        )
    source_snapshots=c.execute(
        "SELECT model_id,source,model_key,source_data FROM model_sources "
        "WHERE source_data IS NOT NULL AND source_data<>''"
    ).fetchall()
    for link in source_snapshots:
        try:
            snapshot=json.loads(link["source_data"] or "{}")
        except Exception:
            snapshot={}
        if isinstance(snapshot,dict):
            _register_model_hashes(
                c,link["model_id"],link["source"],link["model_key"],snapshot
            )

    # One card cannot represent two different native model keys from the same
    # source. Repair that objective corruption signal before reconciliation.
    detangle=repair_impossible_merged_cards(conn,c)
    if detangle.get("cards"):
        print(
            "AbyssBeacon detangle repair: split "
            f"{detangle['cards']} impossible merged card(s) into "
            f"{detangle['cards'] + detangle['created']} card(s)"
        )
        print("AbyssBeacon detangle safety: using user's manual models.db backup")

    # Reconcile every pre-existing *unambiguous* exact-SHA cross-source pair.
    # This repairs databases populated before SHA merging existed; no rescan is
    # required once the hashes are already indexed.
    sha_reconcile = reconcile_cross_source_sha256_duplicates(c)
    if sha_reconcile.get("merged"):
        print(
            "SHA256 reconciliation: "
            f"merged {sha_reconcile['merged']} duplicate card(s) across "
            f"{sha_reconcile['groups']} cross-source hash group(s)"
            + (f"; skipped {sha_reconcile['skipped_ambiguous']} ambiguous group(s)"
               if sha_reconcile.get("skipped_ambiguous") else "")
        )

    # Reconcile pre-existing CivitAI/Red duplicate cards. Red is the canonical
    # presentation source; the regular CivitAI URL remains as a fallback link.
    pairs = c.execute("""
        SELECT r.id AS red_id, c.id AS civ_id,
               r.model_key AS model_key,
               r.url AS red_url, c.url AS civ_url,
               r.favorite AS red_fav, c.favorite AS civ_fav,
               r.viewed AS red_view, c.viewed AS civ_view
        FROM models r
        JOIN models c ON c.model_key=r.model_key
        WHERE r.source='civitaired' AND c.source='civitai'
    """).fetchall()
    for pair in pairs:
        red_id, civ_id = pair["red_id"], pair["civ_id"]
        c.execute("UPDATE models SET favorite=?, viewed=? WHERE id=?", (
            max(int(pair["red_fav"] or 0), int(pair["civ_fav"] or 0)),
            max(int(pair["red_view"] or 0), int(pair["civ_view"] or 0)),
            red_id,
        ))
        civ_source = c.execute("SELECT source_data FROM model_sources WHERE source='civitai' AND model_key=?", (pair["model_key"],)).fetchone()
        civ_source_data = (civ_source["source_data"] if civ_source else "") or ""
        c.execute("DELETE FROM model_sources WHERE source='civitai' AND model_key=?", (pair["model_key"],))
        c.execute("INSERT OR REPLACE INTO model_sources(model_id,source,url,model_key,source_data) VALUES(?,?,?,?,?)",
                  (red_id, 'civitai', pair["civ_url"] or '', pair["model_key"], civ_source_data))
        c.execute("UPDATE model_sources SET model_id=? WHERE model_id=?", (red_id, civ_id))
        c.execute("UPDATE model_file_hashes SET model_id=? WHERE model_id=?", (red_id, civ_id))
        c.execute("DELETE FROM model_media WHERE model_id=?", (civ_id,))
        c.execute("DELETE FROM models WHERE id=?", (civ_id,))

    conn.commit()
    conn.close()


from datetime import datetime


def start_scan():

    conn = connect()
    c = conn.cursor()

    c.execute(
        """
        INSERT INTO scan_runs
        (
            started
        )
        VALUES (?)
        """,
        (
            datetime.utcnow().isoformat(),
        )
    )

    scan_id = c.lastrowid

    conn.commit()
    conn.close()

    return scan_id



def add_scan_result(
    scan_id,
    source,
    stats
):

    conn = connect()
    c = conn.cursor()

    c.execute(
        """
        INSERT INTO scan_results
        (
            scan_id,
            source,
            processed,
            added,
            updated,
            media,
            images,
            videos
        )

        VALUES (?,?,?,?,?,?,?,?)
        """,
        (
            scan_id,
            source,
            stats.get("processed", 0),
            stats.get("added", 0),
            stats.get("updated", 0),
            stats.get("media", 0),
            stats.get("images", 0),
            stats.get("videos", 0)
        )
    )

    conn.commit()
    conn.close()



def finish_scan(
    scan_id,
    duration,
    stats
):

    conn = connect()
    c = conn.cursor()

    c.execute(
        """
        UPDATE scan_runs

        SET
            finished=?,
            duration=?,
            total_processed=?,
            total_added=?,
            total_updated=?,
            total_media=?,
            total_images=?,
            total_videos=?

        WHERE id=?

        """,
        (
            datetime.utcnow().isoformat(),
            duration,
            stats.get("processed", 0),
            stats.get("added", 0),
            stats.get("updated", 0),
            stats.get("media", 0),
            stats.get("images", 0),
            stats.get("videos", 0),
            scan_id
        )
    )

    conn.commit()
    conn.close()



def get_scan_history(limit=20):

    conn = connect()
    c = conn.cursor()

    c.execute(
        """
        SELECT *
        FROM scan_runs
        ORDER BY id DESC
        LIMIT ?
        """,
        (
            limit,
        )
    )

    rows = c.fetchall()

    conn.close()

    return rows

def record_scan_model_change(scan_id, model_id, change_type):
    """Remember which concrete cards were new/changed in a normal scan."""
    if not scan_id or not model_id or change_type not in ("new", "updated"):
        return
    conn = connect()
    conn.execute(
        """
        INSERT OR IGNORE INTO scan_model_changes (scan_id, model_id, change_type)
        VALUES (?, ?, ?)
        """,
        (int(scan_id), int(model_id), change_type),
    )
    conn.commit()
    conn.close()


def get_scan_model_ids(scan_id, change_type):
    if not scan_id or change_type not in ("new", "updated"):
        return set()
    conn = connect()
    rows = conn.execute(
        "SELECT model_id FROM scan_model_changes WHERE scan_id=? AND change_type=?",
        (int(scan_id), change_type),
    ).fetchall()
    conn.close()
    return {int(row[0]) for row in rows}



def get_scan_results(scan_id):
    conn = connect()
    c = conn.cursor()
    c.execute("SELECT * FROM scan_results WHERE scan_id = ? ORDER BY id", (scan_id,))
    rows = c.fetchall()
    conn.close()
    return rows


def get_models_missing_description(limit=100, offset=0):
    """Return models whose description is blank, oldest IDs first for stable backfill batches."""
    conn = connect()
    rows = conn.execute(
        """SELECT id, model_key, source, url, card_data, name, sha
           FROM models
           WHERE description IS NULL OR TRIM(description) = ''
           ORDER BY id ASC
           LIMIT ? OFFSET ?""",
        (max(1, int(limit)), max(0, int(offset))),
    ).fetchall()
    conn.close()
    return rows


def count_models_missing_description():
    conn = connect()
    value = conn.execute(
        "SELECT COUNT(*) FROM models WHERE description IS NULL OR TRIM(description) = ''"
    ).fetchone()[0]
    conn.close()
    return int(value or 0)



def model_metadata_hash(model):
    """Return AbyssBeacon's stable model fingerprint for retention validation."""
    return _stable_model_metadata_hash(_model_mapping(model))



def remember_retention_tombstone(source, model_key, metadata_hash="", activity_at=""):
    """Remember a model removed by retention so normal discovery can avoid re-import loops."""
    source = str(source or "").strip().lower()
    model_key = str(model_key or "").strip()
    if not source or not model_key:
        return False
    conn = connect()
    conn.execute(
        """
        INSERT INTO retention_tombstones
            (source, model_key, metadata_hash, activity_at, deleted_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(source, model_key) DO UPDATE SET
            metadata_hash=excluded.metadata_hash,
            activity_at=excluded.activity_at,
            deleted_at=excluded.deleted_at
        """,
        (
            source,
            model_key,
            str(metadata_hash or ""),
            str(activity_at or ""),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()
    conn.close()
    return True


def remember_retention_tombstones_for_model_ids(model_ids):
    """Persist retention memory for every source identity about to be deleted.

    SHA-merged cards can represent several providers. Remembering only the
    canonical models.source/model_key lets another source immediately resurrect
    the same deleted card, so each model_sources identity gets its own tombstone.
    """
    ids = [int(value) for value in (model_ids or []) if str(value).isdigit()]
    if not ids:
        return 0

    conn = connect()
    now = datetime.now(timezone.utc).isoformat()
    identities = {}

    for offset in range(0, len(ids), 500):
        chunk = ids[offset:offset + 500]
        placeholders = ",".join("?" for _ in chunk)

        canonical_rows = conn.execute(
            f"""SELECT id,source,model_key,metadata_hash,card_data,updated,created,
                       last_seen,first_seen,url,name,display_name,author,sha,
                       description,base_model,architecture,model_type,pipeline,
                       tags,display_tags,license,files
                FROM models
                WHERE id IN ({placeholders})""",
            chunk,
        ).fetchall()

        canonical_by_id = {int(row["id"]): row for row in canonical_rows}

        source_rows = conn.execute(
            f"""SELECT model_id,source,model_key,url,source_data
                FROM model_sources
                WHERE model_id IN ({placeholders})""",
            chunk,
        ).fetchall()

        # Source memberships are authoritative when present.
        for link in source_rows:
            model_id = int(link["model_id"])
            source = str(link["source"] or "").strip().lower()
            model_key = str(link["model_key"] or "").strip()
            if not source or not model_key:
                continue

            try:
                snapshot = json.loads(link["source_data"] or "{}")
                if not isinstance(snapshot, dict):
                    snapshot = {}
            except Exception:
                snapshot = {}

            canonical = canonical_by_id.get(model_id)
            activity = (
                snapshot.get("updated")
                or snapshot.get("created")
                or (canonical["updated"] if canonical else "")
                or (canonical["created"] if canonical else "")
                or (canonical["last_seen"] if canonical else "")
                or (canonical["first_seen"] if canonical else "")
                or ""
            )

            tombstone_hash = str(snapshot.get("metadata_hash") or "").strip()

            # TensorHub compares against its cheap listing signature.
            if source == "tensorhub":
                try:
                    listing_hash = str(
                        (((snapshot.get("card_data") or {}).get("tensorhub") or {}).get("listing_hash"))
                        or ""
                    ).strip()
                    if listing_hash:
                        tombstone_hash = listing_hash
                except Exception:
                    pass

            # Older source snapshots may not contain metadata_hash. Build a
            # stable source-local fingerprint rather than borrowing the
            # canonical card's potentially unrelated provider hash.
            if not tombstone_hash:
                fingerprint_source = dict(snapshot)
                fingerprint_source.update({
                    "source": source,
                    "model_key": model_key,
                    "url": str(link["url"] or ""),
                })
                tombstone_hash = _stable_model_metadata_hash(fingerprint_source)

            identities[(source, model_key)] = (
                tombstone_hash,
                str(activity or ""),
            )

        # Defensive fallback for legacy rows with no model_sources entry.
        linked_model_ids = {int(row["model_id"]) for row in source_rows}
        for row in canonical_rows:
            model_id = int(row["id"])
            if model_id in linked_model_ids:
                continue

            source = str(row["source"] or "").strip().lower()
            model_key = str(row["model_key"] or "").strip()
            if not source or not model_key:
                continue

            activity = (
                row["updated"] or row["created"] or row["last_seen"]
                or row["first_seen"] or ""
            )
            tombstone_hash = str(row["metadata_hash"] or "")

            if source == "tensorhub":
                try:
                    card_obj = json.loads(row["card_data"] or "{}")
                    listing_hash = str(
                        ((card_obj.get("tensorhub") or {}).get("listing_hash"))
                        or ""
                    ).strip()
                    if listing_hash:
                        tombstone_hash = listing_hash
                except Exception:
                    pass

            identities[(source, model_key)] = (
                tombstone_hash,
                str(activity or ""),
            )

    for (source, model_key), (metadata_hash, activity_at) in identities.items():
        conn.execute(
            """
            INSERT INTO retention_tombstones
                (source, model_key, metadata_hash, activity_at, deleted_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(source, model_key) DO UPDATE SET
                metadata_hash=excluded.metadata_hash,
                activity_at=excluded.activity_at,
                deleted_at=excluded.deleted_at
            """,
            (source, model_key, str(metadata_hash or ""), str(activity_at or ""), now),
        )

    conn.commit()
    conn.close()
    return len(identities)



def get_retention_tombstones(source):
    source = str(source or "").strip().lower()
    if not source:
        return {}
    conn = connect()
    rows = conn.execute(
        "SELECT model_key, metadata_hash, activity_at, deleted_at FROM retention_tombstones WHERE source=?",
        (source,),
    ).fetchall()
    conn.close()
    return {
        str(row["model_key"]): {
            "metadata_hash": str(row["metadata_hash"] or ""),
            "activity_at": str(row["activity_at"] or ""),
            "deleted_at": str(row["deleted_at"] or ""),
        }
        for row in rows
        if str(row["model_key"] or "").strip()
    }


def clear_retention_tombstone(source, model_key):
    source = str(source or "").strip().lower()
    model_key = str(model_key or "").strip()
    if not source or not model_key:
        return 0
    conn = connect()
    cur = conn.execute(
        "DELETE FROM retention_tombstones WHERE source=? AND model_key=?",
        (source, model_key),
    )
    conn.commit()
    count = cur.rowcount
    conn.close()
    return int(count or 0)
