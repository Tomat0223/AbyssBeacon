from scan_logging import verbose_print as print
NAME = "civitaired"
DISPLAY = "CivitAI Red"
ENABLED = True

import html
import json
import os
import re
import time
from datetime import datetime, timedelta

import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from scanners.http_retry import get_with_backoff, get_cached_text_with_backoff

import database
import scan_control
from scanners.common.model import Model
from scanners.common import processors
from secrets_manager import get_civitaired_credentials
from utils.loader import load_model_types

BASE_URL = "https://civitai.red"
LIST_API = BASE_URL + "/api/trpc/model.getAll"
VERSION_API = BASE_URL + "/api/v1/model-versions/{version_id}"
CIVITAI_MODEL_DETAIL_API = "https://civitai.com/api/v1/models/{model_id}"
IMAGE_BASE = "https://image.civitai.com/xG1nkqKTMzGDvpLrqFT7WA"
DEBUG_SCANNERS = False

session = requests.Session()
session.headers.update({
    # Red's authenticated tRPC endpoint expects the same client context as
    # the website.  These are ordinary request headers, not credentials.
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://civitai.red/models",
    "x-client": "web",
    "x-client-version": "5.1.11",
})


def debug_print(*args, **kwargs):
    if DEBUG_SCANNERS:
        print(*args, **kwargs)


def _plain_text(value):
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
    pairs = [(value, _parse_date(value)) for value in values if value]
    pairs = [(raw, parsed) for raw, parsed in pairs if parsed is not None]
    return max(pairs, key=lambda pair: pair[1])[0] if pairs else ""


def _clean_cookie_value(value, cookie_name):
    """Accept either a raw cookie value or an accidentally pasted name=value pair."""
    value = str(value or "").strip().strip('"').strip("'")
    prefix = cookie_name + "="
    if value.startswith(prefix):
        value = value[len(prefix):]
        value = value.split(";", 1)[0].strip()
    return value


def _credentials():
    creds = get_civitaired_credentials()
    token = _clean_cookie_value(creds.get("session_token"), "__Secure-civ-token")
    device = _clean_cookie_value(creds.get("device_token"), "__Secure-civ-device")
    cookies = {}
    if token:
        cookies["__Secure-civ-token"] = token
    if device:
        cookies["__Secure-civ-device"] = device
    return cookies


def _cookie_header(cookies):
    # Build the exact browser-style Cookie header instead of relying on
    # requests' temporary cookie jar.  The values never leave the local PC
    # except in the request to civitai.red itself.
    parts = []
    for name in ("__Secure-civ-token", "__Secure-civ-device"):
        value = cookies.get(name)
        if value:
            parts.append(f"{name}={value}")
    return "; ".join(parts)


def _devalue_decode(serialized):
    """Decode the flattened devalue payload returned by CivitAI Red tRPC."""
    flat = json.loads(serialized) if isinstance(serialized, str) else serialized
    if not isinstance(flat, list):
        return flat

    cache = {}
    active = set()
    sentinels = {
        -1: None,  # undefined
        -2: None,  # hole
        -3: None,  # NaN isn't useful to ModelRadar
        -4: None,  # +Infinity
        -5: None,  # -Infinity
        -6: 0,
    }

    def deref(ref):
        if isinstance(ref, bool):
            return ref
        if not isinstance(ref, int):
            return ref
        if ref < 0:
            return sentinels.get(ref)
        if ref >= len(flat):
            return ref
        if ref in cache:
            return cache[ref]
        if ref in active:
            return None

        active.add(ref)
        value = flat[ref]
        if isinstance(value, dict):
            out = {}
            cache[ref] = out
            for key, item in value.items():
                out[key] = deref(item)
        elif isinstance(value, list):
            if value and value[0] == "Date":
                out = value[1] if len(value) > 1 else None
                cache[ref] = out
            elif value and value[0] == "Set":
                out = [deref(item) for item in value[1:]]
                cache[ref] = out
            elif value and value[0] == "Map":
                out = {}
                cache[ref] = out
                pairs = value[1:]
                for index in range(0, len(pairs), 2):
                    key = deref(pairs[index])
                    val = deref(pairs[index + 1]) if index + 1 < len(pairs) else None
                    out[key] = val
            else:
                out = []
                cache[ref] = out
                out.extend(deref(item) for item in value)
        else:
            out = value
            cache[ref] = out

        active.remove(ref)
        return out

    return deref(0)


def _decode_response(response):
    payload = response.json()
    result = payload.get("result") or {}
    data = result.get("data")
    if isinstance(data, str):
        return _devalue_decode(data)
    if isinstance(data, dict) and "json" in data:
        return data.get("json")
    return data if isinstance(data, dict) else {}


def _period_for_days(days):
    if days <= 1:
        return "Day"
    if days <= 7:
        return "Week"
    if days <= 31:
        return "Month"
    if days <= 365:
        return "Year"
    return "AllTime"


def _sort_label(sort_mode):
    return {
        "newest": "Newest",
        "downloads": "Most Downloaded",
        "highest_rated": "Highest Rated",
    }.get(sort_mode, "Newest")


def _red_base_model(value):
    """Translate ModelRadar architecture labels to Red's exact base-model values."""
    raw = str(value or "").strip()
    normalized = raw.casefold().replace("_", " ").replace("-", " ")
    normalized = re.sub(r"\s+", " ", normalized).strip()
    aliases = {
        "minimax h3": "MiniMax H3",
        "h3": "MiniMax H3",
        "krea": "Krea 2",
        "krea 2": "Krea 2",
    }
    return aliases.get(normalized, raw)


def _request_page(base_model="", model_type="", query="", sort_mode="newest", search_days=7, cursor="", tagname="", username=""):
    cookies = _credentials()
    if not cookies.get("__Secure-civ-token"):
        raise PermissionError("CivitAI Red connection is not configured")

    values = {
        "period": _period_for_days(search_days),
        "periodMode": "published",
        "sort": _sort_label(sort_mode),
        "followed": False,
        "newCreators": False,
        "hidden": False,
        "pending": False,
        # 31 is the browsing level used by Red's own all-level model listing.
        "browsingLevel": 31,
        "disablePoi": True,
        "disableMinor": True,
        "direction": "forward",
        "authed": True,
    }
    if base_model:
        values["baseModels"] = [_red_base_model(base_model)]
    if model_type:
        values["types"] = [model_type]
    if query:
        values["query"] = query
    if tagname:
        values["tagname"] = str(tagname).strip().casefold()
    if username:
        # Red's creator page uses an exact username filter on the same model.getAll endpoint.
        values["username"] = str(username).strip()
    if cursor:
        values["cursor"] = cursor

    params = {"input": json.dumps({"json": values}, separators=(",", ":"))}
    request_headers = {
        "Cookie": _cookie_header(cookies),
        "x-client-date": str(int(time.time() * 1000)),
    }
    if tagname:
        request_headers["Referer"] = f"{BASE_URL}/tag/{str(tagname).strip().casefold()}"
    elif username:
        request_headers["Referer"] = f"{BASE_URL}/user/{requests.utils.quote(str(username).strip())}/models?sort=Newest&periodMode=published&period=AllTime"
    debug_print("CivitAI Red request:", LIST_API, values)
    response = get_with_backoff(
        session, LIST_API, provider="CivitAI Red",
        label=(f"{base_model or query or tagname or 'models'}" + (f" cursor {cursor}" if cursor else "")),
        pace_key="CivitAI Red", min_interval=0.75,
        params=params, headers=request_headers, timeout=45
    )
    if response.status_code == 429:
        raise RuntimeError("HTTP 429 after 3 retries")
    if response.status_code in (401, 403):
        # Do not include cookie/token material in diagnostics.  A short body
        # excerpt is useful because Red may return a tRPC/Cloudflare reason.
        detail = ""
        try:
            detail = re.sub(r"\s+", " ", response.text or "").strip()[:240]
        except Exception:
            detail = ""
        suffix = f" Red said: {detail}" if detail else ""
        raise PermissionError(
            f"CivitAI Red rejected the browser session (HTTP {response.status_code})."
            f"{suffix}"
        )
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        detail = ""
        try:
            detail = re.sub(r"\s+", " ", response.text or "").strip()[:240]
        except Exception:
            detail = ""
        if detail:
            raise RuntimeError(f"HTTP {response.status_code}: {detail}") from exc
        raise
    return _decode_response(response)


