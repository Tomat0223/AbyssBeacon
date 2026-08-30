from scan_logging import verbose_print as print

import html
import re
import time
import uuid
import json
import shutil
import subprocess
from urllib.parse import quote
from datetime import datetime, timezone, timedelta


import database
import scan_control
from secrets_manager import (
    get_seaart_scan_session,
    get_seaart_download_session,
    seaart_scan_configured,
    seaart_download_configured,
)
from scanners.common.model import Model
from scanners.common import processors
from seaart_browser import browser_session_saved, live_session

NAME = "seaart"
DISPLAY = "SeaArt"
ENABLED = True
BASE = "https://www.seaart.ai"
SEARCH_API = BASE + "/api/v1/square/v3/search/list"
DETAIL_API = BASE + "/api/v1/model/detail"
DOWNLOAD_API = BASE + "/api/v1/resource/getDownloadLink"
ACCOUNT_MY_API = BASE + "/api/v1/account/my"
TAG_LIST_API = BASE + "/api/v1/square/v3/model/list"
CREATOR_LIST_API = BASE + "/api/v1/square/v3/model/account_list"


_CURL = shutil.which("curl.exe") or shutil.which("curl")


def _saved_headers(purpose="scan"):
    if purpose == "download":
        headers = get_seaart_download_session()
        message = (
            "SeaArt Direct Downloads are not connected. Open Source Accounts and import "
            "a signed-in getDownloadLink request."
        )
    else:
        headers = get_seaart_scan_session()
        message = (
            "SeaArt public scanning is not connected. Open Source Accounts and import "
            "a working SeaArt model-list/search request."
        )
    if not headers:
        raise RuntimeError(message)
    return {str(k): str(v) for k, v in headers.items() if str(k).strip() and str(v).strip()}


def _curl_post(url, payload, referer=None, timeout=30, purpose="scan"):
    """POST JSON through native curl using SeaArt's imported browser-session headers.

    The exact Firefox/Chromium cURL succeeds from the command line while Python
    requests receives HTTP 403. Using curl as the transport preserves the client
    behavior SeaArt accepts without exposing individual browser identifiers in UI.
    """
    if not _CURL:
        raise RuntimeError("SeaArt scanning requires curl/curl.exe, but it was not found on this system.")

    headers = _saved_headers(purpose)

    if purpose == "download":
        # Authenticated SeaArt downloads are more sensitive to the browser request
        # context than public scanning. Reproduce the copied getDownloadLink
        # request as closely as possible instead of replacing its Referer,
        # X-Request-Id, fetch headers, language, or browser metadata.
        #
        # Only values curl must calculate itself are removed.
        for key in list(headers):
            # Accept-Encoding from the copied browser request may advertise zstd
            # even when the local curl build cannot decode it. --compressed will
            # advertise only the encodings this curl actually supports.
            #
            # X-Request-Id is also per-request. Replaying the same ID can make
            # SeaArt accept the first Test Downloads request and then reject the
            # next real download as "auth token invalid".
            if key.lower() in {
                "content-length", "host", "accept-encoding", "x-request-id"
            }:
                headers.pop(key, None)

        headers["X-Request-Id"] = str(uuid.uuid4())
        headers.setdefault("Referer", referer or (BASE + "/model"))
        headers.setdefault("Accept", "application/json, text/plain, */*")
        headers.setdefault("Content-Type", "application/json")
        headers.setdefault("Origin", BASE)
        headers.setdefault("X-Platform", "web")
        headers.setdefault("X-Project-Id", "seaart")
        headers.setdefault("X-App-Id", "web_global_seaart")
    else:
        # Public scan captures are reusable across many pages, so replace fields
        # that are tied to one browser request while preserving browser identity.
        for key in list(headers):
            lk = key.lower()
            if lk in {
                "x-request-id", "referer", "content-length", "host", "connection",
                "priority", "te", "accept-encoding"
            }:
                headers.pop(key, None)
        headers["X-Request-Id"] = str(uuid.uuid4())
        headers["Referer"] = referer or (BASE + "/model")
        headers.setdefault("Accept", "application/json, text/plain, */*")
        headers.setdefault("Accept-Language", "en-US")
        headers.setdefault("Content-Type", "application/json")
        headers.setdefault("Origin", BASE)
        headers.setdefault("X-Platform", "web")
        headers.setdefault("X-Project-Id", "seaart")
        headers.setdefault("X-App-Id", "web_global_seaart")

    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    command = [
        _CURL,
        "--silent", "--show-error", "--compressed",
        "--request", "POST",
        "--max-time", str(max(5, int(timeout))),
        "--write-out", "\\n__MODELRADAR_HTTP__:%{http_code}",
        url,
    ]
    for name, value in headers.items():
        command.extend(["--header", f"{name}: {value}"])
    command.extend(["--data-raw", body])

    last = None
    for attempt in range(3):
        if scan_control.should_stop():
            raise RuntimeError("SeaArt scan stopped")
        try:
            proc = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=max(10, int(timeout) + 5),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            stdout = proc.stdout or ""
            marker = "\n__MODELRADAR_HTTP__:"
            if marker in stdout:
                raw, code_text = stdout.rsplit(marker, 1)
            else:
                raw, code_text = stdout, "0"
            try:
                http_code = int(code_text.strip().splitlines()[0])
            except Exception:
                http_code = 0

            if proc.returncode != 0:
                raise RuntimeError((proc.stderr or "SeaArt curl request failed").strip())
            if http_code == 403:
                if purpose == "download":
                    raise PermissionError(
                        "SeaArt authentication expired or was rejected. Reconnect your SeaArt "
                        "Account Token in Source Accounts and try again."
                    )
                raise PermissionError(
                    "SeaArt public scanning session expired or was rejected. Refresh SeaArt "
                    "Models, copy a fresh working request as cURL, and reconnect Public Scanning."
                )
            if http_code == 429 or http_code >= 500:
                last = RuntimeError(f"SeaArt HTTP {http_code}")
                if attempt < 2:
                    time.sleep(1.25 * (attempt + 1))
                    continue
                raise last
            if http_code >= 400:
                raise RuntimeError(f"SeaArt HTTP {http_code}: {raw[:300].strip()}")

            data = json.loads(raw)
            status = data.get("status") if isinstance(data, dict) else None
            if isinstance(status, dict) and status.get("code") not in (None, 0, 10000, "10000"):
                raise RuntimeError(status.get("msg") or f"SeaArt API code {status.get('code')}")
            return data
        except (subprocess.SubprocessError, ValueError, RuntimeError, PermissionError) as exc:
            last = exc
            if isinstance(exc, PermissionError):
                break
            if attempt < 2:
                time.sleep(1.25 * (attempt + 1))
    raise last or RuntimeError("SeaArt request failed")


