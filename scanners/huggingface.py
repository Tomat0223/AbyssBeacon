from scan_logging import verbose_print as print
NAME = "huggingface"
DISPLAY = "Hugging Face"
ENABLED = True
import requests, time, database, re, html
from concurrent.futures import ThreadPoolExecutor, as_completed
from scanners.common import metadata, media, processors
from scanners.common.repository_classifier import classify_repository, needs_repository_classification_refresh

from datetime import datetime, timedelta
from urllib.parse import quote, urljoin, urlparse, unquote

import scan_control, scan_status
from secrets_manager import get_source_token
from scanners.http_retry import get_with_backoff


HF_API = "https://huggingface.co/api/models"


session = requests.Session()

session.headers.update({
    "User-Agent": "AbyssBeacon/1.0"
})


def _apply_auth():
    token = get_source_token("huggingface")
    if token:
        session.headers["Authorization"] = f"Bearer {token}"
    else:
        session.headers.pop("Authorization", None)


PRIMARY_FILE_EXTENSIONS = (
    ".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf"
)
MAX_RECURSIVE_TREE_ENTRIES = 100000
HF_REPOSITORY_INVENTORY_KEY = "hf_repository_inventory"
HF_REPOSITORY_INVENTORY_VERSION = 1

# One-time library maintenance revision for Hugging Face repository snapshots.
# This is intentionally separate from the repository classifier version: v1 means
# the saved repository has been checked with a complete recursive inventory plus
# README-embedded media discovery. Future HF maintenance passes can bump this
# without pretending the classification algorithm itself changed.
HF_LIBRARY_REFRESH_KEY = "hf_library_refresh"
HF_LIBRARY_REFRESH_VERSION = 1


def _json_mapping(value):
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            import json
            decoded = json.loads(value or "{}")
            return dict(decoded) if isinstance(decoded, dict) else {}
        except Exception:
            return {}
    return {}


def _json_list(value):
    if isinstance(value, list):
        return list(value)
    if isinstance(value, str):
        try:
            import json
            decoded = json.loads(value or "[]")
            return list(decoded) if isinstance(decoded, list) else []
        except Exception:
            return []
    return []


def repository_inventory_marker(card_data, *, complete, revision="", method="", file_count=0):
    card = _json_mapping(card_data)
    card[HF_REPOSITORY_INVENTORY_KEY] = {
        "version": HF_REPOSITORY_INVENTORY_VERSION,
        "complete": bool(complete),
        "revision": str(revision or ""),
        "method": str(method or ""),
        "file_count": max(0, int(file_count or 0)),
    }
    return card


def stored_repository_inventory_status(model_row, expected_revision=""):
    """Return whether a saved HF row contains a complete recursive tree."""
    if model_row is None:
        return False
    try:
        card_data = _json_mapping(model_row["card_data"])
    except Exception:
        return False
    marker = card_data.get(HF_REPOSITORY_INVENTORY_KEY)
    if not isinstance(marker, dict):
        return False
    if int(marker.get("version") or 0) < HF_REPOSITORY_INVENTORY_VERSION:
        return False
    if not bool(marker.get("complete")):
        return False
    expected_revision = str(expected_revision or "").strip()
    marker_revision = str(marker.get("revision") or "").strip()
    if expected_revision and marker_revision and marker_revision != expected_revision:
        return False
    return True


def stored_repository_files(model_row):
    if model_row is None:
        return []
    try:
        return _json_list(model_row["files"])
    except Exception:
        return []


def library_refresh_marker(card_data, *, status="complete", reason="", inventory_complete=True, readme_checked=True):
    card = _json_mapping(card_data)
    card[HF_LIBRARY_REFRESH_KEY] = {
        "version": HF_LIBRARY_REFRESH_VERSION,
        "status": str(status or "complete"),
        "inventory_complete": bool(inventory_complete),
        "readme_checked": bool(readme_checked),
        "reason": str(reason or ""),
    }
    return card


def stored_library_refresh_version(value):
    """Return the completed HF library-refresh revision stored in card data."""
    card = _json_mapping(value)
    marker = card.get(HF_LIBRARY_REFRESH_KEY)
    if not isinstance(marker, dict):
        return 0
    try:
        version = int(marker.get("version") or 0)
    except (TypeError, ValueError):
        return 0
    status = str(marker.get("status") or "").strip().casefold()
    if status not in {"complete", "checked", "source_unavailable"}:
        return 0
    return version