def _fetch_pages(base_model="", model_type="", query="", sort_mode="newest", search_days=7, max_items=100, tagname="", username=""):
    collected = []
    cursor = ""
    page = 0
    seen_cursors = set()

    while len(collected) < max_items:
        if scan_control.should_stop():
            break
        page += 1
        payload = _request_page(
            base_model=base_model,
            model_type=model_type,
            query=query,
            sort_mode=sort_mode,
            search_days=search_days,
            cursor=cursor,
            tagname=tagname,
            username=username,
        )
        items = payload.get("items") or [] if isinstance(payload, dict) else []
        if not isinstance(items, list):
            items = []
        remaining = max_items - len(collected)
        collected.extend(items[:remaining])
        print(f"CivitAI Red batch {page} returned: {len(items)}")

        raw_next_cursor = payload.get("nextCursor") if isinstance(payload, dict) else None
        next_cursor = "" if raw_next_cursor in (None, "", -1, "-1", False) else str(raw_next_cursor)
        debug_print("CivitAI Red next cursor:", next_cursor)
        if not items or len(collected) >= max_items or not next_cursor:
            break
        if next_cursor in seen_cursors:
            print("CivitAI Red pagination stopped: repeated cursor")
            break
        seen_cursors.add(next_cursor)
        cursor = next_cursor

    return collected


def _configured_model_type_label(label):
    wanted = str(label or "").replace(" ", "").casefold()
    if not wanted:
        return ""
    for configured in load_model_types().keys():
        if str(configured).replace(" ", "").casefold() == wanted:
            return configured
    return str(label or "")


def _model_type(api_type, text=""):
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
        "other": "Other",
    }
    normalized = str(api_type or "").strip().lower()
    direct = mapping.get(normalized) or mapping.get(normalized.replace(" ", ""))
    if direct:
        return _configured_model_type_label(direct)
    return processors.classify_model_type(text)


def _media_url(media):
    raw = str(media.get("url") or "").strip()
    if not raw:
        return ""
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    name = str(media.get("name") or "").strip()
    if not name:
        media_type = str(media.get("type") or "image").lower()
        ext = "mp4" if media_type == "video" else "jpeg"
        name = f"{media.get('id') or raw}.{ext}"
    # CivitAI stores the media UUID separately from the filename. Its frontend
    # builds source URLs as <cdn>/<uuid>/<filename>; transformations are optional.
    return f"{IMAGE_BASE}/{raw}/{name}"


def _optimized_red_video_url(url, width=450):
    """Return the canonical CivitAI transcoded MP4 URL used by Red's frontend.

    Red media URLs can already contain an image transform segment. Appending a
    second transform creates an invalid URL. Rebuild from the stable CDN token +
    media UUID instead, matching the site's own:
      /<uuid>/transcode=true,width=...,optimized=true/<filename>.mp4
    """
    url = str(url or "").strip()
    if not url or "image.civitai.com/" not in url:
        return url

    match = re.search(
        r"(https://image\.civitai\.com/[^/]+/([0-9a-fA-F-]{20,}))/.*?/([^/?#]+)$",
        url,
    )
    if not match:
        # Also handle raw URLs with no transform directory.
        match = re.search(
            r"(https://image\.civitai\.com/[^/]+/([0-9a-fA-F-]{20,}))/([^/?#]+)$",
            url,
        )
        if not match:
            return url
        base = match.group(1)
        filename = match.group(3)
    else:
        base = match.group(1)
        filename = match.group(3)

    stem = re.sub(r"\.(?:webm|mp4|mov|mkv)$", "", filename, flags=re.IGNORECASE)
    return f"{base}/transcode=true,width={int(width)},optimized=true/{stem}.mp4"


def _red_video_poster_url(url, width=450):
    """Build the still poster URL used by CivitAI's rendered video cards."""
    url = str(url or "").strip()
    match = re.search(r"(https://image\.civitai\.com/[^/]+/([0-9a-fA-F-]{20,}))/", url)
    if not match:
        return ""
    base = match.group(1)
    media_id = match.group(2)
    return (
        f"{base}/anim=false,transcode=true,width={int(width)},"
        f"original=false,optimized=true/{media_id}.jpeg"
    )


def _listing_media(item, version):
    media = []
    version_name = str(version.get("name") or version.get("id") or "version")
    version_id = version.get("id")
    for position, image in enumerate(item.get("images") or []):
        if not isinstance(image, dict):
            continue
        raw_url = _media_url(image)
        if not raw_url:
            continue
        media_type = "video" if str(image.get("type") or "image").lower() == "video" else "image"
        url = _optimized_red_video_url(raw_url) if media_type == "video" else raw_url
        filename = str(image.get("name") or f"preview-{position + 1}")
        meta = dict(image.get("meta") or image.get("metadata") or {})
        meta.update({
            "filename": filename,
            "path": f"{version_name}/{filename}",
            "civitai_red_model_version": version_name,
            "civitai_red_model_version_id": version_id,
        })
        media_id = image.get("id") or image.get("imageId") or image.get("image_id")
        if media_id:
            meta["civitai_red_media_id"] = media_id
        if image.get("width"):
            meta.setdefault("width", image.get("width"))
        if image.get("height"):
            meta.setdefault("height", image.get("height"))
        if image.get("nsfwLevel") is not None:
            meta.setdefault("maturity_level", image.get("nsfwLevel"))
        media.append({
            "type": media_type,
            "url": url,
            "fallback_url": raw_url if media_type == "video" and raw_url != url else "",
            "thumbnail": "" if media_type == "image" else (_red_video_poster_url(url) or url),
            "filename": filename,
            "path": f"{version_name}/{filename}",
            "metadata": meta,
            "position": position,
        })
    return media



def _red_media_url_token(value):
    text = str(value or "")
    match = re.search(
        r"image\.civitai\.com/[^/]+/([0-9a-fA-F-]{20,})/",
        text,
        flags=re.IGNORECASE,
    )
    return match.group(1).lower() if match else ""


def _fetch_version_image_list(version_id, limit=100):
    """Fetch Red's public version-scoped image list.

    This endpoint is useful for older version tabs because the model page often
    embeds only the current tab's complete gallery. It also exposes the numeric
    media id required by image.getGenerationData.
    """
    if not version_id:
        return []
    try:
        response = get_with_backoff(
            session,
            BASE_URL + "/api/v1/images",
            provider="CivitAI Red",
            label=f"version image list {version_id}",
            pace_key="CivitAI Red",
            min_interval=0.75,
            params={"modelVersionId": version_id, "limit": int(limit)},
            cookies=_credentials(),
            timeout=25,
        )
        if response.status_code != 200:
            return []
        payload = response.json()
        items = payload.get("items") if isinstance(payload, dict) else []
        return [item for item in (items or []) if isinstance(item, dict)]
    except Exception:
        return []