def _post(url, payload, referer=None, timeout=30):
    return _curl_post(url, payload, referer=referer, timeout=timeout, purpose="scan")


def _post_download(url, payload, referer=None, timeout=30):
    try:
        return _curl_post(
            url,
            payload,
            referer=referer,
            timeout=timeout,
            purpose="download",
        )
    except Exception as exc:
        message = str(exc or "")
        if any(
            marker in message.casefold()
            for marker in (
                "auth token invalid",
                "account not logged in",
                "login expired",
                "token expired",
            )
        ):
            raise PermissionError(
                "SeaArt authentication expired. Open SeaArt Personal while signed in, copy a "
                "fresh /api/v1/account/my request as cURL, reconnect the Account Connection, "
                "and try again."
            ) from exc
        raise

def _plain(value):
    if not value:
        return ""
    text = str(value)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|li|h[1-6]|ul|ol)>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text).replace("\r", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def _iso_ms(value):
    try:
        n = float(value)
        if n > 10_000_000_000:
            n /= 1000.0
        return datetime.fromtimestamp(n, tz=timezone.utc).isoformat()
    except Exception:
        return str(value or "")


def _walk(obj):
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from _walk(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _walk(value)


def _search_items(payload):
    """Extract model cards defensively across SeaArt response wrappers."""
    data = payload.get("data") if isinstance(payload, dict) else payload
    candidates = []
    seen = set()
    for node in _walk(data):
        model_id = node.get("id") or node.get("model_id") or node.get("model_no")
        name = node.get("name") or node.get("title")
        # Search cards consistently carry an id plus model-ish metadata.
        if not model_id or not name:
            continue
        if not any(k in node for k in ("model_ver_no", "model_type", "type", "base_model", "base_model_title", "author", "cover", "cover_v2")):
            continue
        key = str(model_id)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(node)
    return candidates


def _search_profile(settings, term):
    watch = str(settings.get("_watch_architecture") or "").casefold()
    term_cf = str(term or "").casefold()
    if "krea" in watch or "krea" in term_cf:
        return "krea 2", ["Krea Image"]
    if "h3" in watch or "minimax" in watch or "h3" in term_cf or "minimax" in term_cf:
        return "h3", ["Minimax H3 Open"]
    # Future/custom watches still work as literal SeaArt model search terms.
    return str(term or settings.get("_watch_architecture") or "").strip(), []


def _response_offset(payload):
    """Return SeaArt's opaque next-page cursor from whichever wrapper contains it."""
    data = payload.get("data") if isinstance(payload, dict) else payload
    if isinstance(data, dict):
        for key in ("offset", "next_offset", "nextOffset"):
            value = data.get(key)
            if value not in (None, ""):
                return str(value)
    # SeaArt has moved list metadata between wrappers before, so keep this
    # deliberately defensive without mistaking card-level fields for cursors.
    if isinstance(payload, dict):
        for key in ("offset", "next_offset", "nextOffset"):
            value = payload.get(key)
            if value not in (None, ""):
                return str(value)
    return ""



def _remember_creator_identity(card, discovered_via="observed"):
    """Persist SeaArt's stable account id whenever a list/detail card exposes it."""
    if not isinstance(card, dict):
        return
    author = card.get("author") or {}
    if not isinstance(author, dict):
        return
    creator_id = str(author.get("id") or "").strip()
    creator_name = str(author.get("name") or "").strip()
    if not creator_id or not creator_name:
        return
    try:
        database.remember_creator_source_identity(
            creator_name, NAME, creator_id,
            profile_url=BASE + f"/user/{creator_id}",
            discovered_via=discovered_via,
        )
    except Exception:
        pass


def _local_creator_ids(creator):
    creator = str(creator or "").strip()
    if not creator:
        return []
    ids = []
    try:
        for row in database.get_creator_source_identities(source=NAME, creator_name=creator):
            value = str(row.get("source_creator_id") or "").strip()
            if value and value not in ids:
                ids.append(value)
    except Exception:
        pass

    # Backfill from models that predate creator_sources support without requiring
    # a destructive/rescan migration. SeaArt detail rows already preserve the
    # stable author id inside card_data.seaart.author_id.
    if not ids:
        try:
            conn = database.connect()
            rows = conn.execute(
                "SELECT author, card_data FROM models WHERE lower(source)=? AND lower(author)=lower(?)",
                (NAME, creator),
            ).fetchall()
            conn.close()
            for row in rows:
                try:
                    payload = json.loads(row["card_data"] or "{}")
                    creator_id = str((payload.get("seaart") or {}).get("author_id") or "").strip()
                    if creator_id and creator_id not in ids:
                        ids.append(creator_id)
                        database.remember_creator_source_identity(
                            creator, NAME, creator_id,
                            profile_url=BASE + f"/user/{creator_id}",
                            discovered_via="observed",
                        )
                except Exception:
                    continue
        except Exception:
            pass
    return ids


def _creator_name_match(candidate, query):
    candidate = str(candidate or "").strip().casefold()
    query = str(query or "").strip().casefold()
    if not candidate or not query:
        return False
    return candidate == query or query in candidate


def _resolve_creator_ids(creator, settings):
    """Resolve a SeaArt creator name to one or more stable account IDs.

    Prefer identities already learned from normal catalog/detail scans. For a
    creator ModelRadar has never seen, probe SeaArt's model search/catalog and
    harvest exact/close author matches from returned cards. This keeps creator
    scanning useful without pretending a model keyword result itself is the
    creator catalog.
    """
    creator = str(creator or "").strip()
    if not creator:
        return []
    if re.fullmatch(r"[0-9a-fA-F]{32}", creator):
        return [creator]

    ids = _local_creator_ids(creator)
    if ids:
        return ids

    probe_settings = dict(settings or {})
    probe_settings["max_results"] = min(100, max(24, int(probe_settings.get("max_results") or 100)))
    cards = []
    try:
        cards.extend(_fetch_search(creator, probe_settings))
    except Exception:
        pass

    # The real Models catalog is a useful fallback because it exposes author IDs
    # directly and SeaArt's keyword search does not always surface creators.
    # Probe the architectures selected in Search Sources when available.
    wanted = [str(x).strip() for x in (probe_settings.get("_external_architectures") or []) if str(x).strip()]
    seaart_bases = []
    for arch in wanted:
        cf = arch.casefold()
        if "krea" in cf:
            seaart_bases.append("Krea Image")
        elif "h3" in cf or "minimax" in cf:
            seaart_bases.append("Minimax H3 Open")
    for base in dict.fromkeys(seaart_bases):
        try:
            cards.extend(_fetch_catalog(base, probe_settings))
        except Exception:
            continue

    exact = []
    partial = []
    for card in cards:
        author = card.get("author") or {} if isinstance(card, dict) else {}
        if not isinstance(author, dict):
            continue
        name = str(author.get("name") or "").strip()
        creator_id = str(author.get("id") or "").strip()
        if not name or not creator_id:
            continue
        _remember_creator_identity(card, discovered_via="discovery")
        if name.casefold() == creator.casefold():
            if creator_id not in exact:
                exact.append(creator_id)
        elif _creator_name_match(name, creator):
            if creator_id not in partial:
                partial.append(creator_id)
    return exact or partial


def _fetch_creator_catalog(creator_id, settings):
    """Fetch models from SeaArt's personal-center model catalog."""
    max_results = max(1, int((settings or {}).get("max_results") or 100))
    page_size = min(24, max_results)
    collected = []
    known = set()
    page = 1
    offset = ""
    while len(collected) < max_results and not scan_control.should_stop():
        requested = min(page_size, max_results - len(collected))
        payload = {
            "offset": offset,
            "page": page,
            "page_size": requested,
            "order_by": "scope_b",
            "other_account": str(creator_id),
            "scene": "personal_center_refactor_v3",
        }
        response = _post(CREATOR_LIST_API, payload, BASE + f"/user/{creator_id}")
        items = _search_items(response)
        if not items:
            break
        added = 0
        for item in items:
            key = str(item.get("id") or item.get("model_id") or item.get("model_no") or "").strip()
            if not key or key in known:
                continue
            known.add(key)
            _remember_creator_identity(item, discovered_via="explicit")
            collected.append(item)
            added += 1
            if len(collected) >= max_results:
                break
        next_offset = _response_offset(response)
        if not added:
            break
        offset = next_offset
        if len(items) < requested and not next_offset:
            break
        page += 1
    return collected[:max_results]


def _card_architecture(card):
    if not isinstance(card, dict):
        return "Other"
    return processors.classify_architecture(" ".join([
        str(card.get("content_sub_type") or ""),
        str(card.get("base_model") or card.get("base_model_title") or ""),
        str(card.get("title") or card.get("name") or ""),
    ]))


def _external_architecture_card_ok(card, settings):
    wanted = {str(x).strip().casefold() for x in ((settings or {}).get("_external_architectures") or []) if str(x).strip()}
    if not wanted:
        return True
    return str(_card_architecture(card) or "").strip().casefold() in wanted


def _build_card_models(cards, settings, blocked=None, label="SeaArt"):
    blocked = blocked or set()
    models = []
    detail_attempted = 0
    for card in cards:
        if scan_control.should_stop():
            break
        if not _external_architecture_card_ok(card, settings):
            continue
        model_id = str(card.get("id") or card.get("model_id") or card.get("model_no") or "").strip()
        if not model_id:
            continue
        _remember_creator_identity(card)
        try:
            detail_attempted += 1
            model = _build(_detail(model_id))
        except Exception as exc:
            print(f"{label} detail failed for {model_id}: {type(exc).__name__}")
            continue
        if not model or model.author.casefold() in blocked:
            continue
        models.append(model)
    return models, detail_attempted

def _card_activity(card):
    """Best-effort timestamp from a catalog card, used before expensive detail calls."""
    if not isinstance(card, dict):
        return None
    values = []
    for key in ("update_at", "updated_at", "last_ver_create_at", "create_at", "created_at"):
        if card.get(key) not in (None, ""):
            values.append(card.get(key))
    version = card.get("model_ver_info_v2") or card.get("model_ver_info") or {}
    if isinstance(version, dict):
        for key in ("update_at", "create_at"):
            if version.get(key) not in (None, ""):
                values.append(version.get(key))
    for value in values:
        try:
            text = _iso_ms(value)
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            continue
    return None


def _card_activity_text(card):
    """SeaArt catalog activity in the same ISO format stored in ModelRadar."""
    activity = _card_activity(card)
    if activity is None:
        return ""
    try:
        return activity.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except Exception:
        return ""


def _card_version_id(card):
    """Best stable version identifier available without a detail request."""
    if not isinstance(card, dict):
        return ""
    version = card.get("model_ver_info_v2") or card.get("model_ver_info") or {}
    candidates = [
        card.get("model_ver_no"),
        card.get("model_version_id"),
        card.get("ver_id"),
        card.get("version_id"),
    ]
    if isinstance(version, dict):
        candidates.extend([
            version.get("model_ver_id"),
            version.get("model_ver_no"),
            version.get("id"),
        ])
    for value in candidates:
        if value not in (None, ""):
            return str(value)
    return ""


def _fetch_catalog(base_model, settings, live=None):
    """Read SeaArt's current newest-first model search for a watched base model.

    SeaArt retired the older /square/v3/model/list payload used by normal
    ModelRadar discovery. The live Models search now uses /search/list with a
    keyword plus its structured base-model filter. Keep this wrapper separate
    from explicit Search Sources so normal architecture watches can continue to
    use the source-specific profile while sharing the proven pagination path.
    """
    return _fetch_search(base_model, settings, live=live)


def _fetch_search(term, settings, live=None):
    max_results = max(1, int(settings.get("max_results") or 100))
    query, base_models = _search_profile(settings, term)
    if live is not None:
        return live.search_models(query, max_results=max_results)
    page_size = min(24, max_results)
    collected = []
    page = 1
    offset = 0
    while len(collected) < max_results and not scan_control.should_stop():
        payload = {
            "form_type": "sku", "order_by": "new", "base_models": base_models,
            "model_types": [], "scene": "square", "obj_name": query,
            "obj_type": "2", "page": page, "page_size": min(page_size, max_results-len(collected)),
            "offset": offset if page == 1 else "", "ss": 51,
        }
        response = _post(SEARCH_API, payload, BASE + "/search/model/" + quote(query))
        items = _search_items(response)
        if not items:
            break
        before = len(collected)
        known = {str(x.get("id") or x.get("model_id") or x.get("model_no")) for x in collected}
        for item in items:
            key = str(item.get("id") or item.get("model_id") or item.get("model_no"))
            if key not in known:
                collected.append(item); known.add(key)
                if len(collected) >= max_results:
                    break
        if len(collected) == before or len(items) < payload["page_size"]:
            break
        page += 1
    return collected[:max_results]


def _detail(model_id, live=None):
    payload = {"ss": 54, "id": str(model_id), "ver_id": "", "scene": "model_detail_v2"}
    if live is not None:
        return (live.post_json(DETAIL_API, payload, BASE + f"/models/detail/{model_id}").get("data") or {})
    return _post(DETAIL_API, payload, BASE + f"/models/detail/{model_id}").get("data") or {}


def _hash_map(detail):
    out = {}
    for item in detail.get("hashs") or []:
        if isinstance(item, dict) and item.get("type") and item.get("hash"):
            out[str(item["type"]).upper()] = str(item["hash"])
    return out


def _build(detail):
    model_id = str(detail.get("id") or "").strip()
    if not model_id:
        return None
    author_data = detail.get("author") or {}
    author = str(author_data.get("name") or author_data.get("id") or "Unknown")
    _remember_creator_identity({"author": author_data}, discovered_via="observed")
    name = str(detail.get("name") or model_id)
    version_id = str(detail.get("model_ver_no") or ((detail.get("model_ver_info_v2") or {}).get("model_ver_id")) or "")
    base_model = str(detail.get("base_model_title") or detail.get("base_model") or ((detail.get("model_ver_info_v2") or {}).get("base_model_name")) or "")
    version_info = detail.get("model_ver_info_v2") or {}
    model_info = detail.get("model_info_v2") or {}
    raw_model_type = str(
        detail.get("type")
        or detail.get("model_type")
        or version_info.get("model_type")
        or version_info.get("type")
        or model_info.get("model_type")
        or model_info.get("type")
        or "Other"
    ).strip()
    mt = raw_model_type.casefold()
    if mt in {"lora", "lycoris", "locon"}:
        model_type = "LoRA"
    elif mt in {"checkpoint", "model", "ckpt"}:
        model_type = "Checkpoint"
    elif mt in {"workflow", "comfyui workflow"}:
        model_type = "Workflow"
    else:
        model_type = raw_model_type or "Other"
    tags = [str(x.get("title") or x.get("id") or "").strip() for x in (detail.get("tags") or []) if isinstance(x, dict)]
    tags = [x for x in tags if x]
    media = []
    for pos, sample in enumerate(detail.get("samples") or []):
        if not isinstance(sample, dict):
            continue
        url = str(sample.get("url") or sample.get("video_url") or "").strip()
        if not url:
            continue
        media_type = "video" if (sample.get("video_url") or re.search(r"\.(mp4|webm|mov)(?:\?|$)", url, re.I)) else "image"
        media.append({"type": media_type, "url": url, "thumbnail": str(sample.get("cover") or ""), "position": pos})
    cover = str(detail.get("cover") or detail.get("cover_v2") or "")
    if cover and not any(x.get("url") == cover for x in media):
        media.insert(0, {"type": "image", "url": cover, "thumbnail": cover, "position": 0})
    hashes = _hash_map(detail)
    sha256 = hashes.get("SHA256", "")
    # SeaArt's detail API uses an integer capability code here.  `1` is the
    # positive "Allow Downloads" state.  Other non-zero values must not be
    # treated as truthy/downloadable (some correspond to unsupported states).
    try:
        download_code = int(detail.get("download") or 0)
    except (TypeError, ValueError):
        download_code = 0
    source_downloadable = (download_code == 1) and bool(version_id)
    session_can_download = source_downloadable and (browser_session_saved() or seaart_download_configured())
    files = []
    if version_id:
        files.append({
            "id": str(detail.get("file_id") or version_id), "file_id": str(detail.get("file_id") or ""),
            "model_ver_no": version_id, "name": f"{name} - {detail.get('version_name') or detail.get('ver') or 'model'}",
            "path": f"seaart:{version_id}", "primary": True, "sha": sha256,
            "hashes": hashes, "format": "SafeTensor" if source_downloadable else "",
            "seaart_dynamic_download": session_can_download,
        })
    created = _iso_ms(version_info.get("create_at") or detail.get("create_at"))
    updated = _iso_ms(version_info.get("update_at") or detail.get("update_at") or detail.get("last_ver_create_at"))
    stat = detail.get("stat") or {}
    model = Model()
    model.name = name; model.display_name = name; model.author = author; model.source = NAME
    model.model_key = model_id; model.url = BASE + f"/models/detail/{model_id}"
    model.image = cover or next((x["url"] for x in media if x["type"] == "image"), "")
    model.description = _plain(model_info.get("desc") or detail.get("desc") or version_info.get("desc"))
    model.base_model = base_model
    model.architecture = processors.classify_architecture(base_model)
    if model.architecture == "Other":
        model.architecture = processors.classify_architecture(name, tags)
    model.model_type = model_type; model.tags = ",".join(tags); model.display_tags = ([base_model] if base_model else []) + tags[:8]
    model.created = created; model.updated = updated; model.downloads = int(stat.get("num_of_download") or 0); model.likes = int(stat.get("num_of_like") or 0)
    model.files = files; model.media = media; model.preview_count = sum(x["type"] == "image" for x in media); model.has_media = bool(media); model.has_video = any(x["type"] == "video" for x in media)
    # A logged-out ModelRadar session must not make a public SeaArt model look gated.
    # `download` describes source capability; authentication only controls whether
    # ModelRadar itself can obtain the dynamic download URL right now.
    model.sha = sha256 or version_id; model.format = files[0].get("format", "") if files else ""; model.gated = False if source_downloadable else True; model.sensitive = bool(detail.get("nsfw_level") or detail.get("nsfw"))
    model.card_data = {"seaart": {"model_id": model_id, "author_id": author_data.get("id"), "version_id": version_id, "version_name": detail.get("version_name") or detail.get("ver"), "versions": detail.get("model_ver_nos") or [], "file_id": detail.get("file_id"), "hashes": hashes, "download_code": download_code, "downloadable": source_downloadable, "session_can_download": session_can_download, "refer": detail.get("refer"), "tags": tags}}
    return model



def scan_tag(tag_value, max_results=100, sort="NEWEST", tag_name=""):
    """Explicit SeaArt tag/category discovery.

    SeaArt's tag pages use square/v3/model/list rather than the keyword-search
    endpoint. `order_by` is the authoritative sort field; the page's `scene`
    string can still contain the historical `...order_by_hot` label even when
    the UI is set to New.
    """
    tag = str(tag_value or tag_name or "").strip()
    if not tag:
        return []
    try:
        max_results = max(1, int(max_results))
    except (TypeError, ValueError):
        max_results = 100

    requested_sort = str(sort or "NEWEST").strip().upper()
    order_by = {
        "NEWEST": "new",
        "HIGHEST_RATED": "hot",
        "HOT_TODAY": "hot",
        "LATEST_UPDATE": "new",
    }.get(requested_sort, "new")

    collected = []
    known = set()
    page = 1
    offset = ""
    while len(collected) < max_results and not scan_control.should_stop():
        page_size = min(50, max_results - len(collected))
        payload = {
            "tag_ids": [tag],
            "tag": tag,
            "page": page,
            "page_size": page_size,
            "base_models": [],
            "model_types": [],
            "offset": offset,
            "order_by": order_by,
            "scene": "scene_ai_search_list_order_by_hot",
            "sub_channel": [""],
            "time_from": None,
            "time_to": None,
            "canary_for_other": "tag_extend",
            "model_category": [],
        }
        response = _post(TAG_LIST_API, payload, BASE + "/tagInfo/model/" + quote(tag))
        items = _search_items(response)
        if not items:
            break
        added = 0
        for item in items:
            key = str(item.get("id") or item.get("model_id") or item.get("model_no") or "").strip()
            if not key or key in known:
                continue
            known.add(key)
            collected.append(item)
            added += 1
            if len(collected) >= max_results:
                break
        if not added or len(items) < page_size:
            break
        data = response.get("data") if isinstance(response, dict) else {}
        if isinstance(data, dict):
            offset = str(data.get("offset") or data.get("next_offset") or "")
        page += 1

    models = []
    blocked = set()
    try:
        blocked = {str(x).casefold() for x in database.get_blocked_creator_set(NAME)}
    except Exception:
        pass
    for card in collected:
        if scan_control.should_stop():
            break
        model_id = str(card.get("id") or card.get("model_id") or card.get("model_no") or "").strip()
        if not model_id:
            continue
        try:
            model = _build(_detail(model_id))
        except Exception as exc:
            print(f"SeaArt tag detail failed for {model_id}: {type(exc).__name__}")
            continue
        if not model or model.author.casefold() in blocked:
            continue
        # Preserve the tag-page context even when SeaArt's detail payload omits
        # the selected category from its own tag list.
        names = [x.strip() for x in str(model.tags or "").split(",") if x.strip()]
        if tag.casefold() not in {x.casefold() for x in names}:
            names.append(tag)
        model.tags = ",".join(names)
        display = list(getattr(model, "display_tags", []) or [])
        if tag.casefold() not in {str(x).casefold() for x in display}:
            display.append(tag)
        model.display_tags = display
        models.append(model)

    print(f"SeaArt Discovery detailed: {len(models)} model(s) from tag {tag!r}")
    return models

def scan(term, scan_seen_models=None, scan_settings=None, creator=None):
    settings = scan_settings or {}

    if creator:
        blocked = {str(x).casefold() for x in settings.get("_blocked_creators", [])}
        started = time.perf_counter()
        creator_ids = _resolve_creator_ids(creator, settings)
        if not creator_ids:
            print(f"SeaArt creator scan: no account match for {creator!r}")
            return []
        cards = []
        seen = set()
        for creator_id in creator_ids:
            for card in _fetch_creator_catalog(creator_id, settings):
                key = str(card.get("id") or card.get("model_id") or card.get("model_no") or "").strip()
                if key and key not in seen:
                    seen.add(key); cards.append(card)
                if len(cards) >= int(settings.get("max_results") or 100):
                    break
            if len(cards) >= int(settings.get("max_results") or 100):
                break
        models, detailed = _build_card_models(cards, settings, blocked=blocked, label="SeaArt creator")
        print(f"SeaArt creator scan: {creator!r} -> {len(creator_ids)} account(s), {len(cards)} candidate(s), {detailed} detailed, {len(models)} kept in {time.perf_counter()-started:.2f}s")
        return models

    blocked = {str(x).casefold() for x in settings.get("_blocked_creators", [])}
    external_search = bool(settings.get("_external_search"))
    search_mode = str(settings.get("_search_mode") or "text").strip().lower()

    catalog_started = time.perf_counter()
    live = None
    live_ctx = None
    if browser_session_saved():
        live_ctx = live_session()
        live = live_ctx.__enter__()
    # Browser-session discovery is preferred because SeaArt now signs its
        # listing/search requests inside the frontend. Manual cURL remains a fallback.
    if not external_search and search_mode == "base_model":
        cards = _fetch_catalog(term, settings, live=live)
        discovery_kind = "Live browser structured search" if live else "Structured model search"
    else:
        cards = _fetch_search(term, settings, live=live)
        discovery_kind = "Live browser keyword search" if live else "Keyword search"
    catalog_elapsed = time.perf_counter() - catalog_started

    # Anything mode means models + creators. SeaArt's direct keyword endpoint
    # supplies model hits; creator matches are resolved to account IDs and then
    # expanded through the real personal-center model catalog.
    if external_search and str(settings.get("_external_intent") or "anything").lower() == "anything":
        creator_query = str(settings.get("_external_query") or term or "").strip()
        creator_ids = _resolve_creator_ids(creator_query, settings)
        if creator_ids:
            known_ids = {str(x.get("id") or x.get("model_id") or x.get("model_no") or "").strip() for x in cards}
            max_results = max(1, int(settings.get("max_results") or 100))
            for creator_id in creator_ids:
                creator_settings = dict(settings)
                creator_settings["max_results"] = max_results
                for card in _fetch_creator_catalog(creator_id, creator_settings):
                    key = str(card.get("id") or card.get("model_id") or card.get("model_no") or "").strip()
                    if not key or key in known_ids:
                        continue
                    known_ids.add(key); cards.append(card)
                    if len(cards) >= max_results:
                        break
                if len(cards) >= max_results:
                    break
            cards = cards[:max_results]

    results = []
    cutoff = None
    if settings.get("_normal_retention_enabled") and not external_search:
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(days=int(settings.get("_normal_retention_days") or 7))
        except Exception:
            cutoff = None

    rejected_early = 0
    detail_attempted = 0
    detail_started = time.perf_counter()
    for card in cards:
        if scan_control.should_stop():
            break
        model_id = str(card.get("id") or card.get("model_id") or card.get("model_no") or "").strip()
        if not model_id:
            continue

        # First use SeaArt's preserved source snapshot to skip unchanged cards
        # before the expensive detail/media/files request. This remains correct
        # for merged cards because model_sources keeps SeaArt's own state even
        # when another provider owns the canonical models row.
        existing_source = database.get_model_source_snapshot(NAME, model_id)

        if existing_source and not external_search:
            card_updated = _card_activity_text(card)
            card_version = _card_version_id(card)

            stored_card = existing_source.get("card_data") or {}
            if isinstance(stored_card, str):
                try:
                    stored_card = json.loads(stored_card)
                except Exception:
                    stored_card = {}
            if isinstance(stored_card, dict):
                seaart_card = stored_card.get("seaart") or {}
            else:
                seaart_card = {}

            stored_catalog_updated = ""
            stored_catalog_version = ""
            if isinstance(seaart_card, dict):
                stored_catalog_updated = str(
                    seaart_card.get("catalog_updated") or ""
                )
                stored_catalog_version = str(
                    seaart_card.get("catalog_version") or ""
                )

            timestamp_matches = bool(
                card_updated
                and stored_catalog_updated
                and card_updated == stored_catalog_updated
            )
            version_matches = bool(
                card_version
                and stored_catalog_version
                and card_version == stored_catalog_version
            )

            if timestamp_matches or version_matches:
                rejected_early += 1
                continue

        # The catalog is newest-first. When card metadata exposes activity time,
        # avoid a detail/media request for an old model that retention would
        # immediately reject.
        if cutoff and not existing_source and not database.model_exists(model_id, NAME):
            activity = _card_activity(card)
            if activity is not None and activity < cutoff:
                rejected_early += 1
                continue

        try:
            detail_attempted += 1
            detail = _detail(model_id, live=live)
            model = _build(detail)
        except Exception as exc:
            print(f"SeaArt detail failed for {model_id}: {type(exc).__name__}")
            continue
        if not model or model.author.casefold() in blocked:
            continue

        if str(getattr(model, "architecture", "") or "").casefold() == "other":
            model.architecture = processors.classify_architecture_with_watch_fallback(
                settings.get("_watch_architecture"),
                getattr(model, "base_model", ""),
                getattr(model, "name", ""),
                getattr(model, "display_name", ""),
                getattr(model, "tags", ""),
                getattr(model, "description", ""),
            )

        # Persist the exact cheap catalog markers separately from detail.updated.
        # The next scan can reject unchanged cards before requesting detail,
        # media, or file metadata.
        card_data = getattr(model, "card_data", None)
        if not isinstance(card_data, dict):
            card_data = {}
        seaart_card = card_data.get("seaart")
        if not isinstance(seaart_card, dict):
            seaart_card = {}
        seaart_card["catalog_updated"] = _card_activity_text(card)
        seaart_card["catalog_version"] = _card_version_id(card)
        card_data["seaart"] = seaart_card
        model.card_data = card_data
        if cutoff:
            try:
                activity = datetime.fromisoformat((model.updated or model.created).replace("Z", "+00:00"))
                if activity.tzinfo is None:
                    activity = activity.replace(tzinfo=timezone.utc)
                if activity < cutoff and not database.model_exists(model.model_key, NAME):
                    continue
            except Exception:
                pass
        results.append(model)

    detail_elapsed = time.perf_counter() - detail_started
    if not external_search and search_mode == "base_model":
        print("\nSeaArt structured search timing")
        print(f"  Base model         : {term}")
        print(f"  Search query       : {catalog_elapsed:.2f}s")
        print(f"  Candidates         : {len(cards)}")
        print(f"  Rejected early     : {rejected_early}")
        print(f"  Detailed           : {detail_attempted}")
        print(f"  Detail/media/files : {detail_elapsed:.2f}s")
        print(f"  Kept               : {len(results)}")
        print(f"  Total              : {catalog_elapsed + detail_elapsed:.2f}s")
    if live_ctx is not None:
        live_ctx.__exit__(None, None, None)
    return results


def get_download_url(model_ver_no):
    # Prefer the persistent browser session. This keeps SeaArt downloads working
    # without re-importing a short-lived account cURL every ~30 minutes.
    if browser_session_saved():
        with live_session() as live:
            payload = live.post_json(
                DOWNLOAD_API,
                {"model_ver_no": str(model_ver_no)},
                BASE + "/model",
            )
            data = payload.get("data") or {}
            primary = str(data.get("url") or "").strip()
            backup = str(data.get("backup") or "").strip()
            return primary if primary.startswith(("http://", "https://")) else backup

    if not seaart_download_configured():
        raise PermissionError(
            "SeaArt is not connected. Open Source Accounts and connect a SeaArt Browser Session."
        )
    payload = _post_download(
        DOWNLOAD_API,
        {"model_ver_no": str(model_ver_no)},
        BASE + "/model",
    )
    data = payload.get("data") or {}
    primary = str(data.get("url") or "").strip()
    backup = str(data.get("backup") or "").strip()
    return primary if primary.startswith(("http://", "https://")) else backup


def scan_preflight():
    """Validate SeaArt once before normal multi-architecture scan jobs start.

    An enabled-but-disconnected SeaArt source used to let every architecture
    worker discover the same missing/expired session independently, producing
    repeated tracebacks. Keep that failure at the source boundary instead.
    """
    skipped_message = (
        "SeaArt skipped: source is not connected. Open Source Accounts to configure SeaArt."
    )

    # Avoid even making a network/browser request when no scan connection exists.
    if not browser_session_saved() and not seaart_scan_configured():
        return False, skipped_message

    ok, _detail = test_scan_connection()
    if not ok:
        return False, skipped_message
    return True, ""


def test_scan_connection():
    if browser_session_saved():
        try:
            with live_session() as live:
                cards = live.search_models("krea 2", max_results=1)
                if cards:
                    return True, "SeaArt live browser scanning is ready."
                return False, "SeaArt opened successfully, but no model cards were detected on the search page."
        except Exception as exc:
            return False, f"SeaArt live browser scanning failed: {exc}"

    if not seaart_scan_configured():
        return False, "SeaArt Browser Session is not connected yet."
    try:
        payload = {
            "form_type": "sku", "order_by": "new", "base_models": ["Krea Image"],
            "model_types": [], "scene": "square", "obj_name": "krea 2", "obj_type": "2",
            "page": 1, "page_size": 1, "offset": 0, "ss": 51,
        }
        data = _post(SEARCH_API, payload, BASE + "/search/model/krea%202", timeout=15)
        status = data.get("status") if isinstance(data, dict) else {}
        if isinstance(status, dict) and status.get("code") in (10000, "10000"):
            return True, "SeaArt manual public scanning session accepted."
        return False, "SeaArt responded, but the manual public scanning session could not be validated."
    except Exception as exc:
        return False, f"SeaArt manual public scanning session rejected: {exc}"


def test_download_connection():
    """Validate the signed-in SeaArt account without consuming a download chance."""
    if browser_session_saved():
        try:
            with live_session() as live:
                payload = live.post_json(ACCOUNT_MY_API, {"show_exp_level": True}, BASE + "/personal")
            status = payload.get("status") if isinstance(payload, dict) else {}
            data = payload.get("data") if isinstance(payload, dict) else {}
            ok = (
                isinstance(status, dict)
                and status.get("code") in (10000, "10000")
                and isinstance(data, dict)
                and bool(data.get("id"))
            )
            if not ok:
                return False, "SeaArt Browser Session is present, but the account is no longer signed in."
            display_name = str(data.get("name") or "").strip()
            return True, f"SeaArt browser account connected as {display_name}." if display_name else "SeaArt browser account connected."
        except Exception as exc:
            return False, f"SeaArt browser account check failed: {exc}"

    if not seaart_download_configured():
        return False, "SeaArt Browser Session is not connected and no manual Account Connection is configured."

    try:
        payload = _post_download(
            ACCOUNT_MY_API,
            {"show_exp_level": True},
            BASE + "/personal",
            timeout=15,
        )
        status = payload.get("status") if isinstance(payload, dict) else {}
        data = payload.get("data") if isinstance(payload, dict) else {}

        ok = (
            isinstance(status, dict)
            and status.get("code") in (10000, "10000")
            and isinstance(data, dict)
            and bool(data.get("id"))
        )
        if not ok:
            return False, "SeaArt responded, but the stored Account Session is not signed in."

        display_name = str(data.get("name") or "").strip()
        if display_name:
            return True, f"SeaArt account connected as {display_name}."
        return True, "SeaArt account connected. Direct Downloads are ready."

    except Exception as exc:
        message = str(exc or "")
        if any(token in message.casefold() for token in ("account not logged in", "auth token invalid", "authentication expired")):
            return False, (
                "SeaArt authentication expired. Open SeaArt Personal while signed in, copy a "
                "fresh /api/v1/account/my request as cURL, and reconnect the Account Connection."
            )
        return False, f"SeaArt Account Session rejected: {exc}"


def test_connection(mode="all"):
    mode = str(mode or "all").strip().lower()
    if mode == "scan":
        return test_scan_connection()
    if mode == "download":
        return test_download_connection()

    scan_ok, scan_message = test_scan_connection()
    download_ok, download_message = test_download_connection()
    if scan_ok and download_ok:
        return True, f"{scan_message} {download_message}"
    if scan_ok:
        return True, f"{scan_message} Direct Downloads are not connected yet."
    if download_ok:
        return False, f"Direct Downloads are ready, but public scanning is not connected. {scan_message}"
    return False, f"{scan_message} {download_message}"
