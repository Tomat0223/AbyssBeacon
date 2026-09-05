"""Small local card-preview cache.

Only card cover images are cached. Full gallery media remains remote and loads on demand.
"""
from __future__ import annotations

import hashlib
import io
import os
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from PIL import Image, ImageOps

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(BASE_DIR, "static", "cache", "previews")
PUBLIC_PREFIX = "/static/cache/previews/"
MAX_EDGE = 512
QUALITY = 82
WORKERS = 8


def _filename(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8", errors="ignore")).hexdigest() + ".webp"


def cache_preview_url(url: str) -> str:
    url = str(url or "").strip()
    if not url or url.startswith("/static/") or url.startswith("data:"):
        return url

    os.makedirs(CACHE_DIR, exist_ok=True)
    filename = _filename(url)
    path = os.path.join(CACHE_DIR, filename)
    public = PUBLIC_PREFIX + filename
    if os.path.isfile(path) and os.path.getsize(path) > 0:
        return public

    try:
        response = requests.get(
            url,
            timeout=(5, 15),
            headers={"User-Agent": "AbyssBeacon/1.0", "Accept": "image/*"},
        )
        response.raise_for_status()
        if not str(response.headers.get("content-type", "")).lower().startswith("image/"):
            return url
        # Some source previews contain malformed EXIF/TIFF metadata. Pillow can
        # still decode the image correctly, but emits a noisy UserWarning while
        # reading that metadata. Hide only that known recoverable warning here;
        # all other Pillow warnings and actual image failures remain visible.
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message=r"Corrupt EXIF data\..*",
                category=UserWarning,
                module=r"PIL\.TiffImagePlugin",
            )
            with Image.open(io.BytesIO(response.content)) as image:
                image = ImageOps.exif_transpose(image)
                if image.mode not in ("RGB", "RGBA"):
                    image = image.convert("RGB")
                image.thumbnail((MAX_EDGE, MAX_EDGE), Image.Resampling.LANCZOS)
                if image.mode == "RGBA":
                    # WebP supports alpha; keep it when present.
                    image.save(path, "WEBP", quality=QUALITY, method=4)
                else:
                    image.convert("RGB").save(path, "WEBP", quality=QUALITY, method=4)
        return public
    except Exception:
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass
        return url


def cache_model_previews(models) -> int:
    """Cache source cover images concurrently and replace card image paths in-place."""
    candidates = [m for m in models if str(getattr(m, "image", "") or "").startswith(("http://", "https://"))]
    if not candidates:
        return 0

    cached = 0
    with ThreadPoolExecutor(max_workers=min(WORKERS, len(candidates)), thread_name_prefix="modelradar-preview") as pool:
        future_map = {pool.submit(cache_preview_url, m.image): m for m in candidates}
        for future in as_completed(future_map):
            model = future_map[future]
            try:
                result = future.result()
            except Exception:
                continue
            if result and result.startswith(PUBLIC_PREFIX):
                # Preserve the original remote card image before model.image
                # becomes a local cached WebP path.
                try:
                    setattr(model, "_remote_preview_url", str(getattr(model, "image", "") or ""))
                except Exception:
                    pass
                model.image = result
                cached += 1
    return cached


def delete_cached_preview(public_path: str) -> None:
    public_path = str(public_path or "")
    if not public_path.startswith(PUBLIC_PREFIX):
        return
    filename = os.path.basename(public_path)
    path = os.path.join(CACHE_DIR, filename)
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError:
        pass



def _is_remote_url(value: str) -> bool:
    value = str(value or "").strip()
    return value.startswith(("http://", "https://"))


def _cached_preview_missing(public_path: str) -> bool:
    public_path = str(public_path or "").strip()
    if not public_path.startswith(PUBLIC_PREFIX):
        return False
    filename = os.path.basename(public_path)
    path = os.path.join(CACHE_DIR, filename)
    return not (os.path.isfile(path) and os.path.getsize(path) > 0)