def fetch_version_gallery(version_id):
    """Resolve the complete known gallery for one Red version on demand.

    Detail media wins because it may already contain rich generation metadata.
    The public image list fills numeric media ids and any previews omitted from
    the version detail/page state.
    """
    detail = _fetch_version_detail(version_id)
    version = dict(detail) if isinstance(detail, dict) else {}
    version.setdefault("id", version_id)

    detail_media = _detail_media(detail, version) if isinstance(detail, dict) else []

    listed_images = _fetch_version_image_list(version_id)
    listed_media = _listing_media({"images": listed_images}, version) if listed_images else []

    merged = []
    by_token = {}
    by_url = {}

    def add(entry, prefer_existing=False):
        if not isinstance(entry, dict):
            return
        token = _red_media_url_token(entry.get("url") or entry.get("fallback_url"))
        raw_url = str(entry.get("url") or "")
        existing = by_token.get(token) if token else by_url.get(raw_url)
        if existing is None:
            clone = dict(entry)
            clone["metadata"] = dict(entry.get("metadata") or {})
            clone["position"] = len(merged)
            merged.append(clone)
            if token:
                by_token[token] = clone
            if raw_url:
                by_url[raw_url] = clone
            return

        incoming_meta = dict(entry.get("metadata") or {})
        current_meta = dict(existing.get("metadata") or {})

        # Preserve richer detail metadata while filling numeric ids/fields from
        # the public image list.
        for key, value in incoming_meta.items():
            if key not in current_meta or current_meta.get(key) in (None, "", [], {}):
                current_meta[key] = value
        existing["metadata"] = current_meta

        for key in ("thumbnail", "fallback_url", "filename", "path", "type"):
            if not existing.get(key) and entry.get(key):
                existing[key] = entry.get(key)

    # Rich version detail first.
    for entry in detail_media:
        add(entry)

    # Fill missing IDs / omitted images from Red's version image list.
    for entry in listed_media:
        add(entry)

    for i, entry in enumerate(merged):
        entry["position"] = i
        meta = entry.setdefault("metadata", {})
        meta["civitai_red_model_version_id"] = version_id
        if version.get("name"):
            meta["civitai_red_model_version"] = version.get("name")
        meta["_version_gallery_hydrated"] = True

    return {
        "version": version,
        "media": merged,
        "detail_count": len(detail_media),
        "listed_count": len(listed_media),
    }



def _fetch_version_detail(version_id):
    if not version_id:
        return {}
    try:
        response = get_with_backoff(
            session,
            VERSION_API.format(version_id=version_id),
            provider="CivitAI Red",
            label=f"version detail {version_id}",
            pace_key="CivitAI Red", min_interval=0.75,
            cookies=_credentials(),
            timeout=25,
        )
        if response.status_code == 200:
            data = response.json()
            return data if isinstance(data, dict) else {}
        debug_print("CivitAI Red version detail status:", response.status_code)
    except Exception as exc:
        debug_print("CivitAI Red version detail failed:", exc)
    return {}




def _fetch_mirror_model_detail(model_id):
    """Use the shared CivitAI model id to recover the complete mirror tree.

    Red and CivitAI use the same model/version identifiers. Red's listing API
    exposes only one selected version, while the public CivitAI model endpoint
    normally returns the parent description plus all modelVersions. If a model
    is not available there, Red's own page/version fallbacks remain in place.
    """
    if not model_id:
        return {}
    try:
        response = get_with_backoff(
            session,
            CIVITAI_MODEL_DETAIL_API.format(model_id=model_id),
            provider="CivitAI Red",
            label=f"mirror model detail {model_id}",
            pace_key="CivitAI.com", min_interval=1.25,
            timeout=30,
        )
        if response.status_code == 200:
            payload = response.json()
            return payload if isinstance(payload, dict) else {}
        debug_print("CivitAI Red mirror detail status:", response.status_code)
    except Exception as exc:
        debug_print("CivitAI Red mirror detail failed:", exc)
    return {}

def _find_model_record(value, model_id):
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



def _merge_child_records(primary, secondary):
    """Merge files/images by stable id, preferring Red page metadata when present.

    CivitAI's shared REST model tree can occasionally expose a normalized/main
    filename for more than one artifact in the same version. Red's rendered
    version picker carries the per-artifact filename the user actually sees.
    Keep the REST record as a fallback, but let the Red page overwrite non-empty
    fields for the same file/image id.
    """
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
    """Merge version records while preserving Red's per-artifact metadata."""
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
                elif value not in (None, "", [], {}) and (
                    prefer or merged.get(field) in (None, "", [], {})
                ):
                    merged[field] = value

    return out

def _has_ambiguous_multifile_names(versions):
    """True when distinct files in a version do not have trustworthy names.

    Stable file IDs are the identity. A rendered filename-authority lookup is
    only needed when two different IDs share one name, or a name is missing.
    """
    for version in versions or []:
        if not isinstance(version, dict):
            continue
        files = [f for f in (version.get("files") or []) if isinstance(f, dict)]
        if len(files) <= 1:
            continue

        seen = {}
        for file_record in files:
            file_id = str(
                file_record.get("id")
                or file_record.get("fileId")
                or file_record.get("file_id")
                or ""
            ).strip()
            name = str(
                file_record.get("name")
                or (file_record.get("metadata") or {}).get("filename")
                or ""
            ).strip()

            if not name:
                return True

            key = name.casefold()
            previous_id = seen.get(key)
            if previous_id is not None and previous_id != file_id:
                return True
            seen[key] = file_id
    return False


def _version_summary(version):
    paid = version.get("paidAccess") if isinstance(version.get("paidAccess"), dict) else {}
    deadline = version.get("earlyAccessDeadline") or paid.get("endsAt") or ""
    paid_terms = paid.get("terms") if isinstance(paid.get("terms"), dict) else {}
    paid_download = paid_terms.get("download") if isinstance(paid_terms.get("download"), dict) else {}
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
        "paid_price": paid_download.get("price"),
        "donation_goal": version.get("donationGoal"),
        "can_download": version.get("canDownload"),
        "require_auth": version.get("requireAuth"),
        "usage_control": version.get("usageControl") or "",
    }


def _hydrated_page_tag_names(page):
    """Extract model tag names from Red's current hydrated HTML state.

    Red's newer frontend no longer guarantees a legacy __NEXT_DATA__ block.
    The page still embeds the authoritative ``tagsOnModels`` array directly in
    dehydrated query state, with records shaped like:

        {"tag": {"id": 153497, "name": "illustrious", ...}}

    Decode those arrays directly from the HTML rather than depending on a
    particular Next.js script id.
    """
    text = html.unescape(str(page or ""))
    names = []
    seen = set()
    decoder = json.JSONDecoder()
    marker = '"tagsOnModels"'
    start = 0

    def add(name):
        name = str(name or "").strip()
        if not name or name.isdigit():
            return
        key = name.casefold()
        if key not in seen:
            seen.add(key)
            names.append(name)

    while True:
        pos = text.find(marker, start)
        if pos < 0:
            break
        colon = text.find(':', pos + len(marker))
        if colon < 0:
            break
        value_start = colon + 1
        while value_start < len(text) and text[value_start].isspace():
            value_start += 1
        if value_start >= len(text) or text[value_start] != '[':
            start = pos + len(marker)
            continue

        try:
            records, consumed = decoder.raw_decode(text[value_start:])
        except Exception:
            start = pos + len(marker)
            continue

        for entry in records if isinstance(records, list) else []:
            if not isinstance(entry, dict):
                continue
            tag = entry.get('tag') if isinstance(entry.get('tag'), dict) else entry
            if isinstance(tag, dict):
                add(tag.get('name') or tag.get('tagName') or tag.get('label'))

        start = value_start + max(consumed, 1)

    return names


