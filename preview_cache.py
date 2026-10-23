"""Small local card-preview cache.

Only card cover images are cached. Full gallery media remains remote and loads on demand.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import tempfile
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from urllib.parse import urlsplit
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
MAX_PREVIEW_PIXELS = 32_000_000
MAX_PREVIEW_BYTES = 20 * 1024 * 1024
# CPython 3.12 warning filters are process-global. Serialize only header probing,
# not downloads or pixel decoding, while temporarily treating bomb warnings as errors.
_IMAGE_OPEN_LOCK = threading.Lock()
_TRACKER_LOCK = threading.Lock()
_TRACKER = None


def _track_preview(url, source, model_key, reason, **details):
    global _TRACKER
    record = {
        "time": datetime.now(timezone.utc).isoformat(),
        "source": str(source or "unknown")[:80],
        "model_key": str(model_key or "unknown")[:200],
        "url_hash": _filename(url).removesuffix(".webp"),
        "reason": reason,
        **details,
    }
    try:
        record["host"] = urlsplit(url).hostname or ""
    except ValueError:
        record["host"] = "invalid"
    # No URL paths, query strings, request headers, or exception text: CDN links
    # may contain signed tokens. Model identity + URL hash suffice for correlation.
    message = json.dumps(record, ensure_ascii=True)
    important = reason in {"pixel_limit", "pillow_pixel_guard", "download_too_large"}
    level = logging.WARNING if important else logging.INFO
    # Routine unavailable/unsupported cover images belong in the diagnostic
    # file; safety limits remain visible as concise human-readable messages.
    logging.getLogger(__name__).log(
        level, "Preview cache: %s/%s - %s%s", record["source"], record["model_key"],
        reason.replace("_", " "),
        f" ({record['pixels']:,} pixels)" if isinstance(record.get("pixels"), int) else "")
    try:
        with _TRACKER_LOCK:
            if _TRACKER is None:
                directory = os.path.join(BASE_DIR, "logs")
                os.makedirs(directory, exist_ok=True)
                handler = RotatingFileHandler(os.path.join(directory, "preview-cache.log"),
                                              maxBytes=1024 * 1024, backupCount=2, encoding="utf-8")
                handler.setFormatter(logging.Formatter("%(message)s"))
                _TRACKER = logging.Logger("abyssbeacon.preview.tracker")
                _TRACKER.addHandler(handler)
            _TRACKER.warning(message)
    except OSError:
        logging.getLogger(__name__).warning("Could not write preview-cache diagnostic log")



def _filename(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8", errors="ignore")).hexdigest() + ".webp"


def _thumbnail_request_url(url):
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname != "image.tensorartassets.com" or parts.query or parts.username:
        return url
    path = parts.path
    if path.startswith("/cdn-cgi/image/"):
        pieces = path.split("/", 4)
        if len(pieces) != 5:
            return url
        path = "/" + pieces[4]
    return "https://image.tensorartassets.com/cdn-cgi/image/anim=false,plain=false,w=512,f=webp,q=82" + path


def _preview_headers(url, source):
    headers = {"User-Agent": "AbyssBeacon/1.0", "Accept": "image/*"}
    parts = urlsplit(url)
    if (source == "huggingface" and parts.scheme == "https"
            and parts.hostname == "huggingface.co" and not parts.username
            and parts.port in (None, 443)):
        from secrets_manager import get_source_token
        token = get_source_token("huggingface")
        if token:
            headers["Authorization"] = "Bearer " + token
    return headers


def cache_preview_url(url: str, *, source="", model_key="", _try_resize=True) -> str:
    url = str(url or "").strip()
    if not url or url.startswith("/static/") or url.startswith("data:"):
        return url

    os.makedirs(CACHE_DIR, exist_ok=True)
    filename = _filename(url)
    path = os.path.join(CACHE_DIR, filename)
    public = PUBLIC_PREFIX + filename
    if os.path.isfile(path) and os.path.getsize(path) > 0:
        return public

    temporary = None
    request_url = _thumbnail_request_url(url) if _try_resize else url
    try:
        with requests.get(
            request_url, timeout=(5, 15), stream=True,
            headers=_preview_headers(request_url, source),
        ) as response:
            response.raise_for_status()
            if not str(response.headers.get("content-type", "")).lower().startswith("image/"):
                if request_url != url:
                    return cache_preview_url(url, source=source, model_key=model_key, _try_resize=False)
                return url
            try:
                length = int(response.headers.get("content-length", 0))
            except (TypeError, ValueError):
                length = 0
            if length > MAX_PREVIEW_BYTES:
                _track_preview(url, source, model_key, "download_too_large", bytes=length)
                return url
            content = bytearray()
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if len(content) + len(chunk) > MAX_PREVIEW_BYTES:
                    _track_preview(url, source, model_key, "download_too_large", bytes=len(content) + len(chunk))
                    return url
                content.extend(chunk)

        with _IMAGE_OPEN_LOCK:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                warnings.filterwarnings("ignore", message=r"Corrupt EXIF data\..*",
                                        category=UserWarning, module=r"PIL\.TiffImagePlugin")
                image = Image.open(io.BytesIO(content))
        with image:
            width, height = image.size
            if width * height > MAX_PREVIEW_PIXELS:
                _track_preview(url, source, model_key, "pixel_limit", width=width, height=height,
                               pixels=width * height, limit=MAX_PREVIEW_PIXELS)
                return url
            # Check dimensions BEFORE exif_transpose/convert/thumbnail trigger decoding.
            image = ImageOps.exif_transpose(image)
            try:
                if image.mode not in ("RGB", "RGBA"):
                    # Palette PNGs may store a per-entry alpha table. Converting
                    # them directly to RGB both warns and loses transparency.
                    mode = "RGBA" if "transparency" in image.info or "A" in image.getbands() else "RGB"
                    converted = image.convert(mode)
                    image.close()
                    image = converted
                image.thumbnail((MAX_EDGE, MAX_EDGE), Image.Resampling.LANCZOS)
                # A unique temporary file avoids corrupt or deleted output when
                # several models share a cover URL and cache it concurrently.
                fd, temporary = tempfile.mkstemp(dir=CACHE_DIR, suffix=".webp.tmp")
                os.close(fd)
                image.save(temporary, "WEBP", quality=QUALITY, method=4)
                os.replace(temporary, path)
                temporary = None
            finally:
                image.close()
        return public
    except (Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
        # Pillow's own hard/soft protections stay enabled. Never raise its global limit.
        size_match = re.search(r"Image size \((\d+) pixels\)", str(exc))
        _track_preview(url, source, model_key, "pillow_pixel_guard",
                       error_type=type(exc).__name__,
                       pixels=int(size_match.group(1)) if size_match else None,
                       pillow_limit=Image.MAX_IMAGE_PIXELS)
        return url
    except Exception as exc:
        if request_url != url:
            return cache_preview_url(url, source=source, model_key=model_key, _try_resize=False)
        status = getattr(getattr(exc, "response", None), "status_code", None)
        _track_preview(url, source, model_key, "cache_failed", error_type=type(exc).__name__,
                       http_status=status if isinstance(status, int) else None)
        return url
    finally:
        if temporary:
            try:
                os.remove(temporary)
            except OSError:
                pass


def _cache_model_cover(model):
    original = str(getattr(model, "image", "") or "")
    candidates = [original]
    for item in getattr(model, "media", []) or []:
        if not isinstance(item, dict) or str(item.get("type") or "").lower() != "image":
            continue
        candidate = str(item.get("thumbnail") or item.get("url") or "")
        path = urlsplit(candidate).path.lower()
        decorative = (path.endswith(".svg") or "/badges/" in path or "/badge/" in path
                      or urlsplit(candidate).hostname == "img.shields.io")
        if candidate.startswith(("http://", "https://")) and candidate not in candidates and not decorative:
            candidates.append(candidate)
        if len(candidates) >= 4:
            break
    for candidate in candidates:
        if urlsplit(candidate).path.lower().endswith(".svg"):
            continue
        cached = cache_preview_url(candidate,
                                   source=str(getattr(model, "source", "") or ""),
                                   model_key=str(getattr(model, "model_key", "") or ""))
        if cached.startswith(PUBLIC_PREFIX):
            return cached
    return original


def cache_model_previews(models) -> int:
    """Cache source cover images concurrently and replace card image paths in-place."""
    candidates = [m for m in models if str(getattr(m, "image", "") or "").startswith(("http://", "https://"))]
    if not candidates:
        return 0

    cached = 0
    with ThreadPoolExecutor(max_workers=min(WORKERS, len(candidates)), thread_name_prefix="modelradar-preview") as pool:
        future_map = {
            pool.submit(_cache_model_cover, m): m
            for m in candidates
        }
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
            cached = cache_preview_url(url, source="preview-repair", model_key=str(model_id))
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