def _source_snapshot_preview_urls(conn, model_id: int):
    """Return preserved remote card-preview URLs from source snapshots."""
    import json

    urls = []
    try:
        rows = conn.execute(
            "SELECT source_data FROM model_sources WHERE model_id=?",
            (int(model_id),),
        ).fetchall()
        for row in rows:
            try:
                data = json.loads(row[0] or "{}")
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            for key in ("image", "remote_preview_url", "preview_url"):
                value = str(data.get(key) or "").strip()
                if _is_remote_url(value) and value not in urls:
                    urls.append(value)
    except Exception:
        pass
    return urls


def repair_missing_previews(limit: int | None = None):
    """Rebuild missing local card previews from already-stored remote media.

    This does not rescan providers and does not download full galleries.
    """
    import database
    import sqlite3

    conn = database.connect()
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT id,image FROM models ORDER BY id").fetchall()

    missing = []
    for row in rows:
        image = str(row["image"] or "").strip()
        if image.startswith(PUBLIC_PREFIX):
            if _cached_preview_missing(image):
                missing.append(int(row["id"]))
        elif not image:
            missing.append(int(row["id"]))

    if limit is not None:
        try:
            missing = missing[:max(0, int(limit))]
        except (TypeError, ValueError):
            pass

    if not missing:
        conn.close()
        return {"checked": len(rows), "missing": 0, "repaired": 0, "failed": 0}

    placeholders = ",".join("?" for _ in missing)
    media_rows = conn.execute(
        f"""
        SELECT model_id,type,url,thumbnail,position,id
        FROM model_media
        WHERE model_id IN ({placeholders})
        ORDER BY model_id,
                 CASE WHEN lower(type)='image' THEN 0 ELSE 1 END,
                 position,
                 id
        """,
        missing,
    ).fetchall()

    candidates = {model_id: [] for model_id in missing}

    # Newer databases retain the exact source cover independently of the cache.
    for model_id in missing:
        for url in _source_snapshot_preview_urls(conn, model_id):
            if url not in candidates[model_id]:
                candidates[model_id].append(url)

    # Older databases can usually recover from their stored gallery/media rows.
    for row in media_rows:
        model_id = int(row["model_id"])
        for value in (row["url"], row["thumbnail"]):
            value = str(value or "").strip()
            if _is_remote_url(value) and value not in candidates[model_id]:
                candidates[model_id].append(value)

    conn.close()

    def repair_one(model_id):
        # Try several stored candidates because old source URLs occasionally expire.
        for url in candidates.get(model_id, [])[:8]:
            cached = cache_preview_url(url)
            if cached and cached.startswith(PUBLIC_PREFIX):
                return model_id, cached
        return model_id, ""

    repaired_rows = []
    failed = 0
    worker_count = min(WORKERS, max(1, len(missing)))
    with ThreadPoolExecutor(
        max_workers=worker_count,
        thread_name_prefix="modelradar-preview-repair",
    ) as pool:
        futures = [pool.submit(repair_one, model_id) for model_id in missing]
        for future in as_completed(futures):
            try:
                model_id, cached = future.result()
            except Exception:
                failed += 1
                continue
            if cached:
                repaired_rows.append((cached, model_id))
            else:
                failed += 1

    if repaired_rows:
        conn = database.connect()
        conn.executemany(
            "UPDATE models SET image=? WHERE id=?",
            repaired_rows,
        )
        conn.commit()
        conn.close()

    return {
        "checked": len(rows),
        "missing": len(missing),
        "repaired": len(repaired_rows),
        "failed": failed,
    }



def clean_orphaned_previews():
    """Remove cached card previews that are no longer referenced by any model."""
    if not os.path.isdir(CACHE_DIR):
        return {"removed": 0, "bytes_freed": 0}
    try:
        import database
        conn = database.connect()
        rows = conn.execute("SELECT image FROM models WHERE image LIKE ?", (PUBLIC_PREFIX + "%",)).fetchall()
        conn.close()
        referenced = {os.path.basename(str(row[0])) for row in rows if row[0]}
    except Exception:
        referenced = set()

    removed = 0
    bytes_freed = 0
    for name in os.listdir(CACHE_DIR):
        path = os.path.join(CACHE_DIR, name)
        if not os.path.isfile(path) or name in referenced:
            continue
        try:
            bytes_freed += os.path.getsize(path)
            os.remove(path)
            removed += 1
        except OSError:
            pass
    return {"removed": removed, "bytes_freed": bytes_freed}