def _next_data_versions(page, model_id=None):
    """Extract modelVersions directly from Red's __NEXT_DATA__ JSON."""
    text = str(page or "")
    match = re.search(
        r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return []

    try:
        payload = json.loads(html.unescape(match.group(1)))
    except Exception:
        return []

    collected = []

    def walk(value):
        if isinstance(value, dict):
            versions = value.get("modelVersions")
            if isinstance(versions, list):
                if model_id is None or not value.get("id") or str(value.get("id")) == str(model_id):
                    collected.extend(v for v in versions if isinstance(v, dict))
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(payload)
    return _merge_version_lists([], collected)


def _hydrated_page_versions(page, model_id=None):
    """Extract and merge modelVersions from Red's hydrated page state.

    Red may serialize one copy with richer files/media and another with the
    account-specific entitlement fields. Merge every occurrence by version id
    so paidAccess/canDownload/earlyAccessDeadline cannot be lost.
    """
    text = html.unescape(str(page or ""))
    decoder = json.JSONDecoder()
    marker = '"modelVersions"'
    start = 0
    merged_versions = []

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
            if cleaned:
                merged_versions = _merge_version_lists(merged_versions, cleaned)

        start = value_start + max(consumed, 1)

    return merged_versions

def _fetch_civitai_page_versions(model_id):
    """Fetch the regular CivitAI rendered page as filename/type authority.

    Red and regular CivitAI share stable model/version/file IDs, but Red's API
    payload can repeat the primary filename for optional artifacts. The regular
    rendered CivitAI page carries the creator-facing per-file names in its
    hydrated ``modelVersions`` state.

    This request used to set ``max_retries=0``. A single 429 therefore silently
    disabled the filename correction and left Red's duplicated names in place.
    Give this *targeted multi-file metadata lookup* a few retries without
    changing the broader scanner rate-limit policy.
    """
    if not model_id:
        return []
    try:
        status_code, page, _cache_hit = get_cached_text_with_backoff(
            session,
            f"https://civitai.com/models/{model_id}",
            cache_key=("civitai-model-page", str(model_id)),
            provider="CivitAI Red",
            label=f"shared filename metadata {model_id}",
            pace_key="CivitAI.com", min_interval=1.25,
            timeout=25,
            max_retries=3,
        )
        if status_code != 200:
            return []
        versions = _hydrated_page_versions(page, model_id)
        return versions
    except Exception as exc:
        debug_print("CivitAI Red shared page metadata failed:", exc)
        return []



def _visible_red_file_names(page):
    """Recover exact per-file names from Red's rendered download cards.

    Red's model/version APIs can normalize an optional artifact to the primary
    model filename even though the rendered page shows the correct filename.
    The download buttons carry stable fileId values, so pair each button with
    the closest filename-looking text immediately before it.
    """
    text = html.unescape(str(page or ""))
    if not text:
        return {}

    filename_re = re.compile(
        r'(?:title=["\']([^"\']+\.(?:safetensors|ckpt|pt|pth|bin|gguf|zip|json))["\']'
        r'|>([^<>]+\.(?:safetensors|ckpt|pt|pth|bin|gguf|zip|json))<)',
        flags=re.IGNORECASE,
    )
    download_re = re.compile(
        r'/api/download/models/(\d+)\?fileId=(\d+)',
        flags=re.IGNORECASE,
    )

    out = {}
    for match in download_re.finditer(text):
        file_id = str(match.group(2))
        # The filename is rendered before its download button. Keep the window
        # deliberately local so another artifact in the same version cannot win.
        start = max(0, match.start() - 14000)
        chunk = text[start:match.start()]
        candidates = []
        for name_match in filename_re.finditer(chunk):
            raw = name_match.group(1) or name_match.group(2) or ""
            name = re.sub(r"\s+", " ", raw).strip()
            if name:
                candidates.append((name_match.end(), name))
        if candidates:
            out[file_id] = candidates[-1][1]

    return out

def _fetch_model_page_metadata(model_id, version_id=None):
    """Recover model-level metadata that Red does not expose on version detail.

    CivitAI Red embeds a SoftwareApplication JSON-LD record in the rendered
    model page.  In particular, this carries the parent model description even
    when the selected model-version description is null.  Only call this as a
    fallback so normal Red scans do not gain an extra request for every model.
    """
    if not model_id:
        return {}
    try:
        page_url = f"{BASE_URL}/models/{model_id}"

        response = get_with_backoff(
            session,
            page_url,
            provider="CivitAI Red",
            label=f"model page {model_id}",
            pace_key="CivitAI Red", min_interval=0.75,
            cookies=_credentials(),
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:154.0) "
                    "Gecko/20100101 Firefox/154.0"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": page_url,
            },
            timeout=25,
        )
        if response.status_code != 200:
            debug_print("CivitAI Red model page status:", response.status_code)
            return {}

        page = response.text or ""
        out = {}

        # Red's rendered file cards expose the exact creator filenames beside
        # stable fileId download links. Keep these as the final authority for
        # names; the version APIs can repeat the primary filename for optional
        # artifacts even when their ids, sizes, and types are otherwise correct.
        visible_names = _visible_red_file_names(page)
        if visible_names:
            out["file_name_overrides"] = visible_names

        # Red's current app-router pages embed the authoritative model tags in
        # hydrated query state rather than necessarily exposing __NEXT_DATA__.
        # Extract them first so numeric listing tag IDs can be resolved.
        hydrated_tags = _hydrated_page_tag_names(page)
        if hydrated_tags:
            out["tags"] = hydrated_tags

        # Current Red app-router pages embed the full modelVersions array in
        # hydrated query state even when __NEXT_DATA__ is absent. This is the
        # authoritative source for optional-file names such as Enhancement
        # LoRAs that may be normalized incorrectly by the shared REST payload.
        hydrated_versions = _merge_version_lists(
            _next_data_versions(page, model_id),
            _hydrated_page_versions(page, model_id),
        )

        # Keep Red page versions separate here. Regular CivitAI filename
        # authority is fetched independently in _build_model so a Red 403/429
        # cannot prevent filename recovery.
        if hydrated_versions:
            out["versions"] = hydrated_versions

        for match in re.finditer(
            r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            page,
            flags=re.IGNORECASE | re.DOTALL,
        ):
            raw = html.unescape(match.group(1)).strip()
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except Exception:
                continue
            records = payload if isinstance(payload, list) else [payload]
            for record in records:
                if not isinstance(record, dict):
                    continue
                record_type = str(record.get("@type") or "").casefold()
                if record_type == "softwareapplication" or record.get("description"):
                    out.update({
                        "description": record.get("description") or "",
                        "name": record.get("name") or "",
                        "date_published": record.get("datePublished") or "",
                        "author": (record.get("author") or {}).get("name")
                        if isinstance(record.get("author"), dict) else "",
                    })
                    break
            if out.get("description"):
                break

        next_match = re.search(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', page, flags=re.IGNORECASE | re.DOTALL)
        if next_match:
            try:
                next_data = json.loads(html.unescape(next_match.group(1)))
                record = _find_model_record(next_data, model_id)
                if isinstance(record, dict):
                    out["model"] = record
                    # Prefer app-router hydrated versions recovered above.
                    # Fall back to legacy __NEXT_DATA__ only when necessary.
                    if not out.get("versions"):
                        out["versions"] = record.get("modelVersions") or []
                    if not out.get("description"):
                        out["description"] = record.get("description") or ""
            except Exception as exc:
                debug_print("CivitAI Red Next data parse failed:", exc)
        return out
    except Exception as exc:
        debug_print("CivitAI Red model page metadata failed:", exc)
    return {}


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


def _detail_files(detail, version):
    files = []
    version_id = version.get("id")
    version_name = str(version.get("name") or version_id or "version")
    for file_data in detail.get("files") or []:
        if not isinstance(file_data, dict):
            continue
        metadata = file_data.get("metadata") or {}
        name = str(file_data.get("name") or f"civitai-red-{file_data.get('id', '')}")
        file_id = file_data.get("id") or file_data.get("fileId") or file_data.get("file_id")
        url = str(file_data.get("downloadUrl") or file_data.get("url") or "")
        if version_id and file_id:
            # Red requires the individual fileId when a version exposes more
            # than one artifact. This matches Red's own browser download URL.
            url = f"{BASE_URL}/api/download/models/{version_id}?fileId={file_id}"
        elif url:
            url = url.replace("https://civitai.com/", "https://civitai.red/")
        raw_size_kb = file_data.get("sizeKB") or file_data.get("sizeKb") or ""
        try:
            size_bytes = int(float(raw_size_kb) * 1024) if raw_size_kb not in ("", None) else 0
        except (TypeError, ValueError):
            size_bytes = 0
        files.append({
            "name": name,
            "path": f"{version_name}/{name}",
            "primary": bool(file_data.get("primary")) or name.lower().endswith((".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf", ".zip", ".json")),
            "size": raw_size_kb,
            "size_bytes": size_bytes,
            "download_url": url or f"{BASE_URL}/api/download/models/{version_id}",
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
        })
    if not files and version_id:
        files.append({
            "name": f"{version_name} — current version",
            "path": version_name,
            "primary": True,
            "size": "",
            "download_url": f"{BASE_URL}/api/download/models/{version_id}",
            "version_id": version_id,
            "version": version_name,
            "format": "",
        })
    return files


def _detail_media(detail, version):
    images = detail.get("images") or []
    if not images:
        return []
    media = []
    version_name = str(version.get("name") or version.get("id") or "version")
    for position, image in enumerate(images):
        if not isinstance(image, dict) or not image.get("url"):
            continue
        raw_type = str(image.get("type") or "image").lower()
        media_type = "video" if raw_type == "video" else "image"
        raw_media_url = _media_url(image)
        media_url = _optimized_red_video_url(raw_media_url) if media_type == "video" else raw_media_url
        filename = str(image.get("name") or f"preview-{position + 1}.{'mp4' if media_type == 'video' else 'jpg'}")
        meta = dict(image.get("meta") or image.get("metadata") or {})
        meta.update({
            "filename": filename,
            "path": f"{version_name}/{filename}",
            "civitai_red_model_version": version_name,
            "civitai_red_model_version_id": version.get("id"),
        })
        media_id = image.get("id") or image.get("imageId") or image.get("image_id")
        if media_id:
            meta["civitai_red_media_id"] = media_id
        media.append({
            "type": media_type,
            "url": media_url,
            "fallback_url": raw_media_url if media_type == "video" and raw_media_url != media_url else "",
            "thumbnail": "" if media_type == "image" else (_red_video_poster_url(media_url) or media_url),
            "filename": filename,
            "path": f"{version_name}/{filename}",
            "metadata": meta,
            "position": position,
        })
    return media



def fetch_generation_data(media_id):
    """Fetch rich CivitAI Red generation metadata for one media item on demand."""
    try:
        media_id = int(media_id)
    except (TypeError, ValueError):
        return {}
    cookies = _credentials()
    if not cookies.get("__Secure-civ-token"):
        return {}
    params = {"input": json.dumps({"json": {"id": media_id, "authed": True}}, separators=(",", ":"))}
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:154.0) "
            "Gecko/20100101 Firefox/154.0"
        ),
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": f"{BASE_URL}/",
        "x-client": "web",
        "x-client-date": str(int(time.time() * 1000)),
        "Cookie": _cookie_header(cookies),
    }
    try:
        response = session.get(
            BASE_URL + "/api/trpc/image.getGenerationData",
            params=params, headers=headers, timeout=20,
        )
        if response.status_code != 200:
            return {}
        payload = response.json()
    except Exception:
        return {}
    data = ((payload or {}).get("result") or {}).get("data") or {}
    generation = data.get("json") if isinstance(data, dict) else {}
    return generation if isinstance(generation, dict) else {}