def _repository_file_record(model_id, filename, info=None, revision="main"):
    """Normalize one Hugging Face file entry into AbyssBeacon's file shape.

    ``/api/models/<repo>`` and the Hub tree API expose slightly different
    LFS fields. Keeping the normalization here prevents Reload Model and the
    scanner from drifting apart again.
    """
    info = info if isinstance(info, dict) else {}
    filename = str(filename or "").strip()
    if not filename:
        return None

    lower_name = filename.lower()
    lfs = info.get("lfs", {}) or {}
    size = info.get("size", 0) or lfs.get("size", 0) or 0
    lfs_sha = lfs.get("sha256") or lfs.get("oid") or ""
    encoded_path = quote(filename, safe="/")
    resolve_url = f"https://huggingface.co/{model_id}/resolve/main/{encoded_path}"

    return {
        "name": filename.split("/")[-1],
        "path": filename,
        "size": size,
        "size_bytes": size,
        "sha256": lfs_sha,
        "is_lfs": bool(lfs),
        "revision": str(revision or "main"),
        "download_url": f"{resolve_url}?download=true",
        "media_url": resolve_url,
        "primary": lower_name.endswith(PRIMARY_FILE_EXTENSIONS),
    }



HF_README_MEDIA_KEY = "hf_readme_media"
HF_README_MEDIA_VERSION = 3
MAX_REPOSITORY_READMES = 1000
MAX_README_BYTES = 2 * 1024 * 1024
README_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif")
README_VIDEO_EXTENSIONS = (".mp4", ".webm", ".mov", ".m4v")


def _readme_media_type(url):
    try:
        path = unquote(urlparse(str(url or "")).path or "").casefold()
    except Exception:
        path = str(url or "").casefold()
    if path.endswith(README_IMAGE_EXTENSIONS):
        return "image"
    if path.endswith(README_VIDEO_EXTENSIONS):
        return "video"
    return ""


def _normalize_readme_media_url(model_id, value, readme_path="README.md", revision="main"):
    value = html.unescape(str(value or "")).strip().strip("<>")
    if not value or value.startswith(("data:", "javascript:", "#")):
        return ""
    if value.startswith("//"):
        value = "https:" + value
    if value.startswith(("http://", "https://")):
        # A blob URL renders an HTML page rather than the media itself. Convert
        # ordinary Hugging Face blob links into resolve links when possible.
        value = value.replace("huggingface.co/", "huggingface.co/", 1)
        value = value.replace("/blob/", "/resolve/", 1)
        return value
    revision = quote(str(revision or "main").strip() or "main", safe="")
    repository_root = f"https://huggingface.co/{model_id}/resolve/{revision}/"
    if value.startswith("/"):
        return urljoin(repository_root, value.lstrip("/"))
    normalized_readme_path = str(readme_path or "README.md").strip().replace("\\", "/")
    readme_directory = normalized_readme_path.rsplit("/", 1)[0] if "/" in normalized_readme_path else ""
    base = repository_root
    if readme_directory:
        base += quote(readme_directory.strip("/"), safe="/") + "/"
    return urljoin(base, value)


def _media_filename_from_url(url, fallback=""):
    try:
        name = unquote(urlparse(str(url or "")).path.rsplit("/", 1)[-1]).strip()
    except Exception:
        name = ""
    return name or str(fallback or "README media").strip() or "README media"


