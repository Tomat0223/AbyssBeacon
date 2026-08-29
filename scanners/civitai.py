from scan_logging import verbose_print as print
NAME = "civitai"


def _apply_auth():
    token = get_source_token("civitai")
    if token:
        session.headers["Authorization"] = f"Bearer {token}"
    else:
        session.headers.pop("Authorization", None)

DISPLAY = "CivitAI"
ENABLED = True

import builtins
import html
import json
import os
import re
import time
from datetime import datetime, timedelta

import requests

import database
import scan_control
from scanners.common.model import Model
from scanners.common import processors
from utils.loader import load_model_types
from secrets_manager import get_source_token, get_civitai_search_key
from scanners.http_retry import get_with_backoff, get_cached_text_with_backoff

API = "https://civitai.com/api/v1/models"
MODEL_DETAIL_API = "https://civitai.com/api/v1/models/{model_id}"
SEARCH_API = "https://search-new.civitai.com/multi-search"
DEBUG_SCANNERS = False

session = requests.Session()
session.headers.update({"User-Agent": "AbyssBeacon/1.0", "Accept": "application/json"})
if os.environ.get("CIVITAI_API_KEY"):
    session.headers["Authorization"] = f"Bearer {os.environ['CIVITAI_API_KEY']}"

_DETAIL_ENRICHMENT_DISABLED = False


def debug_print(*args, **kwargs):
    if DEBUG_SCANNERS:
        print(*args, **kwargs)