def generation_metadata_for_display(generation):
    """Flatten useful fields for the UI while retaining the complete response."""
    if not isinstance(generation, dict):
        return {}
    meta = generation.get("meta") or {}
    if not isinstance(meta, dict):
        meta = {}
    out = {}
    for source_key, dest_key in (
        ("prompt","prompt"), ("negativePrompt","negative_prompt"),
        ("cfgScale","cfg"), ("steps","steps"), ("sampler","sampler"),
        ("seed","seed"), ("scheduler","scheduler"), ("denoise","denoise"),
        ("width","width"), ("height","height"), ("engine","engine"), ("Model","model"),
    ):
        value=meta.get(source_key)
        if value is not None and value != "":
            out[dest_key]=value
    if meta.get("models"): out["models"]=meta["models"]
    if meta.get("vaes"): out["vaes"]=meta["vaes"]
    resources=generation.get("resources") or []
    if isinstance(resources,list) and resources:
        compact=[]
        for r in resources:
            if not isinstance(r,dict): continue
            compact.append({k:r.get(k) for k in ("modelName","versionName","modelType","baseModel","strength","modelId","modelVersionId") if r.get(k) is not None})
        if compact: out["resources"]=compact
    out["_generation_data"]=generation
    out["_generation_data_cached"]=True
    return out


def _tag_names_from_value(values):
    """Normalize CivitAI/Red tag payloads without stringifying tag objects."""
    names = []
    seen = set()
    if isinstance(values, str):
        values = [part.strip() for part in re.split(r"[,\n]", values) if part.strip()]
    elif isinstance(values, dict):
        # A single tag object is common in hydrated state; an id->name mapping
        # also appears in a few older Red responses.
        if any(key in values for key in ("name", "tagName", "label")):
            values = [values]
        else:
            values = list(values.values())
    if not isinstance(values, (list, tuple, set)):
        return names
    for value in values:
        if isinstance(value, dict):
            nested = value.get("tag") if isinstance(value.get("tag"), dict) else value
            name = nested.get("name") or nested.get("tagName") or nested.get("label")
        else:
            name = value
        name = str(name or "").strip()
        if name and name.casefold() not in seen:
            seen.add(name.casefold())
            names.append(name)
    return names


def _listing_tag_names(item):
    """Return tag names/IDs from Red listing/detail shapes."""
    return _tag_names_from_value(item.get("tags") or item.get("tagNames") or [])