def extract_readme_media(readme_text, model_id, readme_path="README.md", revision="main"):
    """Extract image/video embeds from a Hugging Face model-card README.

    Model cards frequently host previews on cdn-uploads.huggingface.co instead
    of committing them to the repository tree. Those assets are real model
    previews and should participate in AbyssBeacon's normal media viewer.
    """
    text = str(readme_text or "")
    if not text.strip():
        return []

    candidates = []
    model_mentions = []
    for mention in re.finditer(
        r'(?i)([a-z0-9_+.()\[\]-]+(?:/[a-z0-9_+.()\[\] -]+)*\.safetensors)',
        text,
    ):
        reference = str(mention.group(1) or "").strip(" `\"'()[]{}<>.,;:")
        if reference:
            model_mentions.append((mention.start(), reference))

    def nearby_model_references(position):
        if position is None or not model_mentions:
            return []
        ranked = sorted(
            (
                (abs(int(position) - mention_position), mention_position, reference)
                for mention_position, reference in model_mentions
            ),
            key=lambda item: (item[0], item[1]),
        )
        nearest_distance = ranked[0][0]
        if nearest_distance > 2500:
            return []
        # Keep ties only. Multiple different closest references are ambiguous
        # and will be resolved conservatively by the Collection matcher.
        references = []
        seen_references = set()
        for distance, _mention_position, reference in ranked:
            if distance != nearest_distance:
                break
            identity = reference.casefold()
            if identity in seen_references:
                continue
            seen_references.add(identity)
            references.append(reference)
        return references

    def add(raw_url, kind="", label="", position=None):
        url = _normalize_readme_media_url(
            model_id, raw_url, readme_path=readme_path, revision=revision
        )
        if not url:
            return
        media_type = kind or _readme_media_type(url)
        if media_type not in {"image", "video"}:
            return
        # README badges are UI decoration, not model previews.
        lowered = url.casefold()
        if "shields.io/" in lowered or "/badge/" in lowered:
            return
        candidates.append((
            url,
            media_type,
            str(label or "").strip(),
            nearby_model_references(position),
        ))

    # Standard Markdown images.
    for match in re.finditer(
        r'!\[([^\]]*)\]\(\s*(?:<([^>]+)>|([^\s\)]+))(?:\s+["\'][^"\']*["\'])?\s*\)',
        text,
        flags=re.I,
    ):
        add(match.group(2) or match.group(3), "image", match.group(1), match.start())

    def attr(tag, name):
        match = re.search(
            rf'\b{re.escape(name)}\s*=\s*(?:"([^"]+)"|\'([^\']+)\'|([^\s>]+))',
            tag,
            flags=re.I,
        )
        if not match:
            return ""
        return match.group(1) or match.group(2) or match.group(3) or ""

    # HTML embeds are common for video because Markdown itself has no video tag.
    for match in re.finditer(r'<img\b[^>]*>', text, flags=re.I | re.S):
        tag = match.group(0)
        add(attr(tag, "src"), "image", attr(tag, "alt"), match.start())
    for match in re.finditer(r'<video\b[^>]*>', text, flags=re.I | re.S):
        tag = match.group(0)
        add(attr(tag, "poster"), "image", "Video poster", match.start())
        add(attr(tag, "src"), "video", "README video", match.start())
    for match in re.finditer(r'<source\b[^>]*>', text, flags=re.I | re.S):
        tag = match.group(0)
        source_url = attr(tag, "src")
        source_type = str(attr(tag, "type") or "").casefold()
        kind = "video" if source_type.startswith("video/") else ""
        add(source_url, kind, "README video", match.start())

    # Also recognize direct media links. This catches model cards that use a
    # normal link around a video/image instead of an actual embed tag.
    for match in re.finditer(r'https?://[^\s<>"\')\]]+', text, flags=re.I):
        add(match.group(0), position=match.start())

    media_items = []
    seen = set()
    for url, media_type, label, model_references in candidates:
        identity = url.split("#", 1)[0]
        if identity in seen:
            continue
        seen.add(identity)
        filename = _media_filename_from_url(url, label)
        media_path = filename
        try:
            parsed_path = unquote(urlparse(url).path or "")
            resolve_marker = f"/{model_id}/resolve/"
            if resolve_marker in parsed_path:
                revision_and_path = parsed_path.split(resolve_marker, 1)[1]
                if "/" in revision_and_path:
                    media_path = revision_and_path.split("/", 1)[1] or filename
        except Exception:
            media_path = filename
        media_items.append({
            "type": media_type,
            "url": url,
            "thumbnail": "",
            "filename": filename,
            "path": media_path,
            "metadata": {
                "filename": filename,
                "path": media_path,
                "origin": "Hugging Face README",
                "readme_path": str(readme_path or "README.md"),
                "model_references": model_references,
                "association_evidence": (
                    "Nearest safetensors reference in README"
                    if model_references else "README directory"
                ),
                "label": label,
            },
            "position": len(media_items),
        })
    return media_items


def repository_readme_paths(files):
    """Return every repository README path, without imposing a depth limit."""
    paths = []
    seen = set()
    for item in files or []:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or item.get("name") or "").strip().replace("\\", "/")
        if not path or path.rsplit("/", 1)[-1].casefold() != "readme.md":
            continue
        identity = path.casefold()
        if identity in seen:
            continue
        seen.add(identity)
        paths.append(path)
    return sorted(paths, key=lambda path: (path.count("/"), path.casefold()))