def _plain_text(value):
    """Turn CivitAI's HTML descriptions into readable plain text."""
    if not value:
        return ""
    text = str(value)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|li|h[1-6])>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text).replace("\r", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def _parse_date(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def _latest_date(*values):
    parsed = [(v, _parse_date(v)) for v in values if v]
    parsed = [(raw, dt) for raw, dt in parsed if dt is not None]
    return max(parsed, key=lambda pair: pair[1])[0] if parsed else ""


def _item_activity_datetime(item):
    """Newest source activity we can infer from a CivitAI model payload."""
    dates = [
        item.get("updatedAt"),
        item.get("publishedAt"),
        item.get("createdAt"),
        item.get("lastVersionAt"),
    ]
    for version in item.get("modelVersions") or []:
        if not isinstance(version, dict):
            continue
        dates.extend([
            version.get("updatedAt"),
            version.get("publishedAt"),
            version.get("createdAt"),
        ])
    parsed = [_parse_date(value) for value in dates if value]
    parsed = [value for value in parsed if value is not None]
    return max(parsed) if parsed else datetime.min


def _item_downloads(item):
    stats = item.get("stats") or {}
    return int(stats.get("downloadCount") or item.get("downloadCount") or 0)


def _item_rating(item):
    stats = item.get("stats") or {}
    # Different API revisions have exposed slightly different rating fields.
    for key in ("rating", "ratingCount", "favoriteCount", "thumbsUpCount"):
        try:
            value = stats.get(key)
            if value is not None:
                return float(value)
        except Exception:
            pass
    return 0.0


def _fetch_model_pages(label, base_params, max_items):
    """Fetch one CivitAI discovery path and follow its advertised pagination."""
    collected = []
    next_url = API
    next_params = dict(base_params)
    page_number = 0
    page_size = min(100, max_items) if max_items else 100
    next_params["limit"] = page_size

    while next_url and len(collected) < max_items:
        if scan_control.should_stop():
            print("CivitAI scan stopped")
            break

        page_number += 1
        remaining = max_items - len(collected)
        if next_params is not None:
            next_params["limit"] = min(100, remaining)

        debug_print(f"CivitAI {label} request:", next_url, next_params)
        try:
            response = get_with_backoff(
                session, next_url, provider="CivitAI",
                label=f"{label} page {page_number}",
                pace_key="CivitAI.com", min_interval=1.25,
                params=next_params, timeout=30
            )
        except Exception as exc:
            print(f"CivitAI {label} connection error:", exc)
            break

        if response.status_code == 429:
            print(f"CivitAI {label} stopped after repeated rate limiting")
            break
        if response.status_code != 200:
            print(f"CivitAI {label} error:", response.status_code)
            debug_print(response.text[:1000])
            break

        try:
            payload = response.json()
        except Exception:
            print(f"CivitAI {label} returned invalid JSON")
            break

        page_items = payload.get("items") or []
        if not isinstance(page_items, list):
            page_items = []

        collected.extend(page_items[:remaining])
        print(f"CivitAI {label} page {page_number}: {len(page_items)} results")

        metadata = payload.get("metadata") or {}
        debug_print(f"CivitAI {label} metadata:", metadata)
        if DEBUG_SCANNERS and page_items:
            sample = []
            for candidate in page_items[:8]:
                sample.append({
                    "id": candidate.get("id"),
                    "name": candidate.get("name"),
                    "activity": _item_activity_datetime(candidate).isoformat()
                    if _item_activity_datetime(candidate) != datetime.min else None,
                    "tags": (candidate.get("tags") or [])[:8],
                })
            debug_print(f"CivitAI {label} sample:", sample)

        if not page_items or len(collected) >= max_items:
            break

        next_cursor = metadata.get("nextCursor") if isinstance(metadata, dict) else None
        next_page = metadata.get("nextPage") if isinstance(metadata, dict) else None

        # CivitAI's current browse stack exposes cursor pagination. The older
        # REST response also exposed nextPage, so support both shapes. The
        # official CivitAI MCP server uses this same /models endpoint with
        # baseModels/types/cursor filters for model discovery.
        if next_cursor not in (None, ""):
            next_url = API
            next_params = dict(base_params)
            next_params["cursor"] = next_cursor
        elif next_page:
            next_url = str(next_page).replace("http://", "https://", 1)
            next_params = None
        elif len(page_items) < page_size:
            break
        else:
            # Defensive legacy fallback if the server supplies neither cursor
            # nor a nextPage URL.
            next_url = API
            next_params = dict(base_params)
            next_params["page"] = page_number + 1

    return collected




def _configured_model_type_label(label):
    """Return the user's configured spelling for a source-provided model type."""
    wanted = str(label or "").replace(" ", "").casefold()
    if not wanted:
        return ""
    for configured in load_model_types().keys():
        if str(configured).replace(" ", "").casefold() == wanted:
            return configured
    return str(label or "")



def _models_v9_discovery(base_model, max_items, api_sort="Newest", model_type=""):
    """TEST: use CivitAI's live website models_v9 index for architecture discovery.

    The saved CivitAI token is used only for the search request. Search hits are
    then hydrated through AbyssBeacon's existing public detail API, so files,
    media, versions, Early Access and download handling stay unchanged.
    """
    _apply_auth()
    token = get_civitai_search_key()
    if not token:
        builtins.print("CivitAI models_v9 discovery unavailable: no saved website search key")
        return None

    try:
        max_items = max(1, int(max_items))
    except (TypeError, ValueError):
        max_items = 100

    sort_spec = {
        "Newest": ["createdAt:desc"],
        "Most Downloaded": ["metrics.downloadCount:desc"],
        "Highest Rated": ["metrics.thumbsUpCount:desc"],
    }.get(api_sort, ["createdAt:desc"])

    filters = [
        [f'"versions.baseModel"="{base_model}"'],
        "(poi != true) AND (availability != Private) "
        "AND (NOT (nsfwLevel IN [4, 8, 16, 32] AND version.baseModel IN "
        "['SD 3', 'SD 3.5', 'SD 3.5 Medium', 'SD 3.5 Large', 'SD 3.5 Large Turbo', "
        "'SDXL Turbo', 'SVD', 'SVD XT', 'Stable Cascade', 'Ideogram 4.0'])) "
        "AND (nsfwLevel=1 OR nsfwLevel=2)"
    ]
    if model_type:
        filters.append([f'"type"="{model_type}"'])

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Origin": "https://civitai.com",
        "Referer": "https://civitai.com/",
        "X-Meilisearch-Client": "Meilisearch instant-meilisearch (v0.13.5) ; Meilisearch JavaScript (v0.34.0)",
    }

    hits = []
    offset = 0


    while len(hits) < max_items:
        if scan_control.should_stop():
            break

        request_limit = min(100, max_items - len(hits))
        payload = {
            "queries": [{
                "q": "",
                "indexUid": "models_v9",
                "facets": [
                    "category.name", "checkpointType", "fileFormats",
                    "lastVersionAtUnix", "tags.name", "type",
                    "user.username", "versions.baseModel"
                ],
                "attributesToHighlight": [],
                "highlightPreTag": "__ais-highlight__",
                "highlightPostTag": "__/ais-highlight__",
                "limit": request_limit,
                "offset": offset,
                "filter": filters,
                "sort": sort_spec,
            }]
        }

        try:
            response = requests.post(
                SEARCH_API,
                headers=headers,
                json=payload,
                timeout=30,
            )
        except Exception as exc:
            builtins.print(f"CivitAI models_v9 request failed: {exc}")
            return None

        if response.status_code != 200:
            builtins.print(f"CivitAI models_v9 HTTP {response.status_code}; falling back to public API")
            return None

        try:
            body = response.json()
        except Exception:
            builtins.print("CivitAI models_v9 returned invalid JSON; falling back to public API")
            return None

        results = body.get("results") or []
        result = results[0] if results and isinstance(results[0], dict) else {}
        page_hits = result.get("hits") or []
        if not isinstance(page_hits, list):
            page_hits = []


        if not page_hits:
            break

        hits.extend(page_hits[:request_limit])
        offset += len(page_hits)

        estimated_total = result.get("estimatedTotalHits")
        if len(page_hits) < request_limit:
            break
        if isinstance(estimated_total, int) and offset >= estimated_total:
            break

    # IMPORTANT: do NOT hydrate every website hit here. The previous test
    # fetched /api/v1/models/{id} for every discovered record before AbyssBeacon
    # had a chance to run its unchanged-model precheck, turning a 200-result
    # repeat scan into ~200 paced requests.
    #
    # Instead, normalize the models_v9 hit into the same lightweight shape the
    # existing precheck understands. Only records that survive that precheck
    # are hydrated later in scan().
    items = []
    seen = set()
    for hit in hits[:max_items]:
        if scan_control.should_stop():
            break
        if not isinstance(hit, dict) or hit.get("id") is None:
            continue

        model_id = str(hit.get("id"))
        if model_id in seen:
            continue
        seen.add(model_id)

        versions = hit.get("versions") if isinstance(hit.get("versions"), list) else []
        version = hit.get("version") if isinstance(hit.get("version"), dict) else {}
        if not versions and version:
            versions = [version]

        user = hit.get("user") if isinstance(hit.get("user"), dict) else {}
        creator = hit.get("creator") if isinstance(hit.get("creator"), dict) else {}
        if not creator and user:
            creator = {"username": user.get("username") or ""}

        item = dict(hit)
        item["id"] = hit.get("id")
        item["creator"] = creator
        item["modelVersions"] = [
            dict(v) for v in versions if isinstance(v, dict)
        ]
        item["_models_v9_hit"] = True
        item["_watch_architecture"] = str(base_model or "")

        # Keep the website's current activity fields where our existing helper
        # already knows to look for them.
        if not item.get("updatedAt"):
            item["updatedAt"] = hit.get("lastVersionAt") or hit.get("publishedAt") or ""
        if not item.get("createdAt"):
            item["createdAt"] = hit.get("createdAt") or hit.get("publishedAt") or ""

        items.append(item)

    return items


def _structured_discovery(base_model, max_items, api_sort="Newest", model_type="", query=""):
    """Use CivitAI's current structured REST discovery fields.

    CivitAI's official MCP search_models tool uses /api/v1/models with
    baseModels, types, sort, limit and cursor. This gives AbyssBeacon the same
    important structured filters without relying on CivitAI.com's private
    browser search token.
    """
    params = {"sort": api_sort}
    if base_model:
        params["baseModels"] = base_model
    if model_type:
        params["types"] = model_type
    if query:
        params["query"] = query

    label = "structured"
    pieces = []
    if base_model:
        pieces.append(f"base model={base_model}")
    if model_type:
        pieces.append(f"type={model_type}")
    print("CivitAI structured discovery:" + (" " + ", ".join(pieces) if pieces else ""))
    return _fetch_model_pages(label, params, max_items)


def _model_type(api_type, text):
    mapping = {
        "lora": "LoRA",
        "checkpoint": "Checkpoint",
        "checkpoint merge": "Checkpoint",
        "workflow": "Workflow",
        "workflows": "Workflow",
        "vae": "VAE",
        "controlnet": "ControlNet",
        "textualinversion": "Textual Inversion",
        "hypernetwork": "Hypernetwork",
        "poses": "Poses",
        "aestheticgradient": "Aesthetic Gradient",
    }
    normalized = str(api_type or "").strip().lower()
    direct = mapping.get(normalized) or mapping.get(normalized.replace(" ", ""))
    if direct:
        # CivitAI's explicit type wins over fuzzy filename/tag detection.
        return _configured_model_type_label(direct)
    return processors.classify_model_type(text)

def _file_sha256(file_data):
    hashes = (file_data or {}).get("hashes") or {}
    if isinstance(hashes, dict):
        return str(hashes.get("SHA256") or hashes.get("sha256") or file_data.get("sha256") or "")
    if isinstance(hashes, list):
        for entry in hashes:
            if not isinstance(entry, dict):
                continue
            kind = str(entry.get("type") or entry.get("name") or "").replace("-", "").casefold()
            if kind == "sha256":
                return str(entry.get("hash") or entry.get("value") or "")
    return str((file_data or {}).get("sha256") or "")


def _file_record(file_data, version, model_id):
    file_data = file_data or {}
    metadata = file_data.get("metadata") or {}
    name = file_data.get("name") or metadata.get("filename") or f"civitai-{file_data.get('id', '')}"
    version_name = str(version.get("name") or version.get("id") or "version")
    path = f"{version_name}/{name}"
    file_id = file_data.get("id") or file_data.get("fileId") or file_data.get("file_id")
    version_id = version.get("id")
    if version_id and file_id:
        # CivitAI's browser download identifies the exact artifact with fileId.
        # This matters when a version exposes multiple files/quantizations.
        download_url = f"https://civitai.com/api/download/models/{version_id}?fileId={file_id}"
    else:
        download_url = file_data.get("downloadUrl") or file_data.get("url") or version.get("downloadUrl") or ""
    lower = name.lower()
    primary = bool(file_data.get("primary")) or lower.endswith((".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf"))
    size_kb = file_data.get("sizeKB") or file_data.get("sizeKb") or ""
    try:
        size_bytes = int(float(size_kb) * 1024) if size_kb not in ("", None) else 0
    except (TypeError, ValueError):
        size_bytes = 0
    return {
        "name": name,
        "path": path,
        "primary": primary,
        "size": size_kb,
        "size_bytes": size_bytes,
        "download_url": download_url,
        "file_id": file_id,
        "id": file_id,
        "version_id": version_id,
        "version": version_name,
        "format": metadata.get("format") or file_data.get("format") or "",
        "file_type": file_data.get("type") or "",
        "fp": metadata.get("fp") or "",
        "size_label": metadata.get("size") or "",
        "pickle_scan": file_data.get("pickleScanResult") or "",
        "virus_scan": file_data.get("virusScanResult") or "",
        "sha256": _file_sha256(file_data),
        "hashes": file_data.get("hashes") or {},
    }


def _media_records(versions, model_sensitive=False):
    media = []
    position = 0
    for version in versions:
        version_name = str(version.get("name") or version.get("id") or "version")
        version_id = version.get("id")
        model_files = [f.get("name") for f in (version.get("files") or []) if isinstance(f, dict) and f.get("name")]
        for index, image in enumerate(version.get("images") or []):
            if not isinstance(image, dict) or not image.get("url"):
                continue
            raw_type = str(image.get("type") or "image").lower()
            url = image.get("url") or ""
            media_type = "video" if raw_type == "video" or re.search(r"\.(mp4|webm|mov)(?:\?|$)", url, re.I) else "image"
            filename = f"preview-{index + 1}.{'mp4' if media_type == 'video' else 'jpg'}"
            path = f"{version_name}/{filename}"
            meta = dict(image.get("meta") or {})
            meta.update({
                "filename": filename,
                "path": path,
                "civitai_model_version": version_name,
                "civitai_model_version_id": version_id,
            })
            if model_files:
                meta["model_files"] = model_files
            if image.get("width"):
                meta.setdefault("width", image.get("width"))
            if image.get("height"):
                meta.setdefault("height", image.get("height"))
            if image.get("nsfw") not in (None, False, "None"):
                meta.setdefault("maturity", image.get("nsfw"))
            media.append({
                "type": media_type,
                "url": url,
                "thumbnail": image.get("url") if media_type == "video" else "",
                "filename": filename,
                "path": path,
                "metadata": meta,
                "position": position,
            })
            position += 1
    return media




def _fetch_model_detail(model_id):
    """Fetch one rich model payload without ever stalling a scan on 429.

    CivitAI's per-model endpoint is much more aggressively throttled than its
    browse endpoint. One 429 disables optional detail hydration for the rest of
    the current CivitAI scan; the listing payload remains usable and scanning
    continues normally.
    """
    global _DETAIL_ENRICHMENT_DISABLED
    if not model_id or _DETAIL_ENRICHMENT_DISABLED:
        return {}
    try:
        response = get_with_backoff(
            session,
            MODEL_DETAIL_API.format(model_id=model_id),
            provider="CivitAI",
            label=f"model detail {model_id}",
            pace_key="CivitAI.com", min_interval=1.25,
            timeout=30,
            max_retries=0,
        )
        if response.status_code == 200:
            payload = response.json()
            return payload if isinstance(payload, dict) else {}
        if response.status_code == 429:
            _DETAIL_ENRICHMENT_DISABLED = True
            print("CivitAI detail enrichment paused for this scan after rate limiting; continuing with browse metadata")
            return {}
        debug_print("CivitAI model detail status:", response.status_code)
    except Exception as exc:
        debug_print("CivitAI model detail failed:", exc)
    return {}

def _find_model_record(value, model_id):
    """Find the full model object embedded in CivitAI's Next.js page state."""
    if isinstance(value, dict):
        if str(value.get("id") or "") == str(model_id or "") and isinstance(value.get("modelVersions"), list):
            return value
        for child in value.values():
            found = _find_model_record(child, model_id)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_model_record(child, model_id)
            if found:
                return found
    return None




def _hydrated_page_versions(page, model_id=None):
    """Extract modelVersions arrays from rendered CivitAI page state.

    CivitAI can expose the authoritative per-artifact filenames/types in page
    hydration even when the REST model endpoint repeats a main filename for an
    optional file. Keep the richest matching modelVersions array we can find.
    """
    text = html.unescape(str(page or ""))
    decoder = json.JSONDecoder()
    marker = '"modelVersions"'
    start = 0
    best = []

    while True:
        pos = text.find(marker, start)
        if pos < 0:
            break
        colon = text.find(":", pos + len(marker))
        if colon < 0:
            break
        value_start = colon + 1
        while value_start < len(text) and text[value_start].isspace():
            value_start += 1
        if value_start >= len(text) or text[value_start] != "[":
            start = pos + len(marker)
            continue
        try:
            versions, consumed = decoder.raw_decode(text[value_start:])
        except Exception:
            start = pos + len(marker)
            continue
        if isinstance(versions, list):
            cleaned = [v for v in versions if isinstance(v, dict)]
            if model_id is not None:
                matching = [
                    v for v in cleaned
                    if str(v.get("modelId") or v.get("model_id") or "") == str(model_id)
                ]
                if matching:
                    cleaned = matching
            richness = sum(len(v.get("files") or []) + len(v.get("images") or []) for v in cleaned)
            best_richness = sum(len(v.get("files") or []) + len(v.get("images") or []) for v in best)
            if cleaned and (not best or richness > best_richness):
                best = cleaned
        start = value_start + max(consumed, 1)
    return best

def _fetch_model_page_metadata(model_id):
    """Recover rich description/version state from CivitAI's rendered model page.

    The public REST listing is intentionally kept as the fast discovery path.
    This fallback is used only when the listing is missing author-written text
    or version access fields that the UI needs (for example Early Access).
    """
    global _DETAIL_ENRICHMENT_DISABLED
    if not model_id or _DETAIL_ENRICHMENT_DISABLED:
        return {}
    try:
        status_code, page, _cache_hit = get_cached_text_with_backoff(
            session,
            f"https://civitai.com/models/{model_id}",
            cache_key=("civitai-model-page", str(model_id)),
            provider="CivitAI",
            label=f"model page {model_id}",
            pace_key="CivitAI.com", min_interval=1.25,
            timeout=25,
            max_retries=0,
        )
        if status_code == 429:
            _DETAIL_ENRICHMENT_DISABLED = True
            print("CivitAI page enrichment paused for this scan after rate limiting; continuing with browse metadata")
            return {}
        if status_code != 200:
            return {}
        out = {}

        # The current site may serialize the complete version tree outside the
        # legacy __NEXT_DATA__ record. This is also where the exact optional
        # artifact names/types are preserved.
        hydrated_versions = _hydrated_page_versions(page, model_id)
        if hydrated_versions:
            out["versions"] = hydrated_versions

        # JSON-LD is a cheap, stable source for the parent model description.
        for match in re.finditer(
            r'<script[^>]+type=["\\\']application/ld\\+json["\\\'][^>]*>(.*?)</script>',
            page,
            flags=re.IGNORECASE | re.DOTALL,
        ):
            raw = html.unescape(match.group(1)).strip()
            try:
                payload = json.loads(raw)
            except Exception:
                continue
            records = payload if isinstance(payload, list) else [payload]
            for record in records:
                if isinstance(record, dict) and (str(record.get("@type") or "").casefold() == "softwareapplication" or record.get("description")):
                    out["description"] = record.get("description") or ""
                    break
            if out.get("description"):
                break

        # __NEXT_DATA__ carries the complete version list, including paid/EA
        # state, even when a model-version description is null.
        match = re.search(r'<script[^>]+id=["\\\']__NEXT_DATA__["\\\'][^>]*>(.*?)</script>', page, flags=re.IGNORECASE | re.DOTALL)
        if match:
            try:
                next_data = json.loads(html.unescape(match.group(1)))
                record = _find_model_record(next_data, model_id)
                if isinstance(record, dict):
                    out["model"] = record
                    if not out.get("description"):
                        out["description"] = record.get("description") or ""
                    if not out.get("versions"):
                        out["versions"] = record.get("modelVersions") or []
            except Exception as exc:
                debug_print("CivitAI Next data parse failed:", exc)
        return out
    except Exception as exc:
        debug_print("CivitAI model page metadata failed:", exc)
        return {}




def _fetch_filename_authority_versions(model_id):
    """Fetch rendered CivitAI file metadata for ambiguous multi-file versions.

    The REST tree can repeat a version's primary filename across optional
    artifacts even though their stable file IDs, sizes and types are distinct.
    Use the rendered page as filename/type authority only when needed.
    """
    if not model_id:
        return []
    try:
        status_code, page, _cache_hit = get_cached_text_with_backoff(
            session,
            f"https://civitai.com/models/{model_id}",
            cache_key=("civitai-model-page", str(model_id)),
            provider="CivitAI",
            label=f"filename metadata {model_id}",
            pace_key="CivitAI.com", min_interval=1.25,
            timeout=25,
            max_retries=3,
        )
        if status_code != 200:
            return []
        return _hydrated_page_versions(page, model_id)
    except Exception as exc:
        debug_print("CivitAI filename metadata failed:", exc)
        return []


def _has_ambiguous_multifile_names(versions):
    """True when one version exposes multiple files with duplicate/missing names."""
    for version in versions or []:
        if not isinstance(version, dict):
            continue
        files = [f for f in (version.get("files") or []) if isinstance(f, dict)]
        if len(files) <= 1:
            continue
        names = [str(f.get("name") or "").strip().casefold() for f in files]
        nonempty = [name for name in names if name]
        if len(nonempty) != len(files) or len(set(nonempty)) < len(nonempty):
            return True
    return False

def _merge_child_records(primary, secondary):
    """Merge files/images by stable identity, preferring page metadata."""
    out = []
    index = {}
    for records, prefer in ((primary or [], False), (secondary or [], True)):
        for record in records:
            if not isinstance(record, dict):
                continue
            key = str(
                record.get("id")
                or record.get("fileId")
                or record.get("uuid")
                or record.get("url")
                or record.get("name")
                or ""
            ).casefold()
            if not key:
                out.append(dict(record))
                continue
            if key not in index:
                merged = dict(record)
                index[key] = merged
                out.append(merged)
                continue
            merged = index[key]
            for field, value in record.items():
                if value in (None, "", [], {}):
                    continue
                if prefer or merged.get(field) in (None, "", [], {}):
                    merged[field] = value
    return out


def _merge_version_lists(primary, secondary):
    """Merge version records, letting rendered page metadata fix REST files."""
    out = []
    index = {}
    for versions, prefer in ((primary or [], False), (secondary or [], True)):
        for version in versions:
            if not isinstance(version, dict):
                continue
            key = str(version.get("id") or version.get("name") or "").casefold()
            if not key:
                continue
            if key not in index:
                merged = dict(version)
                if isinstance(merged.get("files"), list):
                    merged["files"] = [dict(x) for x in merged["files"] if isinstance(x, dict)]
                if isinstance(merged.get("images"), list):
                    merged["images"] = [dict(x) for x in merged["images"] if isinstance(x, dict)]
                index[key] = merged
                out.append(merged)
                continue
            merged = index[key]
            for field, value in version.items():
                if field in {"files", "images"}:
                    if isinstance(value, list):
                        merged[field] = _merge_child_records(merged.get(field) or [], value)
                elif value not in (None, "", [], {}) and (prefer or merged.get(field) in (None, "", [], {})):
                    merged[field] = value
    return out

def _version_summary(version):
    paid = version.get("paidAccess") if isinstance(version.get("paidAccess"), dict) else {}
    deadline = version.get("earlyAccessDeadline") or paid.get("endsAt") or ""
    can_download = version.get("canDownload")
    if can_download is None:
        # Public API versions without an explicit flag are normally downloadable
        # when their files are public; keep None so the UI does not invent a lock.
        can_download = None
    return {
        "id": version.get("id"),
        "name": str(version.get("name") or version.get("id") or "Version"),
        "description": _plain_text(version.get("description") or ""),
        "base_model": version.get("baseModel") or "",
        "base_model_type": version.get("baseModelType") or "",
        "trained_words": version.get("trainedWords") or [],
        "availability": version.get("availability") or "",
        "status": version.get("status") or "",
        "published_at": version.get("publishedAt") or "",
        "early_access_deadline": deadline,
        "paid_access": paid or None,
        "donation_goal": version.get("donationGoal"),
        "can_download": can_download,
        "require_auth": version.get("requireAuth"),
        "usage_control": version.get("usageControl") or "",
    }


def _source_activity(item):
    """Stable CivitAI listing activity marker used for unchanged checks.

    CivitAI's browse payload commonly places the useful activity timestamp on
    modelVersions rather than on the parent model. Use only fields that exist
    in the listing payload, but include those listing-version timestamps. The
    exact same helper is also used when persisting model.updated, so the next
    scan compares like-for-like instead of rebuilding unchanged models.
    """
    if not isinstance(item, dict):
        return ""

    values = [
        item.get("updatedAt"),
        item.get("publishedAt"),
        item.get("createdAt"),
        item.get("lastVersionAt"),
    ]
    for version in item.get("modelVersions") or []:
        if not isinstance(version, dict):
            continue
        values.extend([
            version.get("updatedAt"),
            version.get("publishedAt"),
            version.get("createdAt"),
        ])

    return _latest_date(*values)


def _listing_version_id(item):
    """Best stable latest-version id available in a CivitAI listing."""
    versions = [
        v for v in (item.get("modelVersions") or [])
        if isinstance(v, dict)
    ] if isinstance(item, dict) else []
    if not versions:
        return ""

    def version_time(version):
        parsed = [
            _parse_date(version.get("updatedAt")),
            _parse_date(version.get("publishedAt")),
            _parse_date(version.get("createdAt")),
        ]
        parsed = [value for value in parsed if value is not None]
        return max(parsed) if parsed else datetime.min

    latest = max(versions, key=version_time)
    return str(latest.get("id") or "")


def _tag_names(values):
    """Normalize CivitAI tag payloads into readable names."""
    if isinstance(values, str):
        values = [part.strip() for part in re.split(r"[,\n]", values) if part.strip()]
    elif isinstance(values, dict):
        values = [values] if any(k in values for k in ("name", "tagName", "label")) else list(values.values())
    if not isinstance(values, (list, tuple, set)):
        return []
    out = []
    seen = set()
    for value in values:
        if isinstance(value, dict):
            nested = value.get("tag") if isinstance(value.get("tag"), dict) else value
            value = nested.get("name") or nested.get("tagName") or nested.get("label")
        text = str(value or "").strip()
        if text and text.casefold() not in seen:
            seen.add(text.casefold())
            out.append(text)
    return out


def _build_model(item, enrich=False):
    model_id = item.get("id")
    force_page = bool(item.get("_force_page"))
    listing_versions = [
        dict(v) for v in (item.get("modelVersions") or [])
        if isinstance(v, dict)
    ]
    versions = list(listing_versions)

    # Discovery responses are optimized for browsing and can expose only the
    # selected/latest version.  Hydrate the exact model before building the
    # AbyssBeacon record so descriptions, every version, per-version galleries,
    # files and Early Access state survive creator/search scans too.
    detail_model = _fetch_model_detail(model_id) if enrich else {}
    if detail_model:
        item = {**item, **detail_model}
        detail_versions = [
            v for v in (detail_model.get("modelVersions") or [])
            if isinstance(v, dict)
        ]
        if detail_versions:
            # CivitAI's website discovery payload can carry paidAccess and
            # canDownload fields that the public REST detail endpoint omits.
            # Merge instead of replacing so access state survives hydration.
            versions = _merge_version_lists(detail_versions, listing_versions)

    # A one-version REST response is ambiguous: CivitAI has historically
    # returned only the selected/latest revision from this endpoint. The page
    # state is authoritative for the visible version picker, so verify it.
    has_multi_file_version = any(
        len([f for f in (v.get("files") or []) if isinstance(f, dict)]) > 1
        for v in versions
    )
    ambiguous_multifile_names = _has_ambiguous_multifile_names(versions)

    # The structured CivitAI detail response already contains exact filenames
    # for most multi-file versions. Only pay for the rendered model page when
    # those names are actually ambiguous (missing or duplicated across stable
    # file IDs). This preserves the MiniMax-style filename correction without
    # adding one rendered-page request to every multi-file model.
    filename_versions = (
        _fetch_filename_authority_versions(model_id)
        if ambiguous_multifile_names
        else []
    )
    if filename_versions:
        versions = _merge_version_lists(versions, filename_versions)

    needs_page = force_page or (
        enrich and (
            not _plain_text(item.get("description") or "")
            or not versions
            or (has_multi_file_version and not filename_versions)
        )
    )
    page_metadata = _fetch_model_page_metadata(model_id) if needs_page else {}
    page_versions = [v for v in (page_metadata.get("versions") or []) if isinstance(v, dict)]
    if page_versions:
        versions = _merge_version_lists(versions, page_versions)
    latest_version = versions[0] if versions else {}

    name = str(item.get("name") or f"CivitAI {model_id}")
    author = str((item.get("creator") or {}).get("username") or ((page_metadata.get("model") or {}).get("user") or {}).get("username") or "")
    tags_list = _tag_names(item.get("tags") or [])
    tags = ",".join(tags_list)
    description = _plain_text(item.get("description") or page_metadata.get("description") or latest_version.get("description") or item.get("meta") or "")
    base_model = str(latest_version.get("baseModel") or "")
    trained_words = " ".join(str(x) for x in (latest_version.get("trainedWords") or []))
    text = " ".join([name, description, tags, base_model, trained_words, str(item.get("type") or "")])

    files = []
    for version in versions:
        for file_data in version.get("files") or []:
            if isinstance(file_data, dict):
                files.append(_file_record(file_data, version, model_id))

    sensitive = bool(item.get("nsfw"))
    media = _media_records(versions, sensitive)
    preview = next((m["url"] for m in media if m.get("type") == "image"), "")
    has_video = any(m.get("type") == "video" for m in media)

    created = item.get("publishedAt") or item.get("createdAt") or latest_version.get("createdAt") or ""
    updated = _source_activity(item) or created

    stats = item.get("stats") or {}
    model = Model()
    model.name = name
    model.display_name = name
    model.author = author
    model.source = NAME
    model.model_key = str(model_id)
    model.url = f"https://civitai.com/models/{model_id}"
    model.image = preview
    model.description = description
    model.base_model = base_model
    # CivitAI supplies an explicit base model. Prefer that over fuzzy title/tag
    # matching so, for example, an H3 workflow merely mentioning Krea does not
    # become a Krea 2 model.
    model.architecture = processors.classify_architecture(base_model) if base_model else "Other"
    if model.architecture == "Other":
        watch_architecture = str(item.get("_watch_architecture") or "").strip()
        if watch_architecture:
            model.architecture = processors.classify_architecture_with_watch_fallback(
                watch_architecture,
                base_model,
                name,
                tags,
                item.get("description"),
            )
    model.model_type = _model_type(item.get("type"), text)
    model.tags = tags
    model.display_tags = ([base_model] if base_model else []) + tags_list[:8]
    model.created = created
    model.updated = updated
    model.downloads = int(stats.get("downloadCount") or item.get("downloadCount") or 0)
    model.likes = int(stats.get("favoriteCount") or item.get("favoriteCount") or 0)
    model.license = ""
    model.pipeline = ""
    model.files = files
    model.media = media
    model.preview_count = sum(1 for m in media if m.get("type") == "image")
    model.has_media = bool(media)
    model.has_video = has_video
    version_summaries = [_version_summary(v) for v in versions]
    # A model with at least one downloadable version remains downloadable at
    # card level. Per-version Early Access/restrictions live in card_data.
    has_downloadable_version = any(
        not v.get("paid_access")
        and not str(v.get("early_access_deadline") or "").strip()
        and v.get("can_download") is not False
        for v in version_summaries
    )
    model.gated = bool(version_summaries) and not has_downloadable_version
    model.sensitive = sensitive or any(
        str((img.get("nsfw") if isinstance(img, dict) else "") or "").lower() not in {"", "none", "false", "0"}
        for version in versions for img in (version.get("images") or [])
    )
    model.card_data = {
        "civitai_id": model_id,
        "type": item.get("type"),
        "nsfw": item.get("nsfw"),
        "mode": item.get("mode"),
        "version_id": latest_version.get("id"),
        "version_name": latest_version.get("name"),
        "base_model": base_model,
        "trained_words": latest_version.get("trainedWords") or [],
        "versions": version_summaries,
    }
    model.format = next((f.get("format") for f in files if f.get("format")), "")
    model.sha = str(latest_version.get("id") or "")
    return model


def scan_tag(tag_value, max_results=100, sort="NEWEST", tag_name=""):
    """Explicit regular-CivitAI tag discovery via the public REST API."""
    _apply_auth()
    tag = str(tag_value or tag_name or "").strip()
    if not tag:
        return []
    try:
        max_results = max(1, int(max_results))
    except (TypeError, ValueError):
        max_results = 100

    requested_sort = str(sort or "NEWEST").strip().upper()
    api_sort = {
        "NEWEST": "Newest",
        "HIGHEST_RATED": "Highest Rated",
        "MOST_DOWNLOADED": "Most Downloaded",
    }.get(requested_sort, "Newest")

    print(f"CivitAI Discovery: tag={tag!r}, sort={api_sort}, limit={max_results}")
    items = _fetch_model_pages("discovery tag", {"sort": api_sort, "tag": tag}, max_results)
    blocked = {str(x).casefold() for x in database.get_blocked_creator_set(NAME)}
    models = []
    seen = set()
    for item in items:
        if scan_control.should_stop():
            break
        if not isinstance(item, dict) or item.get("id") is None:
            continue
        key = str(item.get("id"))
        if key in seen:
            continue
        seen.add(key)
        author = str((item.get("creator") or {}).get("username") or "")
        if author and author.casefold() in blocked:
            continue
        model = _build_model(item)
        # Preserve the explicit Discovery tag even if the API payload omits it.
        existing = [part.strip() for part in str(getattr(model, "tags", "") or "").split(",") if part.strip()]
        # Older CivitAI models store tags space-separated; do not destroy those.
        if not existing and getattr(model, "tags", ""):
            existing = [str(model.tags).strip()]
        if tag.casefold() not in str(getattr(model, "tags", "") or "").casefold().split() and tag.casefold() not in {x.casefold() for x in existing}:
            model.tags = (str(getattr(model, "tags", "") or "").strip() + " " + tag).strip()
        display = list(getattr(model, "display_tags", []) or [])
        if tag.casefold() not in {str(x).casefold() for x in display}:
            display.append(tag)
        model.display_tags = display
        models.append(model)
    print(f"CivitAI Discovery built: {len(models)} model(s)")
    return models



def test_connection():
    api_token = get_source_token("civitai")
    search_key = get_civitai_search_key()

    if not api_token:
        return False, "CivitAI API key is not configured."
    if not search_key:
        return False, "CivitAI website search key is not configured."

    try:
        api_response = requests.get(
            "https://civitai.com/api/v1/me",
            headers={
                "Authorization": f"Bearer {api_token}",
                "Accept": "application/json",
                "User-Agent": "AbyssBeacon/1.0",
            },
            timeout=12,
        )
        if api_response.status_code != 200:
            return False, f"CivitAI rejected the API key (HTTP {api_response.status_code})."
    except Exception as exc:
        return False, f"CivitAI API test failed: {type(exc).__name__}."

    payload = {
        "queries": [{
            "q": "",
            "indexUid": "models_v9",
            "limit": 1,
            "offset": 0,
            "filter": ["(poi != true) AND (availability != Private) AND (nsfwLevel=1 OR nsfwLevel=2)"],
            "sort": ["createdAt:desc"],
        }]
    }
    try:
        search_response = requests.post(
            SEARCH_API,
            headers={
                "Authorization": f"Bearer {search_key}",
                "Content-Type": "application/json",
                "Accept": "*/*",
                "Origin": "https://civitai.com",
                "Referer": "https://civitai.com/",
                "X-Meilisearch-Client": "Meilisearch instant-meilisearch (v0.13.5) ; Meilisearch JavaScript (v0.34.0)",
            },
            json=payload,
            timeout=12,
        )
        if search_response.status_code != 200:
            return False, f"CivitAI API key works, but website search rejected the search key (HTTP {search_response.status_code})."
    except Exception as exc:
        return False, f"CivitAI API key works, but website search test failed: {type(exc).__name__}."

    return True, "Connected to CivitAI. API/download access and signed-in models_v9 discovery are ready."


def scan(term, scan_seen_models=None, scan_settings=None, creator=None):
    global _DETAIL_ENRICHMENT_DISABLED
    _DETAIL_ENRICHMENT_DISABLED = False
    _apply_auth()
    scan_settings = scan_settings or {}
    search_days = int(scan_settings.get("search_days", 7))
    max_results = max(1, int(scan_settings.get("max_results", 100)))
    sort_mode = scan_settings.get("sort", "newest")
    api_sort = {"newest": "Newest", "downloads": "Most Downloaded", "highest_rated": "Highest Rated"}.get(sort_mode, "Newest")

    if scan_seen_models is None:
        scan_seen_models = set()

    cutoff = datetime.utcnow() - timedelta(days=search_days)

    print(f"CivitAI {'creator' if creator else 'search'}: {creator or term}")
    print(f"CivitAI maximum results: {'all available' if creator else max_results} (automatic pagination)")

    if creator:
        # Creator scans are exact-owner scans. Walk the catalog until the source
        # ends (with a generous safety ceiling) just like our other sources.
        creator_limit = 10000
        items = _fetch_model_pages(
            "creator",
            {"sort": api_sort, "username": creator},
            creator_limit,
        )
        print(f"CivitAI creator results inspected: {len(items)}")
    else:
        # Architecture scans should use CivitAI's real Base Model field rather
        # than fuzzy name/tag matching. CivitAI's official MCP server uses the
        # same public /models endpoint with baseModels + cursor pagination.
        configured_architecture = str(scan_settings.get("_architecture") or "").strip()
        architecture_context = str(scan_settings.get("_architecture_context") or "").strip()
        configured_type = str(scan_settings.get("_model_type") or "").strip()

        if configured_architecture or architecture_context:
            resolved_architecture = configured_architecture or architecture_context

            items = _models_v9_discovery(
                resolved_architecture,
                max_results,
                api_sort=api_sort,
                model_type=configured_type,
            )

            if items is None:
                items = _structured_discovery(
                    resolved_architecture,
                    max_results,
                    api_sort=api_sort,
                    model_type=configured_type,
                    query=str(term or "").strip() if architecture_context else "",
                )
                print(f"CivitAI structured fallback results inspected: {len(items)}")
        else:
            # Free-text/custom search terms still use the public API's name +
            # tag routes, then merge and locally rank the unique candidates.
            path_limit = max_results
            name_items = _fetch_model_pages(
                "name",
                {"sort": api_sort, "query": term},
                path_limit,
            ) if term else []
            tag_items = _fetch_model_pages(
                "tag",
                {"sort": api_sort, "tag": term},
                path_limit,
            ) if term else []

            unique = {}
            for candidate in name_items + tag_items:
                model_id = candidate.get("id") if isinstance(candidate, dict) else None
                if model_id is None:
                    continue
                current = unique.get(str(model_id))
                if current is None or _item_activity_datetime(candidate) > _item_activity_datetime(current):
                    unique[str(model_id)] = candidate

            pool = list(unique.values())
            if sort_mode == "downloads":
                pool.sort(key=lambda item: (_item_downloads(item), _item_activity_datetime(item)), reverse=True)
            elif sort_mode == "highest_rated":
                pool.sort(key=lambda item: (_item_rating(item), _item_activity_datetime(item)), reverse=True)
            else:
                pool.sort(key=_item_activity_datetime, reverse=True)

            items = pool[:max_results]
            print(
                f"CivitAI text discovery: {len(name_items)} name matches + "
                f"{len(tag_items)} tag matches -> {len(pool)} unique; "
                f"using {len(items)}"
            )
            if DEBUG_SCANNERS:
                debug_print("CivitAI merged newest sample:", [
                    {
                        "id": item.get("id"),
                        "name": item.get("name"),
                        "activity": _item_activity_datetime(item).isoformat()
                        if _item_activity_datetime(item) != datetime.min else None,
                    }
                    for item in items[:20]
                ])

    processed = []
    duplicates = 0
    old_models = 0
    media_count = 0
    gated_count = 0

    v9_discovered = sum(
        1 for item in items
        if isinstance(item, dict) and item.get("_models_v9_hit")
    )
    v9_precheck_skipped = 0
    v9_detail_fetches = 0

    for item in items:
        if scan_control.should_stop():
            break
        model_id = item.get("id")
        if model_id is None:
            continue
        seen_key = (NAME, str(model_id))
        if seen_key in scan_seen_models:
            continue
        scan_seen_models.add(seen_key)

        author = str((item.get("creator") or {}).get("username") or "")
        blocked = {str(x).casefold() for x in (scan_settings.get("_blocked_creators") or [])}
        if author and author.casefold() in blocked:
            continue

        # IMPORTANT: reject out-of-window browse results from the cheap listing
        # payload BEFORE any per-model hydration/detail request. This restores
        # the fast path we previously had: models_v9 is discovery-only and old
        # records should never cost an API detail request just to be discarded.
        if not creator:
            listing_activity = _item_activity_datetime(item)
            if listing_activity != datetime.min and listing_activity < cutoff:
                old_models += 1
                if item.get("_models_v9_hit"):
                    v9_precheck_skipped += 1
                continue

        # Normal browse scans should stay cheap. If the listing already says an
        # existing card has the same source activity, do not spend a detail
        # request rebuilding it. Creator scans may request rich hydration, but
        # only for records that actually need rebuilding.
        # Merged cards may keep CivitAI only in model_sources while another
        # provider (often CivitAI Red) owns the canonical models row. Read the
        # preserved CivitAI snapshot first so unchanged detection still works
        # for merged cards.
        existing_source = database.get_model_source_snapshot(NAME, str(model_id))
        source_updated = _source_activity(item)
        source_sha = _listing_version_id(item)

        if existing_source:
            db_updated = str(existing_source.get("updated") or "")
            db_sha = str(existing_source.get("sha") or "")
            if (
                source_sha
                and db_sha == source_sha
                and source_updated
                and db_updated == source_updated
            ) or (
                source_updated
                and db_updated == source_updated
            ):
                duplicates += 1
                if item.get("_models_v9_hit"):
                    v9_precheck_skipped += 1
                continue
        else:
            # Compatibility fallback for older/unmerged rows that predate
            # model_sources membership snapshots.
            existing = database.get_model(str(model_id), NAME)
            if existing:
                db_updated = str(existing["updated"] or "")
                db_sha = str(existing["sha"] or "")
                if (
                    source_sha
                    and db_sha == source_sha
                    and source_updated
                    and db_updated == source_updated
                ) or (
                    source_updated
                    and db_updated == source_updated
                ):
                    duplicates += 1
                    if item.get("_models_v9_hit"):
                        v9_precheck_skipped += 1
                    continue

        # models_v9 is now discovery-only. Hydrate only records that survived
        # the cheap DB/source-activity precheck so new/changed models still get
        # the rich REST payload needed for files, media, access and downloads.
        if item.get("_models_v9_hit"):
            detail = _fetch_model_detail(model_id)
            if detail:
                v9_detail_fetches += 1
                # Preserve website activity fields/marker while allowing the
                # rich API detail to supply versions/files/media.
                item = {**item, **detail, "_models_v9_hit": True}

        model = _build_model(item, enrich=bool(creator))

        # Creator scans are explicit requests for the creator's catalog, so
        # Search Days never trims them. Discovery scans use newest source
        # activity (created/updated), matching AbyssBeacon cleanup semantics.
        if not creator:
            active_date = _parse_date(model.updated) or _parse_date(model.created)
            if active_date and active_date < cutoff:
                old_models += 1
                continue

        existing = database.get_model(model.model_key, NAME)
        if existing:
            db_updated = str(existing["updated"] or "")
            db_sha = str(existing["sha"] or "")
            if (model.sha and db_sha == model.sha and db_updated == model.updated) or (model.updated and db_updated == model.updated):
                duplicates += 1
                continue

        processed.append(model)
        media_count += len(model.media)
        if model.gated:
            gated_count += 1

    print("\n========================================")
    print("CivitAI Scan Complete")
    print("========================================")
    print(f"Processed models : {len(processed)}")
    print(f"Old models       : {old_models}")
    print(f"Duplicates       : {duplicates}")
    if v9_discovered:
        print(f"v9 discovered    : {v9_discovered}")
        print(f"v9 precheck skip : {v9_precheck_skipped}")
        print(f"v9 detail fetches: {v9_detail_fetches}")
    print(f"Media files      : {media_count}")
    print(f"Mature models    : {sum(1 for m in processed if m.sensitive)}")
    print("========================================")
    return processed