def _build_model(item, enrich=True):
    model_id = item.get("id")
    listing_version = item.get("version") or {}
    if not isinstance(listing_version, dict):
        listing_version = {}
    listing_version_id = listing_version.get("id")
    detail = _fetch_version_detail(listing_version_id) if enrich else {}

    # Red's listing endpoint exposes only the selected version. Hydrate from
    # the shared model id first; this preserves all versions/files/media even
    # when Red's own version-detail endpoint only describes one revision.
    mirror_detail = _fetch_mirror_model_detail(model_id) if enrich else {}
    mirror_versions = [v for v in (mirror_detail.get("modelVersions") or []) if isinstance(v, dict)]

    # Red's version-detail response can carry entitlement fields that the public
    # CivitAI mirror omits. Merge the selected listing/detail into the mirror
    # tree before deciding whether the rendered Red page is required.
    structured_versions = _merge_version_lists(mirror_versions, [listing_version] if listing_version else [])
    if isinstance(detail, dict) and detail:
        detail_version = dict(detail)
        if not detail_version.get("id") and listing_version_id:
            detail_version["id"] = listing_version_id
        structured_versions = _merge_version_lists(structured_versions, [detail_version])

    current_structured = next(
        (v for v in structured_versions if str(v.get("id")) == str(listing_version_id)),
        structured_versions[0] if structured_versions else {},
    )
    access_requires_red_page = bool(
        current_structured.get("requireAuth") is True
        or current_structured.get("canDownload") is False
        or isinstance(current_structured.get("paidAccess"), dict)
        or current_structured.get("earlyAccessDeadline")
    )

    listing_tags = _listing_tag_names(item)
    # Red and regular CivitAI share model ids. The mirror detail request above
    # is already part of the Red enrichment path and normally carries the
    # human-readable tag names even when Red's listing gives only numeric ids.
    # Use it first: this resolves tags without adding another network request.
    mirror_tags = _tag_names_from_value(mirror_detail.get("tags") or mirror_detail.get("tagNames") or [])
    has_numeric_listing_tags = any(str(tag).strip().isdigit() for tag in listing_tags)
    # Red-specific information stays authoritative for Red. Fetch the rendered
    # Red page when it adds something the structured responses cannot safely
    # provide: the full version picker, Red's own readable tag mapping (including
    # Red/NSFW-specific tags), or a fallback when mirror detail is unavailable.
    #
    # Multi-file status by itself no longer forces this page request.
    force_red_page = bool(item.get("_force_red_page"))
    # Red's rendered model page is the authoritative source for account/access
    # state such as paidAccess + canDownload. Those fields are not reliably
    # exposed by the mirror/detail APIs. We already only reach _build_model for
    # new/changed records, so paying one Red page request here is preferable to
    # silently labelling paid models as Downloadable.
    needs_red_page = bool(enrich)
    page_metadata = _fetch_model_page_metadata(
        model_id,
        listing_version_id,
    ) if needs_red_page else {}
    page_versions = [v for v in (page_metadata.get("versions") or []) if isinstance(v, dict)]

    if force_red_page:
        target_page_version = next(
            (v for v in page_versions if str(v.get("id")) == str(listing_version_id)),
            page_versions[0] if page_versions else {},
        )
        print(
            "CivitAI Red reload page access:",
            f"model={model_id}",
            f"version={listing_version_id or '-'}",
            f"page_versions={len(page_versions)}",
            f"paid={bool(target_page_version.get('paidAccess'))}",
            f"canDownload={target_page_version.get('canDownload')}",
            f"earlyAccessDeadline={target_page_version.get('earlyAccessDeadline') or '-'}",
        )

    # Merge Red's own rendered data first so we reuse everything already paid
    # for before considering another request to regular CivitAI.
    versions = _merge_version_lists(structured_versions, page_versions)
    if not versions:
        versions = [listing_version] if listing_version else []

    # Red's rendered download cards may already carry exact per-file names.
    # Apply those before deciding whether regular CivitAI filename authority is
    # necessary.
    file_name_overrides = page_metadata.get("file_name_overrides") or {}
    if file_name_overrides:
        for version_record in versions:
            for file_record in (version_record.get("files") or []):
                if not isinstance(file_record, dict):
                    continue
                file_id = str(file_record.get("id") or file_record.get("fileId") or file_record.get("file_id") or "")
                exact_name = file_name_overrides.get(file_id)
                if exact_name:
                    file_record["name"] = exact_name

    # Only make the extra regular-CivitAI rendered-page request when the data
    # still proves ambiguous after all Red information has been reused. This
    # keeps the exact-name fix for cases such as MiniMax H3 while avoiding a
    # duplicate rendered-page request for every ordinary multi-file Red model.
    needs_shared_filename_page = enrich and _has_ambiguous_multifile_names(versions)
    if needs_shared_filename_page:
        shared_versions = _fetch_civitai_page_versions(model_id)
        if shared_versions:
            versions = _merge_version_lists(versions, shared_versions)

    version = next((v for v in versions if str(v.get("id")) == str(listing_version_id)), versions[0] if versions else listing_version)
    version_id = version.get("id")

    mirror_user = mirror_detail.get("creator") or mirror_detail.get("user") or {}
    name = str(item.get("name") or mirror_detail.get("name") or (page_metadata.get("model") or {}).get("name") or f"CivitAI Red {model_id}")
    author = str((item.get("user") or {}).get("username") or (mirror_user.get("username") if isinstance(mirror_user, dict) else "") or ((page_metadata.get("model") or {}).get("user") or {}).get("username") or "")
    base_model = str(version.get("baseModel") or "")
    if not base_model:
        bases = item.get("baseModels") or []
        base_model = str(bases[0]) if bases else ""

    parent_description = item.get("description") or mirror_detail.get("description") or page_metadata.get("description") or ""
    description = _plain_text(parent_description or detail.get("description") or version.get("description") or "")
    trained_words = detail.get("trainedWords") or version.get("trainedWords") or []
    text = " ".join([name, description, base_model, str(item.get("type") or ""), " ".join(map(str, trained_words))])

    # Prefer complete page-version file metadata. Fall back to the current
    # version API when page state is unavailable.
    files = []
    for page_version in versions:
        page_detail = {"files": page_version.get("files") or []}
        files.extend(_detail_files(page_detail, page_version))
    if not files:
        files = _detail_files(detail, version)

    # Keep each version's own gallery. CivitAI/Red switch previews when the
    # selected version changes, so flatten them into model_media with version
    # identity in metadata; gallery.js can filter them client-side.
    media = []
    for page_version in versions:
        version_media = _detail_media({"images": page_version.get("images") or []}, page_version)
        for entry in version_media:
            entry["position"] = len(media)
            media.append(entry)
    if not media:
        media = _detail_media(detail, version) or _listing_media(item, version)
    preview = next((entry.get("url") for entry in media if entry.get("type") == "image"), "")
    has_video = any(entry.get("type") == "video" for entry in media)

    rank = item.get("rank") or {}
    created = item.get("createdAt") or version.get("createdAt") or item.get("publishedAt") or ""
    updated = _latest_date(item.get("lastVersionAt"), item.get("publishedAt"), version.get("publishedAt"), version.get("createdAt")) or created

    nsfw_level = item.get("nsfwLevel") or version.get("nsfwLevel") or 0
    try:
        sensitive_level = int(nsfw_level or 0) > 1
    except Exception:
        sensitive_level = bool(nsfw_level)

    model = Model()
    model.name = name
    model.display_name = name
    model.author = author
    model.source = NAME
    model.model_key = str(model_id)
    model.url = f"{BASE_URL}/models/{model_id}"
    model.image = preview
    model.description = description
    model.base_model = base_model
    model.architecture = processors.classify_architecture(base_model) if base_model else "Other"
    model.model_type = _model_type(item.get("type"), text)
    tag_names = list(listing_tags)
    resolved_page_tags = _tag_names_from_value(page_metadata.get("tags") or [])
    resolved_tags = []
    resolved_seen = set()
    for tag in mirror_tags + resolved_page_tags:
        text_tag = str(tag or "").strip()
        if not text_tag or text_tag.isdigit() or text_tag.casefold() in resolved_seen:
            continue
        resolved_seen.add(text_tag.casefold())
        resolved_tags.append(text_tag)

    if resolved_tags:
        # Numeric listing values are opaque tag IDs. Once either the already-
        # fetched CivitAI mirror payload or Red page state gives readable names,
        # remove the numeric placeholders and retain the complete readable set.
        tag_names = [tag for tag in tag_names if not str(tag).strip().isdigit()]
        known = {str(tag).casefold() for tag in tag_names}
        for tag in resolved_tags:
            key = tag.casefold()
            if key not in known:
                known.add(key)
                tag_names.append(tag)
    model.tags = ",".join(tag_names)
    model.display_tags = ([base_model] if base_model else []) + tag_names[:8]
    model.created = created
    model.updated = updated
    model.downloads = int(rank.get("downloadCount") or 0)
    model.likes = int(rank.get("thumbsUpCount") or 0)
    model.license = ""
    model.pipeline = ""
    model.files = files
    model.media = media
    model.preview_count = sum(1 for entry in media if entry.get("type") == "image")
    model.has_media = bool(media)
    model.has_video = has_video
    version_summaries = [_version_summary(v) for v in versions]
    any_downloadable = any(
        not v.get("paid_access")
        and not str(v.get("early_access_deadline") or "").strip()
        and v.get("can_download") is not False
        and str(v.get("availability") or "Public").casefold() == "public"
        for v in version_summaries
    )
    model.gated = bool(version_summaries) and not any_downloadable
    model.sensitive = bool(item.get("nsfw")) or sensitive_level
    model.card_data = {
        "civitai_red_id": model_id,
        "type": item.get("type"),
        "nsfw": item.get("nsfw"),
        "nsfw_level": nsfw_level,
        "availability": item.get("availability"),
        "version_id": version_id,
        "version_name": version.get("name"),
        "base_model": base_model,
        "trained_words": trained_words,
        "tags": tag_names,
        "versions": version_summaries,
    }
    model.format = next((entry.get("format") for entry in files if entry.get("format")), "")
    model.sha = str(version_id or "")
    return model