def fetch_repository_readme_media(
    model_id,
    files,
    *,
    root_readme_text="",
    revision="main",
    include_nested=False,
    max_readmes=MAX_REPOSITORY_READMES,
):
    """Collect embeds from root and nested READMEs listed by the Hub tree.

    Nested cards are fetched only during an existing metadata refresh path.
    Requests stay on the repository raw-file endpoint, are bounded in count and
    response size, and run with a small worker pool so large Collections remain
    practical without turning normal duplicate scans into repeated crawling.
    """
    model_id = str(model_id or "").strip()
    if not model_id:
        return []

    readme_paths = repository_readme_paths(files)
    root_path = next((path for path in readme_paths if path.casefold() == "readme.md"), "README.md")
    nested_paths = (
        [path for path in readme_paths if path.casefold() != "readme.md"]
        if include_nested
        else []
    )
    limit = max(0, int(max_readmes or 0))
    if limit and len(nested_paths) > limit:
        print(
            f"Hugging Face nested README media: checking the first {limit} of "
            f"{len(nested_paths)} nested README files for {model_id}"
        )
        nested_paths = nested_paths[:limit]

    collected = extract_readme_media(
        root_readme_text,
        model_id,
        readme_path=root_path,
        revision=revision,
    ) if str(root_readme_text or "").strip() else []

    if not nested_paths:
        return collected

    _apply_auth()
    request_headers = dict(session.headers)
    revision_path = quote(str(revision or "main").strip() or "main", safe="")

    def fetch_one(path):
        worker_session = requests.Session()
        worker_session.headers.update(request_headers)
        try:
            encoded_path = quote(path, safe="/")
            response = get_with_backoff(
                worker_session,
                f"https://huggingface.co/{model_id}/raw/{revision_path}/{encoded_path}",
                provider="Hugging Face",
                label=f"nested README {model_id}/{path}",
                max_retries=2,
                timeout=12,
            )
            if response.status_code != 200:
                return path, []
            content = response.content or b""
            if len(content) > MAX_README_BYTES:
                content = content[:MAX_README_BYTES]
            encoding = response.encoding or "utf-8"
            text = content.decode(encoding, errors="replace")
            return path, extract_readme_media(
                text,
                model_id,
                readme_path=path,
                revision=revision,
            )
        except Exception:
            return path, []
        finally:
            worker_session.close()

    results = {}
    with ThreadPoolExecutor(max_workers=min(6, len(nested_paths))) as executor:
        futures = {executor.submit(fetch_one, path): path for path in nested_paths}
        for future in as_completed(futures):
            path, items = future.result()
            results[path] = items

    for path in nested_paths:
        collected = merge_media_items(collected, results.get(path) or [])
    return collected


def stored_readme_media(card_data):
    card = _json_mapping(card_data)
    marker = card.get(HF_README_MEDIA_KEY)
    if not isinstance(marker, dict):
        return []
    if int(marker.get("version") or 0) < HF_README_MEDIA_VERSION:
        return []
    items = marker.get("items")
    return list(items) if isinstance(items, list) else []


def readme_media_marker(card_data, media_items):
    card = _json_mapping(card_data)
    cleaned = []
    for item in media_items or []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        media_type = str(item.get("type") or "").strip().lower()
        if not url or media_type not in {"image", "video"}:
            continue
        cleaned.append({
            "type": media_type,
            "url": url,
            "thumbnail": str(item.get("thumbnail") or ""),
            "filename": str(item.get("filename") or ""),
            "path": str(item.get("path") or item.get("filename") or ""),
            "metadata": item.get("metadata") if isinstance(item.get("metadata"), dict) else {},
        })
    card[HF_README_MEDIA_KEY] = {
        "version": HF_README_MEDIA_VERSION,
        "items": cleaned,
    }
    return card


def merge_media_items(repository_media, readme_media):
    merged = []
    seen = set()
    for item in list(repository_media or []) + list(readme_media or []):
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        identity = url.split("#", 1)[0]
        if identity in seen:
            continue
        seen.add(identity)
        record = dict(item)
        record["position"] = len(merged)
        merged.append(record)
    return merged


def media_summary(media_items):
    items = list(media_items or [])
    first_image = next((str(i.get("url") or "") for i in items if str(i.get("type") or "").lower() == "image"), "")
    return {
        "image": first_image,
        "preview_count": sum(1 for i in items if str(i.get("type") or "").lower() == "image"),
        "has_video": any(str(i.get("type") or "").lower() == "video" for i in items),
        "has_media": bool(items),
        "media": items,
    }


def files_from_model_details(model_id, details):
    """Return normalized files supplied by the regular model-info endpoint."""
    details = details if isinstance(details, dict) else {}
    revision = details.get("sha", "") or "main"
    files = []
    for sibling in details.get("siblings", []) or []:
        if not isinstance(sibling, dict):
            continue
        filename = sibling.get("rfilename") or sibling.get("path") or ""
        record = _repository_file_record(model_id, filename, sibling, revision)
        if record:
            files.append(record)
    return files


def _next_link(response):
    link_header = response.headers.get("Link", "") or ""
    if not link_header:
        return ""
    try:
        from requests.utils import parse_header_links
        links = parse_header_links(link_header.rstrip("> ").replace(">,<", ">, <"))
        for link in links:
            if link.get("rel") == "next" and link.get("url"):
                return urljoin(response.url, link["url"])
    except Exception:
        pass
    return ""


def fetch_recursive_repository_inventory(model_id, revision="main", max_entries=MAX_RECURSIVE_TREE_ENTRIES):
    """Fetch the repository's complete recursive tree and report completeness.

    The list may still contain useful partial data if a very large repository
    hits the safety cap, but ``complete`` is False in that case so a later scan
    or Reload Model can retry rather than permanently treating it as complete.
    """
    revision = str(revision or "main")
    encoded_revision = quote(revision, safe="")
    next_url = f"https://huggingface.co/api/models/{model_id}/tree/{encoded_revision}"
    next_params = {"recursive": "true", "expand": "false"}
    files = []
    entries_seen = 0
    page_number = 0
    complete = False

    while next_url and entries_seen < max_entries:
        if scan_control.should_stop():
            return files, False
        page_number += 1
        response = get_with_backoff(
            session,
            next_url,
            provider="Hugging Face",
            label=f"recursive repository tree {model_id} page {page_number}",
            params=next_params,
            timeout=20,
        )
        if response.status_code != 200:
            return files, False

        try:
            entries = response.json()
        except Exception:
            return files, False
        if not isinstance(entries, list):
            return files, False
        if not entries:
            complete = True
            break

        remaining = max_entries - entries_seen
        page_entries = entries[:remaining]
        entries_seen += len(page_entries)

        for item in page_entries:
            if not isinstance(item, dict) or str(item.get("type") or "").lower() != "file":
                continue
            record = _repository_file_record(
                model_id,
                item.get("path") or item.get("rfilename") or "",
                item,
                revision,
            )
            if record:
                files.append(record)

        if len(entries) > len(page_entries):
            complete = False
            break

        next_url = _next_link(response)
        next_params = None
        if not next_url:
            complete = True
            break

    if entries_seen >= max_entries and next_url:
        complete = False
        print(
            f"Hugging Face recursive tree capped at {max_entries} entries for {model_id}; "
            f"{len(files)} files captured"
        )
    elif files and page_number > 1:
        print(
            f"Hugging Face recursive tree: {len(files)} files across "
            f"{page_number} pages for {model_id}"
        )

    return files, complete


def fetch_recursive_repository_files(model_id, revision="main", max_entries=MAX_RECURSIVE_TREE_ENTRIES):
    """Compatibility wrapper returning only recursive repository files."""
    files, _complete = fetch_recursive_repository_inventory(
        model_id, revision=revision, max_entries=max_entries
    )
    return files


def repository_files_with_status(
    model_id,
    details,
    *,
    force_recursive=False,
    allow_recursive_fallback=True,
):
    """Return ``(files, complete, method)`` for one Hugging Face repository.

    ``force_recursive`` is used for new/changed repositories and explicit
    Reload Model actions. It guarantees that nested files are considered even
    when the regular model-info endpoint already exposes plausible top-level
    weights.
    """
    details = details if isinstance(details, dict) else {}
    shallow_files = files_from_model_details(model_id, details)
    revision = details.get("sha", "") or "main"

    if force_recursive:
        recursive_files, complete = fetch_recursive_repository_inventory(
            model_id, revision=revision
        )
        if recursive_files or complete:
            return recursive_files, complete, "recursive_tree"
        return shallow_files, False, "model_info_fallback"

    if not allow_recursive_fallback:
        return shallow_files, False, "model_info"

    model_name = str(model_id or "").rsplit("/", 1)[-1].casefold()
    archive_named = any(token in model_name for token in ("archive", "collection", "bundle"))
    has_primary = any(bool(item.get("primary")) for item in shallow_files)
    if has_primary and not archive_named:
        return shallow_files, False, "model_info"

    recursive_files, complete = fetch_recursive_repository_inventory(
        model_id, revision=revision
    )
    if recursive_files or complete:
        return recursive_files, complete, "recursive_tree"
    return shallow_files, False, "model_info_fallback"