def _listing_architecture(item):
    """Classify a Red listing without opening its version-detail endpoint."""
    version = item.get("version") or {}
    if not isinstance(version, dict):
        version = {}
    values = []
    if version.get("baseModel"):
        values.append(str(version.get("baseModel")))
    for value in item.get("baseModels") or []:
        value = str(value or "").strip()
        if value and value not in values:
            values.append(value)
    for value in values:
        arch = processors.classify_architecture(value)
        if arch and arch != "Other":
            return arch
    return processors.classify_architecture(" ".join(values)) if values else "Other"


def _creator_models(username, scan_seen_models, scan_settings):
    """Fetch models from Red's exact creator catalog (`username` filter)."""
    username = str(username or "").strip()
    if not username:
        return []

    search_days = int(scan_settings.get("search_days", 7) or 7)
    max_results = max(1, int(scan_settings.get("max_results", 100) or 100))
    wanted_architectures = {
        str(value).strip().casefold()
        for value in (scan_settings.get("_external_architectures") or [])
        if str(value).strip()
    }

    started = time.perf_counter()
    try:
        items = _fetch_pages(
            username=username,
            sort_mode="newest",
            search_days=search_days,
            max_items=max_results,
        )
    except PermissionError as exc:
        print("CivitAI Red authentication error:", exc)
        return []
    except Exception as exc:
        print(f"CivitAI Red creator search failed for {username}: {exc}")
        debug_print(repr(exc))
        return []

    candidates = []
    seen_ids = set()
    arch_rejected = 0
    unchanged = 0
    for item in items:
        if scan_control.should_stop():
            break
        if not isinstance(item, dict) or item.get("id") is None:
            continue
        model_id = str(item.get("id"))
        if model_id in seen_ids:
            continue
        seen_ids.add(model_id)

        item_user = str((item.get("user") or {}).get("username") or "").strip()
        if item_user and item_user.casefold() != username.casefold():
            continue

        if wanted_architectures:
            arch = _listing_architecture(item).casefold()
            if arch not in wanted_architectures:
                arch_rejected += 1
                continue

        seen_key = (NAME, model_id)
        if scan_seen_models is not None and seen_key in scan_seen_models:
            continue
        if scan_seen_models is not None:
            scan_seen_models.add(seen_key)

        version = item.get("version") or {}
        if not isinstance(version, dict):
            version = {}
        source_updated = _latest_date(
            item.get("lastVersionAt"), item.get("publishedAt"),
            version.get("publishedAt"), version.get("createdAt")
        )
        source_sha = str(version.get("id") or "")
        existing = database.get_model(model_id, NAME)
        if existing:
            db_updated = str(existing["updated"] or "")
            db_sha = str(existing["sha"] or "")
            if (source_sha and db_sha == source_sha and db_updated == source_updated) or (source_updated and db_updated == source_updated):
                unchanged += 1
                # Search/creator scans still need the existing row represented in
                # results for accurate unchanged accounting only when the caller
                # has no separate DB lookup. Skip expensive re-detailing here.
                continue

        candidates.append(item)

    models = []
    failed = 0
    workers = min(4, len(candidates))
    if candidates:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="civitai-red-creator") as executor:
            futures = {executor.submit(_build_model, item, True): item for item in candidates}
            for future in as_completed(futures):
                if scan_control.should_stop():
                    break
                try:
                    model = future.result()
                    if model:
                        models.append(model)
                except Exception as exc:
                    failed += 1
                    debug_print("CivitAI Red creator detail failed:", repr(exc))

    print(
        f"CivitAI Red creator scan: {username!r} -> {len(items)} listing result(s), "
        f"{arch_rejected} architecture reject(s), {unchanged} unchanged, "
        f"{len(models)} detailed, {failed} failed in {time.perf_counter() - started:.2f}s"
    )
    return models

def scan_tag(tag_value, max_results=100, sort="NEWEST", tag_name=""):
    """Explicit CivitAI Red tag discovery for ModelRadar Discovery Scan."""
    if not _credentials().get("__Secure-civ-token"):
        raise PermissionError("CivitAI Red is not connected. Add the Red browser session under Source Accounts first.")

    tag = str(tag_value or tag_name or "").strip()
    if not tag:
        return []
    try:
        max_results = max(1, int(max_results))
    except (TypeError, ValueError):
        max_results = 100

    requested_sort = str(sort or "NEWEST").strip().upper()
    sort_mode = {
        "NEWEST": "newest",
        "HIGHEST_RATED": "highest_rated",
    }.get(requested_sort, "newest")

    blocked = {str(x).casefold() for x in database.get_blocked_creator_set(NAME)}
    print(f"CivitAI Red Discovery: tag={tag!r}, sort={_sort_label(sort_mode)}, limit={max_results}")
    items = _fetch_pages(
        tagname=tag,
        sort_mode=sort_mode,
        search_days=31,
        max_items=max_results,
    )

    unique = {}
    for item in items:
        if not isinstance(item, dict) or item.get("id") is None:
            continue
        author = str((item.get("user") or {}).get("username") or "")
        if author and author.casefold() in blocked:
            continue
        unique.setdefault(str(item.get("id")), item)

    if not unique:
        return []

    models = []
    workers = min(4, len(unique))
    print(f"CivitAI Red Discovery details: {len(unique)} model(s), {workers} worker(s)")
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="civitai-red-discovery") as executor:
        futures = {executor.submit(_build_model, item, True): model_id for model_id, item in unique.items()}
        failed = 0
        for future in as_completed(futures):
            if scan_control.should_stop():
                break
            try:
                model = future.result()
                if model:
                    # The tag page itself is authoritative context even when the
                    # listing/detail payload omits its tag collection. Preserve
                    # the requested discovery tag so local `tag:<name>` filters
                    # can reliably find models imported from this scan.
                    existing_tags = [part.strip() for part in str(getattr(model, "tags", "") or "").split(",") if part.strip()]
                    if tag.casefold() not in {name.casefold() for name in existing_tags}:
                        existing_tags.append(tag)
                    model.tags = ",".join(existing_tags)

                    display_tags = list(getattr(model, "display_tags", []) or [])
                    if tag.casefold() not in {str(name).casefold() for name in display_tags}:
                        display_tags.append(tag)
                    model.display_tags = display_tags
                    models.append(model)
            except Exception as exc:
                failed += 1
                debug_print(f"CivitAI Red Discovery detail failed {futures[future]}: {exc!r}")
    print(f"CivitAI Red Discovery detailed: {len(models)}, failed: {failed}")
    return models