def repository_files(model_id, details, *, allow_recursive_fallback=True):
    """Compatibility helper returning the best available repository files."""
    files, _complete, _method = repository_files_with_status(
        model_id,
        details,
        allow_recursive_fallback=allow_recursive_fallback,
    )
    return files



def scan(
    term,
    scan_seen_models=None,
    scan_settings=None,
    creator=None
):
    _apply_auth()

    detail_fetches = 0
    readme_fetches = 0
    preview_models = 0
    gated_models = 0
    media_files = 0

    scan_settings = scan_settings or {}

    SEARCH_DAYS = int(scan_settings.get("search_days", 7))
    MAX_RESULTS = max(1, int(scan_settings.get("max_results", 100)))
    SORT_MODE = scan_settings.get("sort", "newest_updated")

    sort_map = {
        "newest_updated": "lastModified",
        "newest_created": "createdAt",
        "downloads": "downloads",
        "likes": "likes",
        "trending": "trendingScore"
    }
    api_sort = sort_map.get(SORT_MODE, "lastModified")

    start_time = time.perf_counter()

    cutoff = datetime.utcnow() - timedelta(days=SEARCH_DAYS)


    results = []

    if scan_seen_models is None:
        scan_seen_models = set()

    duplicates = 0
    old_models = 0


    query = term

    if creator:
        print(f"\nCREATOR SCAN: {creator}")
    else:
        print(f"\nSEARCH TERM: {query}")

    # AbyssBeacon exposes a total result ceiling rather than page controls.
    # Hugging Face pagination is followed via the API's Link header.
    per_request = 1000
    target_results = 10000 if creator else MAX_RESULTS
    params = {
        "limit": min(per_request, target_results),
        "sort": api_sort,
        "direction": -1
    }

    if creator:
        params["author"] = creator
    else:
        params["search"] = query

    items = []
    next_url = HF_API
    next_params = params
    page_number = 0

    while next_url and len(items) < target_results:
        if scan_control.should_stop():
            print("Hugging Face scan stopped")
            return results

        page_number += 1
        try:
            r = get_with_backoff(
                session, next_url, provider="Hugging Face",
                label=f"search page {page_number}",
                params=next_params, timeout=15
            )
        except Exception as e:
            print("HF connection error:", e)
            break

        if r.status_code == 429:
            print("Hugging Face search stopped after repeated rate limiting")
            break

        if r.status_code != 200:
            print("HF ERROR:", r.status_code)
            break

        page_items = r.json()
        if not isinstance(page_items, list):
            break

        remaining = target_results - len(items)
        items.extend(page_items[:remaining])
        print(f"Hugging Face page {page_number}: {len(page_items)} results")

        if len(items) >= target_results or not page_items:
            break

        link_header = r.headers.get("Link", "")
        next_link = ""
        if link_header:
            try:
                from requests.utils import parse_header_links
                links = parse_header_links(link_header.rstrip("> ").replace(">,<", ">, <"))
                for link in links:
                    if link.get("rel") == "next" and link.get("url"):
                        next_link = link["url"]
                        break
            except Exception:
                next_link = ""

        if not next_link:
            break

        next_url = next_link
        next_params = None

    print(f"Hugging Face results inspected: {len(items)}")

    for item in items:

        if scan_control.should_stop():
            print("Hugging Face scan stopped")
            return results


        model_id = item.get("id")

        seen_key = ("huggingface", model_id.lower())

        if seen_key in scan_seen_models:
            continue

        scan_seen_models.add(seen_key)


        author = model_id.split("/")[0] if "/" in model_id else ""
        blocked = {str(x).casefold() for x in (scan_settings.get("_blocked_creators") or [])}
        if author and author.casefold() in blocked:
            continue

        model_url = f"https://huggingface.co/{model_id}"

        model_key = model_id.lower()

        existing_model = database.get_model(
            model_key,
            "huggingface"
        )

        details = None
        classification_refresh = False
        same_repo_revision = False
        stored_inventory_complete = False
        stored_library_refresh_complete = False


        # FAST DUPLICATE CHECK USING TIMESTAMP + COMPLETE TREE/MEDIA STATE

        if existing_model:

            classification_refresh = needs_repository_classification_refresh(
                existing_model["card_data"],
                source="huggingface",
            )

            api_sha = item.get(
                "sha",
                ""
            )

            db_sha = existing_model["sha"] or ""


            api_modified = item.get(
                "lastModified",
                ""
            )

            db_modified = existing_model["updated"] or ""


            # SHA is the strongest indicator. If unavailable, use the source's
            # modified timestamp as the same-revision fallback.
            if api_sha and db_sha:
                same_repo_revision = api_sha == db_sha
            elif api_modified and db_modified:
                same_repo_revision = api_modified == db_modified

            stored_inventory_complete = stored_repository_inventory_status(
                existing_model,
                expected_revision=api_sha if api_sha and db_sha else "",
            )
            stored_library_refresh_complete = (
                stored_library_refresh_version(existing_model["card_data"])
                >= HF_LIBRARY_REFRESH_VERSION
            )

            # An unchanged repository is a true duplicate only after AbyssBeacon
            # has already captured a complete recursive tree *and* checked the
            # README for embedded media for this maintenance revision. Older rows
            # therefore repair themselves the next time a normal HF scan sees them.
            if (
                same_repo_revision
                and stored_inventory_complete
                and stored_library_refresh_complete
                and not classification_refresh
            ):
                duplicates += 1
                continue


            # model requires refresh / metadata migration / inventory completion


            scan_status.update_status(
                status="running",
                source="huggingface",
                current=model_id,
            )


        created = item.get(
            "createdAt",
            ""
        )

        # Search Days follows the selected time-based sort. For popularity
        # sorts, keep the age window based on creation date.
        cutoff_value = (
            item.get("lastModified", "")
            if SORT_MODE == "newest_updated"
            else created
        )

        if cutoff_value and not creator:

            try:

                model_date = datetime.fromisoformat(
                    cutoff_value.replace(
                        "Z",
                        "+00:00"
                    )
                ).replace(
                    tzinfo=None
                )

                if model_date < cutoff:

                    old_models += 1
                    continue

            except Exception:

                pass


        # FAST DUPLICATE CHECK USING SEARCH RESULT

        repo_sha = item.get(
            "sha",
            ""
        )


        # ONLY FETCH DETAILS FOR NEW OR CHANGED MODELS

        try:

            time.sleep(0.05)

            detail_fetches += 1

            detail_response = get_with_backoff(
                session,
                f"https://huggingface.co/api/models/{model_id}",
                provider="Hugging Face",
                label=f"model detail {model_id}",
                # Hugging Face only includes RepoSibling size/LFS metadata
                # when file metadata is requested. This maps to
                # HfApi.model_info(..., files_metadata=True).
                params={"blobs": "true"},
                timeout=15
            )

            if detail_response.status_code != 200:
                print(
                    "DETAIL ERROR:",
                    model_id,
                    detail_response.status_code
                )
                continue

            details = detail_response.json()

            card_data = details.get("cardData", {}) or {}
            existing_card_data = _json_mapping(existing_model["card_data"]) if existing_model else {}
            cached_readme_media = stored_readme_media(existing_card_data)

            gated = False

            if details.get("gated"):
                gated = True

            card_data["gated"] = gated

            if gated:
                gated_models += 1

            # Processed Hugging Face repositories get their raw README once. Beyond
            # description/classification text, model cards can contain preview images
            # and videos hosted on cdn-uploads.huggingface.co that never appear in the
            # repository file tree. Unchanged repositories are skipped above, so this
            # does not add a README request to every duplicate on every scan.
            api_description = metadata.extract_description(details)
            stored_description = (existing_model["description"] or "") if existing_model else ""
            readme_text = ""
            readme_loaded = False
            readme_checked = False
            try:
                readme_fetches += 1
                readme_response = get_with_backoff(
                    session,
                    f"https://huggingface.co/{model_id}/raw/main/README.md",
                    provider="Hugging Face",
                    label=f"README {model_id}",
                    timeout=10
                )
                readme_checked = True
                if readme_response.status_code == 200:
                    readme_text = str(readme_response.text or "")
                    readme_loaded = True
            except Exception:
                readme_text = ""

            if readme_loaded:
                details["readme"] = readme_text
            else:
                # Preserve earlier classification evidence if the README request is
                # temporarily unavailable. Cached embedded media is reused below too.
                details["readme"] = stored_description if (not api_description and stored_description) else ""


        except Exception as e:

            print(
                "DETAIL ERROR:",
                model_id,
                type(e).__name__,
                e
            )

            continue

        raw_tags = item.get(
            "tags",
            []
        )


        if isinstance(raw_tags, list):

            tag_values = []
            for tag in raw_tags:
                if not isinstance(tag, str):
                    continue
                text = tag.strip()
                if not text:
                    continue
                prefix = text.split(":", 1)[0].strip().casefold() if ":" in text else ""
                if prefix in {"region", "license", "library_name", "pipeline_tag"}:
                    continue
                tag_values.append(text)
            tags = ",".join(dict.fromkeys(tag_values))

        else:

            tags = str(raw_tags or "")
            if tags.casefold().startswith("region:"):
                tags = ""

        sensitive = metadata.detect_sensitive(
            model_id,
            tags,
            card_data,
            details.get("tags", [])
        )

        # Repository inventory policy:
        #   * new/changed repositories -> complete recursive tree
        #   * unchanged repositories with a stored complete tree -> reuse it
        #   * older shallow-only rows -> complete them once
        # This keeps nested files correct without re-walking huge unchanged
        # repositories on every scan.
        repo_sha = str(details.get("sha") or repo_sha or "").strip()
        reuse_stored_tree = bool(
            existing_model
            and same_repo_revision
            and stored_inventory_complete
        )

        if reuse_stored_tree:
            files = stored_repository_files(existing_model)
            inventory_complete = True
            inventory_method = "stored_recursive_tree"
        else:
            files, inventory_complete, inventory_method = repository_files_with_status(
                model_id,
                details,
                force_recursive=True,
            )

        card_data = repository_inventory_marker(
            card_data,
            complete=inventory_complete,
            revision=repo_sha,
            method=inventory_method,
            file_count=len(files),
        )

        repository_media_data = media.extract_media(
            files,
            f"https://huggingface.co/{model_id}/resolve/main"
        )
        repository_readmes = repository_readme_paths(files)
        media_classification = classify_repository({
            "source": "huggingface",
            "model_id": model_id,
            "details": details,
            "files": files,
            "tags": details.get("tags") or item.get("tags") or [],
            "library": details.get("library_name") or details.get("libraryName") or "",
        }) or {}
        include_nested_readmes = media_classification.get("container") == "collection"
        if readme_loaded or repository_readmes:
            refreshed_readme_media = fetch_repository_readme_media(
                model_id,
                files,
                root_readme_text=readme_text,
                revision=repo_sha or "main",
                include_nested=include_nested_readmes,
            )
            readme_media = refreshed_readme_media or cached_readme_media
        else:
            readme_media = cached_readme_media
        card_data = readme_media_marker(card_data, readme_media)
        if inventory_complete and readme_checked:
            card_data = library_refresh_marker(
                card_data,
                status="complete",
                inventory_complete=True,
                readme_checked=True,
            )
        model_media = merge_media_items(repository_media_data.get("media") or [], readme_media)
        media_data = media_summary(model_media)

        preview = media_data["image"]

        preview_count = media_data["preview_count"]

        has_video = media_data["has_video"]

        has_media = media_data["has_media"]


        if preview:
            preview_models += 1


        model_media_count = len(model_media)
        media_files += model_media_count

        raw_model = {

            "details": details,

            "model_id": model_id,

            "model_key": model_key,

            "tags": tags,

            "files": files,

            "image": preview,

            "preview_count": preview_count,

            "has_media": has_media,

            "has_video": has_video,

            "media": model_media,

            "gated": gated,

            "card_data": card_data,

            "pipeline": details.get("pipeline_tag") or details.get("pipelineTag") or "",
            "library": details.get("library_name") or details.get("libraryName") or "",

            "sensitive": sensitive,

            "source": "huggingface",

            "url": model_url,

            "sha": repo_sha,

            "_existing": bool(existing_model),

            "_existing_id":
                existing_model["id"] if existing_model else None

        }


        processed_model = processors.build_model(
            raw_model
        )

        if str(getattr(processed_model, "architecture", "") or "").casefold() == "other":
            processed_model.architecture = processors.classify_architecture_with_watch_fallback(
                scan_settings.get("_watch_architecture"),
                getattr(processed_model, "base_model", ""),
                getattr(processed_model, "name", ""),
                getattr(processed_model, "display_name", ""),
                getattr(processed_model, "tags", ""),
                getattr(processed_model, "description", ""),
                raw_model.get("card_data"),
            )


        results.append(
            processed_model
        )


    elapsed = time.perf_counter() - start_time


    print("\n========================================")
    print("Hugging Face Scan Complete")
    print("========================================")
    print(f"Processed models : {len(results)}")
    print(f"Old models : {old_models}")
    print(f"Duplicates : {duplicates}")
    print(f"Time       : {elapsed:.2f} seconds")
    print(f"Detail fetches: {detail_fetches}")
    print(f"README fetches: {readme_fetches}")
    print(f"Models with previews: {preview_models}")
    print(f"Gated models: {gated_models}")
    print(f"Media files found: {media_files}")


    return results