def scan_preflight():
    """Skip normal CivitAI Red scan jobs once when no local session is saved."""
    if not _credentials().get("__Secure-civ-token"):
        return False, (
            "CivitAI Red skipped: source is not connected. "
            "Open Source Accounts to configure CivitAI Red."
        )
    return True, ""


def test_connection():
    cookies = _credentials()
    if not cookies.get("__Secure-civ-token"):
        return False, "No __Secure-civ-token is saved."
    device_note = ""
    if not cookies.get("__Secure-civ-device"):
        device_note = " Your browser normally sends __Secure-civ-device too; add it if Red rejects the test."
    try:
        payload = _request_page(sort_mode="newest", search_days=1)
        items = payload.get("items") or [] if isinstance(payload, dict) else []
        return True, f"Connected. CivitAI Red returned {len(items)} models."
    except PermissionError as exc:
        return False, str(exc) + device_note
    except Exception as exc:
        return False, f"CivitAI Red connection failed: {exc}" + device_note


def scan(term, scan_seen_models=None, scan_settings=None, creator=None):
    scan_settings = scan_settings or {}
    search_days = int(scan_settings.get("search_days", 7))
    max_results = max(1, int(scan_settings.get("max_results", 100)))
    sort_mode = scan_settings.get("sort", "newest")

    if creator:
        if not _credentials().get("__Secure-civ-token"):
            print("CivitAI Red is not connected. Open Options > Scanner > Source Accounts to add the local Red session token.")
            return []
        return _creator_models(creator, scan_seen_models, scan_settings)

    if not _credentials().get("__Secure-civ-token"):
        print("CivitAI Red is not connected. Open Options > Scanner > Source Accounts to add the local Red session token.")
        return []

    if scan_seen_models is None:
        scan_seen_models = set()

    configured_architecture = str(scan_settings.get("_architecture") or "").strip()
    architecture_context = str(scan_settings.get("_architecture_context") or "").strip()
    configured_type = str(scan_settings.get("_model_type") or "").strip()
    base_model = configured_architecture or architecture_context
    query = str(term or "").strip() if architecture_context or not configured_architecture else ""

    print(f"CivitAI Red search: {query or base_model or term}")
    print(f"CivitAI Red maximum results: {max_results} (cursor pagination)")

    try:
        items = _fetch_pages(
            base_model=base_model,
            model_type=configured_type,
            query=query,
            sort_mode=sort_mode,
            search_days=search_days,
            max_items=max_results,
        )
    except PermissionError as exc:
        print("CivitAI Red authentication error:", exc)
        return []
    except Exception as exc:
        print("CivitAI Red search failed:", exc)
        debug_print(repr(exc))
        return []

    # Anything mode means both lanes. Red exposes creator catalogs through an
    # exact `username` filter on model.getAll, so try the query as a creator
    # username as well as a normal model search and merge the results.
    creator_models = []
    if scan_settings.get("_external_search") and scan_settings.get("_external_intent") == "anything" and query:
        creator_settings = dict(scan_settings)
        creator_settings["max_results"] = max_results
        creator_models = _creator_models(query, scan_seen_models, creator_settings)

    print(f"CivitAI Red results inspected: {len(items)} / {max_results}")
    cutoff = datetime.utcnow() - timedelta(days=search_days)
    processed = []
    duplicates = 0
    old_models = 0
    media_count = 0
    invalid_models = 0
    already_seen = 0
    build_errors = 0

    for item in items:
        if scan_control.should_stop():
            break
        if not isinstance(item, dict) or item.get("id") is None:
            invalid_models += 1
            debug_print("CivitAI Red skip: invalid/missing model id", repr(item)[:300])
            continue
        model_id = str(item.get("id"))
        seen_key = (NAME, model_id)
        if seen_key in scan_seen_models:
            already_seen += 1
            debug_print(f"CivitAI Red skip: already seen this scan: {model_id} {item.get('name', '')}")
            continue
        scan_seen_models.add(seen_key)

        author = str((item.get("user") or {}).get("username") or "")
        blocked = {str(x).casefold() for x in (scan_settings.get("_blocked_creators") or [])}
        if author and author.casefold() in blocked:
            continue

        # Date filtering and duplicate checks happen before optional version
        # enrichment so routine scans stay fast.
        activity = _parse_date(_latest_date(item.get("lastVersionAt"), item.get("publishedAt"), item.get("createdAt")))
        if activity and activity < cutoff:
            old_models += 1
            continue

        version = item.get("version") or {}
        source_updated = _latest_date(item.get("lastVersionAt"), item.get("publishedAt"), version.get("publishedAt"), version.get("createdAt"))
        source_sha = str(version.get("id") or "")
        existing = database.get_model(model_id, NAME)
        if existing:
            db_updated = str(existing["updated"] or "")
            db_sha = str(existing["sha"] or "")
            if (source_sha and db_sha == source_sha and db_updated == source_updated) or (source_updated and db_updated == source_updated):
                duplicates += 1
                continue

        try:
            model = _build_model(item, enrich=True)
        except Exception as exc:
            build_errors += 1
            debug_print(f"CivitAI Red skip: build failed: {model_id} {item.get('name', '')}: {exc!r}")
            continue
        processed.append(model)
        media_count += len(model.media)

    if creator_models:
        existing_keys = {str(getattr(model, "model_key", "") or "") for model in processed}
        for model in creator_models:
            key = str(getattr(model, "model_key", "") or "")
            if key and key in existing_keys:
                continue
            processed.append(model)
            if key:
                existing_keys.add(key)
            media_count += len(getattr(model, "media", []) or [])

    print("\n========================================")
    print("CivitAI Red Scan Complete")
    print("========================================")
    print(f"Processed models : {len(processed)}")
    print(f"Old models       : {old_models}")
    print(f"Duplicates       : {duplicates}")
    print(f"Already seen     : {already_seen}")
    print(f"Invalid records  : {invalid_models}")
    print(f"Build errors     : {build_errors}")
    print(f"Skipped total    : {already_seen + invalid_models + build_errors}")
    print(f"Media files      : {media_count}")
    print(f"Mature models    : {sum(1 for model in processed if model.sensitive)}")
    print("========================================")
    return processed
