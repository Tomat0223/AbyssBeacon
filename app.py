import os
import subprocess
import sys
import json
import re
import sqlite3
import threading
import logging
import sys
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import quote, unquote, urlparse, parse_qsl, urlencode, urlunparse
from pathlib import Path

_missing_dependencies = []
try:
    from flask import Flask, render_template, request, redirect, Response
except ModuleNotFoundError:
    _missing_dependencies.append("Flask")
try:
    import requests
except ModuleNotFoundError:
    _missing_dependencies.append("requests")
try:
    from werkzeug.serving import make_server
    from jinja2 import ChoiceLoader, FileSystemLoader
except ModuleNotFoundError:
    # Werkzeug/Jinja2 are installed by Flask, so report Flask as the actionable dependency.
    if "Flask" not in _missing_dependencies:
        _missing_dependencies.append("Flask")
try:
    import PIL  # noqa: F401
except ModuleNotFoundError:
    _missing_dependencies.append("Pillow")

if _missing_dependencies:
    print("\nAbyssBeacon is missing required package(s): " + ", ".join(dict.fromkeys(_missing_dependencies)))
    print("Install them with: python -m pip install -r requirements.txt\n")
    raise SystemExit(1)

import database
import installer
import active_downloads
from scanners.common import metadata, processors
import scanner_runner
import scanner
import scan_status
from diagnostics import generate_diagnostic_report
from scanners.source_registry import SOURCE_INFO

from seaart_browser import (
    browser_session_status as seaart_browser_status,
    start_browser_connection as start_seaart_browser_connection,
    finish_browser_connection as finish_seaart_browser_connection,
    disconnect_browser_session as disconnect_seaart_browser_session,
    set_preferred_browser as set_seaart_browser_preference,
)

from settings_manager import (
    load_settings,
    save_settings,
    normalize_search_settings,
    normalize_scan_limits
)

from secrets_manager import (
    civitaired_configured,
    get_civitaired_credentials,
    set_civitaired_credentials,
    clear_civitaired_credentials,
    configured_sources,
    set_source_token,
    clear_source_token,
    get_source_token,
    set_civitai_search_key,
    get_civitai_search_key,
    clear_civitai_credentials,
    civitai_search_configured,
    set_seaart_scan_session,
    set_seaart_download_session,
    set_seaart_account_session,
    set_seaart_account_token,
    clear_seaart_scan_session,
    clear_seaart_download_session,
    clear_seaart_curl_session,
    seaart_connection_status,
)





from utils.loader import (
    load_architectures,
    load_model_types,
    save_config
)

app = Flask(
    __name__,
    template_folder="templates"
)

app.jinja_loader = ChoiceLoader([
    app.jinja_loader,
    FileSystemLoader("user_settings")
])


def _download_file_key(file_data):
    if not isinstance(file_data, dict):
        return ""
    return str(file_data.get("path") or file_data.get("name") or file_data.get("model_file_id") or file_data.get("id") or "").strip().lower()

def _civitai_download_url(file_data, current_url=""):
    """Return CivitAI's exact per-file browser download endpoint.

    A model version can contain several artifacts. CivitAI identifies the
    selected artifact with ?fileId=<id>; keeping that identity also ensures the
    local installer downloads the same file the user selected in AbyssBeacon.
    """
    if not isinstance(file_data, dict):
        return str(current_url or "").strip()

    version_id = (
        file_data.get("version_id")
        or file_data.get("model_version_id")
        or file_data.get("modelVersionId")
    )
    file_id = (
        file_data.get("file_id")
        or file_data.get("fileId")
        or file_data.get("id")
    )

    if version_id not in (None, "") and file_id not in (None, ""):
        return (
            f"https://civitai.com/api/download/models/{version_id}"
            f"?fileId={file_id}"
        )

    return str(current_url or "").strip()


def _civitaired_download_url(file_data, current_url=""):
    """Return the exact CivitAI Red per-file download URL.

    Red's browser route is:
      /api/download/models/<version_id>?fileId=<file_id>
    A version-only URL can return HTTP 403 for multi-file versions.
    """
    if not isinstance(file_data, dict):
        return str(current_url or "").strip()

    version_id = (
        file_data.get("version_id")
        or file_data.get("model_version_id")
        or file_data.get("modelVersionId")
    )
    file_id = (
        file_data.get("file_id")
        or file_data.get("fileId")
        or file_data.get("id")
    )

    if version_id not in (None, "") and file_id not in (None, ""):
        return (
            f"https://civitai.red/api/download/models/{version_id}"
            f"?fileId={file_id}"
        )

    return str(current_url or "").strip()


def _huggingface_download_url(model, file_data, current_url=""):
    """Return Hugging Face's stable resolve URL for the selected repository file.

    Hugging Face's browser download starts at /resolve/<revision>/<path>?download=true
    and then redirects to its signed CDN/Xet URL. We intentionally keep the
    stable resolve URL and let requests follow the fresh redirect each time.
    """
    if not isinstance(file_data, dict):
        return str(current_url or "").strip()

    model_key = str(
        model.get("model_key")
        or model.get("model_id")
        or ""
    ).strip()
    path = str(
        file_data.get("path")
        or file_data.get("rfilename")
        or file_data.get("name")
        or ""
    ).strip()
    revision = str(file_data.get("revision") or "main").strip() or "main"

    if model_key and path:
        return (
            f"https://huggingface.co/{model_key}/resolve/"
            f"{quote(revision, safe='')}/{quote(path, safe='/')}?download=true"
        )

    return str(current_url or "").strip()


def _modelscope_download_url(model, file_data, current_url=""):
    """Return ModelScope's stable resolve URL for one repository file.

    The website starts from:
      https://modelscope.cn/models/<repo>/resolve/<revision>/<path>

    ModelScope then redirects to a temporary signed CDN/LFS URL. Keep the
    stable repository URL and allow Python/browser redirects to obtain a fresh
    CDN URL each time instead of storing the temporary auth_key URL.
    """
    if not isinstance(file_data, dict):
        return str(current_url or "").strip()

    model_key = str(
        model.get("model_key")
        or model.get("model_id")
        or ""
    ).strip()
    path = str(
        file_data.get("path")
        or file_data.get("name")
        or ""
    ).strip()
    revision = str(
        file_data.get("revision")
        or model.get("revision")
        or "master"
    ).strip() or "master"

    if model_key and path:
        return (
            f"https://modelscope.cn/models/{model_key}/resolve/"
            f"{quote(revision, safe='')}/{quote(path, safe='/')}"
        )

    return str(current_url or "").strip()


def _install_preview_url(model_id, preferred_source=""):
    """Return the best full-resolution image already known by AbyssBeacon.

    Prefer media from the source the user selected, then fall back to any
    full-resolution gallery image. `url` is intentionally preferred over the
    source thumbnail field.
    """
    try:
        rows = database.get_media(int(model_id))
    except Exception:
        return ""

    preferred_source = str(preferred_source or "").strip().lower()
    candidates = []

    for row in rows or []:
        try:
            item = dict(row)
        except Exception:
            continue
        if str(item.get("type") or "").strip().lower() != "image":
            continue

        url = str(item.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            continue

        source = str(item.get("source") or "").strip().lower()
        position = item.get("position")
        try:
            position = int(position)
        except (TypeError, ValueError):
            position = 999999

        # Source-matched image first, then gallery order.
        rank = (0 if preferred_source and source == preferred_source else 1, position)
        candidates.append((rank, url))

    if not candidates:
        return ""

    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def _install_preview_video_url(model_id, preferred_source=""):
    """Return the best source preview video known by AbyssBeacon.

    This is only used as an installed-library companion when no image preview
    can be saved. Prefer the selected source, then normal gallery order.
    """
    try:
        rows = database.get_media(int(model_id))
    except Exception:
        return ""

    preferred_source = str(preferred_source or "").strip().lower()
    candidates = []
    for row in rows or []:
        try:
            item = dict(row)
        except Exception:
            continue
        if str(item.get("type") or "").strip().lower() != "video":
            continue
        url = str(item.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            continue
        source = str(item.get("source") or "").strip().lower()
        try:
            position = int(item.get("position"))
        except (TypeError, ValueError):
            position = 999999
        candidates.append(((0 if preferred_source and source == preferred_source else 1, position), url))

    if not candidates:
        return ""
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def _local_download_headers(source, source_page_url=""):
    """Authentication/browser headers needed for AbyssBeacon's local downloader.

    CivitAI Red authorizes its /api/download/models/... endpoint as a same-site
    browser navigation. Replaying only the two auth cookies can be rejected with
    HTTP 403, so mirror the stable browser request context as well.
    """
    source = str(source or "").strip().lower()
    headers = {}

    if source == "civitaired":
        creds = get_civitaired_credentials()
        session_token = str(creds.get("session_token") or "").strip()
        device_token = str(creds.get("device_token") or "").strip()
        cookies = []
        if session_token:
            cookies.append(f"__Secure-civ-token={session_token}")
        if device_token:
            cookies.append(f"__Secure-civ-device={device_token}")
        if cookies:
            headers["Cookie"] = "; ".join(cookies)

        # Match the successful Firefox download request closely enough for Red's
        # download middleware, while keeping only stable/reusable headers.
        headers["User-Agent"] = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:154.0) "
            "Gecko/20100101 Firefox/154.0"
        )
        headers["Accept"] = (
            "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        )
        headers["Accept-Language"] = "en-US,en;q=0.9"
        referer = str(source_page_url or "").strip()
        if not referer.startswith(("http://", "https://")):
            referer = "https://civitai.red/models"
        headers["Referer"] = referer

    elif source == "civitai":
        token = str(get_source_token("civitai") or "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"

    elif source == "huggingface":
        # Public repositories work without a token. Private/gated repositories
        # use the same Hugging Face token already configured in AbyssBeacon.
        token = str(get_source_token("huggingface") or "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        headers["Accept"] = "*/*"

    return headers



def _download_file_sha256(file_data):
    """Return only a real artifact SHA-256 suitable for cross-source matching."""
    if not isinstance(file_data, dict):
        return ""

    candidates = [
        file_data.get("sha256"),
        file_data.get("sha"),
        file_data.get("hash"),
    ]
    hashes = file_data.get("hashes")
    if isinstance(hashes, dict):
        candidates.extend([
            hashes.get("SHA256"),
            hashes.get("sha256"),
            hashes.get("Sha256"),
        ])
    elif isinstance(hashes, list):
        for item in hashes:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("type") or item.get("name") or "").replace("-", "").casefold()
            if kind == "sha256":
                candidates.append(item.get("hash") or item.get("value"))

    for value in candidates:
        value = str(value or "").strip().lower()
        if re.fullmatch(r"[0-9a-f]{64}", value):
            return value
    return ""


def _download_history_sha256(row):
    value = str((row or {}).get("sha") or "").strip().lower()
    return value if re.fullmatch(r"[0-9a-f]{64}", value) else ""


def _download_identity_key(file_data):
    """Stable same-source path/name identity used for legacy download history."""
    if not isinstance(file_data, dict):
        return ""
    value = (
        file_data.get("path")
        or file_data.get("name")
        or file_data.get("filename")
        or ""
    )
    return str(value or "").strip().replace("\\", "/").casefold()


def _history_identity_key(row):
    if not isinstance(row, dict):
        return ""
    value = row.get("file_key") or row.get("filename") or ""
    return str(value or "").strip().replace("\\", "/").casefold()


def _parse_download_timestamp(value):
    value = str(value or "").strip()
    if not value:
        return None
    try:
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def _download_record_sha(model, file_data):
    # Persist the real artifact SHA when available. Keep the older fallback so
    # non-SHA providers retain their existing history metadata.
    return (
        _download_file_sha256(file_data)
        or str((file_data or {}).get("sha") or (file_data or {}).get("hash") or model.get("sha") or "")
    )


def _download_file_fingerprint(model, file_data):
    """Stable-enough version fingerprint. Prefer file/hash identifiers; fall back to source update time."""
    if not isinstance(file_data, dict):
        return ""
    strong = {
        "source_file_id": str(file_data.get("model_file_id") or file_data.get("id") or file_data.get("file_id") or ""),
        "file_sha": str(file_data.get("sha") or file_data.get("hash") or file_data.get("hashes") or ""),
        "model_sha": str(model.get("sha") or ""),
        "size": str(file_data.get("size_bytes") or file_data.get("size") or ""),
        "path": str(file_data.get("path") or file_data.get("name") or ""),
    }
    # Some providers do not expose hashes/version IDs. Source updated is then the
    # best signal that the downloadable artifact may have changed.
    if not (strong["source_file_id"] or strong["file_sha"] or strong["model_sha"]):
        strong["source_updated"] = str(model.get("updated") or model.get("created") or "")
    raw = json.dumps(strong, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    import hashlib
    return hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()


def _model_download_status(model, history_rows, tracking_enabled=True):
    if not tracking_enabled or not history_rows:
        return "none"
    try:
        files = model.get("files") or []
        if isinstance(files, str): files = json.loads(files or "[]")
    except Exception:
        files = []
    current = set()
    for file_data in files:
        if isinstance(file_data, dict):
            fp = _download_file_fingerprint(model, file_data)
            if fp: current.add(fp)
    historical = {str(row.get("file_fingerprint") or "") for row in history_rows}
    return "downloaded" if current.intersection(historical) else "update"


def _annotate_download_state(model, history_lookup, preferences, source_snapshots=None):
    """Annotate download state across every source membership on a merged card.

    Current is intentionally conservative: old history must not become an
    "update" merely because scanner metadata normalization changed a generated
    fingerprint. We match current artifacts using, in order:
      1. real SHA-256 (cross-source safe);
      2. same-source stable file ID;
      3. exact generated fingerprint;
      4. same-source path/filename when the source has NOT become newer.

    Update Available requires positive evidence: the downloaded source has a
    newer source timestamp and its current artifact no longer matches.
    """
    tracking = preferences.get("track_downloads", True) is not False
    memberships = []

    for snapshot in source_snapshots or []:
        if not isinstance(snapshot, dict):
            continue
        source = str(snapshot.get("source") or "").strip().lower()
        model_key = str(snapshot.get("model_key") or "").strip()
        if source and model_key:
            memberships.append(snapshot)

    if not memberships:
        memberships.append(model)

    all_rows = []
    current_sha256 = set()
    definitive_update = False
    any_current_match = False

    # Cross-source SHA set first. This lets CivitAI/CivitAI Red mirrors count
    # as the same installed artifact when they expose the same real file SHA.
    for snapshot in memberships:
        try:
            files = snapshot.get("files") or []
            if isinstance(files, str):
                files = json.loads(files or "[]")
        except Exception:
            files = []
        for file_data in files:
            sha = _download_file_sha256(file_data)
            if sha:
                current_sha256.add(sha)

    for snapshot in memberships:
        source = str(snapshot.get("source") or "").strip().lower()
        model_key = str(snapshot.get("model_key") or "").strip()
        rows = history_lookup.get((source, model_key), []) if tracking else []
        if not rows:
            continue

        all_rows.extend(rows)

        try:
            files = snapshot.get("files") or []
            if isinstance(files, str):
                files = json.loads(files or "[]")
        except Exception:
            files = []

        current_fingerprints = set()
        current_file_ids = set()
        current_identity_keys = set()

        for file_data in files:
            if not isinstance(file_data, dict):
                continue
            fp = _download_file_fingerprint(snapshot, file_data)
            if fp:
                current_fingerprints.add(fp)

            file_id = str(
                file_data.get("model_file_id")
                or file_data.get("id")
                or file_data.get("file_id")
                or ""
            ).strip()
            if file_id:
                current_file_ids.add(file_id)

            identity = _download_identity_key(file_data)
            if identity:
                current_identity_keys.add(identity)

        current_updated = _parse_download_timestamp(
            snapshot.get("updated") or snapshot.get("created")
        )

        for row in rows:
            historical_fp = str(row.get("file_fingerprint") or "").strip()
            historical_file_id = str(row.get("source_file_id") or "").strip()
            historical_sha = _download_history_sha256(row)
            historical_identity = _history_identity_key(row)
            downloaded_source_updated = _parse_download_timestamp(row.get("source_updated"))

            # Strong matches always win, even when the source's general
            # "updated" timestamp changed for README/tags/metadata only.
            if historical_sha and historical_sha in current_sha256:
                any_current_match = True
                break
            if historical_file_id and historical_file_id in current_file_ids:
                any_current_match = True
                break
            if historical_fp and historical_fp in current_fingerprints:
                any_current_match = True
                break

            source_is_newer = bool(
                current_updated
                and downloaded_source_updated
                and current_updated > downloaded_source_updated
            )

            # Legacy history often predates the stronger SHA recording. A
            # stable same-source path/name is enough to remain Current unless
            # we have positive evidence that this source has advanced.
            if (
                historical_identity
                and historical_identity in current_identity_keys
                and not source_is_newer
            ):
                any_current_match = True
                break

            # Only call this an update when the source is demonstrably newer
            # and none of the strong identities above still match.
            if source_is_newer:
                definitive_update = True

        if any_current_match:
            break

    if any_current_match:
        state = "downloaded"
    elif all_rows and definitive_update:
        state = "update"
    elif all_rows:
        # We know AbyssBeacon downloaded this model, but we lack positive proof
        # of a newer artifact. Prefer Current over a false update badge.
        state = "downloaded"
    else:
        state = "none"

    model["download_status"] = state
    model["downloaded"] = state in {"downloaded", "update"}
    model["update_available"] = state == "update"
    model["show_download_status"] = bool(preferences.get("show_download_status_cards", True))
    if all_rows:
        model["last_downloaded_at"] = max(
            (str(row.get("downloaded_at") or "") for row in all_rows),
            default="",
        )
    return model


# Read-only cache used by the Updates Available feed filter.  The canonical
# update decision still comes from _annotate_download_state(); this cache only
# prevents /feed/counts and /feed/chunk from rebuilding the same set several
# times during one UI interaction.  No SQLite writes occur here.
_UPDATE_FILTER_CACHE = {"at": 0.0, "ids": set()}
_UPDATE_FILTER_CACHE_LOCK = threading.Lock()


def _update_available_model_ids(max_age_seconds=10.0):
    now = time.monotonic()
    with _UPDATE_FILTER_CACHE_LOCK:
        if now - float(_UPDATE_FILTER_CACHE.get("at") or 0.0) <= max_age_seconds:
            return set(_UPDATE_FILTER_CACHE.get("ids") or set())

        settings = load_settings()
        preferences = settings.get("preferences", {})
        if preferences.get("track_downloads", True) is False:
            ids = set()
        else:
            history = database.get_download_history_lookup()
            conn = database.connect()
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    """SELECT * FROM models
                       WHERE id IN (SELECT DISTINCT model_id FROM download_history WHERE model_id IS NOT NULL)"""
                ).fetchall()
                model_ids = [int(row["id"]) for row in rows]
                snapshots_by_model = {}
                if model_ids:
                    placeholders = ",".join("?" for _ in model_ids)
                    for source_row in conn.execute(
                        f"SELECT model_id,source,model_key,url,source_data FROM model_sources WHERE model_id IN ({placeholders})",
                        model_ids,
                    ).fetchall():
                        try:
                            snapshot = json.loads(source_row["source_data"] or "{}")
                            if not isinstance(snapshot, dict):
                                snapshot = {}
                        except Exception:
                            snapshot = {}
                        snapshot["source"] = str(source_row["source"] or "").lower()
                        snapshot["model_key"] = str(source_row["model_key"] or "")
                        snapshot["url"] = str(source_row["url"] or snapshot.get("url") or "")
                        snapshots_by_model.setdefault(int(source_row["model_id"]), []).append(snapshot)
            finally:
                conn.close()

            ids = set()
            for row in rows:
                model = dict(row)
                mid = int(model["id"])
                _annotate_download_state(model, history, preferences, snapshots_by_model.get(mid, []))
                if model.get("update_available"):
                    ids.add(mid)

        _UPDATE_FILTER_CACHE["at"] = now
        _UPDATE_FILTER_CACHE["ids"] = set(ids)
        return set(ids)


def _source_access_status(source, gated=False, card_data=None):
    source = str(source or "").lower()
    try:
        card = card_data or {}
        for _ in range(2):
            if not isinstance(card, str):
                break
            card = json.loads(card)
        if not isinstance(card, dict):
            card = {}
    except Exception:
        card = {}
    if source == "huggingface":
        hf_access=str(card.get("hf_download_access") or "").strip().lower()
        if hf_access=="downloadable":
            return "downloadable"
        if hf_access=="gated":
            return "gated"
        # Older rows without a probe still fall through to the source/API flag.
    if source == "tensorhub":
        access = str(((card.get("tensorhub") or {}).get("download_access") or "")).strip().lower()
        if access == "downloadable": return "downloadable"
        if access in {"paid_access", "paid", "buffet"}: return "paid_access"
        if access in {"gated", "non_downloadable", "restricted", "disabled"}: return "gated"
        return "unconfirmed"
    if source == "seaart":
        sea = card.get("seaart") or {}
        # New SeaArt scans store the raw capability code so we can distinguish
        # an explicit Allow Downloads=1 from other non-zero/unsupported states.
        # Older rows did not retain that code, so treat them as unknown until
        # the next SeaArt scan rather than displaying a misleading arrow.
        if "download_code" in sea:
            try:
                return "downloadable" if int(sea.get("download_code") or 0) == 1 else "gated"
            except (TypeError, ValueError):
                return "unconfirmed"
        return "unconfirmed"
    if source in {"civitai", "civitaired"}:
        versions = card.get("versions") or [] if isinstance(card, dict) else []
        if isinstance(versions, list) and versions:
            # CivitAI exposes versions newest/current first. The source badge
            # should describe the version the drawer opens on, not become
            # "Downloadable" merely because some older version is public.
            current = next((v for v in versions if isinstance(v, dict)), None)
            if current:
                state = _version_access_state(current, "public", True)
                if state == "downloadable":
                    return "public"
                if state == "early_access":
                    return "early_access"
                if state == "paid_access":
                    return "paid_access"
                if state == "gated":
                    return "gated"
    return "gated" if gated else "public"




def _infer_source_author(source, model_key="", url=""):
    """Best-effort creator recovery for sources whose stable key embeds the owner.

    Used only when an older model_sources snapshot predates author preservation.
    We intentionally do not guess for numeric/opaque source IDs.
    """
    source = str(source or "").strip().lower()
    model_key = str(model_key or "").strip()
    url = str(url or "").strip()

    if source in {"huggingface", "modelscope"}:
        if "/" in model_key:
            owner = model_key.split("/", 1)[0].strip()
            if owner:
                return owner

        # Conservative URL fallback for old rows where model_key was missing.
        try:
            from urllib.parse import urlparse
            parts = [part for part in urlparse(url).path.split("/") if part]
            if source == "huggingface" and len(parts) >= 2:
                return parts[0]
            if source == "modelscope":
                if "models" in parts:
                    idx = parts.index("models")
                    if len(parts) > idx + 1:
                        return parts[idx + 1]
                if len(parts) >= 2:
                    return parts[-2]
        except Exception:
            pass

    return ""


def _source_snapshot_for_download(model, target_source):
    """Clone a canonical CivitAI mirror snapshot for the selected mirror host."""
    snap = {
        "files": model.get("files") or [],
        "card_data": model.get("card_data") or {},
        "sha": model.get("sha") or "",
        "updated": model.get("updated") or "",
        "created": model.get("created") or "",
        "gated": int(bool(model.get("gated", 0))),
    }
    try:
        files = json.loads(snap["files"] or "[]") if isinstance(snap["files"], str) else list(snap["files"] or [])
    except Exception:
        files = []
    host = "civitai.com" if target_source == "civitai" else "civitai.red"
    other = "civitai.red" if host == "civitai.com" else "civitai.com"
    for item in files:
        if isinstance(item, dict) and item.get("download_url"):
            item["download_url"] = str(item["download_url"]).replace(other, host)
    snap["files"] = files
    return snap


def _tensorhub_snapshot_version_name(raw_name, project_name=""):
    raw = re.sub(r"\s+", " ", str(raw_name or "")).strip(" /\\|:-")
    if len(raw) < 3:
        return ""

    normalized = re.sub(r"[^a-z0-9]+", "", raw.casefold())
    project_normalized = re.sub(r"[^a-z0-9]+", "", str(project_name or "").casefold())
    if project_normalized and normalized == project_normalized:
        return ""

    if re.fullmatch(
        r"\d{4}[-/]\d{1,2}[-/]\d{1,2}(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?",
        raw,
        re.I,
    ):
        return ""

    if re.fullmatch(
        r"(?:model|version|ver|v|epoch|checkpoint|lora)[ _-]*\d*(?:\.\d+)*",
        raw,
        re.I,
    ):
        return ""

    return raw


def _repair_tensorhub_snapshot_versions(files, card_data, project_name=""):
    """Upgrade old TensorHub Epoch-only labels in memory.

    Existing DB snapshots already contain `source_name`, so this can restore
    meaningful labels immediately without forcing a source/creator rescan.
    """
    if not isinstance(card_data, dict):
        return files, card_data

    version_lists = []
    top_versions = card_data.get("versions")
    if isinstance(top_versions, list):
        version_lists.append(top_versions)

    tensor_data = card_data.get("tensorhub")
    if isinstance(tensor_data, dict):
        tensor_versions = tensor_data.get("versions")
        if isinstance(tensor_versions, list) and tensor_versions is not top_versions:
            version_lists.append(tensor_versions)

    label_by_id = {}

    for versions in version_lists:
        for meta in versions:
            if not isinstance(meta, dict):
                continue

            version_id = str(meta.get("id") or "").strip()
            source_name = _tensorhub_snapshot_version_name(
                meta.get("source_name"),
                project_name,
            )
            epoch = meta.get("epoch")

            if source_name:
                label = source_name
            elif epoch not in (None, ""):
                label = f"Epoch {epoch}"
            else:
                label = str(meta.get("name") or "").strip() or "Current version"

            meta["name"] = label
            if version_id:
                label_by_id[version_id] = label

    # Files are keyed to the nested TensorHub version ID, so update their
    # in-memory version label too. This also feeds Local Installer's friendly
    # filename fallback for generic names such as V1.safetensors.
    for file_data in files if isinstance(files, list) else []:
        if not isinstance(file_data, dict):
            continue
        version_id = str(file_data.get("version_id") or "").strip()
        if version_id and version_id in label_by_id:
            file_data["version"] = label_by_id[version_id]

    return files, card_data


def _decode_source_snapshot(link, canonical=None):
    source = str(link.get("source") or "").lower()
    try:
        snap = json.loads(link.get("source_data") or "{}") if isinstance(link.get("source_data"), str) else (link.get("source_data") or {})
    except Exception:
        snap = {}
    if canonical and source == str(canonical.get("source") or "").lower() and not snap:
        # Build a clean source snapshot from persisted model fields only.  Do
        # not copy transient render-only keys such as download_sources back
        # into the source snapshot; doing so creates a self-referential list
        # once the snapshot is appended to model["download_sources"].
        snapshot_keys = (
            "files", "card_data", "sha", "updated", "created", "gated",
            "url", "model_key", "source", "format", "quantization",
            "parameters", "license", "pipeline", "base_model",
            "architecture", "model_type", "name", "display_name", "author",
            "description", "image", "tags", "display_tags", "sensitive",
            "downloads", "likes", "has_media", "has_video", "preview_count"
        )
        snap = {key: canonical.get(key) for key in snapshot_keys if key in canonical}

    # CivitAI and CivitAI Red mirror the same model/version identifiers. Older
    # merged rows may have an empty snapshot for one side because model_sources
    # existed before source-specific download metadata. Reuse the sibling
    # snapshot only for this exact same-key mirror pair, then point its direct
    # file URLs at the source the user selected. This is deterministic and does
    # not broaden duplicate matching by name.
    if not snap and canonical and source in {"civitai", "civitaired"}:
        canonical_source = str(canonical.get("source") or "").lower()
        if canonical_source in {"civitai", "civitaired"} and str(link.get("model_key") or "") == str(canonical.get("model_key") or ""):
            snap = _source_snapshot_for_download(canonical, source)
            # This is only a compatibility shell for old merged rows that did
            # not preserve source_data yet.  Never let the sibling provider's
            # card_data become maturity evidence for this source.
            snap["_mirrored_download_fallback"] = True
            snap["_mirrored_from_source"] = canonical_source

    # Source-specific download snapshots intentionally focus on provider data and
    # may omit the canonical card identity. The local installer still needs that
    # identity for friendly filenames and library folders. Fill only blank values
    # so source-specific metadata remains authoritative when it exists.
    if canonical:
        for identity_key in (
            "name", "display_name", "author", "architecture", "base_model",
            "model_type", "pipeline", "description",
        ):
            if snap.get(identity_key) in (None, "") and canonical.get(identity_key) not in (None, ""):
                snap[identity_key] = canonical.get(identity_key)

    files = snap.get("files") or []
    if isinstance(files, str):
        try: files = json.loads(files or "[]")
        except Exception: files = []
    card_data = snap.get("card_data") or {}
    if isinstance(card_data, str):
        try: card_data = json.loads(card_data or "{}")
        except Exception: card_data = {}

    files = files if isinstance(files, list) else []
    if source == "tensorhub":
        project_name = str(
            snap.get("display_name")
            or snap.get("name")
            or (canonical or {}).get("display_name")
            or (canonical or {}).get("name")
            or ""
        ).strip()
        files, card_data = _repair_tensorhub_snapshot_versions(
            files,
            card_data,
            project_name,
        )

    return {
        **dict(link),
        **snap,
        "source": source,
        "files": files,
        "card_data": card_data,
    }



def _format_download_size(file_data, source=""):
    if not isinstance(file_data, dict):
        return ""
    if file_data.get("size_display"):
        return str(file_data.get("size_display") or "")
    bytes_value = file_data.get("size_bytes") or 0
    try:
        bytes_value = float(bytes_value)
    except (TypeError, ValueError):
        bytes_value = 0
    if not bytes_value:
        raw_size = file_data.get("size")
        try:
            numeric = float(raw_size)
        except (TypeError, ValueError):
            numeric = 0
        if numeric > 0:
            bytes_value = numeric * 1024 if str(source or "").lower() in {"civitai", "civitaired"} else numeric
    if bytes_value > 0:
        units = ["B", "KB", "MB", "GB", "TB"]
        unit_index = 0
        while bytes_value >= 1024 and unit_index < len(units) - 1:
            bytes_value /= 1024
            unit_index += 1
        decimals = 0 if unit_index == 0 or bytes_value >= 100 else (1 if bytes_value >= 10 else 2)
        return f"{bytes_value:.{decimals}f} {units[unit_index]}"
    return str(file_data.get("size_label") or file_data.get("model_size") or "").strip()


def _version_access_state(version, source_access="public", has_files=False):
    """Return downloadable / early_access / paid_access / gated / unknown."""
    version = version if isinstance(version, dict) else {}
    deadline = str(version.get("early_access_deadline") or "").strip()
    paid_access = version.get("paid_access") if isinstance(version.get("paid_access"), dict) else {}
    can_download = version.get("can_download")
    availability = str(version.get("availability") or "Public").strip().casefold()

    # CivitAI uses paidAccess for both timed Early Access and permanent paid
    # entitlement. A deadline means temporary Early Access; paidAccess without
    # a deadline and canDownload=false is a permanent/current purchase gate.
    if deadline and can_download is False:
        return "early_access"
    # A permanent paidAccess object is itself authoritative. Red's merge/
    # normalization path can preserve paidAccess while canDownload becomes None,
    # so requiring an explicit False here incorrectly labels paid models as
    # downloadable. Only an explicit can_download=True proves ownership/access.
    if paid_access and not deadline and can_download is not True:
        return "paid_access"

    # Some providers (notably TensorHub) persist the authoritative access state
    # directly on each version rather than in a CivitAI-style paid_access object.
    # Preserve that explicit paid state unless can_download=True proves the
    # authenticated account can download it.
    explicit_access = str(version.get("access_status") or "").strip().casefold()
    if explicit_access == "paid_access" and can_download is not True:
        return "paid_access"

    if deadline:
        try:
            deadline_dt = datetime.fromisoformat(deadline.replace("Z", "+00:00"))
            now = datetime.now(deadline_dt.tzinfo) if deadline_dt.tzinfo else datetime.now()
            if deadline_dt > now and can_download is not True:
                return "early_access"
        except Exception:
            pass
    if availability and availability != "public":
        return "gated"
    if can_download is False:
        return "gated"
    if source_access == "gated":
        return "gated"
    if source_access == "unconfirmed":
        # Keep the same tri-state vocabulary used by the download template.
        # TensorHub "unconfirmed" is deliberately attemptable: the provider
        # often omits a definitive access flag even when the signed URL works.
        return "unconfirmed"
    if has_files or can_download is True:
        return "downloadable"
    return "unconfirmed"


_PRIMARY_DOWNLOAD_EXTENSIONS = (
    ".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf", ".onnx"
)

_IMAGE_DOWNLOAD_EXTENSIONS = (
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".avif"
)


def _download_file_sort_key(file_data):
    """Put useful model artifacts first and repository support files last."""
    if not isinstance(file_data, dict):
        return (999, "")

    path = str(file_data.get("path") or file_data.get("name") or "").replace("\\", "/")
    basename = path.rsplit("/", 1)[-1].casefold()
    lower = path.casefold()
    ext = Path(basename).suffix.casefold()

    if file_data.get("_download_directory"):
        priority = 900
    elif ext == ".safetensors":
        priority = 0
    elif ext == ".gguf":
        priority = 10
    elif ext == ".ckpt":
        priority = 20
    elif ext in {".pt", ".pth", ".onnx"}:
        priority = 30
    elif ext == ".bin":
        priority = 40
    elif file_data.get("_download_primary"):
        priority = 50
    elif basename in {".gitattributes", ".gitignore"}:
        priority = 990
    elif ext in _IMAGE_DOWNLOAD_EXTENSIONS:
        priority = 700
    elif ext in {".json", ".yaml", ".yml"}:
        priority = 750
    elif ext in {".md", ".markdown", ".txt", ".text"}:
        priority = 800
    else:
        priority = 850

    # Within each usefulness bucket, put the largest artifacts first. This is
    # especially helpful for repositories containing many .safetensors files:
    # the main/full weights naturally rise above tiny auxiliary tensors while
    # support files still remain in their lower-priority buckets.
    size_value = file_data.get("size_bytes")
    if size_value in (None, "", 0, "0"):
        size_value = file_data.get("size")
    try:
        size_value = float(size_value or 0)
    except (TypeError, ValueError):
        size_value = 0

    # Some snapshots only retained a human-readable label.
    if size_value <= 0:
        label = str(
            file_data.get("size_display")
            or file_data.get("size_label")
            or file_data.get("model_size")
            or ""
        ).strip().upper()
        match = re.match(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*(B|KB|MB|GB|TB)\s*$", label)
        if match:
            amount = float(match.group(1))
            unit = match.group(2)
            multiplier = {
                "B": 1,
                "KB": 1024,
                "MB": 1024 ** 2,
                "GB": 1024 ** 3,
                "TB": 1024 ** 4,
            }.get(unit, 1)
            size_value = amount * multiplier

    return (priority, -size_value, lower)


def _annotate_download_file(file_data, source=""):
    """Add UI-only download priority/details without changing persisted metadata.

    Scanner-provided `primary` remains authoritative when present. Older
    snapshots (especially ModelScope) are repaired at render time by recognizing
    common model artifact extensions. Repository folders and support files stay
    available in the expanded view but are hidden from the default download list.
    """
    if not isinstance(file_data, dict):
        return file_data

    path = str(file_data.get("path") or file_data.get("name") or "").strip()
    name = str(file_data.get("name") or path or "").strip()
    lower = (path or name).casefold()

    raw_type = str(
        file_data.get("type")
        or file_data.get("file_type")
        or file_data.get("repo_type")
        or ""
    ).strip().casefold()

    size_value = file_data.get("size_bytes")
    if size_value in (None, ""):
        size_value = file_data.get("size")
    try:
        numeric_size = float(size_value or 0)
    except (TypeError, ValueError):
        numeric_size = 0

    basename = (path.rsplit("/", 1)[-1] if path else name)
    has_extension = "." in basename and not basename.startswith(".")

    is_directory = bool(
        file_data.get("is_directory")
        or raw_type in {"tree", "dir", "directory", "folder"}
        or (
            not has_extension
            and numeric_size <= 0
            and basename.casefold() in {
                "images", "image", "vae", "scheduler", "tokenizer",
                "text_encoder", "text_encoder_2", "transformer", "unet",
                "configs", "config", "assets", "examples", "samples",
            }
        )
    )

    explicit_primary = file_data.get("primary")
    inferred_primary = lower.endswith(_PRIMARY_DOWNLOAD_EXTENSIONS)

    ext = Path(basename).suffix.casefold()
    obvious_support = (
        basename.casefold() in {".gitattributes", ".gitignore", "readme.md", "license", "license.md"}
        or ext in {".json", ".yaml", ".yml", ".md", ".markdown", ".txt", ".text"}
        or ext in _IMAGE_DOWNLOAD_EXTENSIONS
    )
    primary = bool(explicit_primary is True or inferred_primary) and not is_directory and not obvious_support

    # Hugging Face repositories often contain dozens of docs, configs, images,
    # scripts and auxiliary weights. Keep the compact download view intentionally
    # strict: only .safetensors are shown until the user chooses Show all files.
    if str(source or "").strip().lower() == "huggingface":
        primary = (ext == ".safetensors") and not is_directory

    format_value = str(file_data.get("format") or "").strip()

    if is_directory:
        kind = "Repository folder"
    elif format_value:
        kind = format_value
    elif lower.endswith(_IMAGE_DOWNLOAD_EXTENSIONS):
        kind = "Image"
    elif ext == ".json":
        kind = "JSON"
    elif ext in {".md", ".markdown"}:
        kind = "Markdown"
    elif ext in {".txt", ".text"}:
        kind = "Text"
    elif ext in {".yaml", ".yml"}:
        kind = "YAML"
    elif ext:
        kind = "Model file" if primary else "Support file"
    else:
        kind = "Model file" if primary else "Support file"

    file_data["_download_primary"] = primary
    file_data["_download_directory"] = is_directory
    file_data["_download_kind"] = kind
    return file_data


def _model_version_share_url(source, source_url, version):
    """Return the best public page URL for one source/version.

    Most providers expose a stable model page and do not need a separate
    version URL. CivitAI/CivitAI Red are the important exception: their
    selected version is represented by ``modelVersionId`` on the model page.
    Preserve any other harmless query parameters while replacing that one.
    """
    source = str(source or "").strip().lower()
    source_url = str(source_url or "").strip()
    version = version if isinstance(version, dict) else {}

    for key in ("url", "page_url", "web_url", "model_url"):
        candidate = str(version.get(key) or "").strip()
        if candidate.startswith(("https://", "http://")):
            return candidate

    if not source_url:
        return ""

    version_id = str(version.get("id") or "").strip()
    if source in {"civitai", "civitaired"} and version_id:
        try:
            parsed = urlparse(source_url)
            query = [
                (key, value)
                for key, value in parse_qsl(parsed.query, keep_blank_values=True)
                if key.casefold() != "modelversionid"
            ]
            query.append(("modelVersionId", version_id))
            return urlunparse(parsed._replace(query=urlencode(query)))
        except Exception:
            separator = "&" if "?" in source_url else "?"
            return f"{source_url}{separator}modelVersionId={quote(version_id, safe='')}"

    return source_url


def _source_version_groups(src):
    """Group one source snapshot's flat file list back into model versions."""
    files = src.get("files") or []
    card = src.get("card_data") or {}
    versions_meta = card.get("versions") or [] if isinstance(card, dict) else []
    if not isinstance(versions_meta, list):
        versions_meta = []

    groups = []
    by_id = {}
    by_name = {}

    for meta in versions_meta:
        if not isinstance(meta, dict):
            continue
        name = str(meta.get("name") or meta.get("id") or "Version")
        group = dict(meta)
        group["name"] = name
        group["files"] = []
        group["key"] = str(meta.get("id") or name).casefold()
        groups.append(group)
        if meta.get("id") is not None:
            by_id[str(meta.get("id"))] = group
        by_name[name.casefold()] = group

    for idx, file_data in enumerate(files):
        if not isinstance(file_data, dict):
            continue
        file_data = _annotate_download_file(file_data, src.get("source"))
        file_data["_download_index"] = idx
        file_data["size_display"] = _format_download_size(file_data, src.get("source"))
        version_id = file_data.get("version_id")
        version_name = str(file_data.get("version") or "").strip()
        group = by_id.get(str(version_id)) if version_id is not None else None
        if not group and version_name:
            group = by_name.get(version_name.casefold())
        if not group and version_id is None and not version_name:
            group = by_name.get("current version")
        if not group:
            name = version_name or "Current version"
            group = {
                "id": version_id,
                "name": name,
                "files": [],
                "key": str(version_id or name).casefold(),
            }
            groups.append(group)
            if version_id is not None:
                by_id[str(version_id)] = group
            by_name[name.casefold()] = group
        group["files"].append(file_data)

    # Older providers/snapshots with no version metadata still get one sensible
    # group instead of losing their downloads.
    if not groups and files:
        groups = [{"id": None, "name": "Current version", "key": "current", "files": files}]

    for group in groups:
        group_files = [
            file_data
            for file_data in group.get("files", [])
            if isinstance(file_data, dict)
        ]

        # Use the Hugging Face-style compact chooser everywhere:
        # when a version contains one or more .safetensors files, those are the
        # only files shown by default. Docs, images, configs, scripts and other
        # repository/support files remain available through Show all.
        #
        # If a source/version genuinely has no .safetensors artifact (workflow,
        # GGUF-only repo, checkpoint-only repo, etc.), preserve the existing
        # primary-artifact inference so the compact chooser never becomes empty.
        safetensors_files = []
        for file_data in group_files:
            path = str(
                file_data.get("path")
                or file_data.get("name")
                or ""
            ).replace("\\", "/")
            if (
                Path(path.rsplit("/", 1)[-1]).suffix.casefold() == ".safetensors"
                and not file_data.get("_download_directory")
            ):
                safetensors_files.append(file_data)

        if safetensors_files:
            safetensors_ids = {id(file_data) for file_data in safetensors_files}
            for file_data in group_files:
                file_data["_download_primary"] = id(file_data) in safetensors_ids

        group["files"].sort(key=_download_file_sort_key)
        group["access_status"] = _version_access_state(
            group,
            src.get("access_status"),
            bool(group.get("files")),
        )
    return groups


def _load_download_file(model_row, file_index):
    try:
        files = json.loads(model_row.get("files") or "[]") if isinstance(model_row.get("files"), str) else (model_row.get("files") or [])
    except Exception:
        files = []
    if not isinstance(files, list) or file_index < 0 or file_index >= len(files):
        return None
    file_data = files[file_index]
    if isinstance(file_data, str):
        filename = str(file_data)
        download_url = ""
        if str(model_row.get("source") or "") == "huggingface" and model_row.get("model_key"):
            download_url = f"https://huggingface.co/{model_row['model_key']}/resolve/main/{quote(filename, safe='/')}?download=true"
        file_data = {"name": filename.split("/")[-1], "path": filename, "download_url": download_url}
    return file_data if isinstance(file_data, dict) else None


def _tensorhub_signed_download_url(model_file_id):
    token = get_source_token("tensorhub")
    if not token:
        raise PermissionError("TensorHub Art is not connected. Add your TensorHub session token in Source Accounts.")
    response = requests.get(
        "https://api.tensorhub.art/community-web/v1/model/file/url",
        params={"modelFileId": str(model_file_id), "useTcdn": "true"},
        headers={
            "Authorization": f"Bearer {token}", "Cookie": f"ta_token_prod={token}", "Accept": "*/*",
            "Origin": "https://tensorhub.art", "Referer": "https://tensorhub.art/", "X-Request-Package-Id": "3023",
            "X-Request-Lang": "en-US", "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:153.0) Gecko/20100101 Firefox/153.0",
        }, timeout=15,
    )
    response.raise_for_status(); payload=response.json()
    signed_url=str(((payload.get("data") or {}).get("url") or "")).strip()
    if str(payload.get("code", "")) != "0" or not signed_url.startswith(("https://", "http://")):
        raise RuntimeError("TensorHub did not provide a downloadable URL for this file.")
    return signed_url


def _seaart_dynamic_download_url(model_ver_no):
    from scanners import seaart
    url = seaart.get_download_url(model_ver_no)
    if not str(url or "").startswith(("https://", "http://")):
        raise RuntimeError("SeaArt did not provide a downloadable URL for this version.")
    return url


def parse_datetime(value):
    """Parse ISO dates plus Unix second/millisecond timestamps."""

    if value is None or value == "":
        return None

    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()

        if not text:
            return None

        try:
            number = float(text)

            # Unix milliseconds are currently around 13 digits.
            if abs(number) > 100000000000:
                number /= 1000.0

            parsed = datetime.fromtimestamp(number, tz=timezone.utc)

        except (TypeError, ValueError, OverflowError, OSError):
            try:
                parsed = datetime.fromisoformat(
                    text.replace("Z", "+00:00")
                )
            except Exception:
                return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed.astimezone(timezone.utc)


def time_since(value):

    parsed = parse_datetime(value)

    if not parsed:
        return str(value) if value not in (None, "") else ""

    now = datetime.now(timezone.utc)
    seconds = max(0, int((now - parsed).total_seconds()))

    if seconds < 60:
        return "just now"

    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min ago"

    hours = minutes // 60
    if hours < 24:
        return f"{hours} hr ago" if hours == 1 else f"{hours} hrs ago"

    days = hours // 24
    if days < 30:
        return f"{days} day ago" if days == 1 else f"{days} days ago"

    months = days // 30
    if months < 12:
        return f"{months} month ago" if months == 1 else f"{months} months ago"

    years = months // 12
    return f"{years} year ago" if years == 1 else f"{years} years ago"


def format_date(value):

    parsed = parse_datetime(value)

    if not parsed:
        return "Unknown" if not value else str(value)

    return parsed.strftime("%B %d, %Y")


app.jinja_env.filters["time_since"] = time_since
app.jinja_env.filters["format_date"] = format_date


@app.template_filter("source_label")
def source_label(value):
    source = str(value or "").strip().lower()
    labels = {
        "huggingface": "Hugging Face",
        "modelscope": "ModelScope",
        "civitai": "CivitAI",
        "civitaired": "CivitAI Red",
        "tensorhub": "TensorHub Art",
        "seaart": "SeaArt",
    }
    return labels.get(source, str(value or "").strip() or "Unknown source")


@app.template_filter("description_text")
def description_text(value):
    """Normalize source-provided HTML/Markdown descriptions for safe readable display."""
    return metadata.extract_description({"description": value or ""})


@app.template_filter("updated_date")
def updated_date(model):

    created_dt = parse_datetime(model["created"] or "")
    updated_dt = parse_datetime(model["updated"] or "")

    if not updated_dt:
        return "Never"

    if created_dt and updated_dt <= created_dt:
        return "Never"

    return updated_dt.strftime("%B %d, %Y")


@app.template_filter("activity_time")
def activity_time(model):

    parts = activity_time_parts(model)
    if not parts["relative"]:
        return ""
    return f'{parts["label"]} {parts["relative"]} ago'


@app.template_filter("activity_time_parts")
def activity_time_parts(model):
    created = model["created"] or ""
    updated = model["updated"] or ""
    try:
        retention_mode = str(model["retention_mode"] or "source")
        creator_discovered_at = model["creator_discovered_at"] or ""
    except (KeyError, IndexError):
        retention_mode = "source"
        creator_discovered_at = ""
    try:
        first_seen = model["first_seen"] or ""
    except (KeyError, IndexError):
        first_seen = ""
    created_dt = parse_datetime(created)
    updated_dt = parse_datetime(updated)
    first_seen_dt = parse_datetime(first_seen)
    creator_discovered_dt = parse_datetime(creator_discovered_at)

    if retention_mode == "creator_added" and creator_discovered_dt:
        chosen = creator_discovered_at
        label = "Added"
    elif not created_dt and not updated_dt:
        if not first_seen_dt:
            return {"label": "", "relative": "", "suffix": ""}
        chosen = first_seen
        label = "Scanned"
    elif updated_dt and (not created_dt or updated_dt > created_dt):
        chosen = updated
        label = "Updated"
    else:
        chosen = created or updated
        label = "Created" if created_dt else "Updated"

    relative = time_since(chosen)
    # time_since already includes "ago"; keep the cyan portion to just the time value.
    suffix = "" if relative == "just now" else "ago"
    if relative.endswith(" ago"):
        relative = relative[:-4]
    return {"label": label, "relative": relative, "suffix": suffix}


log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)
logging.getLogger("werkzeug").disabled = True

database.initialize()


@app.route("/settings", methods=["GET", "POST"])
def settings_page():

    settings = load_settings()

    if request.method == "POST":

        data = request.json or {}
        incoming = data.get("search_settings", {})
        incoming_limits = data.get("scan_limits", settings.get("scan_limits", {}))

        if not isinstance(incoming, dict):
            return {
                "success": False,
                "error": "Search settings must be an object."
            }, 400

        # Normalize on the server as well as in the UI. This hard-locks
        # source API limits even if settings.json is edited manually or a
        # browser submits an out-of-range value.
        # This first centralization step removes max-result inputs from each
        # source panel, but normal scanners still consume their existing source
        # values until the scanner-wiring patch lands. Preserve those values
        # while applying the source-specific settings that are still editable.
        merged_search_settings = {}
        existing_search_settings = settings.get("search_settings", {})
        for source, current in existing_search_settings.items():
            merged = dict(current or {})
            update = incoming.get(source, {})
            if isinstance(update, dict):
                merged.update(update)
            merged_search_settings[source] = merged

        normalized = normalize_search_settings(merged_search_settings)
        normalized_limits = normalize_scan_limits(
            incoming_limits,
            legacy_search_settings=normalized,
        )

        if normalized_limits.get("global_max_results") is None:
            prefs = settings.get("preferences", {})
            if not prefs.get("auto_cleanup_enabled", False):
                return {
                    "success": False,
                    "error": "Unlimited search results requires Automatic Retention. Normal scans must have either a retention-day window or a finite result limit."
                }, 400

        settings["search_settings"] = normalized
        settings["scan_limits"] = normalized_limits
        save_settings(settings)

        return {
            "success": True,
            "search_settings": normalized,
            "scan_limits": normalized_limits,
        }

    return render_template(
        "settings.html",
        search_settings=settings.get("search_settings", {}),
        scan_limits=settings.get("scan_limits", {}),
        preferences=settings.get("preferences", {}),
    )


@app.route("/api/diagnostic-report", methods=["GET"])
def diagnostic_report():
    """Return a paste-ready support report without credential values."""
    report = generate_diagnostic_report()
    download = str(request.args.get("download") or "").strip().lower() in {"1", "true", "yes"}
    headers = {"Cache-Control": "no-store"}
    if download:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
        headers["Content-Disposition"] = f'attachment; filename="AbyssBeacon_Diagnostic_{stamp}.txt"'
    return Response(report, status=200, mimetype="text/plain", headers=headers)


@app.route("/settings/accounts", methods=["GET", "POST", "DELETE"])
def source_accounts_settings():
    if request.method == "GET":
        return render_template(
            "accounts.html",
            configured=configured_sources(),
            civitai_api_configured=bool(get_source_token('civitai')),
            civitai_search_configured=civitai_search_configured(),
            source_health=scan_status.get_source_health(),
            seaart_status=seaart_connection_status(),
            seaart_browser=seaart_browser_status(),
        )

    payload = request.get_json(silent=True) or {}
    source = str(payload.get("source") or request.args.get("source") or "civitaired").strip().lower()

    if request.method == "DELETE":
        if source == "civitaired":
            clear_civitaired_credentials()
            message = "CivitAI Red connection cleared."
        elif source == "seaart":
            mode = str(request.args.get("mode") or "all").strip().lower()
            if mode == "scan":
                clear_seaart_scan_session()
                message = "SeaArt public scanning session cleared."
            elif mode == "download":
                clear_seaart_download_session()
                message = "SeaArt Direct Download session cleared."
            else:
                clear_seaart_curl_session()
                message = "All SeaArt browser sessions cleared."
        elif source == "civitai":
            clear_civitai_credentials()
            message = "CivitAI API and website-search credentials cleared."
        elif source in {"huggingface", "modelscope", "tensorhub"}:
            clear_source_token(source)
            message = f"{source} token cleared."
        else:
            return {"success": False, "error": "Unknown source."}, 400
        return {"success": True, "message": message}

    if source == "civitaired":
        session_token = str(payload.get("session_token") or "").strip()
        device_token = str(payload.get("device_token") or "").strip()

        existing = get_civitaired_credentials()
        if not session_token and not existing.get("session_token"):
            return {"success": False, "error": "A CivitAI Red session token is required."}, 400

        set_civitaired_credentials(session_token, device_token)
        return {
            "success": True,
            "message": "CivitAI Red connection saved locally. Session values persist independently."
        }

    if source == "seaart":
        mode = str(payload.get("mode") or "scan").strip().lower()
        curl_text = str(payload.get("curl_text") or "").strip()
        token = str(payload.get("token") or "").strip()

        try:
            if mode == "download":
                if curl_text:
                    headers = set_seaart_account_session(curl_text)
                    message = (
                        "SeaArt Account Connection imported. AbyssBeacon kept only the reusable "
                        "T + browser/device identity needed for signed-in requests; volatile "
                        "request IDs and Cloudflare cookies were discarded."
                    )
                elif token:
                    # Compatibility only. T-only auth is known to be insufficient
                    # for current SeaArt account requests.
                    set_seaart_account_token(token)
                    return {
                        "success": False,
                        "error": (
                            "SeaArt binds T to browser/device identity, so T alone cannot be "
                            "used reliably. Copy a signed-in /api/v1/account/my request as cURL instead."
                        ),
                    }, 400
                else:
                    return {
                        "success": False,
                        "error": "Paste the signed-in SeaArt account/my request copied as cURL first.",
                    }, 400
            else:
                if not curl_text:
                    return {"success": False, "error": "Paste a SeaArt Copy as cURL request first."}, 400
                set_seaart_scan_session(curl_text)
                message = "SeaArt public scanning session imported."
        except ValueError as exc:
            return {"success": False, "error": str(exc)}, 400

        return {
            "success": True,
            "message": message,
            "seaart_status": seaart_connection_status(),
        }

    if source == "civitai":
        token = str(payload.get("token") or "").strip()
        search_key = str(payload.get("search_key") or "").strip()

        if not token and not search_key:
            return {
                "success": False,
                "error": "Enter an API key, a website search key, or both."
            }, 400

        saved = []
        if token:
            set_source_token("civitai", token)
            saved.append("API key")
        if search_key:
            set_civitai_search_key(search_key)
            saved.append("website search key")

        return {
            "success": True,
            "message": "Saved " + " and ".join(saved) + ". Existing blank fields were left unchanged."
        }

    if source in {"huggingface", "modelscope", "tensorhub"}:
        token = str(payload.get("token") or "").strip()
        if not token:
            return {"success": False, "error": "An access token/API key is required."}, 400
        set_source_token(source, token)
        labels = {"huggingface": "Hugging Face", "modelscope": "ModelScope", "tensorhub": "TensorHub Art"}
        return {"success": True, "message": f"{labels[source]} token saved locally."}

    return {"success": False, "error": "Unknown source."}, 400


@app.route("/settings/accounts/seaart/browser/finish", methods=["POST"])
def seaart_browser_finish():
    status = finish_seaart_browser_connection()
    return {"success": not bool(status.get("state") == "error"), "seaart_browser": status}


@app.route("/settings/accounts/seaart/browser", methods=["GET", "POST", "DELETE"])
def seaart_browser_connection():
    if request.method == "GET":
        return {"success": True, "seaart_browser": seaart_browser_status()}
    if request.method == "DELETE":
        status = disconnect_seaart_browser_session()
        return {"success": not bool(status.get("state") == "error"), "seaart_browser": status}
    payload = request.get_json(silent=True) or {}
    browser = str(payload.get("browser") or "").strip().lower()
    if browser:
        try:
            set_seaart_browser_preference(browser)
        except ValueError as exc:
            return {"success": False, "error": str(exc), "seaart_browser": seaart_browser_status()}, 400
    status = start_seaart_browser_connection()
    return {"success": True, "seaart_browser": status}


@app.route("/settings/accounts/test", methods=["POST"])
def source_accounts_test():
    payload = request.get_json(silent=True) or {}
    source = str(payload.get("source") or "civitaired").strip().lower()

    if source == "civitaired":
        from scanners import civitaired
        success, message = civitaired.test_connection()
        return {"success": success, "message": message}, (200 if success else 400)

    if source == "seaart":
        from scanners import seaart
        mode = str(payload.get("mode") or "all").strip().lower()
        success, message = seaart.test_connection(mode)
        return {"success": success, "message": message, "seaart_status": seaart_connection_status()}, (200 if success else 400)

    if source == "civitai":
        from scanners import civitai
        success, message = civitai.test_connection()
        return {"success": success, "message": message}, (200 if success else 400)

    token = get_source_token(source)
    if not token:
        return {"success": False, "message": "No token is saved for this source."}, 400

    tests = {
        "huggingface": ("https://huggingface.co/api/whoami-v2", "Hugging Face"),
        "modelscope": ("https://modelscope.cn/openapi/v1/users/me", "ModelScope"),
        "tensorhub": (None, "TensorHub Art"),
    }
    if source not in tests:
        return {"success": False, "message": "Unknown source."}, 400

    url, label = tests[source]
    try:
        if source == "tensorhub":
            # Validate the normal TensorHub website session token against the same
            # signed-download endpoint used by tensorhub.art. Prefer a file that
            # TensorHub detail data already marked DOWNLOADABLE; using an arbitrary
            # file can produce a perfectly valid API response with no URL when that
            # particular model is gated/restricted.
            conn = database.connect()
            rows = conn.execute(
                "SELECT files, card_data, url FROM models "
                "WHERE source='tensorhub' AND files IS NOT NULL AND files != '' "
                "ORDER BY id DESC LIMIT 500"
            ).fetchall()
            conn.close()

            candidates = []
            fallback = []
            for row in rows:
                try:
                    files = json.loads(row["files"] or "[]")
                except Exception:
                    continue
                try:
                    card = json.loads(row["card_data"] or "{}")
                except Exception:
                    card = {}
                access = str(((card.get("tensorhub") or {}).get("download_access") or "")).strip().lower()
                referer = str(row["url"] or "https://tensorhub.art/").strip() or "https://tensorhub.art/"
                for item in files if isinstance(files, list) else []:
                    if not isinstance(item, dict) or not item.get("model_file_id"):
                        continue
                    candidate = (str(item["model_file_id"]), referer)
                    fallback.append(candidate)
                    if access == "downloadable":
                        candidates.append(candidate)

            pool = candidates or fallback
            if not pool:
                return {"success": False, "message": "No TensorHub file ID is available yet. Scan TensorHub once, then test again."}, 400

            api_url = "https://api.tensorhub.art/community-web/v1/model/file/url"
            last_reason = ""
            # Try a few known downloadable files so one stale/deleted record does
            # not incorrectly make a valid account look disconnected.
            for file_id, referer in pool[:5]:
                response = requests.get(
                    api_url,
                    params={"modelFileId": file_id, "useTcdn": "true"},
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Cookie": f"ta_token_prod={token}",
                        "Accept": "*/*",
                        "Origin": "https://tensorhub.art",
                        "Referer": referer,
                        "X-Request-Package-Id": "3023",
                        "X-Request-Lang": "en-US",
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:153.0) Gecko/20100101 Firefox/153.0",
                    },
                    timeout=12,
                )
                if response.status_code in (401, 403):
                    return {"success": False, "message": f"TensorHub rejected the saved session token (HTTP {response.status_code})."}, 400
                if response.status_code != 200:
                    last_reason = f"HTTP {response.status_code}"
                    continue
                try:
                    payload = response.json() if response.content else {}
                except Exception:
                    last_reason = "invalid JSON response"
                    continue
                data = payload.get("data") if isinstance(payload, dict) else {}
                signed = ((data or {}).get("url") if isinstance(data, dict) else "") or ""
                if str(payload.get("code", "")) == "0" and str(signed).startswith(("https://", "http://")):
                    return {"success": True, "message": "Connected to TensorHub Art. Direct downloads are available."}
                code = str(payload.get("code", "")).strip()
                message = str(payload.get("message", "")).strip()
                last_reason = " / ".join(x for x in (f"code {code}" if code else "", message) if x) or "no signed URL returned"

            return {
                "success": False,
                "message": f"TensorHub accepted the session, but the tested file did not return a signed download URL ({last_reason}). Try testing again after scanning a known downloadable TensorHub model."
            }, 400
        response = requests.get(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json", "User-Agent": "AbyssBeacon/1.0"}, timeout=12)
        if response.status_code == 200:
            data = response.json() if response.content else {}
            username = ""
            if isinstance(data, dict):
                username = str(data.get("name") or data.get("username") or data.get("userName") or "")
                if not username and isinstance(data.get("Data"), dict):
                    username = str(data["Data"].get("username") or data["Data"].get("name") or "")
            suffix = f" as {username}" if username else ""
            return {"success": True, "message": f"Connected to {label}{suffix}."}
        return {"success": False, "message": f"{label} rejected the token (HTTP {response.status_code})."}, 400
    except Exception as exc:
        return {"success": False, "message": f"{label} connection failed: {type(exc).__name__}."}, 400




@app.route("/api/install/test-path", methods=["POST"])
def test_install_path():
    payload = request.get_json(silent=True) or {}
    raw = str(payload.get("path") or "").strip()
    if not raw:
        return {"success": False, "message": "Choose your ComfyUI folder first."}, 400

    path = installer.resolve_comfy_root(raw)
    if not path.exists():
        return {"success": False, "message": f"Folder does not exist: {path}"}, 400
    if not path.is_dir():
        return {"success": False, "message": f"That path is not a folder: {path}"}, 400

    models = path / "models"
    workflows = path / "user" / "default" / "workflows"

    if not models.exists():
        return {
            "success": False,
            "message": (
                f"This does not look like a ComfyUI root because its models folder "
                f"was not found: {models}"
            ),
        }, 400

    familiar = [
        name
        for name in ("checkpoints", "loras", "vae", "diffusion_models")
        if (models / name).exists()
    ]
    found = f" Models: {', '.join(familiar)}." if familiar else " Models folder found."
    workflow_note = (
        " Workflows folder found."
        if workflows.exists()
        else " Workflows will use user/default/workflows when needed."
    )

    return {
        "success": True,
        "normalized_path": str(path),
        "message": f"ComfyUI folder is ready: {path}.{found}{workflow_note}",
    }


def _download_access_is_restricted(model):
    """Return True only when the source metadata explicitly marks this model restricted."""
    if not isinstance(model, dict):
        return False

    if bool(model.get("gated")):
        return True

    tags = str(model.get("tags") or "").casefold()
    if any(word in tags for word in ("gated", "restricted", "paid", "private")):
        return True

    card = model.get("card_data")
    if isinstance(card, str) and card.strip():
        try:
            card = json.loads(card)
        except Exception:
            card = {}
    if isinstance(card, dict):
        for value in card.values():
            if not isinstance(value, dict):
                continue
            state = str(
                value.get("download_access")
                or value.get("access_status")
                or value.get("access")
                or ""
            ).strip().casefold()
            if state in {
                "gated", "restricted", "paid", "private",
                "non_downloadable", "disabled",
            }:
                return True
    return False


def _restricted_download_payload(model, source_name, source_url, detail=""):
    label = source_label(source_name)
    message = (
        f"This model is restricted on {label}. Your current account does not "
        "have download access."
    )
    if detail:
        message += f" {detail}"
    return {
        "success": False,
        "error": message,
        "restricted": True,
        "source": source_name,
        "source_label": label,
        "source_url": str(source_url or model.get("url") or "").strip(),
        "action_label": f"Open on {label}",
    }



def _civitai_include_mature_media_enabled():
    settings = load_settings()
    search_settings = settings.get("search_settings", {}) if isinstance(settings.get("search_settings"), dict) else {}
    civitai_settings = search_settings.get("civitai", {}) if isinstance(search_settings.get("civitai"), dict) else {}
    return bool(civitai_settings.get("include_mature_media", False))

def _civitai_media_limit():
    settings = load_settings()
    prefs = settings.get("preferences", {}) if isinstance(settings.get("preferences"), dict) else {}
    try:
        return max(0, int(prefs.get("media_per_model_limit", 100)))
    except (TypeError, ValueError):
        return 100


def _queue_civit_refresh(item):
    """Refresh one queued CivitAI/Red model and persist the fresh source snapshot."""
    source_name = str(item.get("source") or "").strip().lower()
    model_id = int(item.get("model_id") or 0)
    model_key = str(item.get("model_key") or "").strip()
    links = [dict(row) for row in database.get_model_sources(model_id)]
    link = next((row for row in links if str(row.get("source") or "").lower() == source_name), None)
    if not link:
        raise RuntimeError("The queued source is no longer attached to this AbyssBeacon card.")

    source_url = str(link.get("url") or "").strip()
    if source_name == "civitai":
        from scanners import civitai as civitai_scanner
        civitai_scanner._apply_auth()
        civitai_scanner._DETAIL_ENRICHMENT_DISABLED = False
        details = civitai_scanner._fetch_model_detail(model_key)
        if not isinstance(details, dict) or not details:
            raise RuntimeError("CivitAI did not return fresh model details.")
        details["_force_page"] = True
        refreshed = civitai_scanner._build_model(
            details,
            enrich=False,
            include_mature_media=_civitai_include_mature_media_enabled(),
            media_limit=_civitai_media_limit(),
        )
    elif source_name == "civitaired":
        from scanners import civitaired as civitaired_scanner
        try:
            previous = json.loads(link.get("source_data") or "{}")
            if not isinstance(previous, dict):
                previous = {}
        except Exception:
            previous = {}
        card = previous.get("card_data") or {}
        if isinstance(card, str):
            try: card=json.loads(card or "{}")
            except Exception: card={}
        seed={"id": model_key}
        previous_version_id=str((card or {}).get("version_id") or "").strip()
        if previous_version_id:
            seed["version"]={"id": previous_version_id}
        refreshed = civitaired_scanner._build_model(seed, enrich=True)
        if not refreshed or not str(getattr(refreshed, "model_key", "") or "").strip():
            raise RuntimeError("CivitAI Red did not return fresh model details.")
    else:
        raise RuntimeError("Queued downloads currently support CivitAI and CivitAI Red only.")

    snapshot=refreshed.as_dict()
    database.refresh_model_source_snapshot(
        model_id,
        source_name,
        model_key,
        snapshot,
        url=source_url or refreshed.url or "",
    )
    snapshot["source"]=source_name
    snapshot["model_key"]=model_key
    snapshot["url"]=source_url or refreshed.url or ""
    return snapshot


def _queue_target_version(snapshot, item):
    snapshot = dict(snapshot or {})
    snapshot["access_status"] = _source_access_status(
        snapshot.get("source"), snapshot.get("gated"), snapshot.get("card_data")
    )
    if snapshot["access_status"] == "public" and snapshot.get("files"):
        snapshot["access_status"]="downloadable"
    versions=_source_version_groups(snapshot)
    wanted_id=str(item.get("version_id") or "").strip()
    wanted_name=str(item.get("version_name") or "").strip().casefold()

    version=None
    if wanted_id:
        version=next((v for v in versions if str(v.get("id") or "") == wanted_id), None)
    if version is None and wanted_name:
        version=next((v for v in versions if str(v.get("name") or "").strip().casefold() == wanted_name), None)
    if version is None and len(versions)==1:
        version=versions[0]
    return version


def _queue_best_file(version):
    if not isinstance(version, dict):
        return None
    files=[f for f in version.get("files", []) if isinstance(f, dict) and not f.get("_download_directory")]
    primary=[f for f in files if f.get("_download_primary")]
    candidates=primary or files
    return candidates[0] if candidates else None


def _watch_target_file(snapshot, version, item):
    """Resolve one watched CivitAI artifact without guessing a different file."""
    if not isinstance(version, dict):
        return None
    files=[f for f in version.get("files", []) if isinstance(f, dict) and not f.get("_download_directory")]
    wanted_id=str(item.get("file_id") or "").strip()
    wanted_fp=str(item.get("file_fingerprint") or "").strip()
    wanted_name=str(item.get("file_name") or "").strip().casefold()
    wanted_index=item.get("file_index")

    if wanted_id:
        match=next((f for f in files if str(f.get("model_file_id") or f.get("id") or f.get("file_id") or "").strip()==wanted_id),None)
        if match:
            return match
    if wanted_fp:
        match=next((f for f in files if _download_file_fingerprint(snapshot,f)==wanted_fp),None)
        if match:
            return match
    if wanted_name:
        match=next((f for f in files if str(f.get("name") or f.get("path") or "").split("/")[-1].strip().casefold()==wanted_name),None)
        if match:
            return match
    try:
        wanted_index=int(wanted_index)
    except (TypeError,ValueError):
        wanted_index=-1
    if wanted_index>=0:
        match=next((f for f in files if int(f.get("_download_index",-1))==wanted_index),None)
        if match:
            return match
    return None


def _check_download_watchlist_after_scan(model_id=None, refresh=True):
    items=database.get_download_watchlist()
    if model_id is not None:
        items=[item for item in items if int(item.get("model_id") or 0)==int(model_id)]
    waiting=[item for item in items if str(item.get("status") or "waiting").lower() in {"waiting","error"}]
    stats={"checked":len(waiting),"waiting":0,"ready":[],"errors":0}
    if not waiting:
        return stats
    now=datetime.now(timezone.utc).isoformat()
    refreshed_cache={}
    for item in waiting:
        watch_id=int(item["id"])
        try:
            cache_key=(str(item.get("source") or "").lower(),str(item.get("model_key") or ""))
            snapshot=refreshed_cache.get(cache_key)
            if snapshot is None:
                if refresh:
                    snapshot=_queue_civit_refresh(item)
                else:
                    conn=database.connect()
                    row=conn.execute("SELECT * FROM models WHERE id=?",(int(item.get("model_id") or 0),)).fetchone()
                    conn.close()
                    canonical=dict(row) if row else {}
                    links=[dict(r) for r in database.get_model_sources(int(item.get("model_id") or 0))]
                    link=next((r for r in links if str(r.get("source") or "").lower()==str(item.get("source") or "").lower()),None)
                    if not link:
                        raise RuntimeError("The watched source is no longer attached to this model.")
                    snapshot=_decode_source_snapshot(link,canonical)
                    snapshot["source"]=str(item.get("source") or "").lower()
                    snapshot["model_key"]=str(link.get("model_key") or item.get("model_key") or "")
                refreshed_cache[cache_key]=snapshot
            version=_queue_target_version(snapshot,item)
            if not version:
                database.update_download_watch_item(
                    watch_id,status="error",last_checked=now,
                    last_error="The watched version was not found in refreshed source metadata.",
                )
                stats["errors"]+=1
                continue
            file_data=_watch_target_file(snapshot,version,item)
            if not file_data:
                database.update_download_watch_item(
                    watch_id,status="error",last_checked=now,
                    last_error="The exact watched file was not found. Open the model and choose the file again.",
                )
                stats["errors"]+=1
                continue

            access=str(version.get("access_status") or "").lower()
            file_id=str(file_data.get("model_file_id") or file_data.get("id") or file_data.get("file_id") or "")
            file_name=str(file_data.get("name") or file_data.get("path") or item.get("file_name") or "").split("/")[-1]
            fingerprint=_download_file_fingerprint(snapshot,file_data)
            file_index=int(file_data.get("_download_index", item.get("file_index") or -1))
            size_display=str(file_data.get("size_display") or item.get("file_size_display") or "")
            common=dict(
                last_checked=now,last_error="",file_id=file_id,file_name=file_name,
                file_fingerprint=fingerprint,file_index=file_index,file_size_display=size_display,
            )
            if access=="downloadable":
                database.update_download_watch_item(
                    watch_id,status="available",available_at=now,dismissed_at="",**common
                )
                stats["ready"].append(f"{item.get('model_name') or item.get('model_key')} / {file_name}")
            else:
                database.update_download_watch_item(watch_id,status="waiting",**common)
                stats["waiting"]+=1
        except Exception as exc:
            database.update_download_watch_item(watch_id,status="error",last_checked=now,last_error=str(exc))
            stats["errors"]+=1
    return stats


def _queue_local_install(item, snapshot, version, file_data):
    """Install a queue item through the same Local Installer + Active Downloads pipeline."""
    model_id=int(item.get("model_id") or 0)
    source_name=str(item.get("source") or "").lower()
    file_index=int(file_data.get("_download_index"))
    # Resolve target exactly as /download/source does.
    target=str(file_data.get("download_url") or "").strip()
    if source_name=="civitaired":
        target=_civitaired_download_url(file_data,target)
    else:
        target=_civitai_download_url(file_data,target)
    if not target.startswith(("http://","https://")):
        raise RuntimeError("The released version did not provide a downloadable file URL.")

    conn=database.connect()
    row=conn.execute("SELECT * FROM models WHERE id=?",(model_id,)).fetchone()
    conn.close()
    if not row:
        raise RuntimeError("The queued AbyssBeacon card no longer exists.")
    canonical=dict(row)
    model=dict(snapshot)
    model["id"]=model_id
    for key in ("name","display_name","author","architecture","base_model","model_type","description"):
        if model.get(key) in (None,"") and canonical.get(key) not in (None,""):
            model[key]=canonical.get(key)
    canonical_image=str(canonical.get("image") or "").strip()
    if canonical_image.startswith("/static/cache/previews/"):
        model["_cached_preview"]=canonical_image
    model["_install_preview_url"]=_install_preview_url(model_id,source_name)
    model["_install_preview_video_url"]=_install_preview_video_url(model_id,source_name)

    prefs=load_settings().get("preferences",{})
    if str(prefs.get("download_behavior") or "browser").lower()!="local":
        raise RuntimeError("Download When Available requires Library → Local Installer.")

    filename=str(file_data.get("name") or file_data.get("path") or "Model file").split("/")[-1]
    job_id=active_downloads.create_job(
        model_id=model_id,
        model_name=str(model.get("display_name") or model.get("name") or filename),
        source=source_name,
        filename=filename,
        retry_url=f"/download/source/{model_id}/{source_name}/{file_index}",
        total_bytes=file_data.get("size_bytes") or file_data.get("size") or 0,
    )
    def progress(stage=None, downloaded_bytes=None, total_bytes=None, part_path=None):
        if active_downloads.cancel_requested(job_id):
            raise installer.DownloadCancelled("Download canceled by user.")
        if active_downloads.pause_requested(job_id):
            raise installer.DownloadPaused("Download paused by user.")
        stage=str(stage or "Downloading")
        active_downloads.update(
            job_id,
            status="downloading" if stage in {"Connecting", "Downloading"} else "installing",
            stage=stage,
            downloaded_bytes=downloaded_bytes,
            total_bytes=total_bytes,
            part_path=part_path,
        )
    try:
        result=installer.install_model_file(
            model,file_data,source_name,target,prefs,
            download_headers=_local_download_headers(source_name, model.get("url") or ""),
            progress_callback=progress,
        )
    except installer.DownloadPaused:
        active_downloads.paused(job_id)
        raise
    except installer.DownloadCancelled:
        active_downloads.canceled(job_id)
        raise
    except Exception as exc:
        active_downloads.fail(job_id,str(exc))
        print(
            "DOWNLOAD FAILED:",
            source_name,
            str(model.get("display_name") or model.get("name") or model_id),
            type(exc).__name__,
            str(exc),
        )
        raise
    active_downloads.complete(job_id)

    fingerprint=_download_file_fingerprint(model,file_data)
    source_file_id=file_data.get("model_file_id") or file_data.get("id") or file_data.get("file_id") or ""
    if prefs.get("track_downloads",True) is not False:
        database.record_download(
            model_id,source_name,model.get("model_key"),source_file_id,
            _download_file_key(file_data),
            result.get("filename") or filename,
            _download_record_sha(model,file_data),
            model.get("updated") or "",
            fingerprint,
            file_data.get("version_id") or "",
            file_data.get("version") or "",
        )
    database.record_installed_file(
        model_id,source_name,model.get("model_key"),source_file_id,
        fingerprint,result.get("path"),result.get("filename"),
        file_data.get("version_id") or "",
        file_data.get("version") or "",
    )
    return result


def _check_download_queue_after_scan():
    items=database.get_download_queue()
    stats={"checked":len(items),"waiting":0,"ready":[],"installed":[],"errors":0}
    if not items:
        return stats
    prefs=load_settings().get("preferences",{})
    mode=str(prefs.get("queued_download_behavior") or "ask").lower()
    now=datetime.now(timezone.utc).isoformat()
    for item in items:
        queue_id=int(item["id"])
        try:
            snapshot=_queue_civit_refresh(item)
            version=_queue_target_version(snapshot,item)
            if not version:
                database.update_download_queue_item(
                    queue_id,status="error",last_checked=now,
                    last_error="The queued version was not found in refreshed source metadata.",
                )
                stats["errors"]+=1
                continue
            release_at=str(version.get("early_access_deadline") or item.get("release_at") or "")
            access=str(version.get("access_status") or "").lower()
            file_data=_queue_best_file(version)
            if access!="downloadable" or not file_data:
                database.update_download_queue_item(
                    queue_id,status="waiting",last_checked=now,last_error="",release_at=release_at,
                )
                stats["waiting"]+=1
                continue

            label=f"{item.get('model_name') or item.get('model_key')} / {version.get('name')}"
            if mode=="auto":
                database.update_download_queue_item(queue_id,status="installing",last_checked=now,last_error="",release_at=release_at)
                _queue_local_install(item,snapshot,version,file_data)
                database.update_download_queue_item(queue_id,status="completed",last_checked=now,last_error="")
                stats["installed"].append(label)
            else:
                database.update_download_queue_item(queue_id,status="ready",last_checked=now,last_error="",release_at=release_at)
                stats["ready"].append(label)
        except installer.DownloadCancelled:
            database.update_download_queue_item(queue_id,status="ready",last_checked=now,last_error="Automatic install was canceled.")
            stats["ready"].append(str(item.get("model_name") or item.get("model_key") or "Queued model"))
        except Exception as exc:
            database.update_download_queue_item(queue_id,status="error",last_checked=now,last_error=str(exc))
            stats["errors"]+=1
    return stats


def _print_access_followup_summary(queue_stats=None, watch_stats=None):
    queue_stats=queue_stats or {"checked":0,"waiting":0,"ready":[],"installed":[],"errors":0}
    watch_stats=watch_stats or {"checked":0,"waiting":0,"ready":[],"errors":0}
    early=int(queue_stats.get("checked") or 0)
    paid=int(watch_stats.get("checked") or 0)
    if not early and not paid:
        return

    waiting=int(queue_stats.get("waiting") or 0)+int(watch_stats.get("waiting") or 0)
    print(
        f"ACCESS: scanned {early} Early Access · {paid} Paid Access · {waiting} waiting"
    )
    ready=list(queue_stats.get("ready") or [])+list(watch_stats.get("ready") or [])
    if ready:
        print("  Ready for download: " + "; ".join(ready))
    installed=list(queue_stats.get("installed") or [])
    if installed:
        print("  Installed automatically: " + "; ".join(installed))
    errors=int(queue_stats.get("errors") or 0)+int(watch_stats.get("errors") or 0)
    if errors:
        print(f"  Access errors: {errors} (see Download Manager)")



@app.route("/api/download-queue", methods=["GET","POST"])
def download_queue_api():
    if request.method=="GET":
        return {"success":True,"items":database.get_download_queue()}
    data=request.get_json(silent=True) or {}
    model_id=int(data.get("model_id") or 0)
    source=str(data.get("source") or "").lower()
    version_id=str(data.get("version_id") or "")
    version_name=str(data.get("version_name") or "")
    if source not in {"civitai","civitaired"}:
        return {"success":False,"error":"Download When Available currently supports CivitAI and CivitAI Red."},400
    conn=database.connect()
    row=conn.execute("SELECT * FROM models WHERE id=?",(model_id,)).fetchone()
    conn.close()
    if not row:
        return {"success":False,"error":"Model not found."},404
    canonical=dict(row)
    links=[dict(r) for r in database.get_model_sources(model_id)]
    link=next((r for r in links if str(r.get("source") or "").lower()==source),None)
    if not link:
        return {"success":False,"error":"This source is no longer attached to the model."},404
    model_key=str(link.get("model_key") or "")
    release_at=str(data.get("release_at") or "")
    database.add_download_queue_item(
        model_id,source,model_key,version_id,version_name,
        canonical.get("display_name") or canonical.get("name") or model_key,
        link.get("url") or "",release_at,
    )
    return {"success":True,"items":database.get_download_queue()}


@app.route("/api/download-queue/<int:queue_id>", methods=["DELETE"])
def download_queue_remove(queue_id):
    return {"success":True,"deleted":database.remove_download_queue_item(queue_id)}


@app.route("/api/download-queue/<int:queue_id>/install", methods=["POST"])
def download_queue_install(queue_id):
    item=next((x for x in database.get_download_queue() if int(x.get("id") or 0)==queue_id),None)
    if not item:
        return {"success":False,"error":"Queue item not found."},404
    try:
        snapshot=_queue_civit_refresh(item)
        version=_queue_target_version(snapshot,item)
        if not version or str(version.get("access_status") or "").lower()!="downloadable":
            database.update_download_queue_item(queue_id,status="waiting",last_checked=datetime.now(timezone.utc).isoformat())
            return {"success":False,"error":"This version is not downloadable yet."},409
        file_data=_queue_best_file(version)
        if not file_data:
            return {"success":False,"error":"No downloadable model file was found for this version."},409
        database.update_download_queue_item(queue_id,status="installing",last_error="")
        result=_queue_local_install(item,snapshot,version,file_data)
        database.update_download_queue_item(queue_id,status="completed",last_checked=datetime.now(timezone.utc).isoformat(),last_error="")
        return {"success":True,**result}
    except Exception as exc:
        database.update_download_queue_item(queue_id,status="ready",last_error=str(exc))
        return {"success":False,"error":str(exc)},502


@app.route("/api/download-watchlist", methods=["GET","POST"])
def download_watchlist_api():
    if request.method=="GET":
        return {"success":True,"items":database.get_download_watchlist()}
    data=request.get_json(silent=True) or {}
    model_id=int(data.get("model_id") or 0)
    source=str(data.get("source") or "").lower()
    if source not in {"civitai","civitaired"}:
        return {"success":False,"error":"Paid-file watching currently supports CivitAI and CivitAI Red."},400
    conn=database.connect()
    row=conn.execute("SELECT * FROM models WHERE id=?",(model_id,)).fetchone()
    conn.close()
    if not row:
        return {"success":False,"error":"Model not found."},404
    canonical=dict(row)
    links=[dict(r) for r in database.get_model_sources(model_id)]
    link=next((r for r in links if str(r.get("source") or "").lower()==source),None)
    if not link:
        return {"success":False,"error":"This source is no longer attached to the model."},404

    snapshot=_decode_source_snapshot(link,canonical)
    snapshot["source"]=source
    snapshot["model_key"]=str(link.get("model_key") or "")
    version=_queue_target_version(snapshot,data)
    if not version:
        return {"success":False,"error":"That version is no longer present in the cached source metadata."},409
    if str(version.get("access_status") or "").lower()!="paid_access":
        return {"success":False,"error":"Watchlist is only for files that currently require paid access."},409
    file_data=_watch_target_file(snapshot,version,data)
    if not file_data:
        return {"success":False,"error":"The exact file could not be identified. Reload the model and try again."},409

    model_key=str(link.get("model_key") or "")
    file_id=str(file_data.get("model_file_id") or file_data.get("id") or file_data.get("file_id") or "")
    file_name=str(file_data.get("name") or file_data.get("path") or "Model file").split("/")[-1]
    database.add_download_watch_item(
        model_id,source,model_key,str(version.get("id") or data.get("version_id") or ""),
        str(version.get("name") or data.get("version_name") or ""),
        canonical.get("display_name") or canonical.get("name") or model_key,
        link.get("url") or "",file_id,file_name,_download_file_fingerprint(snapshot,file_data),
        int(file_data.get("_download_index",-1)),str(file_data.get("size_display") or ""),
    )
    return {"success":True,"items":database.get_download_watchlist()}


@app.route("/api/download-watchlist/<int:watch_id>", methods=["DELETE"])
def download_watchlist_remove(watch_id):
    return {"success":True,"deleted":database.remove_download_watch_item(watch_id)}


@app.route("/api/download-watchlist/notifications", methods=["GET"])
def download_watchlist_notifications():
    return {"success":True,"items":database.get_download_watchlist(include_dismissed=False)}


@app.route("/api/download-watchlist/<int:watch_id>/dismiss", methods=["POST"])
def download_watchlist_dismiss(watch_id):
    return {
        "success":True,
        "updated":database.update_download_watch_item(
            watch_id,dismissed_at=datetime.now(timezone.utc).isoformat()
        ),
    }


@app.route("/api/model/<int:model_id>/refresh-download-source/<source>", methods=["POST"])
def refresh_download_source(model_id, source, _batch=False):
    """Reload one source/model snapshot without running a broad source scan."""
    source_name = str(source or "").strip().lower()
    supported_sources = {
        "huggingface", "modelscope", "civitai", "civitaired", "tensorhub", "seaart"
    }
    if source_name not in supported_sources:
        return {
            "success": False,
            "error": f"Direct model reload is not available for {source_label(source_name)} yet.",
        }, 400

    links = [dict(row) for row in database.get_model_sources(model_id)]
    link = next(
        (
            row for row in links
            if str(row.get("source") or "").strip().lower() == source_name
        ),
        None,
    )
    label = source_label(source_name)
    if not link:
        return {
            "success": False,
            "error": f"{label} source link was not found for this card.",
        }, 404

    model_key = str(link.get("model_key") or "").strip()
    if not model_key:
        return {
            "success": False,
            "error": f"{label} model identity is missing.",
        }, 400

    try:
        try:
            snapshot = json.loads(link.get("source_data") or "{}")
            if not isinstance(snapshot, dict):
                snapshot = {}
        except Exception:
            snapshot = {}

        source_url = str(link.get("url") or "").strip()
        files = []
        gated = False
        refreshed_card_data = {}

        if source_name == "modelscope":
            from scanners import modelscope as modelscope_scanner

            modelscope_scanner._apply_auth()
            details = modelscope_scanner.get_details(model_key)
            if not isinstance(details, dict):
                details = {}

            revision = str(
                details.get("Revision")
                or details.get("revision")
                or "master"
            ).strip() or "master"

            versions_meta, version_files = modelscope_scanner.extract_versions_from_details(details, model_key)
            files = version_files or modelscope_scanner.get_files(model_key, revision)
            if not isinstance(files, list):
                files = []

            refreshed_card_data = dict(snapshot.get("card_data") or {})
            refreshed_card_data["versions"] = versions_meta
            ms_card = refreshed_card_data.get("modelscope")
            ms_card = dict(ms_card) if isinstance(ms_card, dict) else {}
            ms_card["versions"] = versions_meta
            refreshed_card_data["modelscope"] = ms_card
            snapshot["card_data"] = refreshed_card_data

            version_media = modelscope_scanner.extract_media_from_details(details, model_key)
            media_by_url = {str(item.get("url") or ""): item for item in version_media if isinstance(item, dict) and item.get("url")}
            conn = database.connect(); conn.row_factory = sqlite3.Row
            media_rows = conn.execute(
                "SELECT id,url,metadata FROM model_media WHERE model_id=? AND lower(source)='modelscope'",
                (model_id,),
            ).fetchall(); conn.close()
            for media_row in media_rows:
                incoming = media_by_url.get(str(media_row["url"] or ""))
                if not incoming: continue
                try:
                    stored_meta = json.loads(media_row["metadata"] or "{}")
                    if not isinstance(stored_meta, dict): stored_meta = {}
                except Exception:
                    stored_meta = {}
                stored_meta.update(dict(incoming.get("metadata") or {}))
                database.update_media_metadata(media_row["id"], model_id, stored_meta)

            try:
                gated = bool(modelscope_scanner.detect_gated_model({}, details))
            except Exception:
                gated = bool(details.get("Private") or details.get("private"))

            snapshot["files"] = files
            snapshot["gated"] = int(gated)

            updated = (
                details.get("LastUpdatedTime")
                or details.get("last_modified")
                or details.get("updated_at")
                or ""
            )
            if updated:
                try:
                    updated = modelscope_scanner.normalize_timestamp(updated)
                except Exception:
                    updated = str(updated)
                snapshot["updated"] = updated

            description = (
                details.get("description")
                or details.get("Description")
                or details.get("ModelDescription")
                or details.get("model_description")
                or ""
            )
            if description:
                snapshot["description"] = str(description)

            source_url = source_url or f"https://modelscope.cn/models/{model_key}"

        elif source_name == "huggingface":
            from scanners import huggingface as huggingface_scanner

            huggingface_scanner._apply_auth()
            response = huggingface_scanner.get_with_backoff(
                huggingface_scanner.session,
                f"https://huggingface.co/api/models/{model_key}",
                provider="Hugging Face",
                label=f"reload model detail {model_key}",
                params={"blobs": "true"},
                timeout=15,
            )

            source_url = source_url or f"https://huggingface.co/{model_key}"
            if response.status_code in (401, 403):
                return {
                    "success": False,
                    "restricted": True,
                    "source_label": "Hugging Face",
                    "source_url": source_url,
                    "action_label": "Open on Hugging Face",
                    "error": (
                        "Hugging Face still does not expose this repository's metadata "
                        "to the currently configured account/token."
                    ),
                }, response.status_code
            if response.status_code != 200:
                return {
                    "success": False,
                    "source_label": "Hugging Face",
                    "source_url": source_url,
                    "action_label": "Open on Hugging Face",
                    "error": f"Hugging Face reload failed (HTTP {response.status_code}).",
                }, 502

            details = response.json()
            if not isinstance(details, dict):
                details = {}

            gated = bool(details.get("gated"))
            card_data = details.get("cardData", {}) or {}
            if not isinstance(card_data, dict):
                card_data = {}
            card_data["gated"] = gated

            files = []
            for sibling in details.get("siblings", []) or []:
                if not isinstance(sibling, dict):
                    continue
                filename = str(sibling.get("rfilename") or "").strip()
                if not filename:
                    continue

                lower_name = filename.lower()
                lfs = sibling.get("lfs", {}) or {}
                size = sibling.get("size", 0) or lfs.get("size", 0) or 0
                primary = lower_name.endswith((
                    ".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf"
                ))
                encoded_path = quote(filename, safe="/")
                resolve_url = f"https://huggingface.co/{model_key}/resolve/main/{encoded_path}"

                files.append({
                    "name": filename.split("/")[-1],
                    "path": filename,
                    "size": size,
                    "size_bytes": size,
                    "sha256": lfs.get("sha256", ""),
                    "is_lfs": bool(lfs),
                    "revision": details.get("sha", "") or "main",
                    "download_url": f"{resolve_url}?download=true",
                    "media_url": resolve_url,
                    "primary": primary,
                })

            hf_access_granted = False
            hf_access_checked = False
            hf_access_status = None
            hf_access_state = "unconfirmed"
            probe_candidates = [item for item in files if item.get("primary")] or list(files)

            if probe_candidates:
                probe_url = str(probe_candidates[0].get("media_url") or "").strip()
                if probe_url:
                    probe_response = None
                    try:
                        probe_response = huggingface_scanner.session.get(
                            probe_url,
                            headers={"Range": "bytes=0-0"},
                            stream=True,
                            allow_redirects=True,
                            timeout=15,
                        )
                        hf_access_status = probe_response.status_code
                        if probe_response.status_code in (200, 206):
                            hf_access_checked = True
                            hf_access_granted = True
                            hf_access_state = "downloadable"
                        elif probe_response.status_code in (401, 403):
                            hf_access_checked = True
                            hf_access_granted = False
                            hf_access_state = "gated"
                    except Exception:
                        hf_access_state = "unconfirmed"
                    finally:
                        if probe_response is not None:
                            try:
                                probe_response.close()
                            except Exception:
                                pass

            card_data["hf_download_access"] = hf_access_state
            # Persist the definitive denial/grant in the same metadata the
            # detail/download UI reads later. API-level `gated` can remain true
            # even for an account that has been granted access, so the probe
            # is intentionally authoritative when available.
            snapshot["files"] = files
            snapshot["gated"] = int(hf_access_state == "gated" if hf_access_checked else gated)
            snapshot["hf_access_granted"] = int(hf_access_granted)
            snapshot["hf_access_checked"] = int(hf_access_checked)
            snapshot["card_data"] = card_data
            if details.get("sha"):
                snapshot["sha"] = str(details.get("sha"))
            if details.get("lastModified"):
                snapshot["updated"] = str(details.get("lastModified"))
            description = metadata.extract_description(details)
            if description:
                snapshot["description"] = str(description)

        elif source_name == "civitai":
            from scanners import civitai as civitai_scanner

            civitai_scanner._apply_auth()
            # Preserve the revision this CivitAI source snapshot represents.
            # Reload must refresh that exact modelVersionId rather than turning
            # the parent model's historical galleries into one combined gallery.
            previous_card = snapshot.get("card_data") or {}
            if isinstance(previous_card, str):
                try:
                    previous_card = json.loads(previous_card or "{}")
                except Exception:
                    previous_card = {}
            selected_version_id = str((previous_card or {}).get("version_id") or "").strip()

            # A previous broad scan may have disabled optional detail hydration
            # after a 429. An explicit user reload gets one fresh detail attempt.
            civitai_scanner._DETAIL_ENRICHMENT_DISABLED = False
            details = civitai_scanner._fetch_model_detail(model_key)
            if not isinstance(details, dict) or not details:
                return {
                    "success": False,
                    "source_label": label,
                    "source_url": source_url or f"https://civitai.com/models/{model_key}",
                    "action_label": f"Open on {label}",
                    "error": "CivitAI did not return fresh model details. Try again in a moment.",
                }, 502

            details["_force_page"] = True
            details["_force_version"] = True
            if selected_version_id:
                details["_selected_version_id"] = selected_version_id


            refreshed = civitai_scanner._build_model(
                details,
                enrich=False,
                include_mature_media=_civitai_include_mature_media_enabled(),
                media_limit=_civitai_media_limit(),
            )
            snapshot = refreshed.as_dict()
            files = list(refreshed.files or [])
            gated = bool(refreshed.gated)
            refreshed_card_data = refreshed.card_data or {}
            source_url = source_url or refreshed.url or f"https://civitai.com/models/{model_key}"

        elif source_name == "civitaired":
            from scanners import civitaired as civitaired_scanner

            card = snapshot.get("card_data") or {}
            if isinstance(card, str):
                try:
                    card = json.loads(card or "{}")
                except Exception:
                    card = {}
            version_id = str((card or {}).get("version_id") or "").strip()
            seed = {"id": model_key, "_force_red_page": True}
            if version_id:
                seed["version"] = {"id": version_id}

            refreshed = civitaired_scanner._build_model(seed, enrich=True)
            if not refreshed or not str(getattr(refreshed, "model_key", "") or "").strip():
                return {
                    "success": False,
                    "source_label": label,
                    "source_url": source_url or f"https://civitai.red/models/{model_key}",
                    "action_label": f"Open on {label}",
                    "error": "CivitAI Red did not return fresh model details.",
                }, 502

            snapshot = refreshed.as_dict()
            files = list(refreshed.files or [])
            gated = bool(refreshed.gated)
            refreshed_card_data = refreshed.card_data or {}
            source_url = source_url or refreshed.url or f"https://civitai.red/models/{model_key}"

        elif source_name == "tensorhub":
            from scanners import tensorhub as tensorhub_scanner
            from scanners.common.model import Model as ScannerModel

            tensorhub_scanner._apply_auth()
            seed = ScannerModel()
            for key, value in snapshot.items():
                if hasattr(seed, key):
                    setattr(seed, key, value)
            seed.source = "tensorhub"
            seed.model_key = model_key
            seed.url = source_url or str(snapshot.get("url") or "")
            if not isinstance(seed.card_data, dict):
                try:
                    seed.card_data = json.loads(seed.card_data or "{}")
                except Exception:
                    seed.card_data = {}

            refreshed, ok, reason = tensorhub_scanner._fetch_public_detail(
                seed,
                force_access_probe=True,
            )
            if not ok:
                return {
                    "success": False,
                    "source_label": label,
                    "source_url": source_url or f"https://tensorhub.art/models/{model_key}",
                    "action_label": f"Open on {label}",
                    "error": f"TensorHub model reload failed: {reason}",
                }, 502

            snapshot = refreshed.as_dict()
            files = list(refreshed.files or [])
            gated = bool(refreshed.gated)
            refreshed_card_data = refreshed.card_data or {}
            source_url = source_url or refreshed.url or f"https://tensorhub.art/models/{model_key}"

            # TensorHub's public detail API has a third "unconfirmed" access
            # state. Resolve that ambiguity the same way we do for Hugging Face:
            # ask the authenticated signed-URL endpoint for one real model file,
            # then fetch only the first byte of the returned URL. This confirms
            # actual download access without downloading the model.
            tensor_access_checked = False
            tensor_access_granted = False
            tensor_access_status = "unconfirmed"

            # The scanner's enriched detail response can identify a real paid
            # PROJECT_DOWNLOAD even though the signed-file endpoint only says
            # that access is denied. Preserve that stronger paid classification
            # through Reload Model instead of collapsing it back to "gated".
            scanner_tensor_card = (refreshed_card_data.get("tensorhub") or {}) if isinstance(refreshed_card_data, dict) else {}
            scanner_tensor_access = str(scanner_tensor_card.get("download_access") or "").strip().lower()
            scanner_versions = (refreshed_card_data.get("versions") or []) if isinstance(refreshed_card_data, dict) else []
            scanner_has_paid_version = any(
                isinstance(version, dict)
                and str(version.get("access_status") or "").strip().lower() == "paid_access"
                for version in scanner_versions
            )
            scanner_paid_access = scanner_tensor_access in {"paid_access", "paid", "buffet"} or scanner_has_paid_version

            tensor_probe_http = None
            tensor_signed_http = None
            tensor_probe_reason = ""

            tensor_candidates = [
                item for item in files
                if isinstance(item, dict) and str(item.get("model_file_id") or "").strip()
            ]
            if tensor_candidates:
                model_file_id = str(tensor_candidates[0].get("model_file_id") or "").strip()
                token = get_source_token("tensorhub")
                if token:
                    signed_response = None
                    probe_response = None
                    try:
                        signed_response = requests.get(
                            "https://api.tensorhub.art/community-web/v1/model/file/url",
                            params={"modelFileId": model_file_id, "useTcdn": "true"},
                            headers={
                                "Authorization": f"Bearer {token}",
                                "Cookie": f"ta_token_prod={token}",
                                "Accept": "*/*",
                                "Origin": "https://tensorhub.art",
                                "Referer": source_url or "https://tensorhub.art/",
                                "X-Request-Package-Id": "3023",
                                "X-Request-Lang": "en-US",
                                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:153.0) Gecko/20100101 Firefox/153.0",
                            },
                            timeout=15,
                        )
                        tensor_signed_http = signed_response.status_code

                        if signed_response.status_code in (401, 403):
                            tensor_access_checked = True
                            tensor_access_status = "gated"
                            tensor_probe_reason = f"signed URL HTTP {signed_response.status_code}"
                        elif signed_response.status_code == 200:
                            try:
                                payload = signed_response.json()
                            except Exception:
                                payload = {}
                            signed_url = str(((payload.get("data") or {}).get("url") or "")).strip()
                            api_ok = str(payload.get("code", "")) == "0"

                            if api_ok and signed_url.startswith(("https://", "http://")):
                                tensor_access_checked = True
                                probe_response = requests.get(
                                    signed_url,
                                    headers={"Range": "bytes=0-0"},
                                    stream=True,
                                    allow_redirects=True,
                                    timeout=15,
                                )
                                tensor_probe_http = probe_response.status_code
                                if probe_response.status_code in (200, 206):
                                    tensor_access_granted = True
                                    tensor_access_status = "downloadable"
                                elif probe_response.status_code in (401, 403):
                                    tensor_access_status = "gated"
                                    tensor_probe_reason = f"file probe HTTP {probe_response.status_code}"
                                else:
                                    tensor_access_checked = False
                                    tensor_access_status = "unconfirmed"
                                    tensor_probe_reason = f"file probe HTTP {probe_response.status_code}"
                            else:
                                # The same endpoint used for real TensorHub downloads
                                # accepted the request but declined to issue a file URL.
                                # That is a definitive no-access signal for this file.
                                tensor_access_checked = True
                                tensor_access_status = "gated"
                                tensor_probe_reason = str(payload.get("message") or payload.get("msg") or "signed URL not issued")
                        else:
                            tensor_probe_reason = f"signed URL HTTP {signed_response.status_code}"
                    except Exception as exc:
                        tensor_probe_reason = str(exc)
                    finally:
                        if probe_response is not None:
                            try:
                                probe_response.close()
                            except Exception:
                                pass
                        if signed_response is not None:
                            try:
                                signed_response.close()
                            except Exception:
                                pass
                else:
                    tensor_probe_reason = "TensorHub account session is not configured"
            else:
                tensor_probe_reason = "no TensorHub model file ID was available to test"

            # A failed/denied signed URL proves that the account cannot
            # download the file, but it does not tell us *why*. If model/detail
            # already proved this is paid access, keep the paid state.
            if tensor_access_status == "gated" and scanner_paid_access:
                tensor_access_status = "paid_access"

            if not isinstance(refreshed_card_data, dict):
                refreshed_card_data = {}
            tensor_card = refreshed_card_data.get("tensorhub")
            if not isinstance(tensor_card, dict):
                tensor_card = {}
                refreshed_card_data["tensorhub"] = tensor_card
            tensor_card["download_access"] = tensor_access_status
            parent_access = tensor_card.get("access")
            if tensor_access_status == "paid_access" and isinstance(parent_access, dict):
                parent_access["status"] = "paid_access"
                parent_access["downloadable"] = False
                parent_access["authoritative"] = True
            tensor_card["access_probe_checked"] = bool(tensor_access_checked)
            tensor_card["access_probe_http"] = tensor_probe_http
            tensor_card["signed_url_http"] = tensor_signed_http
            snapshot["card_data"] = refreshed_card_data
            if tensor_access_checked:
                gated = not tensor_access_granted
                snapshot["gated"] = int(gated)

        elif source_name == "seaart":
            from scanners import seaart as seaart_scanner

            details = seaart_scanner._detail(model_key)
            if not isinstance(details, dict) or not details:
                return {
                    "success": False,
                    "source_label": label,
                    "source_url": source_url or f"https://www.seaart.ai/models/detail/{model_key}",
                    "action_label": f"Open on {label}",
                    "error": "SeaArt did not return fresh model details.",
                }, 502

            refreshed = seaart_scanner._build(details)
            if refreshed is None:
                return {
                    "success": False,
                    "source_label": label,
                    "source_url": source_url or f"https://www.seaart.ai/models/detail/{model_key}",
                    "action_label": f"Open on {label}",
                    "error": "SeaArt returned model details that AbyssBeacon could not parse.",
                }, 502

            snapshot = refreshed.as_dict()
            files = list(refreshed.files or [])
            gated = bool(refreshed.gated)
            refreshed_card_data = refreshed.card_data or {}
            source_url = source_url or refreshed.url or f"https://www.seaart.ai/models/detail/{model_key}"

        database.refresh_model_source_snapshot(
            model_id,
            source_name,
            model_key,
            snapshot,
            url=source_url,
        )

        if source_name in {"civitai", "civitaired"}:
            try:
                refreshed_media = list(getattr(refreshed, "media", []) or [])
                # Explicit reload is authoritative for this provider's own
                # gallery, including an empty result. Merged source galleries
                # remain independent; only the canonical provider updates the
                # storage-level models.image/media summary columns.
                database.refresh_canonical_model_media(
                    model_id,
                    source_name,
                    refreshed_media,
                    fallback_image=str(getattr(refreshed, "image", "") or ""),
                )
            except Exception as media_exc:
                print(f"{label} media refresh skipped: {media_exc}")

        primary_files = [
            item for item in files
            if isinstance(item, dict) and (
                item.get("primary")
                or str(item.get("path") or item.get("name") or "").casefold().endswith(
                    (".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf", ".onnx")
                )
            )
        ]

        if source_name == "huggingface":
            access_word = (
                "granted" if hf_access_granted
                else "gated" if hf_access_checked
                else "unconfirmed"
            )
            if not _batch:
                print(
                    f"RELOAD MODEL: {label} model_id={model_id} key={model_key} "
                    f"files={len(files)} model_files={len(primary_files)} "
                    f"repository_access={access_word} probe_http={hf_access_status} "
                    f"source_gated={bool(gated)}"
                )

            if hf_access_granted:
                return {
                    "success": True,
                    "download_ready": True,
                    "restricted": False,
                    "source_label": label,
                    "source_url": source_url,
                    "message": (
                        "Hugging Face model reloaded. Repository download access "
                        "is available for this account."
                    ),
                    "files": len(files),
                    "primary_files": len(primary_files),
                    "gated": gated,
                    "repository_access": "granted",
                }

            if hf_access_checked:
                return {
                    "success": True,
                    "download_ready": False,
                    "restricted": True,
                    "source_label": label,
                    "source_url": source_url,
                    "message": (
                        "Hugging Face model reloaded. Repository download access "
                        "is still gated for this account."
                    ),
                    "files": len(files),
                    "primary_files": len(primary_files),
                    "gated": gated,
                    "repository_access": "gated",
                }

            return {
                "success": True,
                "download_ready": False,
                "restricted": bool(gated),
                "source_label": label,
                "source_url": source_url,
                "message": (
                    "Hugging Face model reloaded, but repository download access "
                    "could not be confirmed because there was no file available to test."
                ),
                "files": len(files),
                "primary_files": len(primary_files),
                "gated": gated,
                "repository_access": "unconfirmed",
            }

        # Source-aware result text. These sources expose access in different
        # ways, so avoid pretending a raw file count means the same thing for all.
        card_for_access = refreshed_card_data or snapshot.get("card_data") or {}
        access_state = _source_access_status(source_name, gated, card_for_access)
        if not _batch:
            print(
                f"RELOAD MODEL: {label} model_id={model_id} key={model_key} "
                f"files={len(files)} model_files={len(primary_files)} access={access_state} gated={bool(gated)}"
            )
            if source_name == "tensorhub":
                print(
                    f"  TensorHub access probe: repository_access={tensor_access_status} "
                    f"file_url_http={tensor_signed_http} probe_http={tensor_probe_http} "
                    f"reason={tensor_probe_reason or '-'}"
                )

        if source_name == "modelscope":
            if primary_files:
                message = (
                    f"ModelScope model reloaded. Found {len(primary_files)} downloadable "
                    f"model file{'s' if len(primary_files) != 1 else ''}."
                )
            else:
                message = (
                    "ModelScope model reloaded. Download access is still restricted for this account."
                    if gated
                    else "ModelScope model reloaded. No downloadable model file is currently exposed."
                )

        elif source_name in {"civitai", "civitaired"}:
            if access_state == "early_access":
                message = f"{label} model reloaded. The current version is still in Early Access."
            elif access_state == "paid_access":
                message = f"{label} model reloaded. The current version requires paid access."
            elif access_state == "gated":
                message = f"{label} model reloaded. The current version is still restricted."
            elif primary_files:
                message = f"{label} model reloaded. The current version is available for download."
            else:
                message = f"{label} model reloaded. No downloadable model file is currently exposed."

        elif source_name == "tensorhub":
            # Prefer the real signed-URL/one-byte probe over TensorHub's metadata
            # access flag. The metadata can legitimately be "unconfirmed" even
            # when the download endpoint can make a definitive decision.
            if tensor_access_status == "downloadable":
                access_state = "downloadable"
                message = "TensorHub Art model reloaded. Download access is available for this account."
            elif tensor_access_status == "gated":
                # The byte probe only tells us that access is blocked. Preserve
                # a stronger paid-access classification from TensorHub metadata.
                if access_state == "paid_access":
                    message = "TensorHub Art model reloaded. This version requires paid access."
                else:
                    access_state = "gated"
                    message = "TensorHub Art model reloaded. Download access is still restricted for this account."
            else:
                message = (
                    "TensorHub Art model reloaded, but download access could not be confirmed "
                    f"({tensor_probe_reason or 'the access probe did not return a definitive result'})."
                )

        elif source_name == "seaart":
            sea = card_for_access.get("seaart") if isinstance(card_for_access, dict) else {}
            sea = sea if isinstance(sea, dict) else {}
            if access_state == "downloadable":
                if sea.get("session_can_download"):
                    message = "SeaArt model reloaded. This model is downloadable and a download session is configured."
                else:
                    message = "SeaArt model reloaded. This model is downloadable at SeaArt; a valid account session may still be required."
            elif access_state == "gated":
                message = "SeaArt model reloaded. The source still does not allow downloads for this model."
            else:
                message = "SeaArt model reloaded. Download availability is still unconfirmed."
        else:
            message = f"{label} model reloaded."

        return {
            "success": True,
            "download_ready": bool(primary_files) and access_state not in {"gated", "early_access", "paid_access"},
            "restricted": access_state in {"gated", "early_access", "paid_access"},
            "source_label": label,
            "source_url": source_url,
            "message": message,
            "files": len(files),
            "primary_files": len(primary_files),
            "gated": gated,
            "access": access_state,
        }

    except Exception as exc:
        return {
            "success": False,
            "error": f"{label} reload failed: {exc}",
            "source_label": label,
            "source_url": str(link.get("url") or ""),
            "action_label": f"Open on {label}",
        }, 502


@app.route("/api/model/<int:model_id>/refresh-download-sources", methods=["POST"])
def refresh_download_sources(model_id):
    """Reload every supported source snapshot attached to one canonical card."""
    supported_sources = {
        "huggingface", "modelscope", "civitai", "civitaired", "tensorhub", "seaart"
    }
    links = [dict(row) for row in database.get_model_sources(model_id)]

    # Preserve source-link order, but never reload the same source twice.
    sources = []
    seen = set()
    for link in links:
        source_name = str(link.get("source") or "").strip().lower()
        if source_name in supported_sources and source_name not in seen:
            seen.add(source_name)
            sources.append(source_name)

    if not sources:
        return {
            "success": False,
            "error": "No reloadable sources are attached to this model.",
        }, 404

    conn = database.connect()
    try:
        row = conn.execute(
            "SELECT display_name, name FROM models WHERE id=?", (model_id,)
        ).fetchone()
    finally:
        conn.close()
    model_name = (
        (row["display_name"] or row["name"]) if row else f"Model {model_id}"
    )

    print(f"RELOAD MODEL: {model_name} ({model_id})")

    results = []
    refreshed_count = 0
    for source_name in sources:
        label = source_label(source_name)
        try:
            result = refresh_download_source(model_id, source_name, _batch=True)
            status_code = 200
            payload = result
            if isinstance(result, tuple):
                payload, status_code = result[0], result[1]
            if not isinstance(payload, dict):
                payload = {"success": False, "error": "Unexpected reload response."}

            ok = bool(payload.get("success")) and int(status_code or 200) < 400
            if ok:
                refreshed_count += 1
                files = int(payload.get("files") or 0)
                access = str(payload.get("access") or payload.get("repository_access") or "").strip()
                detail = f"refreshed · {files} file{'s' if files != 1 else ''}"
                if access:
                    detail += f" · {access}"
                print(f"  {label:<14}: {detail}")
            else:
                error = str(payload.get("error") or payload.get("message") or f"HTTP {status_code}").strip()
                print(f"  {label:<14}: failed · {error}")

            results.append({
                "source": source_name,
                "source_label": label,
                "success": ok,
                "status": int(status_code or 200),
                "files": int(payload.get("files") or 0),
                "access": payload.get("access") or payload.get("repository_access") or "",
                "restricted": bool(payload.get("restricted")),
                "source_url": payload.get("source_url") or "",
                "message": payload.get("message") or "",
                "error": payload.get("error") or "",
            })
        except Exception as exc:
            print(f"  {label:<14}: failed · {exc}")
            results.append({
                "source": source_name,
                "source_label": label,
                "success": False,
                "status": 500,
                "files": 0,
                "access": "",
                "restricted": False,
                "source_url": "",
                "message": "",
                "error": str(exc),
            })

    print(f"RELOAD COMPLETE: {refreshed_count}/{len(sources)} sources refreshed")

    if refreshed_count:
        try:
            _check_download_watchlist_after_scan(model_id=model_id, refresh=False)
        except Exception as watch_exc:
            print(f"Download watchlist reload check skipped: {watch_exc}")

    failed = [item for item in results if not item["success"]]
    if refreshed_count:
        message = f"Reloaded {refreshed_count} of {len(sources)} source{'s' if len(sources) != 1 else ''}."
        if failed:
            message += " Some sources could not be refreshed."
        return {
            "success": True,
            "partial": bool(failed),
            "message": message,
            "refreshed": refreshed_count,
            "total": len(sources),
            "results": results,
        }

    return {
        "success": False,
        "error": "None of this model's attached sources could be refreshed.",
        "refreshed": 0,
        "total": len(sources),
        "results": results,
    }, 502


def _download_sidecar_preferences(preferences):
    """Apply one-download sidecar overrides without changing saved defaults."""
    prefs = dict(preferences or {})
    for query_name, pref_name in (("save_info", "save_model_info"), ("save_preview", "save_model_preview")):
        raw = request.args.get(query_name)
        if raw is not None:
            prefs[pref_name] = str(raw).strip().lower() in {"1", "true", "yes", "on"}
    return prefs


@app.route("/download/source/<int:model_id>/<source>/<int:file_index>")
def tracked_source_download(model_id, source, file_index):
    conn = database.connect(); row = conn.execute("SELECT * FROM models WHERE id=?", (model_id,)).fetchone(); conn.close()
    if not row: return "Model not found.", 404
    canonical = dict(row)
    links = [dict(r) for r in database.get_model_sources(model_id)]
    link = next((r for r in links if str(r.get("source") or "").lower() == str(source or "").lower()), None)
    if not link: return "Download source not found.", 404
    model = _decode_source_snapshot(link, canonical)
    source_page_url = str(link.get("url") or model.get("url") or canonical.get("url") or "").strip()

    # Source snapshots may retain the original remote image URL even though the
    # canonical card has already been cached under static/cache/previews. Pass
    # that cached card image through explicitly so local installs can copy it.
    canonical_image = str(canonical.get("image") or "").strip()
    if canonical_image.startswith("/static/cache/previews/"):
        model["_cached_preview"] = canonical_image
    model["_install_preview_url"] = _install_preview_url(model_id, source)
    model["_install_preview_video_url"] = _install_preview_video_url(model_id, source)

    if not model.get("files") and str(source).lower() == str(canonical.get("source") or "").lower():
        model["files"] = canonical.get("files") or []
    file_data = _load_download_file(model, file_index)
    if not file_data: return "Download file not found for this source. Rescan this source to refresh its download metadata.", 404

    source_name = str(source or "").lower()

    # Paid CivitAI/CivitAI Red versions use the release queue instead of a
    # speculative direct request. CivitAI can return a small HTML/error payload
    # from a protected download URL; without this guard that response can be
    # mistaken for the requested .safetensors file.
    if source_name in {"civitai", "civitaired"}:
        paid_version = None
        for version in _source_version_groups(model):
            for version_file in version.get("files", []):
                if int(version_file.get("_download_index", -1)) == int(file_index):
                    paid_version = version
                    break
            if paid_version is not None:
                break
        if paid_version and str(paid_version.get("access_status") or "").lower() == "paid_access":
            payload = _restricted_download_payload(
                model,
                source_name,
                source_page_url,
                "This paid version is waiting for access. Add it with Download when available; AbyssBeacon will recheck it after scans.",
            )
            payload["paid_access"] = True
            payload["queue_available"] = True
            return payload, 403

    try:
        if source_name == "tensorhub" and file_data.get("model_file_id"):
            target = _tensorhub_signed_download_url(file_data.get("model_file_id"))
        elif source_name == "seaart" and file_data.get("model_ver_no"):
            target = _seaart_dynamic_download_url(file_data.get("model_ver_no"))
        else:
            target = str(file_data.get("download_url") or "").strip()
            if not target and source_name == "huggingface" and model.get("model_key"):
                filename = str(file_data.get("path") or file_data.get("name") or "")
                if filename: target = f"https://huggingface.co/{model['model_key']}/resolve/main/{quote(filename, safe='/')}?download=true"
        if source_name == "civitaired":
            target = _civitaired_download_url(file_data, target)
        elif source_name == "civitai":
            target = _civitai_download_url(file_data, target)
        elif source_name == "huggingface":
            target = _huggingface_download_url(model, file_data, target)
        elif source_name == "modelscope":
            target = _modelscope_download_url(model, file_data, target)
        if not target.startswith(("https://", "http://")):
            return "Direct download is unavailable for this source/file.", 502
        prefs = _download_sidecar_preferences(load_settings().get("preferences", {}))
        fingerprint = _download_file_fingerprint(model, file_data)
        source_file_id = file_data.get("model_file_id") or file_data.get("id") or file_data.get("file_id") or ""

        download_job_id = None
        if str(prefs.get("download_behavior") or "browser").lower() == "local":
            _job_filename = str(file_data.get("name") or file_data.get("path") or "Model file").split("/")[-1]
            _job_model_name = str(model.get("display_name") or model.get("name") or _job_filename or "Model")
            try:
                _job_total = int(file_data.get("size_bytes") or file_data.get("size") or 0)
            except (TypeError, ValueError):
                _job_total = 0
            _resume_requested = (str(request.args.get("resume") or "").strip() == "1")
            _requested_job_id = str(request.args.get("job_id") or "").strip()
            _retry_url = (
                f"/download/source/{model_id}/{source_name}/{file_index}"
                f"?save_info={1 if prefs.get('save_model_info', True) else 0}"
                f"&save_preview={1 if prefs.get('save_model_preview', True) else 0}"
            )
            _matching_job = active_downloads.find_matching(
                model_id=model_id, source=source_name, filename=_job_filename,
                retry_url=_retry_url,
            )
            if _matching_job and _matching_job.get("status") in {"starting", "downloading", "installing", "canceling", "pausing"}:
                return {"success": True, "already_active": True, "job_id": _matching_job.get("id")}, 200
            if _requested_job_id and _matching_job and str(_matching_job.get("id")) == _requested_job_id:
                # Resume is an explicit Download Manager action. Reuse the exact
                # persisted job and its .part file.
                download_job_id = _requested_job_id
                _resume_requested = True
                if not active_downloads.reactivate(download_job_id):
                    return {"success": False, "error": "This saved download can no longer be resumed."}, 409
            elif _matching_job and _matching_job.get("status") in {"paused", "failed"}:
                # A normal green Download click must never implicitly resume a
                # saved partial. Leave the job untouched and send the user to
                # Download Manager, where Resume is the authoritative action.
                return {
                    "success": True,
                    "existing_partial": True,
                    "job_id": _matching_job.get("id"),
                    "downloaded_bytes": _matching_job.get("downloaded_bytes", 0),
                    "message": "A saved partial download already exists. Resume it from Download Manager or discard it to start over.",
                }, 200
            else:
                download_job_id = active_downloads.create_job(
                    model_id=model_id,
                    model_name=_job_model_name,
                    source=source_name,
                    filename=_job_filename,
                    retry_url=_retry_url,
                    total_bytes=_job_total,
                )

            def _progress_callback(stage=None, downloaded_bytes=None, total_bytes=None, part_path=None):
                if active_downloads.cancel_requested(download_job_id):
                    raise installer.DownloadCancelled("Download canceled by user.")
                if active_downloads.pause_requested(download_job_id):
                    raise installer.DownloadPaused("Download paused by user.")
                _stage = str(stage or "Downloading")
                _status = "downloading" if _stage in {"Connecting", "Downloading"} else "installing"
                active_downloads.update(
                    download_job_id,
                    status=_status,
                    stage=_stage,
                    downloaded_bytes=downloaded_bytes,
                    total_bytes=total_bytes,
                    part_path=part_path,
                )

            try:
                result = installer.install_model_file(
                    model,
                    file_data,
                    source_name,
                    target,
                    prefs,
                    download_headers=_local_download_headers(source_name, source_page_url),
                    progress_callback=_progress_callback,
                    resume_existing=_resume_requested,
                )
            except installer.DownloadPaused:
                active_downloads.paused(download_job_id)
                return {"success": False, "paused": True, "error": "Download paused. Resume to continue the partial file."}, 409
            except installer.DownloadCancelled:
                active_downloads.canceled(download_job_id)
                return {"success": False, "canceled": True, "error": "Download canceled."}, 409
            except Exception as _download_exc:
                active_downloads.fail(download_job_id, str(_download_exc))
                print(
                    "DOWNLOAD FAILED:",
                    source_name,
                    str(model.get("display_name") or model.get("name") or model_id),
                    type(_download_exc).__name__,
                    str(_download_exc),
                )
                raise
            active_downloads.complete(download_job_id)
            if prefs.get("track_downloads", True) is not False:
                database.record_download(model_id, source_name, model.get("model_key"), source_file_id, _download_file_key(file_data), result.get("filename") or file_data.get("name") or file_data.get("path") or "", _download_record_sha(model, file_data), model.get("updated") or "", fingerprint, file_data.get("version_id") or "", file_data.get("version") or "")
            database.record_installed_file(
                model_id, source_name, model.get("model_key"), source_file_id,
                fingerprint, result.get("path"), result.get("filename"),
                file_data.get("version_id") or "", file_data.get("version") or ""
            )
            return {"success": True, "installed": True, **result}

        if prefs.get("track_downloads", True) is not False:
            database.record_download(model_id, source_name, model.get("model_key"), source_file_id, _download_file_key(file_data), file_data.get("name") or file_data.get("path") or "", _download_record_sha(model, file_data), model.get("updated") or "", fingerprint, file_data.get("version_id") or "", file_data.get("version") or "")
        return redirect(target, code=302)
    except PermissionError as exc:
        if _download_access_is_restricted(model):
            payload = _restricted_download_payload(
                model,
                str(source or "").lower(),
                source_page_url,
            )
            if "application/json" in request.headers.get("Accept", ""):
                return payload, 403
            return payload["error"], 403
        if "application/json" in request.headers.get("Accept", ""):
            return {"success": False, "error": str(exc)}, 401
        return str(exc), 401
    except requests.HTTPError as exc:
        status = getattr(exc.response, "status_code", 502)
        if status in (401, 403) and _download_access_is_restricted(model):
            payload = _restricted_download_payload(
                model,
                str(source or "").lower(),
                source_page_url,
            )
            if "application/json" in request.headers.get("Accept", ""):
                return payload, 403
            return payload["error"], 403
        message = f"Download request failed (HTTP {status})."
        if "application/json" in request.headers.get("Accept", ""):
            return {"success": False, "error": message}, 502
        return message, 502
    except Exception as exc:
        message = f"Download request failed: {exc}"
        if "application/json" in request.headers.get("Accept", ""):
            return {"success": False, "error": message}, 502
        return message, 502


@app.route("/download/model/<int:model_id>/<int:file_index>")
def tracked_model_download(model_id, file_index):
    conn = database.connect(); row = conn.execute("SELECT * FROM models WHERE id=?", (model_id,)).fetchone(); conn.close()
    if not row: return "Model not found.", 404
    model = dict(row)
    source_page_url = str(model.get("url") or "").strip()
    canonical_image = str(model.get("image") or "").strip()
    if canonical_image.startswith("/static/cache/previews/"):
        model["_cached_preview"] = canonical_image
    model["_install_preview_url"] = _install_preview_url(
        model.get("id"),
        model.get("source"),
    )
    model["_install_preview_video_url"] = _install_preview_video_url(
        model.get("id"),
        model.get("source"),
    )
    file_data = _load_download_file(model, file_index)
    if not file_data: return "Download file not found.", 404
    try:
        source_name = str(model.get("source") or "").lower()
        if source_name == "tensorhub" and file_data.get("model_file_id"):
            target = _tensorhub_signed_download_url(file_data.get("model_file_id"))
        elif source_name == "seaart" and file_data.get("model_ver_no"):
            target = _seaart_dynamic_download_url(file_data.get("model_ver_no"))
        else:
            target = str(file_data.get("download_url") or "").strip()
        if source_name == "civitaired":
            target = _civitaired_download_url(file_data, target)
        elif source_name == "civitai":
            target = _civitai_download_url(file_data, target)
        elif source_name == "huggingface":
            target = _huggingface_download_url(model, file_data, target)
        elif source_name == "modelscope":
            target = _modelscope_download_url(model, file_data, target)
        if not target.startswith(("https://", "http://")):
            return "Direct download is unavailable for this file.", 502
        prefs = _download_sidecar_preferences(load_settings().get("preferences", {}))
        fingerprint = _download_file_fingerprint(model, file_data)
        source_name = str(model.get("source") or "").lower()
        source_file_id = file_data.get("model_file_id") or file_data.get("id") or file_data.get("file_id") or ""
        download_job_id = None
        if str(prefs.get("download_behavior") or "browser").lower() == "local":
            _job_filename = str(file_data.get("name") or file_data.get("path") or "Model file").split("/")[-1]
            _job_model_name = str(model.get("display_name") or model.get("name") or _job_filename or "Model")
            try:
                _job_total = int(file_data.get("size_bytes") or file_data.get("size") or 0)
            except (TypeError, ValueError):
                _job_total = 0
            _resume_requested = (str(request.args.get("resume") or "").strip() == "1")
            _requested_job_id = str(request.args.get("job_id") or "").strip()
            _retry_url = (
                f"/download/model/{model_id}/{file_index}"
                f"?save_info={1 if prefs.get('save_model_info', True) else 0}"
                f"&save_preview={1 if prefs.get('save_model_preview', True) else 0}"
            )
            _matching_job = active_downloads.find_matching(
                model_id=model_id, source=source_name, filename=_job_filename,
                retry_url=_retry_url,
            )
            if _matching_job and _matching_job.get("status") in {"starting", "downloading", "installing", "canceling", "pausing"}:
                return {"success": True, "already_active": True, "job_id": _matching_job.get("id")}, 200
            if _requested_job_id and _matching_job and str(_matching_job.get("id")) == _requested_job_id:
                # Resume is an explicit Download Manager action. Reuse the exact
                # persisted job and its .part file.
                download_job_id = _requested_job_id
                _resume_requested = True
                if not active_downloads.reactivate(download_job_id):
                    return {"success": False, "error": "This saved download can no longer be resumed."}, 409
            elif _matching_job and _matching_job.get("status") in {"paused", "failed"}:
                # A normal green Download click must never implicitly resume a
                # saved partial. Leave the job untouched and send the user to
                # Download Manager, where Resume is the authoritative action.
                return {
                    "success": True,
                    "existing_partial": True,
                    "job_id": _matching_job.get("id"),
                    "downloaded_bytes": _matching_job.get("downloaded_bytes", 0),
                    "message": "A saved partial download already exists. Resume it from Download Manager or discard it to start over.",
                }, 200
            else:
                download_job_id = active_downloads.create_job(
                    model_id=model_id,
                    model_name=_job_model_name,
                    source=source_name,
                    filename=_job_filename,
                    retry_url=_retry_url,
                    total_bytes=_job_total,
                )

            def _progress_callback(stage=None, downloaded_bytes=None, total_bytes=None, part_path=None):
                if active_downloads.cancel_requested(download_job_id):
                    raise installer.DownloadCancelled("Download canceled by user.")
                if active_downloads.pause_requested(download_job_id):
                    raise installer.DownloadPaused("Download paused by user.")
                _stage = str(stage or "Downloading")
                _status = "downloading" if _stage in {"Connecting", "Downloading"} else "installing"
                active_downloads.update(
                    download_job_id,
                    status=_status,
                    stage=_stage,
                    downloaded_bytes=downloaded_bytes,
                    total_bytes=total_bytes,
                    part_path=part_path,
                )

            try:
                result = installer.install_model_file(
                    model,
                    file_data,
                    source_name,
                    target,
                    prefs,
                    download_headers=_local_download_headers(source_name, source_page_url),
                    progress_callback=_progress_callback,
                    resume_existing=_resume_requested,
                )
            except installer.DownloadPaused:
                active_downloads.paused(download_job_id)
                return {"success": False, "paused": True, "error": "Download paused. Resume to continue the partial file."}, 409
            except installer.DownloadCancelled:
                active_downloads.canceled(download_job_id)
                return {"success": False, "canceled": True, "error": "Download canceled."}, 409
            except Exception as _download_exc:
                active_downloads.fail(download_job_id, str(_download_exc))
                print(
                    "DOWNLOAD FAILED:",
                    source_name,
                    str(model.get("display_name") or model.get("name") or model_id),
                    type(_download_exc).__name__,
                    str(_download_exc),
                )
                raise
            active_downloads.complete(download_job_id)
            if prefs.get("track_downloads", True) is not False:
                database.record_download(model_id, source_name, model.get("model_key"), source_file_id, _download_file_key(file_data), result.get("filename") or file_data.get("name") or file_data.get("path") or "", _download_record_sha(model, file_data), model.get("updated") or "", fingerprint, file_data.get("version_id") or "", file_data.get("version") or "")
            database.record_installed_file(
                model_id, source_name, model.get("model_key"), source_file_id,
                fingerprint, result.get("path"), result.get("filename"),
                file_data.get("version_id") or "", file_data.get("version") or ""
            )
            return {"success": True, "installed": True, **result}
        if prefs.get("track_downloads", True) is not False:
            database.record_download(
                model_id, source_name, model.get("model_key"), source_file_id,
                _download_file_key(file_data), file_data.get("name") or file_data.get("path") or "",
                _download_record_sha(model, file_data),
                model.get("updated") or "", fingerprint,
            )
        return redirect(target, code=302)
    except PermissionError as exc:
        if _download_access_is_restricted(model):
            payload = _restricted_download_payload(
                model,
                str(model.get("source") or "").lower(),
                source_page_url,
            )
            if "application/json" in request.headers.get("Accept", ""):
                return payload, 403
            return payload["error"], 403
        if "application/json" in request.headers.get("Accept", ""):
            return {"success": False, "error": str(exc)}, 401
        return str(exc), 401
    except requests.HTTPError as exc:
        status = getattr(exc.response, "status_code", 502)
        if status in (401, 403) and _download_access_is_restricted(model):
            payload = _restricted_download_payload(
                model,
                str(model.get("source") or "").lower(),
                source_page_url,
            )
            if "application/json" in request.headers.get("Accept", ""):
                return payload, 403
            return payload["error"], 403
        message = f"Download request failed (HTTP {status})."
        if "application/json" in request.headers.get("Accept", ""):
            return {"success": False, "error": message}, 502
        return message, 502
    except Exception as exc: return f"Download request failed: {exc}", 502



@app.route("/api/active-downloads")
def active_download_list():
    return {"success": True, **active_downloads.snapshot()}


@app.route("/api/active-downloads/<job_id>/pause", methods=["POST"])
def active_download_pause(job_id):
    accepted = active_downloads.request_pause(job_id)
    return {"success": accepted, "job_id": job_id}, (200 if accepted else 409)


@app.route("/api/active-downloads/<job_id>/cancel", methods=["POST"])
def active_download_cancel(job_id):
    accepted = active_downloads.request_cancel(job_id)
    return {"success": accepted, "job_id": job_id}, (200 if accepted else 409)


@app.route("/api/active-downloads/<job_id>", methods=["DELETE"])
def active_download_dismiss(job_id):
    return {"success": True, "dismissed": active_downloads.dismiss(job_id)}


@app.route("/api/active-downloads/<job_id>/discard", methods=["POST"])
def active_download_discard(job_id):
    discarded = active_downloads.discard(job_id)
    return {"success": discarded, "job_id": job_id}, (200 if discarded else 409)


@app.route("/api/download-history/<int:history_id>/open-folder", methods=["POST"])
def download_history_open_folder(history_id):
    local_path=database.get_download_history_open_path(history_id)
    if not local_path:
        return {"success":False,"error":"No Local Installer path is recorded for this download."},404

    path=Path(local_path).expanduser()
    folder=path if path.is_dir() else path.parent
    if not folder.exists():
        return {"success":False,"error":"The saved folder no longer exists."},404

    try:
        if os.name=="nt":
            os.startfile(str(folder))
        elif sys.platform=="darwin":
            subprocess.Popen(["open",str(folder)])
        else:
            subprocess.Popen(["xdg-open",str(folder)])
        return {"success":True}
    except Exception as exc:
        return {"success":False,"error":f"Could not open the saved folder: {exc}"},500


@app.route("/api/installed-files/model/<int:model_id>", methods=["GET", "DELETE"])
def installed_files_for_model(model_id):
    rows = database.get_installed_files_for_model(model_id)
    files = []
    for row in rows:
        path = Path(str(row.get("local_path") or ""))
        try:
            size_bytes = path.stat().st_size if path.is_file() else 0
        except OSError:
            size_bytes = 0
        files.append({
            "id": int(row.get("id") or 0),
            "path": str(path),
            "filename": str(row.get("filename") or path.name),
            "version_id": str(row.get("version_id") or ""),
            "version_name": str(row.get("version_name") or ""),
            "exists": path.is_file(),
            "size_bytes": int(size_bytes or 0),
        })
    if request.method == "GET":
        return {"success": True, "files": files}

    payload = request.get_json(silent=True) or {}
    selected_ids = payload.get("file_ids") if isinstance(payload, dict) else None
    if selected_ids is None:
        selected_ids = [item["id"] for item in files]
    selected_ids = {int(value) for value in selected_ids if str(value).isdigit()}
    selected = [item for item in files if item["id"] in selected_ids]
    if not selected:
        return {"success": False, "error": "No tracked local files were selected."}, 400

    deleted = []
    failed = []
    successfully_deleted_ids = set()
    affected_folders = set()
    for item in selected:
        path = Path(item["path"])
        affected_folders.add(path.parent)
        if not path.is_file():
            failed.append({"path": str(path), "error": "Recorded file was not found on disk."})
            continue
        try:
            path.unlink()
            # Do not clear ownership/tracking unless the physical deletion is verified.
            if path.exists():
                failed.append({"path": str(path), "error": "File still exists after the delete operation."})
                continue
            deleted.append(str(path))
            successfully_deleted_ids.add(int(item["id"]))
        except Exception as exc:
            failed.append({"path": str(path), "error": str(exc)})

    # Clear tracking only for files that were actually removed from disk. This
    # keeps failed deletions manageable/retryable instead of silently forgetting them.
    if successfully_deleted_ids:
        database.clear_tracking_for_installed_records(model_id, successfully_deleted_ids)

    # Shared sidecars are folder-level assets. Remove them only after the last
    # AbyssBeacon-tracked model file in that folder has actually been deleted.
    remaining = database.get_installed_files_for_model(model_id)
    remaining_folders = {
        Path(str(row.get("local_path") or "")).parent
        for row in remaining if str(row.get("local_path") or "").strip()
    }
    for folder in affected_folders:
        if folder in remaining_folders:
            continue
        # Only folders whose selected model files were successfully removed are
        # eligible for sidecar cleanup.
        successful_in_folder = any(
            int(item["id"]) in successfully_deleted_ids and Path(item["path"]).parent == folder
            for item in selected
        )
        if not successful_in_folder:
            continue
        sidecars = [folder / "AbyssBeacon Info.txt", folder / "ModelRadar Info.txt", *folder.glob("preview.*")]
        for sidecar in sidecars:
            if not sidecar.is_file():
                continue
            try:
                sidecar.unlink()
                if sidecar.exists():
                    failed.append({"path": str(sidecar), "error": "Sidecar still exists after the delete operation."})
                else:
                    deleted.append(str(sidecar))
            except Exception as exc:
                failed.append({"path": str(sidecar), "error": str(exc)})

        # Do not leave empty per-model install folders behind. Only remove the
        # exact folder that contained a successfully deleted tracked file, and
        # only when it is truly empty after sidecar cleanup. Any unrelated or
        # older-version file keeps the folder intact.
        try:
            if folder.is_dir() and not any(folder.iterdir()):
                folder.rmdir()
                if folder.exists():
                    failed.append({"path": str(folder), "error": "Empty model folder still exists after cleanup."})
                else:
                    deleted.append(str(folder))
        except Exception as exc:
            failed.append({"path": str(folder), "error": f"Could not remove empty model folder: {exc}"})

    if failed:
        return {
            "success": False,
            "deleted": deleted,
            "failed": failed,
            "remaining": len(remaining),
            "error": "Some selected local files could not be deleted. AbyssBeacon kept tracking for any failed model-file deletions."
        }, 409
    return {"success": True, "deleted": deleted, "remaining": len(remaining)}


@app.route("/api/download-history/model/<int:model_id>", methods=["GET", "DELETE"])
def download_history_clear_model(model_id):
    rows = database.get_download_history_for_model(model_id)
    if request.method == "GET":
        # Present file identities rather than repeated download events. A file
        # downloaded twice should still be one choice in the management picker.
        installed_rows = database.get_installed_files_for_model(model_id)
        installed_by_name = {
            str(row.get("filename") or "").strip().casefold(): row
            for row in installed_rows if str(row.get("filename") or "").strip()
        }
        unique_rows = []
        seen = set()
        for row in rows:
            identity = (
                str(row.get("file_fingerprint") or "").strip()
                or str(row.get("source_file_id") or "").strip()
                or (str(row.get("source") or "").strip().lower() + "|" + str(row.get("filename") or "").strip().casefold())
            )
            if identity in seen:
                continue
            seen.add(identity)
            item = dict(row)
            installed = installed_by_name.get(str(item.get("filename") or "").strip().casefold())
            if installed:
                local_path = Path(str(installed.get("local_path") or ""))
                try:
                    item["size_bytes"] = local_path.stat().st_size if local_path.is_file() else 0
                except OSError:
                    item["size_bytes"] = 0
            else:
                item["size_bytes"] = 0
            unique_rows.append(item)
        return {"success": True, "files": unique_rows}
    payload = request.get_json(silent=True) or {}
    selected_ids = payload.get("history_ids") if isinstance(payload, dict) else None
    if selected_ids is None:
        selected_ids = [row.get("id") for row in rows]
    result = database.forget_download_history_records(model_id, selected_ids)
    return {"success": True, "deleted": result.get("history", 0), "forgot_installed": result.get("installed", 0), "model_id": model_id}


@app.route("/api/download-history/recent")
def download_history_recent():
    try:
        limit = max(1, min(200, int(request.args.get("limit") or 30)))
    except (TypeError, ValueError):
        limit = 30

    items = database.get_recent_download_history(limit)
    settings = load_settings()
    preferences = settings.get("preferences", {}) if isinstance(settings.get("preferences", {}), dict) else {}
    source_colors = preferences.get("source_card_colors", {}) if isinstance(preferences.get("source_card_colors", {}), dict) else {}
    comfy_root = str(preferences.get("comfyui_folder") or "").strip()

    try:
        root_path = Path(comfy_root).expanduser().resolve() if comfy_root else None
    except Exception:
        root_path = Path(comfy_root).expanduser() if comfy_root else None

    for item in items:
        source = str(item.get("source") or "").strip().lower()
        default_color = (SOURCE_INFO.get(source) or {}).get("color", "#00eaff")
        item["source_color"] = str(source_colors.get(source) or default_color)

        local_path = str(item.get("local_path") or "").strip()
        display_path = local_path
        if local_path:
            try:
                file_path = Path(local_path).expanduser()
                if root_path:
                    try:
                        relative = file_path.resolve().relative_to(root_path)
                        display_path = "…\\" + str(relative)
                    except Exception:
                        # If an old history row points outside the current
                        # configured root, still keep the useful end of the path.
                        parts = file_path.parts
                        display_path = "…\\" + "\\".join(parts[-6:]) if len(parts) > 6 else str(file_path)
                else:
                    parts = file_path.parts
                    display_path = "…\\" + "\\".join(parts[-6:]) if len(parts) > 6 else str(file_path)
            except Exception:
                display_path = local_path
        item["display_path"] = display_path

    return {"success": True, "items": items}


@app.route("/api/download-history/preview")
def download_history_preview():
    mode=str(request.args.get("mode") or "all"); days=max(0, min(36500, int(request.args.get("days") or 0)))
    return {"success":True,"count":database.preview_download_history_cleanup(mode,days),"mode":mode,"days":days}


@app.route("/api/download-history/clear", methods=["POST"])
def download_history_clear():
    data=request.get_json(silent=True) or {}; mode=str(data.get("mode") or "all"); days=max(0,min(36500,int(data.get("days") or 0)))
    return {"success":True,"deleted":database.clear_download_history(mode,days)}

@app.route("/download/tensorhub/<model_file_id>")
def tensorhub_download(model_file_id):
    # Backward-compatible untracked route for old cached markup. New UI uses /download/model/<id>/<index>.
    try:
        return redirect(_tensorhub_signed_download_url(model_file_id), code=302)
    except PermissionError as exc:
        return str(exc), 401
    except requests.HTTPError as exc:
        status=getattr(exc.response,"status_code",502)
        return f"TensorHub download request failed (HTTP {status}).", 502
    except Exception as exc:
        return f"TensorHub download request failed: {exc}", 502


@app.route("/settings/sources", methods=["GET", "POST"])
def scan_source_settings():
    """Choose which providers are globally available to normal scans and filters."""
    settings = load_settings()
    sources = settings.get("sources", {}) if isinstance(settings.get("sources"), dict) else {}

    if request.method == "POST":
        payload = request.get_json(silent=True) or {}
        requested = payload.get("enabled", [])
        if not isinstance(requested, list):
            requested = []
        enabled = []
        seen = set()
        for value in requested:
            name = str(value or "").strip()
            if name in sources and name not in seen:
                seen.add(name)
                enabled.append(name)
        for name, data in sources.items():
            if isinstance(data, dict):
                data["enabled"] = name in seen
        preferences = settings.setdefault("preferences", {})
        for key in ("selected_sources", "scan_sources", "selected_scan_sources"):
            current = preferences.get(key)
            if isinstance(current, list):
                preferences[key] = [name for name in current if name in seen]
        save_settings(settings)
        return {"success": True, "enabled": enabled}

    enabled = [name for name, data in sources.items() if isinstance(data, dict) and data.get("enabled")]
    display_sources = dict(sorted(
        sources.items(),
        key=lambda item: str(
            (item[1].get("display") if isinstance(item[1], dict) else "") or item[0]
        ).casefold(),
    ))
    return render_template("sources.html", sources=display_sources, enabled_sources=enabled)


@app.route("/settings/architectures", methods=["GET", "POST"])
def architecture_settings():
    """Curated normal-scan targets.

    Architecture/source definitions are application-owned. Users only choose
    which tested targets participate in normal scans; arbitrary one-off terms
    remain available through Search Sources.
    """
    architectures = load_architectures()

    if request.method == "POST":
        payload = request.get_json(silent=True) or {}
        requested = payload.get("enabled", [])
        if not isinstance(requested, list):
            requested = []

        enabled = []
        seen = set()
        for value in requested:
            name = str(value or "").strip()
            if not name or name not in architectures or name in seen:
                continue
            seen.add(name)
            enabled.append(name)

        settings = load_settings()
        if not isinstance(settings.get("preferences"), dict):
            settings["preferences"] = {}
        settings["preferences"]["enabled_architectures"] = enabled
        # A per-scan selection may only contain globally enabled targets.
        current_scan = settings["preferences"].get("scan_architectures")
        if isinstance(current_scan, list):
            settings["preferences"]["scan_architectures"] = [name for name in current_scan if name in seen]
        save_settings(settings)

        return {"success": True, "enabled": enabled}

    settings = load_settings()
    preferences = settings.get("preferences", {}) if isinstance(settings.get("preferences"), dict) else {}
    saved_enabled = preferences.get("enabled_architectures")
    if not isinstance(saved_enabled, list):
        # Upgrade path: existing installs used scan_architectures for both jobs.
        saved_enabled = preferences.get("scan_architectures")

    # Existing installs preserve their current choices. A completely fresh
    # install defaults to the curated registry so every supported family is
    # available until the user opts out.
    if isinstance(saved_enabled, list):
        enabled = [name for name in saved_enabled if name in architectures]
    else:
        enabled = list(architectures.keys())

    groups = []
    group_index = {}
    for name, data in architectures.items():
        data = data if isinstance(data, dict) else {}
        family = str(data.get("family") or "").strip()
        label = str(data.get("label") or name).strip()

        if family:
            key = f"group:{family}"
            if key not in group_index:
                group_index[key] = len(groups)
                groups.append({"name": family, "items": [], "grouped": True})
            groups[group_index[key]]["items"].append({"name": name, "label": label})
        else:
            groups.append({
                "name": label,
                "items": [{"name": name, "label": label}],
                "grouped": False,
            })

    for group in groups:
        group["items"].sort(key=lambda item: str(item.get("label") or item.get("name") or "").casefold())
    groups.sort(key=lambda group: str(group.get("name") or "").casefold())

    return render_template(
        "architectures.html",
        architectures=architectures,
        architecture_groups=groups,
        enabled_architectures=enabled,
    )


@app.route("/settings/model_types", methods=["GET", "POST"])
def model_type_settings():

    if request.method == "POST":

        payload = request.json or {}
        enabled = payload.get("enabled", {}) if isinstance(payload, dict) else {}
        model_types = load_model_types()
        if isinstance(enabled, dict):
            for name, data in model_types.items():
                if isinstance(data, dict) and name in enabled:
                    data["enabled"] = bool(enabled[name])
        save_config("model_types.json", model_types)

        return {"success": True}


    model_types = load_model_types()

    return render_template(
        "model_types.html",
        model_types=model_types
    )


@app.route("/save_preferences", methods=["POST"])
def save_preferences():

    preferences = request.json or {}


    settings = load_settings()

    # A missing/corrupt preferences block should never make the UI fail.
    # load_settings() normally guarantees this, but keep the route defensive.
    if not isinstance(settings.get("preferences"), dict):
        settings["preferences"] = {}

    settings["preferences"].update(preferences)

    # Feed source selection and scan source selection are intentionally one state.
    if isinstance(settings["preferences"].get("selected_sources"), list):
        settings["preferences"]["selected_scan_sources"] = list(settings["preferences"]["selected_sources"])

    save_settings(settings)


    return {
        "success": True
    }



@app.route("/models/mark-seen", methods=["POST"])
def mark_models_seen():
    payload = request.get_json(silent=True) or {}
    if payload.get("all"):
        changed = database.mark_all_viewed()
    else:
        changed = database.mark_models_viewed(payload.get("ids", []))
    return {"success": True, "changed": changed}


@app.route("/scan/history")
def scan_history():
    runs = []
    for row in database.get_scan_history(10):
        item = dict(row)
        item["started_ago"] = time_since(item.get("started"))
        item["finished_ago"] = time_since(item.get("finished")) if item.get("finished") else "running"
        item["sources"] = [dict(result) for result in database.get_scan_results(item["id"])]
        runs.append(item)
    return {"runs": runs}


@app.route("/scan", methods=["POST"])
def scan():

    print("SCAN REQUEST RECEIVED")

    selected_sources = request.form.getlist(
        "sources"
    )

    selected_architectures = [value for value in request.form.getlist("architectures") if value]
    selected_architecture = request.form.get("architecture", "")
    if selected_architecture and selected_architecture not in selected_architectures:
        selected_architectures.append(selected_architecture)


    settings = load_settings()

    architectures = load_architectures()
    preferences = settings.get("preferences", {}) if isinstance(settings.get("preferences"), dict) else {}
    enabled_sources = {name for name, data in settings.get("sources", {}).items() if isinstance(data, dict) and data.get("enabled")}
    selected_sources = [name for name in selected_sources if name in enabled_sources]
    configured_enabled_architectures = preferences.get("enabled_architectures")
    if not isinstance(configured_enabled_architectures, list):
        configured_enabled_architectures = preferences.get("scan_architectures")
    if not isinstance(configured_enabled_architectures, list):
        configured_enabled_architectures = list(architectures.keys())
    enabled_architectures = {name for name in configured_enabled_architectures if name in architectures and name != "Other"}
    selected_architectures = [name for name in selected_architectures if name in enabled_architectures]

    if not selected_sources:
        return {"success": False, "error": "Select at least one enabled source."}, 400
    if not selected_architectures:
        return {"success": False, "error": "Select at least one enabled model architecture."}, 400

    if selected_architectures:
        selected_terms = []
        for architecture_name in selected_architectures:
            selected_terms.extend(architectures.get(architecture_name, {}).get("keywords", []))
    else:
        selected_architectures = [name for name in architectures.keys() if name != "Other"]
        selected_terms = []
        for architecture_name in selected_architectures:
            selected_terms.extend(architectures.get(architecture_name, {}).get("keywords", []))

    # A single architecture keeps the existing optimized source path. Multiple
    # selections intentionally use the combined search-term plan.
    selected_architecture = selected_architectures[0] if len(selected_architectures) == 1 else ""


    settings["preferences"].update({

        "scan_sources": selected_sources,
        "selected_scan_sources": selected_sources,
        "scan_architectures": selected_architectures,
        "selected_architecture": selected_architecture

    })


    save_settings(settings)


    print("======================")
    print("ACTIVE SOURCES FROM UI")
    print([source_label(name) for name in selected_sources])
    print("======================")

    # Start each scan with a clean stop flag BEFORE exposing the running scan
    # to the browser. Resetting from inside the background worker creates a
    # race where a fast Stop click can be erased a moment later.
    import scan_control
    scan_control.reset()

    def run_background_scan():

        scan_status.update_status(
            status="running",
            message="Starting scan..."
        )

        try:

            scanner_runner.run_scan(
                selected_sources,
                selected_terms,
                selected_architecture,
                selected_architectures
            )

            # Automatic retention runs only after a genuinely completed normal scan.
            # Search Sources, stopped scans, and failed scans never trigger it.
            completed_state = scan_status.get_status().get("status")
            if completed_state in ("complete", "complete_with_errors"):
                try:
                    _run_automatic_library_cleanup()
                except Exception as cleanup_exc:
                    print(f"Automatic library cleanup skipped: {cleanup_exc}")
                queue_stats={"checked":0,"waiting":0,"ready":[],"installed":[],"errors":0}
                watch_stats={"checked":0,"waiting":0,"ready":[],"errors":0}
                try:
                    queue_stats=_check_download_queue_after_scan()
                except Exception as queue_exc:
                    print(f"Download queue check skipped: {queue_exc}")
                try:
                    watch_stats=_check_download_watchlist_after_scan()
                except Exception as watch_exc:
                    print(f"Download watchlist check skipped: {watch_exc}")
                _print_access_followup_summary(queue_stats,watch_stats)

            # scanner.run_scan owns the final state (complete/stopped/errors).
            # Do not overwrite it here.

        except Exception as e:

            scan_status.update_status(
                status="error",
                message=str(e)
            )


    threading.Thread(
        target=run_background_scan,
        daemon=True
    ).start()


    return {
        "status": "started"
    }



@app.route("/search/sources", methods=["POST"])
def search_sources_external():
    """Run an ad-hoc provider keyword search without using the persistent watch plan."""
    data = request.get_json(silent=True) or {}
    query = str(data.get("query") or "").strip()
    intent = str(data.get("intent") or "anything").strip().lower()
    depth = str(data.get("depth") or "deep").strip().lower()
    sources = [s for s in (data.get("sources") or []) if s in scanner.ALL_SCANNERS]

    # Search Sources is a true text search first. Architecture choices are a
    # post-search filter and never decide whether a provider is allowed to
    # search for the keyword.
    configured_architectures = load_architectures()
    requested_architectures = data.get("architectures")
    if requested_architectures is None:
        legacy_architecture = str(data.get("architecture") or "").strip()
        requested_architectures = [legacy_architecture] if legacy_architecture else []
    selected_architectures = []
    seen_architectures = set()
    for value in requested_architectures or []:
        name = str(value or "").strip()
        if not name or name not in configured_architectures or name in seen_architectures:
            continue
        seen_architectures.add(name)
        selected_architectures.append(name)

    if not query:
        return {"status": "error", "message": "Enter something to search for."}, 400
    if intent not in {"anything", "models", "creators"}:
        intent = "anything"
    if not sources:
        return {"status": "error", "message": "Select at least one source."}, 400
    if scan_status.get_status().get("status") in ("running", "stopping"):
        return {"status": "error", "message": "A scan is already running."}, 409

    max_results = {"recent": 100, "deep": 200, "all": 1000, "maximum": 100000}.get(depth, 200)
    search_days = 30 if depth == "recent" else 36500
    plan = {source: [] for source in sources}

    for source in sources:
        job = {
            "watch": f"External: {query}",
            "term": query,
            "mode": "text",
            "external_search": True,
            "external_intent": intent,
            "external_query": query,
            "external_architectures": selected_architectures,
        }
        if intent == "creators":
            job["watch"] = f"Creator: {query}"
            job["creator"] = query
        plan[source].append(job)

    print(f'EXTERNAL SEARCH: "{query}" ({intent}, {depth})')
    if selected_architectures:
        print("Architecture filter:", ", ".join(selected_architectures))
    else:
        print("Architecture filter: Any")

    import scan_control
    scan_control.reset()

    def run_external():
        try:
            scanner.run_scan(
                sources,
                explicit_plan=plan,
                search_overrides={
                    "max_results": max_results,
                    "search_days": search_days,
                    "_external_search": True,
                    "_external_maximum": depth == "maximum",
                },
            )
        except Exception as exc:
            scan_status.update_status(status="error", message=str(exc))

    threading.Thread(target=run_external, daemon=True).start()
    return {
        "status": "started",
        "query": query,
        "intent": intent,
        "architectures": selected_architectures,
        "max_results": max_results,
    }


@app.route("/scan/status")
def scan_status_api():

    return scan_status.get_status()


@app.route("/scan/stop", methods=["POST"])
def stop_scan():

    import scan_control

    scan_control.stop_scan()
    print("Stop requested — finishing active requests...")

    scan_status.update_status(
        status="stopping",
        message="Stopping scan safely…"
    )

    return {
        "status":"stopping"
    }


def display_media_url(url, source=""):
    """Return a browser-friendly media URL without download-only parameters."""
    url = (url or "").strip()
    if source == "huggingface" and url:
        url = url.replace("?download=true", "").replace("&download=true", "")
    return url


FEED_INITIAL_CARD_LIMIT = 120
FEED_CHUNK_CARD_LIMIT = 80

# Feed presentation is deliberately separate from database canonical-source
# priority. The canonical row exists for storage/merge stability; this order
# controls only which eligible source supplies the visible feed card.
FEED_PRESENTATION_SOURCE_PRIORITY = (
    "civitai",
    "tensorhub",
    "seaart",
    "huggingface",
    "modelscope",
    "civitaired",
)

SOURCE_VIEW_LABELS = {
    "huggingface": "Hugging Face",
    "modelscope": "ModelScope",
    "civitai": "CivitAI",
    "civitaired": "CivitAI Red",
    "tensorhub": "TensorHub Art",
    "seaart": "SeaArt",
}


def _normalize_maturity_mode(value):
    return "show" if str(value or "hide").strip().lower() == "show" else "hide"


def _boolish(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    marker = str(value or "").strip().lower()
    if marker in {"1", "true", "yes", "on", "adult", "mature", "explicit", "nsfw"}:
        return True
    if marker in {"", "0", "false", "no", "off", "none", "safe", "sfw"}:
        return False
    return bool(marker)


def _media_row_sensitive(row):
    """Classify one stored media item independently from its parent source."""
    row = row if isinstance(row, dict) else dict(row or {})
    metadata_value = row.get("metadata_obj", row.get("metadata"))
    if isinstance(metadata_value, str):
        try:
            media_meta = json.loads(metadata_value or "{}")
        except Exception:
            media_meta = {}
    elif isinstance(metadata_value, dict):
        media_meta = metadata_value
    else:
        media_meta = {}

    if "mature" in media_meta:
        return _boolish(media_meta.get("mature"))
    if "sensitive" in media_meta:
        return _boolish(media_meta.get("sensitive"))

    source_name = str(row.get("source") or "").strip().lower()

    raw_browsing_level = media_meta.get("browsingLevel", media_meta.get("browsing_level"))
    if raw_browsing_level not in (None, "") and source_name in {"civitai", "civitaired"}:
        try:
            browsing_level = int(raw_browsing_level)
            return bool(browsing_level & (4 | 8 | 16 | 32))
        except (TypeError, ValueError):
            pass

    raw_level = media_meta.get("nsfwLevel", media_meta.get("nsfw_level"))
    if raw_level not in (None, ""):
        try:
            level = int(raw_level)
            if source_name in {"civitai", "civitaired"}:
                return bool(level & (4 | 8 | 16 | 32))
            return level > 1
        except (TypeError, ValueError):
            marker = str(raw_level or "").strip().lower()
            if marker in {"r", "x", "xxx", "blocked", "mature", "adult", "explicit", "nsfw"}:
                return True
            if marker in {"pg", "pg-13", "pg13", "safe", "sfw", "none"}:
                return False

    for key in ("maturity", "content_rating", "contentRating", "rating"):
        marker = str(media_meta.get(key) or "").strip().lower()
        if marker in {"r", "x", "xxx", "blocked", "mature", "adult", "explicit", "nsfw"}:
            return True
        if marker in {"pg", "pg-13", "pg13", "safe", "sfw", "none"}:
            return False

    raw_nsfw = media_meta.get("nsfw")
    if isinstance(raw_nsfw, bool):
        return raw_nsfw
    if isinstance(raw_nsfw, str):
        marker = raw_nsfw.strip().lower()
        if marker in {"true", "1", "r", "x", "xxx", "mature", "adult", "explicit", "nsfw"}:
            return True
        if marker in {"false", "0", "pg", "pg-13", "pg13", "safe", "sfw", "none"}:
            return False
    return False


def _media_visible_for_maturity(
    row,
    maturity_mode="hide",
    include_civitai_mature_media=True,
):
    """Return whether one stored media row belongs in the active view.

    Mature Content controls display globally. CivitAI's Include Mature Media
    setting additionally controls whether previously fetched adult CivitAI
    gallery rows are considered active at all. This makes turning Rich Media
    OFF take effect immediately; the next scan/reload then prunes those rows
    from storage as well.
    """
    row = row if isinstance(row, dict) else dict(row or {})
    media_is_mature = _media_row_sensitive(row)
    source_name = str(row.get("source") or "").strip().lower()

    if source_name == "civitai" and media_is_mature and not include_civitai_mature_media:
        return False

    return _normalize_maturity_mode(maturity_mode) == "show" or not media_is_mature


def _source_snapshot_sensitive(snapshot, canonical=None):
    """Classify one provider snapshot without letting sibling sources bleed in."""
    snapshot = snapshot if isinstance(snapshot, dict) else {}

    card_data = snapshot.get("card_data") or {}
    if isinstance(card_data, str):
        try:
            card_data = json.loads(card_data or "{}")
        except Exception:
            card_data = {}
    if not isinstance(card_data, dict):
        card_data = {}

    source_name = str(snapshot.get("source") or "").strip().lower()

    # CivitAI's top-level ``nsfw`` boolean is the provider's model-level
    # maturity state. Its numeric ``nsfwLevel`` is a roll-up/bitmask of content
    # levels represented by the model and can contain mature bits on an
    # otherwise-safe model. Prefer this explicit boolean even over a stale
    # snapshot.sensitive value left by older AbyssBeacon builds; this also
    # repairs previously poisoned merged cards as soon as they are rendered.
    if source_name == "civitai" and "nsfw" in card_data:
        raw_model_nsfw = card_data.get("nsfw")
        if isinstance(raw_model_nsfw, bool):
            return raw_model_nsfw
        if isinstance(raw_model_nsfw, (int, float)):
            return raw_model_nsfw != 0
        if isinstance(raw_model_nsfw, str):
            marker = raw_model_nsfw.strip().lower()
            if marker in {"true", "1", "yes", "on"}:
                return True
            if marker in {"false", "0", "no", "off", "", "none"}:
                return False

    if "sensitive" in snapshot and snapshot.get("sensitive") not in (None, ""):
        return _boolish(snapshot.get("sensitive"))

    # Old CivitAI/CivitAI Red merged rows can synthesize a missing source from
    # its sibling solely so downloads still work.  That compatibility snapshot
    # contains the sibling's card_data, so it is not valid maturity evidence.
    # We may still classify explicit text on the source identity itself.
    if snapshot.get("_mirrored_download_fallback"):
        return bool(metadata.detect_sensitive(
            snapshot.get("name", ""),
            snapshot.get("display_name", ""),
            snapshot.get("tags", ""),
            snapshot.get("display_tags", []),
            {},
            snapshot.get("description", ""),
        ))

    # Several providers expose a numeric maturity level rather than a boolean.
    # Do not use CivitAI's aggregate nsfwLevel here; its explicit ``nsfw``
    # boolean above is the source/model maturity signal.
    if source_name != "civitai":
        for key in ("nsfw_level", "nsfwLevel", "maturity_level", "maturityLevel"):
            raw = card_data.get(key)
            if raw in (None, ""):
                continue
            try:
                level = int(raw)
                if source_name == "civitaired":
                    if level & (4 | 8 | 16 | 32):
                        return True
                elif level > 1:
                    return True
            except Exception:
                marker = str(raw or "").strip().lower()
                if marker in {"mature", "adult", "explicit", "nsfw", "r", "x", "xxx"}:
                    return True

    for key in ("content_rating", "contentRating", "rating"):
        marker = str(card_data.get(key) or "").strip().lower()
        if marker in {"mature", "adult", "explicit", "nsfw", "r", "x", "xxx"}:
            return True

    detected = metadata.detect_sensitive(
        snapshot.get("name", ""),
        snapshot.get("display_name", ""),
        snapshot.get("tags", ""),
        snapshot.get("display_tags", []),
        card_data,
        snapshot.get("description", ""),
    )
    if detected:
        return True

    # Do not fall back to models.sensitive here. That column belongs to the
    # merged/canonical card and legacy databases may already have a sibling's
    # maturity state baked into it. A source is mature only when its own saved
    # snapshot provides evidence. The no-model_sources fallback created by
    # _decode_source_snapshot copies the canonical row's sensitive value into
    # the snapshot explicitly, so true single-source cards still retain it.
    return False


def _source_snapshot_has_media(snapshot, media_rows=None):
    if not isinstance(snapshot, dict):
        return False
    if str(snapshot.get("image") or "").strip():
        return True
    if _boolish(snapshot.get("has_media")):
        return True
    source = str(snapshot.get("source") or "").strip().lower()
    return any(str(row.get("source") or "").strip().lower() == source for row in (media_rows or []))


def _eligible_source_snapshots(snapshots, maturity_mode="hide"):
    snapshots = [dict(item) for item in (snapshots or []) if isinstance(item, dict)]
    for item in snapshots:
        item["sensitive"] = bool(_source_snapshot_sensitive(item))
    if _normalize_maturity_mode(maturity_mode) == "show":
        return snapshots
    return [item for item in snapshots if not item.get("sensitive")]


def _choose_presentation_snapshot(snapshots, selected_sources=None, media_rows=None):
    """Choose a deterministic visible provider without changing merge identity."""
    candidates = [dict(item) for item in (snapshots or []) if isinstance(item, dict)]
    selected = {
        str(value or "").strip().lower()
        for value in (selected_sources or [])
        if str(value or "").strip()
    }
    if selected:
        narrowed = [item for item in candidates if str(item.get("source") or "").strip().lower() in selected]
        if narrowed:
            candidates = narrowed
    if not candidates:
        return None

    priority = {source: index for index, source in enumerate(FEED_PRESENTATION_SOURCE_PRIORITY)}
    candidates.sort(key=lambda item: priority.get(str(item.get("source") or "").strip().lower(), 999))
    with_media = [item for item in candidates if _source_snapshot_has_media(item, media_rows)]
    return (with_media or candidates)[0]


def _apply_presentation_snapshot(model, snapshot):
    """Overlay source-owned display metadata onto one merged feed/detail model."""
    if not isinstance(model, dict) or not isinstance(snapshot, dict):
        return model

    source = str(snapshot.get("source") or model.get("source") or "").strip().lower()
    model["presentation_source"] = source
    model["source"] = source

    # Identity/description values may fall back to the canonical row when an
    # older source snapshot predates those fields. Source-owned operational
    # values must not fall back, or a hidden sibling can leak its media/access.
    for key in (
        "name", "display_name", "author", "description", "base_model",
        "architecture", "model_type", "pipeline", "format", "quantization",
        "parameters", "license", "created", "updated", "downloads", "likes",
        "sha",
    ):
        value = snapshot.get(key)
        if value not in (None, "", [], {}):
            model[key] = value

    model["url"] = str(snapshot.get("url") or "")
    model["model_key"] = str(snapshot.get("model_key") or "")
    model["image"] = str(snapshot.get("image") or "").strip()
    model["files"] = snapshot.get("files") or []
    model["card_data"] = snapshot.get("card_data") or {}
    model["gated"] = bool(snapshot.get("gated"))
    model["tags"] = snapshot.get("tags") or ""
    model["display_tags"] = snapshot.get("display_tags") or []
    model["sensitive"] = bool(snapshot.get("sensitive", False))
    return model


def _architecture_search_terms(architecture):
    """Return every configured source spelling for one AbyssBeacon architecture.

    Unknown source architectures are deliberately not promoted into feed filters.
    They can still be retained as detected metadata until explicitly configured.
    """
    name = str(architecture or "").strip()
    if not name:
        return []

    configured = load_architectures()
    data = configured.get(name)
    if not isinstance(data, dict):
        return [name]

    values = [name, data.get("label", "")]
    values.extend(data.get("keywords", []) or [])
    for source_data in (data.get("source_searches", {}) or {}).values():
        if isinstance(source_data, dict):
            values.extend(source_data.get("terms", []) or [])

    result = []
    seen = set()
    for value in values:
        text = str(value or "").strip()
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            result.append(text)
    return result


def _architecture_filter_condition(architecture):
    """Match one configured architecture in parent/source/version metadata.

    JSON fields are compared as exact labels rather than loose substrings. That
    prevents a broad alias such as "Z-Image" from making Z-Image Turbo cards
    incorrectly qualify for Z-Image Base.
    """
    terms = _architecture_search_terms(architecture)
    if not terms:
        return "", []

    lowered_terms = [term.casefold() for term in terms]
    placeholders = ",".join("?" for _ in lowered_terms)

    condition = f"""(
        lower(COALESCE(architecture, '')) = lower(?)
        OR EXISTS (
            SELECT 1 FROM model_sources ms
            WHERE ms.model_id=models.id
              AND json_valid(ms.source_data)
              AND (
                    lower(COALESCE(json_extract(ms.source_data, '$.architecture'), '')) IN ({placeholders})
                 OR lower(COALESCE(json_extract(ms.source_data, '$.base_model'), '')) IN ({placeholders})
                 OR EXISTS (
                        SELECT 1
                        FROM json_each(
                            CASE
                                WHEN json_type(ms.source_data, '$.versions') = 'array'
                                THEN json_extract(ms.source_data, '$.versions')
                                ELSE '[]'
                            END
                        ) version_row
                        WHERE lower(COALESCE(json_extract(version_row.value, '$.architecture'), '')) IN ({placeholders})
                           OR lower(COALESCE(json_extract(version_row.value, '$.base_model'), '')) IN ({placeholders})
                    )
                 OR EXISTS (
                        SELECT 1
                        FROM json_each(
                            CASE
                                WHEN json_valid(json_extract(ms.source_data, '$.card_data'))
                                 AND json_type(json_extract(ms.source_data, '$.card_data'), '$.versions') = 'array'
                                THEN json_extract(json_extract(ms.source_data, '$.card_data'), '$.versions')
                                ELSE '[]'
                            END
                        ) nested_version_row
                        WHERE lower(COALESCE(json_extract(nested_version_row.value, '$.architecture'), '')) IN ({placeholders})
                           OR lower(COALESCE(json_extract(nested_version_row.value, '$.base_model'), '')) IN ({placeholders})
                    )
              )
        )
    )"""

    # The same alias vocabulary is used in six exact-match clauses above.
    params = [str(architecture).strip()] + lowered_terms * 6
    return condition, params


def _snapshot_architecture_values(snapshot):
    """Collect raw architecture/base-model labels from source + versions.

    This intentionally remembers unknown labels (for example SDXL 1.0) without
    making them selectable architecture filters until architectures.json defines
    them.
    """
    if not isinstance(snapshot, dict):
        return []

    values = []

    def add(value):
        text = str(value or "").strip()
        if text and text.casefold() != "other":
            if text.casefold() not in {item.casefold() for item in values}:
                values.append(text)

    add(snapshot.get("architecture"))
    add(snapshot.get("base_model"))

    versions = snapshot.get("versions")
    if isinstance(versions, list):
        for version in versions:
            if isinstance(version, dict):
                add(version.get("architecture"))
                add(version.get("base_model"))

    nested = snapshot.get("card_data")
    if isinstance(nested, str):
        try:
            nested = json.loads(nested or "{}")
        except Exception:
            nested = {}
    if isinstance(nested, dict):
        add(nested.get("architecture"))
        add(nested.get("base_model"))
        nested_versions = nested.get("versions")
        if isinstance(nested_versions, list):
            for version in nested_versions:
                if isinstance(version, dict):
                    add(version.get("architecture"))
                    add(version.get("base_model"))

    return values


def _classify_detected_architectures(values):
    """Return configured canonical matches plus all raw detected labels."""
    configured = load_architectures()
    searchable = []
    detected = []

    for raw in values or []:
        raw_text = str(raw or "").strip()
        if not raw_text:
            continue

        if raw_text.casefold() not in {item.casefold() for item in detected}:
            detected.append(raw_text)

        canonical = processors.classify_architecture(raw_text)
        if canonical in configured and canonical != "Other":
            if canonical.casefold() not in {item.casefold() for item in searchable}:
                searchable.append(canonical)

    return searchable, detected


def _feed_window_base_query(
    architecture="",
    model_type="",
    status="all",
    sort="activity",
    show_media_only=False,
    sources=None,
    favorite="all",
    creator_favorite="all",
    download_status="all",
    search_text="",
):
    """Build the same base feed query used by the home page and lazy chunks."""
    configured_types = {
        name: data for name, data in load_model_types().items()
        if not isinstance(data, dict) or data.get("enabled", True)
    }
    query = "SELECT * FROM models"
    conditions = []
    params = []

    if architecture:
        architecture_condition, architecture_params = _architecture_filter_condition(architecture)
        if architecture_condition:
            conditions.append(architecture_condition)
            params.extend(architecture_params)

    enabled_model_types = list(configured_types.keys())
    if enabled_model_types:
        placeholders = ",".join("?" for _ in enabled_model_types)
        conditions.append(f"lower(model_type) IN ({placeholders})")
        params.extend(str(value).lower() for value in enabled_model_types)

    if model_type:
        conditions.append("model_type LIKE ?")
        params.append(f"%{model_type}%")

    selected_sources = [
        str(value or "").strip().lower()
        for value in (sources or [])
        if str(value or "").strip()
    ]
    if selected_sources:
        placeholders = ",".join("?" for _ in selected_sources)
        # Cards can merge several providers. Match either the canonical source
        # or any recorded source membership so the count mirrors card filtering.
        conditions.append(
            f"""(
                source IN ({placeholders})
                OR id IN (
                    SELECT model_id
                    FROM model_sources
                    WHERE source IN ({placeholders})
                )
            )"""
        )
        params.extend(selected_sources)
        params.extend(selected_sources)

    if str(status or "all").lower() == "new":
        conditions.append("viewed = 0")
    elif str(status or "all").lower() == "seen":
        conditions.append("viewed = 1")
    elif str(status or "all").lower() == "updated":
        try:
            latest = database.get_scan_history(1)
            latest_scan_id = int(latest[0]["id"]) if latest else 0
        except Exception:
            latest_scan_id = 0
        if latest_scan_id:
            conditions.append(
                "id IN (SELECT model_id FROM scan_model_changes WHERE scan_id=? AND change_type='updated')"
            )
            params.append(latest_scan_id)
        else:
            conditions.append("1=0")

    if show_media_only in (True, 1, "1", "true", "True"):
        conditions.append("has_media = 1")

    favorite = str(favorite or "all").strip().lower()
    if favorite == "favorite":
        conditions.append("favorite = 1")
    elif favorite == "not_favorite":
        conditions.append("COALESCE(favorite, 0) = 0")

    creator_favorite = str(creator_favorite or "all").strip().lower()
    if creator_favorite == "favorite":
        conditions.append("EXISTS (SELECT 1 FROM creators c WHERE c.favorite = 1 AND lower(c.name) = lower(models.author))")
    elif creator_favorite == "not_favorite":
        conditions.append("NOT EXISTS (SELECT 1 FROM creators c WHERE c.favorite = 1 AND lower(c.name) = lower(models.author))")

    download_status = str(download_status or "all").strip().lower()
    if download_status == "downloaded":
        conditions.append("EXISTS (SELECT 1 FROM download_history dh WHERE dh.model_id = models.id)")
    elif download_status == "updates":
        update_ids = sorted(_update_available_model_ids())
        if update_ids:
            placeholders = ",".join("?" for _ in update_ids)
            conditions.append(f"id IN ({placeholders})")
            params.extend(update_ids)
        else:
            conditions.append("1=0")
    elif download_status == "not_downloaded":
        conditions.append("NOT EXISTS (SELECT 1 FROM download_history dh WHERE dh.model_id = models.id)")

    # Plain Search belongs in SQLite, before LIMIT/OFFSET.  The old behavior
    # loaded the newest feed slice first and then hid non-matches in JavaScript,
    # which meant older matches could never be discovered and unrelated cards
    # could flash/remain while Search and lazy loading raced each other.
    #
    # Match the same searchable card metadata used by filters.js.  Every word
    # must occur somewhere in the card metadata, but the words may occur in
    # different fields (e.g. "pytorch lora weights").
    search_terms = [
        term.strip().lower()
        for term in re.split(r"\s+", str(search_text or "").strip())
        if term.strip()
    ]
    if search_terms:
        searchable_sql = """
            lower(
                COALESCE(display_name, '') || ' ' ||
                COALESCE(name, '') || ' ' ||
                COALESCE(author, '') || ' ' ||
                COALESCE(source, '') || ' ' ||
                COALESCE(architecture, '') || ' ' ||
                COALESCE(model_type, '') || ' ' ||
                COALESCE(tags, '') || ' ' ||
                COALESCE(sha, '')
            )
        """
        for term in search_terms:
            conditions.append(f"{searchable_sql} LIKE ?")
            params.append(f"%{term}%")

    if conditions:
        query += " WHERE " + " AND ".join(conditions)

    activity_sql = """
        CASE
            WHEN datetime(updated) IS NULL THEN datetime(created)
            WHEN datetime(created) IS NULL THEN datetime(updated)
            WHEN datetime(updated) >= datetime(created) THEN datetime(updated)
            ELSE datetime(created)
        END
    """
    if sort == "updated":
        order = "ORDER BY (datetime(updated) IS NULL) ASC, datetime(updated) DESC, datetime(created) DESC, id DESC"
    elif sort == "created":
        order = "ORDER BY (datetime(created) IS NULL) ASC, datetime(created) DESC, datetime(updated) DESC, id DESC"
    elif sort == "downloads":
        order = "ORDER BY downloads DESC, datetime(created) DESC"
    elif sort == "likes":
        order = "ORDER BY likes DESC, datetime(created) DESC"
    elif sort == "added":
        order = "ORDER BY datetime(first_seen) DESC, id DESC"
    elif sort == "name_asc":
        order = "ORDER BY lower(COALESCE(NULLIF(display_name, ''), name)) ASC"
    elif sort == "name_desc":
        order = "ORDER BY lower(COALESCE(NULLIF(display_name, ''), name)) DESC"
    else:
        order = f"ORDER BY {activity_sql} DESC, id DESC"

    return query, params, order


def _prepare_feed_chunk_models(
    models,
    preferences,
    sources,
    maturity_mode=None,
    selected_sources=None,
):
    """Prepare a lazy feed batch from maturity-eligible source snapshots."""
    if not models:
        return []

    maturity_mode = _normalize_maturity_mode(
        maturity_mode if maturity_mode is not None else preferences.get("selected_sensitive", "hide")
    )
    include_civitai_mature_media = _civitai_include_mature_media_enabled()
    selected_sources = [
        str(value or "").strip().lower()
        for value in (selected_sources or [])
        if str(value or "").strip()
    ]

    conn = database.connect()
    conn.row_factory = sqlite3.Row
    ids = [int(model["id"]) for model in models if model.get("id") is not None]
    links_by_model = {}
    videos_by_model_source = {}
    images_by_model_source = {}
    video_candidates_by_model_source = {}
    image_candidates_by_model_source = {}

    if ids:
        placeholders = ",".join("?" for _ in ids)
        for row in conn.execute(
            f"SELECT model_id,source,model_key,url,source_data FROM model_sources WHERE model_id IN ({placeholders}) ORDER BY model_id,source",
            ids,
        ).fetchall():
            links_by_model.setdefault(int(row["model_id"]), []).append(dict(row))

        for row in conn.execute(
            f"""
            SELECT model_id,source,url,thumbnail,position,id,metadata
            FROM model_media
            WHERE model_id IN ({placeholders}) AND lower(type)='video'
            ORDER BY model_id,source,position,id
            """,
            ids,
        ).fetchall():
            key = (int(row["model_id"]), str(row["source"] or "").strip().lower())
            video_candidates_by_model_source.setdefault(key, []).append(dict(row))

        for row in conn.execute(
            f"""
            SELECT model_id,source,url,thumbnail,position,id,metadata
            FROM model_media
            WHERE model_id IN ({placeholders}) AND lower(type)='image'
            ORDER BY model_id,source,position,id
            """,
            ids,
        ).fetchall():
            key = (int(row["model_id"]), str(row["source"] or "").strip().lower())
            image_candidates_by_model_source.setdefault(key, []).append(dict(row))
    conn.close()

    history = database.get_download_history_lookup() if preferences.get("track_downloads", True) is not False else {}
    sha_lookup = database.get_model_sha256_lookup(ids)

    card_color_overrides = preferences.get("source_card_colors", {}) if isinstance(preferences.get("source_card_colors", {}), dict) else {}
    source_themes = {
        name: {"color": card_color_overrides.get(name, data.get("color", "#00eaff"))}
        for name, data in sources.items()
    }
    default_color = "#00eaff"

    for model in models:
        mid = int(model["id"])
        original = dict(model)
        model["sha256_list"] = sha_lookup.get(mid, [])

        raw_links = links_by_model.get(mid, [])
        snapshots = []
        for link in raw_links:
            snap = _decode_source_snapshot(link, original)
            snap["sensitive"] = bool(_source_snapshot_sensitive(snap, original))
            snapshots.append(snap)

        if not snapshots:
            fallback_link = {
                "source": original.get("source", ""),
                "url": original.get("url", ""),
                "model_key": original.get("model_key", ""),
                "source_data": "",
            }
            snap = _decode_source_snapshot(fallback_link, original)
            snap["sensitive"] = bool(_source_snapshot_sensitive(snap, original))
            snapshots = [snap]

        # Choose preview media after applying the user's maturity preference.
        # A safe source may store mature CivitAI images when Rich Media is on;
        # those rows must never become a Hide-Mature feed preview.
        for snap in snapshots:
            snap_source = str(snap.get("source") or "").strip().lower()
            key = (mid, snap_source)
            image_candidates = image_candidates_by_model_source.get(key, [])
            video_candidates = video_candidates_by_model_source.get(key, [])
            visible_images = [
                row for row in image_candidates
                if _media_visible_for_maturity(
                    row,
                    maturity_mode,
                    include_civitai_mature_media=include_civitai_mature_media,
                )
            ]
            visible_videos = [
                row for row in video_candidates
                if _media_visible_for_maturity(
                    row,
                    maturity_mode,
                    include_civitai_mature_media=include_civitai_mature_media,
                )
            ]
            first_image = visible_images[0] if visible_images else None
            first_video = visible_videos[0] if visible_videos else None

            if first_image:
                snap["image"] = str(first_image.get("url") or "").strip()
                images_by_model_source[key] = first_image
            elif image_candidates and maturity_mode != "show":
                snap["image"] = ""

            if first_video:
                videos_by_model_source[key] = first_video

            snap["preview_count"] = len(visible_images)
            snap["has_media"] = int(bool(first_image or first_video))
            snap["has_video"] = int(bool(first_video))

        safe_snapshots = [snap for snap in snapshots if not snap.get("sensitive")]
        mature_snapshots = [snap for snap in snapshots if snap.get("sensitive")]
        visible_snapshots = snapshots if maturity_mode == "show" else safe_snapshots
        presentation_pool = visible_snapshots or snapshots

        presentation = _choose_presentation_snapshot(
            presentation_pool,
            selected_sources=selected_sources,
        ) or dict(presentation_pool[0])

        # The canonical row normally points at a local preview cache. Preserve
        # that faster path when the canonical provider wins presentation; an
        # alternate provider still uses its own snapshot/gallery preview.
        presentation_source_name = str(presentation.get("source") or "").strip().lower()
        canonical_source_name = str(original.get("source") or "").strip().lower()
        if presentation_source_name == canonical_source_name and str(original.get("image") or "").strip():
            canonical_media_rows = image_candidates_by_model_source.get((mid, canonical_source_name), [])
            if maturity_mode == "show" or not canonical_media_rows:
                presentation["image"] = original.get("image")

        _apply_presentation_snapshot(model, presentation)

        presentation_source = str(model.get("source") or "").strip().lower()
        presentation_video = videos_by_model_source.get((mid, presentation_source))
        model["image"] = display_media_url(model.get("image"), presentation_source)
        model["card_video"] = display_media_url(
            presentation_video.get("url"), presentation_video.get("source")
        ) if presentation_video else ""
        model["card_video_poster"] = display_media_url(
            presentation_video.get("thumbnail"), presentation_video.get("source")
        ) if presentation_video else ""
        if model["card_video_poster"]:
            clean = model["card_video_poster"].split("?", 1)[0].split("#", 1)[0].lower()
            if model["card_video_poster"] == model["card_video"] or clean.endswith((".mp4", ".webm", ".mov", ".m4v", ".avi", ".mkv")):
                model["card_video_poster"] = ""

        model["has_video"] = bool(model.get("card_video")) or _boolish(presentation.get("has_video"))
        model["has_media"] = bool(model.get("image") or model.get("card_video")) or _boolish(presentation.get("has_media"))
        model["preview_count"] = presentation.get("preview_count", model.get("preview_count", 0)) or 0

        # A merged card is hidden by the Mature preference only when every
        # attached source is mature. Mixed cards remain visible through their
        # safe snapshots and never borrow Red/mature media for presentation.
        model["has_safe_source"] = bool(safe_snapshots)
        model["has_sensitive_source"] = bool(mature_snapshots)
        model["sensitive"] = bool(snapshots) and not bool(safe_snapshots)
        model["all_source_list"] = [str(snap.get("source") or "") for snap in snapshots]
        model["source_list"] = [
            str(snap.get("source") or "") for snap in (visible_snapshots or snapshots)
        ]

        # Build metadata unions only from sources the current maturity setting
        # allows. A hidden source therefore cannot leak tags/creator/family into
        # the visible feed card.
        active_snapshots = visible_snapshots or snapshots
        architecture_list = []
        detected_architectures = []
        combined_tags = []
        seen_tags = set()
        author_sources = []

        for snap in active_snapshots:
            raw_architectures = _snapshot_architecture_values(snap)
            searchable_architectures, detected = _classify_detected_architectures(raw_architectures)
            for architecture in searchable_architectures:
                if architecture.casefold() not in {str(x).casefold() for x in architecture_list}:
                    architecture_list.append(architecture)
            for raw_arch in detected:
                if raw_arch.casefold() not in {str(x).casefold() for x in detected_architectures}:
                    detected_architectures.append(raw_arch)

            values = [
                part.strip()
                for part in re.split(r"[,\n]", str(snap.get("tags") or ""))
                if part.strip()
            ]
            display = snap.get("display_tags") or []
            if isinstance(display, list):
                values.extend(str(value).strip() for value in display if str(value).strip())
            for value in values:
                key = value.casefold()
                if key not in seen_tags:
                    seen_tags.add(key)
                    combined_tags.append(value)

            source = str(snap.get("source") or "").strip().lower()
            author = str(snap.get("author") or "").strip()
            if not author:
                author = _infer_source_author(source, snap.get("model_key", ""), snap.get("url", ""))
            if author and not any(
                item["source"] == source and item["author"].casefold() == author.casefold()
                for item in author_sources
            ):
                author_sources.append({
                    "author": author,
                    "source": source,
                    "color": source_themes.get(source, {}).get("color", default_color),
                })

        presentation_arch = str(model.get("architecture") or "").strip()
        if presentation_arch and presentation_arch.casefold() != "other" and presentation_arch.casefold() not in {str(x).casefold() for x in architecture_list}:
            architecture_list.insert(0, presentation_arch)
        model["architecture_list"] = architecture_list or [presentation_arch or "Other"]
        model["detected_architecture_list"] = detected_architectures
        model["tags"] = ",".join(combined_tags)
        model["display_tags"] = combined_tags
        model["author_sources"] = author_sources or [{
            "author": str(model.get("author") or ""),
            "source": presentation_source,
            "color": source_themes.get(presentation_source, {}).get("color", default_color),
        }]

        model["gated"] = bool(model.get("gated")) or metadata.is_gated(model.get("card_data", ""))
        model["access_status"] = _source_access_status(
            presentation_source,
            model["gated"],
            model.get("card_data"),
        )
        model["gated"] = model["access_status"] in {"gated", "paid_access"}
        if model["access_status"] == "public" and presentation_source not in {"tensorhub", "seaart"}:
            files = model.get("files") or []
            if isinstance(files, str):
                try:
                    files = json.loads(files or "[]")
                except Exception:
                    files = []
            if isinstance(files, list) and any(
                isinstance(file_data, str)
                or (
                    isinstance(file_data, dict)
                    and (
                        file_data.get("primary")
                        or file_data.get("download_url")
                        or file_data.get("model_file_id")
                        or file_data.get("path")
                        or file_data.get("name")
                    )
                )
                for file_data in files
            ):
                model["access_status"] = "downloadable"
        if presentation_source == "tensorhub":
            try:
                card = model.get("card_data") or {}
                if isinstance(card, str):
                    card = json.loads(card or "{}")
                access = str(((card.get("tensorhub") or {}).get("download_access") or "")).strip().lower()
            except Exception:
                access = ""
            if access == "downloadable":
                model["access_status"] = "downloadable"
            elif access in {"paid_access", "paid", "buffet"}:
                model["access_status"] = "paid_access"; model["gated"] = True
            elif access in {"gated", "non_downloadable", "restricted", "disabled"}:
                model["access_status"] = "gated"; model["gated"] = True
            else:
                model["access_status"] = "unconfirmed"
        elif presentation_source == "seaart":
            model["access_status"] = _source_access_status("seaart", model.get("gated"), model.get("card_data"))
            model["gated"] = model["access_status"] in {"gated", "paid_access"}

        _annotate_download_state(
            model,
            history,
            preferences,
            active_snapshots,
        )
        model["source_color"] = source_themes.get(presentation_source, {}).get("color", default_color)

    return models


@app.route("/")
def home():

    _home_started = time.perf_counter()
    _home_marks = {}

    architecture = request.args.get(
        "architecture",
        ""
    )


    model_type = request.args.get(
        "model_type",
        ""
    )


    status = request.args.get(
        "status",
        "all"
    )


    sort = request.args.get(
        "sort",
        "activity"
    )


    settings = load_settings()


    sources=settings.get(
        "sources",
        {}
    )

    architectures = load_architectures()

    model_types = load_model_types()

    preferences = settings.get(
        "preferences",
        {}
    )

    default_enabled_sources = [
        name for name, source in settings.get("sources", {}).items()
        if source.get("enabled")
    ]

    selected_sources = preferences.get("selected_sources", default_enabled_sources)
    selected_scan_sources = preferences.get("selected_scan_sources", default_enabled_sources)


    show_media_only = preferences.get(
            "show_media_only",
            False
        )


    conn = database.connect()

    conn.row_factory = sqlite3.Row


    query = """
    SELECT *
    FROM models
    """


    conditions = []

    params = []


    # SOURCE FILTER
    # Source selection is a live client-side filter and is also shared with SCAN.
    # Always load models from every enabled source so changing Sources can reveal
    # cards immediately without requiring a page refresh.

    # ARCHITECTURE FILTER

    if architecture:
        architecture_condition, architecture_params = _architecture_filter_condition(architecture)
        if architecture_condition:
            conditions.append(architecture_condition)
            params.extend(architecture_params)


    # MODEL TYPE FILTER

    enabled_model_types = list(model_types.keys())
    if enabled_model_types:
        # Model type labels come from several providers and are not guaranteed
        # to use identical capitalization (for example SeaArt returns "LORA").
        # Compare case-insensitively so valid cards are not silently omitted.
        type_placeholders = ",".join("?" for _ in enabled_model_types)
        conditions.append(f"lower(model_type) IN ({type_placeholders})")
        params.extend(str(value).lower() for value in enabled_model_types)

    if model_type:

        conditions.append(
            "model_type LIKE ?"
        )

        params.append(
            f"%{model_type}%"
        )


    # STATUS FILTER

    if status == "new":

        conditions.append(
            "viewed = 0"
        )


    elif status == "seen":

        conditions.append(
            "viewed = 1"
        )

    elif status == "updated":
        try:
            latest = database.get_scan_history(1)
            latest_scan_id = int(latest[0]["id"]) if latest else 0
        except Exception:
            latest_scan_id = 0

        if latest_scan_id:
            conditions.append(
                "id IN (SELECT model_id FROM scan_model_changes WHERE scan_id=? AND change_type='updated')"
            )
            params.append(latest_scan_id)
        else:
            conditions.append("1=0")


    # MEDIA FILTER

    if show_media_only == "1":

        conditions.append(
            "has_media = 1"
        )


    # APPLY FILTERS

    if conditions:

        query += " WHERE "

        query += " AND ".join(
            conditions
        )


    # The navbar count remains the true result count even though only a small
    # first card window is prepared/rendered.
    _feed_count_query = query
    _feed_count_params = list(params)
    _feed_total_count = int(conn.execute(
        f"SELECT COUNT(*) FROM ({_feed_count_query}) AS feed_count",
        _feed_count_params,
    ).fetchone()[0])
    _feed_total_new = int(conn.execute(
        f"SELECT COUNT(*) FROM ({_feed_count_query}) AS feed_count WHERE viewed = 0",
        _feed_count_params,
    ).fetchone()[0])

    # SORTING
    # Keep the feed's default freshness rule aligned with the timestamp shown
    # on cards and with Library Cleanup: whichever source timestamp is newer.
    activity_sql = """
        CASE
            WHEN datetime(updated) IS NULL THEN datetime(created)
            WHEN datetime(created) IS NULL THEN datetime(updated)
            WHEN datetime(updated) >= datetime(created) THEN datetime(updated)
            ELSE datetime(created)
        END
    """

    if sort == "activity":
        query += f"""
        ORDER BY {activity_sql} DESC, id DESC
        """

    elif sort == "updated":
        query += """
        ORDER BY (datetime(updated) IS NULL) ASC,
                 datetime(updated) DESC,
                 datetime(created) DESC,
                 id DESC
        """

    elif sort == "created":
        query += """
        ORDER BY (datetime(created) IS NULL) ASC,
                 datetime(created) DESC,
                 datetime(updated) DESC,
                 id DESC
        """

    elif sort == "downloads":

        query += """
        ORDER BY downloads DESC,
                datetime(created) DESC
        """


    elif sort == "likes":
        query += """
        ORDER BY likes DESC, datetime(created) DESC
        """
    elif sort == "added":
        query += """
        ORDER BY datetime(first_seen) DESC, id DESC
        """
    elif sort == "name_asc":
        query += """
        ORDER BY lower(COALESCE(NULLIF(display_name, ''), name)) ASC
        """
    elif sort == "name_desc":
        query += """
        ORDER BY lower(COALESCE(NULLIF(display_name, ''), name)) DESC
        """
    else:
        # Backward-compatible fallback for old/unknown sort values.
        query += f"""
        ORDER BY {activity_sql} DESC, id DESC
        """


    query += " LIMIT ? OFFSET ?"
    _page_params = list(params) + [FEED_INITIAL_CARD_LIMIT, 0]
    models = [
        dict(row)
        for row in conn.execute(
            query,
            _page_params
        ).fetchall()
    ]
    _home_marks["load_models"] = time.perf_counter()

    # Fetch source membership for every visible card in one query.  The old
    # path issued one SELECT against model_sources per model, which turns a
    # 2,500-card refresh into thousands of SQLite round trips.
    _model_ids = [int(model["id"]) for model in models if model.get("id") is not None]
    _source_lists = {}
    _source_snapshots = {}
    if _model_ids:
        _placeholders = ",".join("?" for _ in _model_ids)
        for _row in conn.execute(
            f"""
            SELECT model_id, source, model_key, url, source_data
            FROM model_sources
            WHERE model_id IN ({_placeholders})
            ORDER BY model_id, source
            """,
            _model_ids,
        ).fetchall():
            _mid = int(_row["model_id"])
            _source_lists.setdefault(_mid, []).append(_row["source"])
            try:
                _snapshot = json.loads(_row["source_data"] or "{}")
                if not isinstance(_snapshot, dict):
                    _snapshot = {}
            except Exception:
                _snapshot = {}
            _snapshot["source"] = str(_row["source"] or "").lower()
            _snapshot["model_key"] = str(_row["model_key"] or "")
            _snapshot["url"] = str(_row["url"] or _snapshot.get("url") or "")
            _source_snapshots.setdefault(_mid, []).append(_snapshot)
    _home_marks["source_membership"] = time.perf_counter()

    # Fetch one card video per visible model in a single query. Videos are
    # loaded lazily in the browser, so this adds no media downloads at startup.
    _card_videos = {}
    if _model_ids:
        _placeholders = ",".join("?" for _ in _model_ids)
        for _row in conn.execute(
            f"""
            SELECT model_id, source, url, thumbnail, position
            FROM model_media
            WHERE model_id IN ({_placeholders})
              AND lower(type)='video'
            ORDER BY model_id, position, id
            """,
            _model_ids,
        ).fetchall():
            _mid = int(_row["model_id"])
            if _mid not in _card_videos:
                _card_videos[_mid] = dict(_row)

    import json
    download_history_lookup = database.get_download_history_lookup() if preferences.get("track_downloads", True) is not False else {}
    sha256_lookup = database.get_model_sha256_lookup([model.get("id") for model in models])
    _home_marks["lookups"] = time.perf_counter()

    for model in models:
        model["sha256_list"] = sha256_lookup.get(model.get("id"), [])

        if model.get("display_tags"):

            try:

                model["display_tags"] = json.loads(
                    model["display_tags"]
                )

            except Exception:

                model["display_tags"] = []

        else:

            model["display_tags"] = []


        model["source_list"] = _source_lists.get(int(model["id"]), []) or [model.get("source", "")]

        model["image"] = display_media_url(model.get("image"), model.get("source"))
        _video = _card_videos.get(int(model["id"]))
        model["card_video"] = display_media_url(_video.get("url"), _video.get("source")) if _video else ""
        model["card_video_poster"] = display_media_url(_video.get("thumbnail"), _video.get("source")) if _video else ""
        # Never hand a browser a video file as the poster image for that same
        # video. Some older CivitAI/Red rows stored thumbnail=url for videos,
        # causing a duplicate MP4 request (one as an image, one as video) and
        # intermittent blank/spinning previews.
        if model["card_video_poster"]:
            _poster_clean = model["card_video_poster"].split("?", 1)[0].split("#", 1)[0].lower()
            if (
                model["card_video_poster"] == model["card_video"]
                or _poster_clean.endswith((".mp4", ".webm", ".mov", ".m4v", ".avi", ".mkv"))
            ):
                model["card_video_poster"] = ""

        model["gated"] = bool(model.get("gated")) or metadata.is_gated(
            model.get("card_data", "")
        )

        model["access_status"] = _source_access_status(model.get("source"), model["gated"], model.get("card_data"))
        model["gated"] = model["access_status"] in {"gated", "paid_access"}
        # For ordinary public repositories, the presence of model files is a
        # positive downloadable signal. TensorHub/SeaArt override this below.
        if model["access_status"] == "public" and str(model.get("source") or "").lower() not in {"tensorhub", "seaart"}:
            try:
                _files = json.loads(model.get("files") or "[]") if isinstance(model.get("files"), str) else (model.get("files") or [])
            except Exception:
                _files = []
            if isinstance(_files, list) and any(isinstance(f, str) or (isinstance(f, dict) and (f.get("primary") or f.get("download_url") or f.get("model_file_id") or f.get("path") or f.get("name"))) for f in _files):
                model["access_status"] = "downloadable"
        if str(model.get("source") or "").lower() == "tensorhub":
            try:
                tensor_card = json.loads(model.get("card_data") or "{}")
                tensor_access = str(((tensor_card.get("tensorhub") or {}).get("download_access") or "")).strip().lower()
            except Exception:
                tensor_access = ""
            if tensor_access == "downloadable":
                model["access_status"] = "downloadable"
            elif tensor_access in {"paid_access", "paid", "buffet"}:
                model["access_status"] = "paid_access"; model["gated"] = True
            elif tensor_access in {"gated", "non_downloadable", "restricted", "disabled"}:
                model["access_status"] = "gated"; model["gated"] = True
            else:
                model["access_status"] = "unconfirmed"
        elif str(model.get("source") or "").lower() == "seaart":
            model["access_status"] = _source_access_status("seaart", model.get("gated"), model.get("card_data"))
            model["gated"] = model["access_status"] in {"gated", "paid_access"}

        _annotate_download_state(
            model,
            download_history_lookup,
            preferences,
            _source_snapshots.get(int(model["id"]), []),
        )

        # Re-evaluate mature-content classification when rendering so older
        # database rows immediately benefit from improved detection rules.
        model["sensitive"] = bool(model.get("sensitive")) or metadata.detect_sensitive(
            model.get("name", ""),
            model.get("display_name", ""),
            model.get("tags", ""),
            model.get("card_data", ""),
            model.get("description", "")
        )


    _home_marks["card_preparation"] = time.perf_counter()
    conn.close()

    # Library summary for the Options panel.
    stats_conn = database.connect()
    library_model_count = stats_conn.execute("SELECT COUNT(*) FROM models").fetchone()[0]
    favorite_model_count = stats_conn.execute("SELECT COUNT(*) FROM models WHERE favorite = 1").fetchone()[0]
    try:
        favorite_creator_rows = stats_conn.execute("SELECT name FROM creators WHERE favorite = 1 ORDER BY lower(name)").fetchall()
        favorite_creator_names = [row[0] for row in favorite_creator_rows]
        favorite_creator_count = len(favorite_creator_names)
    except Exception:
        favorite_creator_names = []
        favorite_creator_count = 0
    stats_conn.close()
    _home_marks["library_stats"] = time.perf_counter()


    card_color_overrides = preferences.get("source_card_colors", {}) if isinstance(preferences.get("source_card_colors", {}), dict) else {}
    source_themes = {
        name: {
            "color": card_color_overrides.get(name, data.get("color", "#00eaff"))
        }
        for name, data in sources.items()
    }

    default_source_color = "#00eaff"


    models = [

        dict(model) | {

            "source_color":
                source_themes.get(
                    model["source"],
                    {}
                ).get(
                    "color",
                    default_source_color
                )

        }

        for model in models

    ]


    # Multi-source creator attribution for SHA-merged cards.  A canonical
    # model may have different uploader names on each source; keep each
    # identity visible instead of letting the canonical row overwrite them.
    if models:
        attribution_conn = database.connect()
        ids = [int(model["id"]) for model in models if model.get("id") is not None]
        placeholders = ",".join("?" for _ in ids)
        attribution_rows = attribution_conn.execute(
            f"SELECT model_id,source,model_key,url,source_data FROM model_sources WHERE model_id IN ({placeholders}) ORDER BY model_id,source",
            ids,
        ).fetchall() if ids else []
        attribution_conn.close()

        by_model = {}
        for row in attribution_rows:
            source = str(row["source"] or "").strip().lower()
            try:
                snapshot = json.loads(row["source_data"] or "{}")
            except Exception:
                snapshot = {}
            author = str(snapshot.get("author") or "").strip() if isinstance(snapshot, dict) else ""
            if not author:
                author = _infer_source_author(source, row["model_key"], row["url"])
            if not author:
                continue
            entry = {
                "author": author,
                "source": source,
                "color": source_themes.get(source, {}).get("color", default_source_color),
            }
            existing = by_model.setdefault(int(row["model_id"]), [])
            # Avoid duplicate pills only when both source and creator are the same.
            if not any(x["source"] == source and x["author"].casefold() == author.casefold() for x in existing):
                existing.append(entry)

        # Build a tag union at the same time. The canonical models row still
        # stores its source's metadata, while model_sources retains alternate
        # source snapshots. Feed-side tag: search should see the union.
        tag_union = {}
        for row in attribution_rows:
            try:
                snapshot = json.loads(row["source_data"] or "{}")
            except Exception:
                snapshot = {}
            values = []
            if isinstance(snapshot, dict):
                values.extend(part.strip() for part in re.split(r"[,\n]", str(snapshot.get("tags") or "")) if part.strip())
                display = snapshot.get("display_tags") or []
                if isinstance(display, list):
                    values.extend(str(x).strip() for x in display if str(x).strip())
            bucket = tag_union.setdefault(int(row["model_id"]), [])
            existing = {str(x).casefold() for x in bucket}
            for value in values:
                if value.casefold() not in existing:
                    bucket.append(value); existing.add(value.casefold())

        for model in models:
            attrs = by_model.get(int(model["id"]), [])
            canonical_author = str(model.get("author") or "").strip()
            canonical_source = str(model.get("source") or "").strip().lower()
            if canonical_author and not any(x["source"] == canonical_source and x["author"].casefold() == canonical_author.casefold() for x in attrs):
                attrs.insert(0, {
                    "author": canonical_author,
                    "source": canonical_source,
                    "color": source_themes.get(canonical_source, {}).get("color", default_source_color),
                })
            model["author_sources"] = attrs or [{
                "author": canonical_author,
                "source": canonical_source,
                "color": model.get("source_color", default_source_color),
            }]

            combined = []
            seen_tags = set()
            for value in [part.strip() for part in re.split(r"[,\n]", str(model.get("tags") or "")) if part.strip()] + list(model.get("display_tags") or []) + tag_union.get(int(model["id"]), []):
                text = str(value or "").strip()
                if text and text.casefold() not in seen_tags:
                    seen_tags.add(text.casefold()); combined.append(text)
            model["tags"] = ",".join(combined)
            model["display_tags"] = combined

    # Re-prepare the first feed window through the same source-aware pipeline
    # used by lazy chunks. The older preparation above remains temporarily for
    # compatibility with unrelated card state, while this final pass makes
    # maturity eligibility and presentation-source choice authoritative.
    models = _prepare_feed_chunk_models(
        models,
        preferences,
        sources,
        maturity_mode=preferences.get("selected_sensitive", "hide"),
        selected_sources=selected_sources,
    )

    _home_marks["source_attribution"] = time.perf_counter()

    # Navbar summary. These values reflect the models currently loaded for
    # the selected sources; client-side filters update the counts live.
    model_count = _feed_total_count
    new_count = _feed_total_new

    last_scan = "never"
    latest_scan_id = 0
    try:
        history = database.get_scan_history(1)
        if history:
            latest_scan_id = int(history[0]["id"] or 0)
            if history[0]["finished"]:
                last_scan = time_since(history[0]["finished"])
    except Exception:
        pass

    # Updated filtering requires concrete model IDs. Older scan history only
    # stored an aggregate count, which cannot safely be mapped back to cards.
    # Keep the navbar count identical to the filterable ID set so it can never
    # advertise "8 updated" and then return zero cards.
    latest_updated_ids = (
        database.get_scan_model_ids(latest_scan_id, "updated")
        if latest_scan_id else set()
    )
    last_scan_updated = len(latest_updated_ids)
    for model in models:
        model["latest_scan_updated"] = int(model.get("id") or 0) in latest_updated_ids


    enabled_source_names = [name for name, data in sources.items() if isinstance(data, dict) and data.get("enabled")]
    enabled_sources_for_ui = dict(sorted(
        ((name, sources[name]) for name in enabled_source_names),
        key=lambda item: str(
            (item[1].get("display") if isinstance(item[1], dict) else "") or item[0]
        ).casefold(),
    ))
    configured_enabled_architectures = preferences.get("enabled_architectures")
    if not isinstance(configured_enabled_architectures, list):
        configured_enabled_architectures = preferences.get("scan_architectures")
    if not isinstance(configured_enabled_architectures, list):
        configured_enabled_architectures = list(architectures.keys())
    enabled_architectures_for_ui = sorted(
        [name for name in configured_enabled_architectures if name in architectures and name != "Other"],
        key=lambda value: str(value).casefold(),
    )
    selected_sources = [name for name in selected_sources if name in enabled_source_names]
    selected_scan_sources = [name for name in selected_scan_sources if name in enabled_source_names]

    _home_marks["pre_render"] = time.perf_counter()
    _home_html = render_template(

        "index.html",

        models=models,

        model_count=model_count,
        new_count=new_count,
        updated_count=last_scan_updated,
        last_scan=last_scan,

        architectures=enabled_architectures_for_ui,

        model_types=model_types.keys(),

        show_media_only=show_media_only,

        preferences=preferences,

        selected_architecture=architecture,

        selected_model_type=model_type,

        selected_sort=sort,

        sources=enabled_sources_for_ui,
        discovery_sources=dict(sorted(
            (
                (name, data)
                for name, data in settings.get("sources", {}).items()
                if data.get("enabled") and SOURCE_INFO.get(name, {}).get("discovery_scan")
            ),
            key=lambda item: str(
                (item[1].get("display") if isinstance(item[1], dict) else "") or item[0]
            ).casefold(),
        )),

        selected_sources=selected_sources,
        selected_scan_sources=selected_scan_sources,
        library_model_count=library_model_count,
        favorite_model_count=favorite_model_count,
        favorite_creator_count=favorite_creator_count,
        favorite_creator_names=favorite_creator_names,
        feed_total_count=_feed_total_count,
        feed_total_new=_feed_total_new,
        feed_total_updated=last_scan_updated,
        feed_initial_count=len(models),
        feed_has_more=len(models) < _feed_total_count

    )
    return _home_html
    




@app.route("/feed/counts")
def feed_counts():
    """Return true SQL counts for the structural feed filters.

    Feed Windowing intentionally materializes only a small card batch. Navbar
    counts must therefore come from SQLite, never from the current DOM size.
    """
    architecture = str(request.args.get("architecture") or "").strip()
    model_type = str(request.args.get("model_type") or "").strip()
    status = str(request.args.get("status") or "all").strip().lower()
    show_media_only = str(request.args.get("media") or "").strip().lower() in {
        "1", "true", "yes", "on"
    }
    favorite = str(request.args.get("favorite") or "all").strip().lower()
    creator_favorite = str(request.args.get("creator_favorite") or "all").strip().lower()
    download_status = str(request.args.get("download_status") or "all").strip().lower()
    search_text = str(request.args.get("search") or "").strip()

    raw_sources = str(request.args.get("sources") or "").strip()
    selected_sources = [
        value.strip().lower()
        for value in raw_sources.split(",")
        if value.strip()
    ]

    def count_for(requested_status):
        query, params, _order = _feed_window_base_query(
            architecture=architecture,
            model_type=model_type,
            status=requested_status,
            show_media_only=show_media_only,
            sources=selected_sources,
            favorite=favorite,
            creator_favorite=creator_favorite,
            download_status=download_status,
            search_text=search_text,
        )
        conn = database.connect()
        try:
            return int(
                conn.execute(
                    f"SELECT COUNT(*) FROM ({query}) AS filtered_feed_count",
                    params,
                ).fetchone()[0]
            )
        finally:
            conn.close()

    return {
        "success": True,
        "total": count_for(status),
        # Keep New/Updated useful as shortcuts even while another status is
        # selected: they describe the same structural slice of the library.
        "new": count_for("new"),
        "updated": count_for("updated"),
    }


@app.route("/feed/chunk")
def feed_chunk():
    try:
        offset = max(0, int(request.args.get("offset", 0) or 0))
        limit = max(20, min(int(request.args.get("limit", FEED_CHUNK_CARD_LIMIT) or FEED_CHUNK_CARD_LIMIT), 120))
    except Exception:
        offset, limit = 0, FEED_CHUNK_CARD_LIMIT

    architecture = request.args.get("architecture", "")
    model_type = request.args.get("model_type", "")
    status = request.args.get("status", "all")
    sort = request.args.get("sort", "activity")
    favorite = request.args.get("favorite", "all")
    creator_favorite = request.args.get("creator_favorite", "all")
    download_status = request.args.get("download_status", "all")
    search_text = str(request.args.get("search") or "").strip()
    raw_sources = str(request.args.get("sources") or "").strip()
    selected_sources = [value.strip().lower() for value in raw_sources.split(",") if value.strip()]
    settings = load_settings()
    preferences = settings.get("preferences", {})
    maturity_mode = _normalize_maturity_mode(
        request.args.get("mature", preferences.get("selected_sensitive", "hide"))
    )
    show_media_only = preferences.get("show_media_only", False)

    base_query, params, order = _feed_window_base_query(
        architecture=architecture,
        model_type=model_type,
        status=status,
        sort=sort,
        show_media_only=show_media_only,
        sources=selected_sources,
        favorite=favorite,
        creator_favorite=creator_favorite,
        download_status=download_status,
        search_text=search_text,
    )

    conn = database.connect()
    conn.row_factory = sqlite3.Row
    total = int(conn.execute(
        f"SELECT COUNT(*) FROM ({base_query}) AS feed_count",
        params,
    ).fetchone()[0])
    rows = conn.execute(
        f"{base_query} {order} LIMIT ? OFFSET ?",
        list(params) + [limit, offset],
    ).fetchall()
    conn.close()

    models = _prepare_feed_chunk_models(
        [dict(row) for row in rows],
        preferences,
        settings.get("sources", {}),
        maturity_mode=maturity_mode,
        selected_sources=selected_sources,
    )
    try:
        latest = database.get_scan_history(1)
        latest_scan_id = int(latest[0]["id"]) if latest else 0
        latest_updated_ids = database.get_scan_model_ids(latest_scan_id, "updated") if latest_scan_id else set()
    except Exception:
        latest_updated_ids = set()
    for model in models:
        model["latest_scan_updated"] = int(model.get("id") or 0) in latest_updated_ids

    html = render_template("components/feed_cards.html", models=models)
    next_offset = offset + len(models)
    return {
        "success": True,
        "html": html,
        "offset": offset,
        "next_offset": next_offset,
        "loaded": len(models),
        "total": total,
        "has_more": next_offset < total,
    }


def _tensorhub_local_tags(query="", limit=30):
    """Return tag IDs/names already observed in TensorHub project metadata."""
    query = str(query or "").casefold().strip()
    # A tiny seed lets a fresh/cleaned library expose the verified tag used
    # during Discovery Scan development. The catalog otherwise grows
    # automatically from TensorHub project metadata already stored locally.
    counts = {("596153971932921874", "PHOTOREALISTIC"): 0}
    conn = database.connect()
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT card_data FROM models WHERE source='tensorhub' AND card_data IS NOT NULL AND card_data != ''").fetchall()
    conn.close()
    for row in rows:
        try:
            card = json.loads(row["card_data"] or "{}")
            tags = ((card.get("tensorhub") or {}).get("project_tags") or [])
        except Exception:
            continue
        for tag in tags:
            if not isinstance(tag, dict):
                continue
            tag_id = str(tag.get("id") or "").strip()
            name = str(tag.get("name") or "").strip()
            if not tag_id or not name:
                continue
            if query and query not in name.casefold() and query not in tag_id:
                continue
            key = (tag_id, name)
            counts[key] = counts.get(key, 0) + 1
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0][1].casefold()))[:max(1, min(int(limit or 30), 100))]
    return [{"id": key[0], "name": key[1], "count": count} for key, count in ordered]


def _normalized_model_tags(source, raw_tags="", card_data=None):
    """Return user-facing source tags as distinct, useful values.

    Older AbyssBeacon rows from TensorHub/ModelScope/Hugging Face may contain a
    space-joined tag string. Normalize those rows at render/autocomplete time
    while new scans migrate to comma-separated storage.
    """
    source = str(source or "").strip().lower()
    raw = str(raw_tags or "").strip()
    values = []

    # TensorHub has authoritative structured projectTags in card_data. Prefer
    # those so multi-word labels such as "STAR TREK" are never split apart.
    if source == "tensorhub":
        try:
            card = json.loads(card_data or "{}") if isinstance(card_data, str) else (card_data or {})
        except Exception:
            card = {}
        project_tags = ((card.get("tensorhub") or {}).get("project_tags") or []) if isinstance(card, dict) else []
        for tag in project_tags:
            if isinstance(tag, dict):
                text = str(tag.get("name") or "").strip()
                if text:
                    values.append(text)

    if not values and raw:
        if source == "huggingface":
            # Hugging Face API tags are machine labels/slugs. Whitespace is a
            # safe legacy delimiter here; human phrases are normally hyphenated.
            values = [part.strip() for part in re.split(r"[\s,\n]+", raw) if part.strip()]
        elif source == "modelscope":
            # Legacy ModelScope rows were joined with spaces even though some
            # values (e.g. "license:Apache License 2.0") contain spaces.
            # Split only where the next keyed metadata/tag value begins.
            values = [
                part.strip()
                for part in re.split(
                    r"[,\n]+|\s+(?=[A-Za-z_][A-Za-z0-9_-]*:)",
                    raw,
                )
                if part.strip()
            ]
        else:
            values = [part.strip() for part in re.split(r"[,\n]+", raw) if part.strip()]

    cleaned = []
    seen = set()
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue

        prefix = ""
        payload = text
        if ":" in text:
            maybe_prefix, maybe_payload = text.split(":", 1)
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", maybe_prefix.strip()):
                prefix = maybe_prefix.strip().casefold()
                payload = maybe_payload.strip()

        if source == "huggingface":
            # These describe repository infrastructure rather than the model's
            # subject/style and already have dedicated metadata fields.
            if prefix in {"region", "license", "library_name", "pipeline_tag"}:
                continue
        elif source == "modelscope":
            # ModelScope exposes these as badges/metadata. Keep semantic task
            # and custom tags, but remove their API namespace prefixes.
            if prefix in {"region", "license", "library", "framework", "format"}:
                continue
            if prefix in {"custom_tag", "task"} and payload:
                text = payload

        key = text.casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        cleaned.append(text)

    return cleaned


def _local_source_tags(source, query="", limit=30):
    """Autocomplete tags already stored for a source.

    TensorHub keeps stable numeric IDs, so retain its specialized resolver.
    Other Discovery sources can use their human-readable tag/slug directly.
    """
    source = str(source or "").strip().lower()
    query_cf = str(query or "").casefold().strip()
    if source == "tensorhub":
        return _tensorhub_local_tags(query, limit)

    counts = {}
    conn = database.connect()
    rows = conn.execute(
        "SELECT tags,display_tags FROM models WHERE source=?", (source,)
    ).fetchall()
    conn.close()
    for row in rows:
        values = _normalized_model_tags(source, row["tags"] or "")
        seen_row = set()
        for value in values:
            key = value.casefold()
            if not key or key in seen_row or (query_cf and query_cf not in key):
                continue
            seen_row.add(key)
            entry = counts.setdefault(key, [value, 0])
            entry[1] += 1
    ordered = sorted(counts.values(), key=lambda item: (-item[1], item[0].casefold()))[:max(1, min(int(limit or 30), 100))]
    return [{"id": name, "name": name, "count": count} for name, count in ordered]


@app.route("/discover/tags")
def discover_tags():
    source = str(request.args.get("source") or "tensorhub").strip().lower()
    query = str(request.args.get("q") or "").strip()
    return {"success": True, "source": source, "tags": _local_source_tags(source, query, 30)}


@app.route("/discover/tag-bank")
def discover_tag_bank():
    """Return the tag catalog used by Discovery Scan's Browse Tags panel."""
    source = str(request.args.get("source") or "tensorhub").strip().lower()
    settings = load_settings()
    configured = settings.get("sources", {})

    if not configured.get(source, {}).get("enabled"):
        return {"success": False, "error": "Enable this source before browsing Discovery tags."}, 400
    if not SOURCE_INFO.get(source, {}).get("discovery_scan"):
        return {"success": False, "error": "This source does not support Discovery Scan tags."}, 400

    try:
        if source == "modelscope":
            from scanners import modelscope as modelscope_scanner

            tags = []
            for item in modelscope_scanner.get_official_tags():
                if not isinstance(item, dict):
                    continue
                slug = str(item.get("slug") or item.get("id") or "").strip()
                name = str(item.get("name") or slug).strip()
                if not slug:
                    continue
                tags.append({
                    "id": slug,
                    "slug": slug,
                    "name": name or slug,
                    "type": str(item.get("type") or "Tags").strip() or "Tags",
                })
            tags.sort(key=lambda item: (
                str(item.get("type") or "").casefold(),
                str(item.get("name") or "").casefold(),
            ))
        else:
            tags = _local_source_tags(source, "", 100)
            for item in tags:
                if isinstance(item, dict) and not item.get("slug"):
                    item["slug"] = item.get("id") or item.get("name") or ""

        return {"success": True, "source": source, "tags": tags}
    except Exception as exc:
        logging.exception("Discovery tag bank failed for %s", source)
        display = SOURCE_INFO.get(source, {}).get("display", source)
        return {"success": False, "error": f"Could not load {display} tags: {exc}"}, 500


@app.route("/discover/scan", methods=["POST"])
def start_discovery_scan():
    data = request.get_json(silent=True) or {}
    source = str(data.get("source") or "tensorhub").strip().lower()
    discovery_type = str(data.get("type") or "tag").strip().lower()
    raw_tag = str(data.get("tag") or "").strip()
    tag_id = str(data.get("tag_id") or "").strip()
    tag_name = str(data.get("tag_name") or "").strip()
    sort_value = str(data.get("sort") or "NEWEST").strip().upper()
    watch_only = bool(data.get("watch_only", True))
    discovery_architectures = [name for name in load_architectures().keys() if name != "Other"] if watch_only else []
    try:
        max_results = max(1, min(5000, int(data.get("max_results") or 100)))
    except (TypeError, ValueError):
        max_results = 100

    settings = load_settings()
    configured = settings.get("sources", {})
    if not configured.get(source, {}).get("enabled"):
        return {"success": False, "error": "Enable this source before running Discovery Scan."}, 400
    if not SOURCE_INFO.get(source, {}).get("discovery_scan"):
        return {"success": False, "error": "This source does not support Discovery Scan yet."}, 400
    if scan_status.get_status().get("status") in {"running", "stopping"}:
        return {"success": False, "error": "A scan is already running."}, 409

    # TensorHub accepts a stable numeric tag ID. Users can select a local
    # autocomplete result, paste a numeric ID, paste a TensorHub tag URL, or
    # type the exact name of a tag AbyssBeacon has already observed.
    if source == "tensorhub":
        import re
        if not tag_id:
            match = re.search(r"(?:/tag/|/models/tag/)?(\d{8,})", raw_tag)
            if match:
                tag_id = match.group(1)
        if not tag_id and raw_tag:
            matches = _tensorhub_local_tags(raw_tag, 100)
            exact = next((item for item in matches if item["name"].casefold() == raw_tag.casefold()), None)
            if exact:
                tag_id = exact["id"]
                tag_name = tag_name or exact["name"]
        if not tag_id:
            return {"success": False, "error": "Choose a known TensorHub tag, or paste a TensorHub tag URL/ID."}, 400
        if not tag_name:
            known = _tensorhub_local_tags(tag_id, 10)
            exact_id = next((item for item in known if item["id"] == tag_id), None)
            tag_name = exact_id["name"] if exact_id else (raw_tag if raw_tag and not raw_tag.isdigit() else f"Tag {tag_id}")

    discovery_value = tag_id
    if source in {"civitai", "civitaired", "seaart", "modelscope"}:
        if not raw_tag:
            label = "CivitAI" if source in {"civitai", "civitaired"} else SOURCE_INFO.get(source, {}).get("display", source)
            return {"success": False, "error": f"Enter a {label} tag/category name."}, 400
        discovery_value = raw_tag
        tag_name = raw_tag

    allowed_sorts = {"NEWEST", "LATEST_UPDATE", "HOT_TODAY", "HIGHEST_RATED"}
    if sort_value not in allowed_sorts:
        sort_value = "NEWEST"
    if source in {"civitai", "civitaired"} and sort_value not in {"NEWEST", "HIGHEST_RATED", "MOST_DOWNLOADED"}:
        sort_value = "NEWEST"
    if source == "seaart" and sort_value not in {"NEWEST", "HIGHEST_RATED"}:
        sort_value = "NEWEST"
    if source == "modelscope":
        # The official-tag endpoint currently exposes source-default ordering.
        sort_value = "NEWEST"

    def background_discovery():
        try:
            scanner.run_discovery_scan(
                source,
                discovery_type,
                discovery_value,
                label=tag_name,
                sort=sort_value,
                max_results=max_results,
                allowed_architectures=discovery_architectures,
            )
        except Exception as exc:
            import traceback
            traceback.print_exc()
            scan_status.update_status(status="error", message=str(exc))

    threading.Thread(target=background_discovery, daemon=True).start()
    return {
        "success": True,
        "status": "started",
        "source": source,
        "type": discovery_type,
        "tag_id": tag_id,
        "tag_name": tag_name,
        "sort": sort_value,
        "max_results": max_results,
    }



@app.route("/creator/<path:author>")
def creator_page(author):
    author = unquote(author).strip()

    conn = database.connect()
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT *
        FROM models
        WHERE lower(author) = lower(?)
        ORDER BY datetime(updated) DESC, datetime(created) DESC
        """,
        (author,)
    ).fetchall()
    models = [dict(row) for row in rows]
    creator_raw_models = [dict(model) for model in models]
    settings = load_settings()
    sources = settings.get("sources", {})

    creator_preferences = settings.get("preferences", {}) if isinstance(settings.get("preferences", {}), dict) else {}
    creator_card_colors = creator_preferences.get("source_card_colors", {}) if isinstance(creator_preferences.get("source_card_colors", {}), dict) else {}
    source_themes = {
        name: creator_card_colors.get(name, data.get("color", "#00eaff"))
        for name, data in sources.items()
    }

    creator_record = database.ensure_creator(author) or {}
    creator_favorite = bool(creator_record.get("favorite"))

    source_counts = {}
    total_downloads = 0
    total_likes = 0
    new_count = 0

    # Creator pages use the same card runtime state as the main feed.  The old
    # creator path reused model_card.html visually but skipped the batched
    # source snapshots, download-history annotation, video preview lookup, and
    # normalized access state that the home route supplies.
    creator_model_ids = [
        int(model["id"]) for model in models
        if model.get("id") is not None
    ]
    creator_source_lists = {}
    creator_source_snapshots = {}
    creator_card_videos = {}

    if creator_model_ids:
        placeholders = ",".join("?" for _ in creator_model_ids)

        for row in conn.execute(
            f"""
            SELECT model_id, source, model_key, url, source_data
            FROM model_sources
            WHERE model_id IN ({placeholders})
            ORDER BY model_id, source
            """,
            creator_model_ids,
        ).fetchall():
            model_id = int(row["model_id"])
            creator_source_lists.setdefault(model_id, []).append(row["source"])
            try:
                snapshot = json.loads(row["source_data"] or "{}")
                if not isinstance(snapshot, dict):
                    snapshot = {}
            except Exception:
                snapshot = {}
            snapshot["source"] = str(row["source"] or "").lower()
            snapshot["model_key"] = str(row["model_key"] or "")
            snapshot["url"] = str(row["url"] or snapshot.get("url") or "")
            creator_source_snapshots.setdefault(model_id, []).append(snapshot)

        for row in conn.execute(
            f"""
            SELECT model_id, source, url, thumbnail, position
            FROM model_media
            WHERE model_id IN ({placeholders})
              AND lower(type)='video'
            ORDER BY model_id, position, id
            """,
            creator_model_ids,
        ).fetchall():
            model_id = int(row["model_id"])
            if model_id not in creator_card_videos:
                creator_card_videos[model_id] = dict(row)

    creator_sha256_lookup = database.get_model_sha256_lookup(creator_model_ids)
    creator_download_history = (
        database.get_download_history_lookup()
        if creator_preferences.get("track_downloads", True) is not False
        else {}
    )

    for model in models:
        model_id = int(model["id"])
        model["sha256_list"] = creator_sha256_lookup.get(model_id, [])
        model["source_color"] = source_themes.get(model.get("source"), "#00eaff")
        model["source_list"] = (
            creator_source_lists.get(model_id)
            or [model.get("source", "")]
        )

        model["image"] = display_media_url(model.get("image"), model.get("source"))

        card_video = creator_card_videos.get(model_id)
        model["card_video"] = (
            display_media_url(card_video.get("url"), card_video.get("source"))
            if card_video else ""
        )
        model["card_video_poster"] = (
            display_media_url(card_video.get("thumbnail"), card_video.get("source"))
            if card_video else ""
        )
        if model["card_video_poster"]:
            poster_clean = (
                model["card_video_poster"]
                .split("?", 1)[0]
                .split("#", 1)[0]
                .lower()
            )
            if (
                model["card_video_poster"] == model["card_video"]
                or poster_clean.endswith((".mp4", ".webm", ".mov", ".m4v", ".avi", ".mkv"))
            ):
                model["card_video_poster"] = ""

        model["gated"] = bool(model.get("gated")) or metadata.is_gated(
            model.get("card_data", "")
        )
        model["access_status"] = _source_access_status(
            model.get("source"),
            model["gated"],
            model.get("card_data"),
        )
        model["gated"] = model["access_status"] in {"gated", "paid_access"}

        if (
            model["access_status"] == "public"
            and str(model.get("source") or "").lower() not in {"tensorhub", "seaart"}
        ):
            try:
                source_files = (
                    json.loads(model.get("files") or "[]")
                    if isinstance(model.get("files"), str)
                    else (model.get("files") or [])
                )
            except Exception:
                source_files = []
            if isinstance(source_files, list) and any(
                isinstance(file_data, str)
                or (
                    isinstance(file_data, dict)
                    and (
                        file_data.get("primary")
                        or file_data.get("download_url")
                        or file_data.get("model_file_id")
                        or file_data.get("path")
                        or file_data.get("name")
                    )
                )
                for file_data in source_files
            ):
                model["access_status"] = "downloadable"

        if str(model.get("source") or "").lower() == "tensorhub":
            try:
                tensor_card = json.loads(model.get("card_data") or "{}")
                tensor_access = str(
                    ((tensor_card.get("tensorhub") or {}).get("download_access") or "")
                ).strip().lower()
            except Exception:
                tensor_access = ""
            if tensor_access == "downloadable":
                model["access_status"] = "downloadable"
            elif tensor_access in {"paid_access", "paid", "buffet"}:
                model["access_status"] = "paid_access"
                model["gated"] = True
            elif tensor_access in {
                "gated", "non_downloadable", "restricted", "disabled"
            }:
                model["access_status"] = "gated"
                model["gated"] = True
            else:
                model["access_status"] = "unconfirmed"
        elif str(model.get("source") or "").lower() == "seaart":
            model["access_status"] = _source_access_status(
                "seaart",
                model.get("gated"),
                model.get("card_data"),
            )
            model["gated"] = model["access_status"] in {"gated", "paid_access"}

        _annotate_download_state(
            model,
            creator_download_history,
            creator_preferences,
            creator_source_snapshots.get(model_id, []),
        )

        model["sensitive"] = bool(model.get("sensitive")) or metadata.detect_sensitive(
            model.get("name", ""), model.get("display_name", ""),
            model.get("tags", ""), model.get("card_data", ""),
            model.get("description", "")
        )

        try:
            model["display_tags"] = json.loads(model.get("display_tags") or "[]")
        except Exception:
            model["display_tags"] = []

        source = model.get("source") or "unknown"
        source_counts[source] = source_counts.get(source, 0) + 1
        total_downloads += int(model.get("downloads") or 0)
        total_likes += int(model.get("likes") or 0)
        if not model.get("viewed"):
            new_count += 1

    # Final creator-card preparation uses the same independent source-snapshot
    # pipeline as the home feed. Keep the legacy preparation above as a low-risk
    # compatibility pass for now, but do not let its canonical maturity/source
    # assumptions decide what the user ultimately sees.
    models = _prepare_feed_chunk_models(
        creator_raw_models,
        creator_preferences,
        sources,
        maturity_mode=creator_preferences.get("selected_sensitive", "hide"),
        selected_sources=creator_preferences.get("selected_sources", []),
    )
    source_counts = {}
    total_downloads = 0
    total_likes = 0
    new_count = 0
    for model in models:
        source_name = str(model.get("source") or "unknown")
        source_counts[source_name] = source_counts.get(source_name, 0) + 1
        total_downloads += int(model.get("downloads") or 0)
        total_likes += int(model.get("likes") or 0)
        if not model.get("viewed"):
            new_count += 1

    conn.close()

    enabled_cross_source_sources = [
        name for name, data in sources.items()
        if data.get("enabled")
    ]
    enabled_creator_sources = [
        name for name in enabled_cross_source_sources
        if SOURCE_INFO.get(name, {}).get("creator_scan", False)
    ]

    # Keep creator history separate from the main scan history so creator
    # activity never changes the navbar's Last scan timestamp.
    conn = database.connect()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS creator_scan_runs (
            id INTEGER PRIMARY KEY, creator TEXT NOT NULL, started TEXT,
            finished TEXT, duration REAL DEFAULT 0, mode TEXT,
            architecture TEXT, model_type TEXT, sources TEXT,
            processed INTEGER DEFAULT 0, added INTEGER DEFAULT 0,
            updated INTEGER DEFAULT 0
        )
    """)
    creator_history_rows = conn.execute("""
        SELECT * FROM creator_scan_runs
        WHERE lower(creator) = lower(?)
        ORDER BY id DESC LIMIT 5
    """, (author,)).fetchall()
    conn.close()

    creator_history = []
    for row in creator_history_rows:
        item = dict(row)
        try:
            item["sources_list"] = json.loads(item.get("sources") or "[]")
        except Exception:
            item["sources_list"] = []
        item["finished_ago"] = time_since(item.get("finished"))
        creator_history.append(item)

    creator_sources = sorted({source for model in models for source in (model.get("source_list") or [model.get("source")]) if source})
    blocked_sources = [source for source in creator_sources if database.is_creator_blocked(source, author)]

    # Prefer provider-specific creator identities learned during scans. For
    # sources with stable public username routes, provide a deterministic
    # fallback so Found On pills can lead back to the creator at the source.
    creator_source_urls = {}
    try:
        for identity in database.get_creator_source_identities(creator_name=author):
            source_name = str(identity.get("source") or "").strip().lower()
            source_creator_id = str(identity.get("source_creator_id") or "").strip()
            profile_url = str(identity.get("profile_url") or "").strip()

            # TensorHub creator identities are learned from the opaque owner ID.
            # Older creator_sources rows may predate profile URL persistence, so
            # derive the stable public profile route from that ID when needed.
            if source_name == "tensorhub" and not profile_url and source_creator_id:
                profile_url = f"https://tensorhub.art/u/{quote(source_creator_id, safe='')}"

            if source_name and profile_url:
                creator_source_urls[source_name] = profile_url
    except Exception:
        pass

    encoded_author = quote(author, safe="")
    creator_source_urls.setdefault("huggingface", f"https://huggingface.co/{encoded_author}")
    creator_source_urls.setdefault("modelscope", f"https://modelscope.cn/profile/{encoded_author}")
    creator_source_urls.setdefault("civitai", f"https://civitai.com/user/{encoded_author}/models")
    creator_source_urls.setdefault("civitaired", f"https://civitai.red/user/{encoded_author}/models")

    # Main navbar/runtime context. Creator pages should not silently lose
    # Active Downloads, Download Manager, Local Installer preferences, settings,
    # or the normal AbyssBeacon scan/search controls.
    architectures = load_architectures()
    model_types = load_model_types()
    default_enabled_sources = [
        name for name, source in sources.items()
        if source.get("enabled")
    ]
    selected_sources = creator_preferences.get(
        "selected_sources",
        default_enabled_sources,
    )
    selected_scan_sources = creator_preferences.get(
        "selected_scan_sources",
        default_enabled_sources,
    )
    enabled_sources_for_ui = dict(sorted(
        (
            (name, data)
            for name, data in sources.items()
            if isinstance(data, dict) and data.get("enabled")
        ),
        key=lambda item: str(item[1].get("display") or item[0]).casefold(),
    ))
    selected_sources = [name for name in selected_sources if name in enabled_sources_for_ui]
    selected_scan_sources = [name for name in selected_scan_sources if name in enabled_sources_for_ui]
    configured_enabled_architectures = creator_preferences.get("enabled_architectures")
    if not isinstance(configured_enabled_architectures, list):
        configured_enabled_architectures = creator_preferences.get("scan_architectures")
    if not isinstance(configured_enabled_architectures, list):
        configured_enabled_architectures = list(architectures.keys())
    enabled_architectures_for_ui = sorted(
        [name for name in configured_enabled_architectures if name in architectures and name != "Other"],
        key=lambda value: str(value).casefold(),
    )
    discovery_sources = dict(sorted(
        (
            (name, data)
            for name, data in sources.items()
            if data.get("enabled") and SOURCE_INFO.get(name, {}).get("discovery_scan")
        ),
        key=lambda item: str(
            (item[1].get("display") if isinstance(item[1], dict) else "") or item[0]
        ).casefold(),
    ))

    last_scan = "never"
    try:
        history = database.get_scan_history(1)
        if history and history[0]["finished"]:
            last_scan = time_since(history[0]["finished"])
    except Exception:
        pass

    stats_conn = database.connect()
    library_model_count = stats_conn.execute(
        "SELECT COUNT(*) FROM models"
    ).fetchone()[0]
    favorite_model_count = stats_conn.execute(
        "SELECT COUNT(*) FROM models WHERE favorite = 1"
    ).fetchone()[0]
    try:
        favorite_creator_rows = stats_conn.execute(
            "SELECT name FROM creators WHERE favorite = 1 ORDER BY lower(name)"
        ).fetchall()
        favorite_creator_names = [row[0] for row in favorite_creator_rows]
        favorite_creator_count = len(favorite_creator_names)
    except Exception:
        favorite_creator_names = []
        favorite_creator_count = 0
    stats_conn.close()

    return render_template(
        "creator.html",
        creator=author,
        models=models,
        model_count=len(models),
        new_count=new_count,
        source_counts=source_counts,
        total_downloads=total_downloads,
        total_likes=total_likes,
        sources=enabled_sources_for_ui,
        discovery_sources=discovery_sources,
        selected_sources=selected_sources,
        enabled_creator_sources=enabled_creator_sources,
        enabled_cross_source_sources=enabled_cross_source_sources,
        creator_architectures=list(load_architectures().keys()),
        creator_model_types=list(load_model_types().keys()),
        creator_history=creator_history,
        creator_last_scan=(creator_history[0] if creator_history else None),
        creator_favorite=creator_favorite,
        creator_sources=creator_sources,
        creator_source_urls=creator_source_urls,
        blocked_sources=blocked_sources,
        # Navbar / shared runtime
        preferences=creator_preferences,
        architectures=enabled_architectures_for_ui,
        model_types=model_types.keys(),
        selected_scan_sources=selected_scan_sources,
        last_scan=last_scan,
        library_model_count=library_model_count,
        favorite_model_count=favorite_model_count,
        favorite_creator_count=favorite_creator_count,
        favorite_creator_names=favorite_creator_names,
        selected_architecture="",
        selected_model_type="",
        selected_sort="activity",
        show_media_only=False,
        creator_page_mode=True,
    )


@app.route("/creator/<path:author>/sources/check")
def check_creator_sources(author):
    """Lightweight exact-username presence check across supported creator sources."""
    author = unquote(author).strip()
    if not author:
        return {"success": False, "error": "Creator name is required."}, 400

    settings = load_settings()
    configured_sources = settings.get("sources", {})

    conn = database.connect()
    conn.row_factory = sqlite3.Row
    stored_rows = conn.execute(
        """
        SELECT source, COUNT(*) AS count
        FROM models
        WHERE lower(author) = lower(?)
        GROUP BY source
        """,
        (author,)
    ).fetchall()
    conn.close()
    stored_counts = {row["source"]: int(row["count"] or 0) for row in stored_rows}

    enabled_sources = [name for name, data in configured_sources.items() if data.get("enabled")]

    # Some providers (notably TensorHub) use opaque creator IDs rather than a
    # stable public username route. AbyssBeacon remembers those provider IDs as
    # soon as a model from that creator is observed, so Matching Sources can
    # safely use the learned account identity without guessing a username.
    known_creator_identities = {}
    try:
        for identity in database.get_creator_source_identities(creator_name=author):
            source_name = str(identity.get("source") or "").strip().lower()
            source_creator_id = str(identity.get("source_creator_id") or "").strip()
            if source_name and source_creator_id:
                known_creator_identities.setdefault(source_name, []).append(identity)
    except Exception:
        known_creator_identities = {}

    results = {}
    for name in enabled_sources:
        stored = stored_counts.get(name, 0)
        identities = known_creator_identities.get(name, [])
        identity_known = bool(identities)
        can_exact_check = bool(SOURCE_INFO.get(name, {}).get("creator_check", False))

        if stored:
            status = "stored"
        elif identity_known:
            status = "known_identity"
        elif can_exact_check:
            status = "unknown"
        else:
            status = "unsupported"

        results[name] = {
            "source": name,
            "display": configured_sources.get(name, {}).get(
                "display",
                SOURCE_INFO.get(name, {}).get("display", name.capitalize()),
            ),
            "stored": stored,
            "found": bool(stored or identity_known),
            "identity_known": identity_known,
            "identity_count": len(identities),
            "status": status,
        }

    if configured_sources.get("huggingface", {}).get("enabled"):
        item = {
            "source": "huggingface",
            "display": configured_sources.get("huggingface", {}).get("display", "Hugging Face"),
            "stored": stored_counts.get("huggingface", 0),
            "found": False,
            "status": "unknown"
        }
        try:
            response = requests.get(
                "https://huggingface.co/api/models",
                params={"author": author, "limit": 1, "sort": "lastModified", "direction": -1},
                timeout=10,
                headers={"User-Agent": "AbyssBeacon/1.0"}
            )
            if response.status_code == 200:
                payload = response.json()
                item["found"] = isinstance(payload, list) and len(payload) > 0
                item["status"] = "found" if item["found"] else "not_found"
            else:
                item["status"] = "error"
                item["error"] = f"HTTP {response.status_code}"
        except Exception as exc:
            item["status"] = "error"
            item["error"] = str(exc)
        results["huggingface"] = item

    if configured_sources.get("modelscope", {}).get("enabled"):
        item = {
            "source": "modelscope",
            "display": configured_sources.get("modelscope", {}).get("display", "ModelScope"),
            "stored": stored_counts.get("modelscope", 0),
            "found": False,
            "status": "unknown"
        }
        try:
            response = requests.get(
                "https://modelscope.cn/openapi/v1/models",
                params={
                    "owner": author,
                    "sort": "last_modified",
                    "page_number": 1,
                    "page_size": 1
                },
                timeout=12,
                headers={"User-Agent": "AbyssBeacon/1.0"}
            )
            if response.status_code == 200:
                payload = response.json()
                models = []
                if isinstance(payload, list):
                    models = payload
                elif isinstance(payload, dict):
                    data = payload.get("data")
                    if isinstance(data, list):
                        models = data
                    elif isinstance(data, dict):
                        models = data.get("models") or data.get("Models") or []
                item["found"] = isinstance(models, list) and len(models) > 0
                item["status"] = "found" if item["found"] else "not_found"
            else:
                item["status"] = "error"
                item["error"] = f"HTTP {response.status_code}"
        except Exception as exc:
            item["status"] = "error"
            item["error"] = str(exc)
        results["modelscope"] = item

    if configured_sources.get("civitai", {}).get("enabled"):
        item = {
            "source": "civitai",
            "display": configured_sources.get("civitai", {}).get("display", "CivitAI"),
            "stored": stored_counts.get("civitai", 0),
            "found": False,
            "status": "unknown"
        }
        try:
            response = requests.get(
                "https://civitai.com/api/v1/models",
                params={"username": author, "limit": 1, "sort": "Newest"},
                timeout=10,
                headers={"User-Agent": "AbyssBeacon/1.0", "Accept": "application/json"}
            )
            if response.status_code == 200:
                payload = response.json()
                models = payload.get("items", []) if isinstance(payload, dict) else []
                item["found"] = isinstance(models, list) and len(models) > 0
                item["status"] = "found" if item["found"] else "not_found"
            else:
                item["status"] = "error"
                item["error"] = f"HTTP {response.status_code}"
        except Exception as exc:
            item["status"] = "error"
            item["error"] = str(exc)
        results["civitai"] = item

    return {
        "success": True,
        "creator": author,
        "results": results,
        "note": "Sources with public username lookup are checked by exact username. Opaque providers such as TensorHub use creator identities AbyssBeacon previously learned from observed models; AbyssBeacon does not guess those accounts by username."
    }


@app.route("/creator/<path:author>/scan", methods=["POST"])
def scan_creator(author):
    author = unquote(author).strip()
    if not author:
        return {"success": False, "error": "Creator name is required."}, 400

    current = scan_status.get_status()
    if current.get("status") == "running":
        return {"success": False, "error": "A scan is already running."}, 409

    payload = request.get_json(silent=True) or {}
    requested_sources = payload.get("sources") or []

    scan_mode = str(payload.get("mode") or "targeted").strip().lower()
    if scan_mode not in {"targeted", "matching", "everything"}:
        scan_mode = "targeted"

    architecture = str(payload.get("architecture") or "").strip()
    requested_architectures = payload.get("architectures") or []
    configured_architectures = load_architectures()
    matching_architectures = []
    seen_matching_architectures = set()
    for value in requested_architectures if isinstance(requested_architectures, list) else []:
        name = str(value or "").strip()
        if not name or name not in configured_architectures or name in seen_matching_architectures:
            continue
        seen_matching_architectures.add(name)
        matching_architectures.append(name)

    # If Matching Sources is used before the main scan preferences have ever
    # been saved, mirror the main SCAN default: all configured architectures
    # except the catch-all Other bucket.
    if scan_mode == "matching" and not matching_architectures:
        matching_architectures = [
            name for name in configured_architectures.keys()
            if name != "Other"
        ]

    model_type = str(payload.get("model_type") or "").strip()

    if scan_mode == "targeted" and not architecture and not model_type:
        return {
            "success": False,
            "error": "Choose an architecture or model type, or select Scan Everything."
        }, 400
    settings = load_settings()
    configured_sources = settings.get("sources", {})

    supported = {name for name, info in SOURCE_INFO.items() if info.get("creator_scan")}
    selected_sources = [
        source for source in requested_sources
        if source in supported
        and source in configured_sources
        and configured_sources[source].get("enabled")
    ]

    if not selected_sources:
        selected_sources = [
            name for name, data in configured_sources.items()
            if data.get("enabled") and name in supported
        ]

    if not selected_sources:
        return {
            "success": False,
            "error": "Enable at least one creator-capable source before scanning a creator."
        }, 400

    # Publish RUNNING before launching the background thread so creator.js
    # cannot poll the old idle/complete state and dismiss the UI at startup.
    scan_status.reset_status()
    scan_status.update_status(
        status="running",
        source="",
        current=author,
        processed=0,
        added=0,
        updated=0,
        media=0,
        images=0,
        videos=0,
        message=f"Starting creator scan for {author}...",
        sources={},
    )

    def background_creator_scan():
        try:
            import scanner
            scanner.run_creator_scan(
                author,
                selected_sources,
                mode=scan_mode,
                architecture=architecture,
                architectures=matching_architectures,
                model_type=model_type
            )
        except Exception as exc:
            import traceback
            traceback.print_exc()
            scan_status.update_status(status="error", message=str(exc))

    threading.Thread(target=background_creator_scan, daemon=True).start()

    return {
        "success": True,
        "status": "started",
        "creator": author,
        "sources": selected_sources,
        "mode": scan_mode,
        "architecture": architecture,
        "architectures": matching_architectures,
        "model_type": model_type
    }



def _library_cleanup_candidates(days, architectures=None, include_unknown=False, creator_days=None, protect_downloaded=True):
    """Return cleanup candidates using each model's retention policy.

    Normal discovery rows use source created/updated dates. Models first added
    by an explicit Creator Scan use their AbyssBeacon creator-discovery timestamp
    instead, because deep creator scans intentionally reach beyond the normal
    source search window.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    creator_cutoff = now - timedelta(days=(days if creator_days is None else creator_days))
    conn = database.connect()
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT id, name, author, source, model_key, architecture, created, updated, first_seen, last_seen, retention_mode, creator_discovered_at, favorite FROM models").fetchall()
    try:
        favorite_creators = {
            str(row[0]).lower()
            for row in conn.execute("SELECT name FROM creators WHERE favorite = 1").fetchall()
        }
    except sqlite3.OperationalError:
        favorite_creators = set()
    conn.close()

    old = []
    unknown = []
    favorite_model_count = 0
    favorite_creator_model_count = 0
    protected_ids = set()
    # Downloaded models are always protected from age-based cleanup/retention.
    downloaded_keys = database.downloaded_model_keys()
    downloaded_ids = database.downloaded_model_ids()

    selected_architectures = {
        str(value).strip().casefold()
        for value in (architectures or [])
        if str(value).strip()
    }

    for row in rows:
        if selected_architectures and str(row["architecture"] or "Other").strip().casefold() not in selected_architectures:
            continue
        retention_mode = str(row["retention_mode"] or "source")
        if retention_mode == "creator_added":
            activity = parse_datetime(row["creator_discovered_at"] or "")
            if not activity:
                # Defensive fallback for a partially migrated row.
                activity = parse_datetime(row["last_seen"] or "") or parse_datetime(row["first_seen"] or "")
        else:
            created_dt = parse_datetime(row["created"] or "")
            updated_dt = parse_datetime(row["updated"] or "")
            source_dates = [dt for dt in (created_dt, updated_dt) if dt]
            if source_dates:
                activity = max(source_dates)
            else:
                activity = parse_datetime(row["last_seen"] or "") or parse_datetime(row["first_seen"] or "")

        if not activity:
            unknown.append(row["id"])
            continue
        if activity.tzinfo is None:
            activity = activity.replace(tzinfo=timezone.utc)
        row_cutoff = creator_cutoff if retention_mode == "creator_added" else cutoff
        if activity >= row_cutoff:
            continue

        old.append(row["id"])
        if bool(row["favorite"]):
            favorite_model_count += 1
            protected_ids.add(row["id"])
        if str(row["author"] or "").lower() in favorite_creators:
            favorite_creator_model_count += 1
            protected_ids.add(row["id"])
        if row["id"] in downloaded_ids or (str(row["source"] or "").lower(), str(row["model_key"] or "")) in downloaded_keys:
            protected_ids.add(row["id"])

    # Unknown-age models are deliberately excluded unless the user explicitly opts in.
    # When included, favorites and favorite creators remain protected exactly like dated models.
    unknown_protected = []
    if include_unknown:
        row_by_id = {row["id"]: row for row in rows}
        for model_id in unknown:
            row = row_by_id.get(model_id)
            if not row:
                continue
            old.append(model_id)
            if bool(row["favorite"]):
                favorite_model_count += 1
                protected_ids.add(model_id)
                unknown_protected.append(model_id)
            if str(row["author"] or "").lower() in favorite_creators:
                favorite_creator_model_count += 1
                protected_ids.add(model_id)
                unknown_protected.append(model_id)
            if model_id in downloaded_ids or (str(row["source"] or "").lower(), str(row["model_key"] or "")) in downloaded_keys:
                protected_ids.add(model_id)
                unknown_protected.append(model_id)

    deletable = [model_id for model_id in old if model_id not in protected_ids]
    return {
        "days": days,
        "cutoff": cutoff.isoformat(),
        "matched_ids": old,
        "deletable_ids": deletable,
        "protected_ids": list(protected_ids),
        "matched": len(old),
        "deletable": len(deletable),
        "protected": len(protected_ids),
        "favorite_models": favorite_model_count,
        "favorite_creator_models": favorite_creator_model_count,
        "unknown_age": len(unknown),
        "include_unknown": bool(include_unknown),
        "architectures": list(architectures or []),
    }





def _delete_library_models(ids):
    """Delete model rows + media/source rows and clean their local previews."""
    ids = list(dict.fromkeys(int(x) for x in ids))
    if not ids:
        return 0
    conn = database.connect()
    cached_previews = []
    for offset in range(0, len(ids), 500):
        chunk = ids[offset:offset + 500]
        placeholders = ",".join("?" for _ in chunk)
        cached_previews.extend(row[0] for row in conn.execute(
            f"SELECT image FROM models WHERE id IN ({placeholders})", chunk
        ).fetchall() if row[0])
    try:
        conn.execute("BEGIN")
        for offset in range(0, len(ids), 500):
            chunk = ids[offset:offset + 500]
            placeholders = ",".join("?" for _ in chunk)
            conn.execute(f"DELETE FROM model_media WHERE model_id IN ({placeholders})", chunk)
            conn.execute(f"DELETE FROM model_sources WHERE model_id IN ({placeholders})", chunk)
            conn.execute(f"DELETE FROM models WHERE id IN ({placeholders})", chunk)
        conn.commit()
    except Exception:
        conn.rollback(); raise
    finally:
        conn.close()
    try:
        from preview_cache import delete_cached_preview
        for preview in cached_previews:
            delete_cached_preview(preview)
    except Exception:
        pass
    return len(ids)


def _run_automatic_library_cleanup():
    settings = load_settings()
    prefs = settings.get("preferences", {}) if isinstance(settings, dict) else {}
    if not prefs.get("auto_cleanup_enabled", False):
        return 0
    days = max(0, min(36500, int(prefs.get("auto_cleanup_days", 7))))
    creator_days = max(0, min(36500, int(prefs.get("creator_cleanup_days", 30))))
    # Automatic retention is global across every architecture.
    result = _library_cleanup_candidates(days, architectures=None, include_unknown=False, creator_days=creator_days, protect_downloaded=True)

    # Detailed cleanup diagnostics are useful for debugging but too noisy for normal scans.
    from scan_logging import verbose_enabled
    deletable_ids = list(result["deletable_ids"])
    if deletable_ids and verbose_enabled():
        diag_conn = database.connect()
        try:
            source_counts = {}
            samples = []
            now = datetime.now(timezone.utc)
            for offset in range(0, len(deletable_ids), 500):
                chunk = deletable_ids[offset:offset + 500]
                placeholders = ",".join("?" for _ in chunk)
                diag_rows = diag_conn.execute(
                    f"SELECT id,name,source,architecture,created,updated,first_seen,last_seen,retention_mode,creator_discovered_at "
                    f"FROM models WHERE id IN ({placeholders})", chunk
                ).fetchall()
                for row in diag_rows:
                    source = str(row["source"] or "unknown")
                    source_counts[source] = source_counts.get(source, 0) + 1
                    if len(samples) < 8:
                        last_seen_dt = parse_datetime(row["last_seen"] or "")
                        seen_age = None
                        if last_seen_dt:
                            if last_seen_dt.tzinfo is None:
                                last_seen_dt = last_seen_dt.replace(tzinfo=timezone.utc)
                            seen_age = max(0.0, (now - last_seen_dt).total_seconds())
                        samples.append((row, seen_age))
        finally:
            diag_conn.close()

        print("\nAUTO CLEANUP DIAGNOSTIC")
        print(f"  Normal retention : {days} day(s)")
        print(f"  Creator retention: {creator_days} day(s)")
        print(f"  Candidates       : {len(deletable_ids)}")
        print("  By source        : " + ", ".join(f"{k}={v}" for k, v in sorted(source_counts.items())))
        print(f"  Unknown-age rows : {result.get('unknown_age', 0)} (not deleted)")
        print("  Sample candidates:")
        for row, seen_age in samples:
            seen_text = "unknown" if seen_age is None else f"{seen_age:.1f}s ago"
            if str(row["retention_mode"] or "source") == "creator_added":
                activity = row["creator_discovered_at"] or row["last_seen"] or row["first_seen"] or "unknown"
                basis = "creator-added"
            else:
                activity = row["updated"] or row["created"] or row["last_seen"] or row["first_seen"] or "unknown"
                basis = "source-date"
            print(f"    [{row['source']}] {row['name']} | activity={activity} | basis={basis} | last_seen={seen_text}")
        print("  NOTE: normal rows use source dates; creator-added rows use their AbyssBeacon added date.")

    tombstones_saved = 0
    if deletable_ids:
        try:
            tombstones_saved = database.remember_retention_tombstones_for_model_ids(deletable_ids)
        except Exception as exc:
            print(f"AUTO CLEANUP retention memory warning: {type(exc).__name__}: {exc}")

    deleted = _delete_library_models(deletable_ids)
    try:
        from preview_cache import clean_orphaned_previews
        cache_result = clean_orphaned_previews()
    except Exception:
        cache_result = {"removed": 0, "bytes_freed": 0}
    if deleted or cache_result.get("removed"):
        memory_text = f"; remembered {tombstones_saved} retention exclusion(s)" if tombstones_saved else ""
        print(f"AUTO CLEANUP: deleted {deleted} old models; removed {cache_result.get('removed', 0)} orphaned previews{memory_text}")
    return deleted



@app.route("/api/library/bulk-preview")
def library_bulk_preview():
    sources = [s.strip().lower() for s in request.args.getlist("source") if s.strip()]
    architectures = [a.strip() for a in request.args.getlist("architecture") if a.strip()]
    mode = str(request.args.get("mode") or "delete_selected").strip()
    conn = database.connect()
    rows = conn.execute("SELECT id, source, model_key, architecture, favorite, author FROM models").fetchall()
    favorite_creators = {str(r[0]).casefold() for r in conn.execute("SELECT name FROM creators WHERE favorite=1").fetchall()}
    conn.close()
    # Downloaded models are protected by default; this bulk tool may only include them
    # when the user explicitly checks the protected-model override.
    downloaded_keys = database.downloaded_model_keys()
    downloaded_ids = database.downloaded_model_ids()
    matched=[]; protected=[]
    for row in rows:
        if sources and str(row["source"] or "").lower() not in sources: continue
        arch=str(row["architecture"] or "Other")
        selected=arch in architectures
        if mode == "keep_selected": selected = not selected
        if not selected: continue
        matched.append(row["id"])
        if int(row["favorite"] or 0) or str(row["author"] or "").casefold() in favorite_creators or row["id"] in downloaded_ids or (str(row["source"] or "").lower(), str(row["model_key"] or "")) in downloaded_keys: protected.append(row["id"])
    return {"success":True,"matched":len(matched),"deletable":len(matched)-len(protected),"protected":len(protected),"sources":sources,"architectures":architectures,"mode":mode}

@app.route("/api/library/bulk-delete", methods=["POST"])
def library_bulk_delete():
    data=request.get_json(silent=True) or {}
    sources=[str(x).strip().lower() for x in data.get("sources",[]) if str(x).strip()]
    architectures=[str(x).strip() for x in data.get("architectures",[]) if str(x).strip()]
    mode=str(data.get("mode") or "delete_selected").strip()
    include_protected=bool(data.get("include_protected",False))
    if not sources: return {"success":False,"error":"Select at least one source."},400
    if not architectures: return {"success":False,"error":"Select at least one architecture."},400
    conn=database.connect(); rows=conn.execute("SELECT id,source,model_key,architecture,favorite,author FROM models").fetchall()
    favorite_creators={str(r[0]).casefold() for r in conn.execute("SELECT name FROM creators WHERE favorite=1").fetchall()}; conn.close()
    downloaded_keys=database.downloaded_model_keys()
    downloaded_ids=database.downloaded_model_ids()
    ids=[]
    for row in rows:
        if str(row["source"] or "").lower() not in sources: continue
        selected=str(row["architecture"] or "Other") in architectures
        if mode == "keep_selected": selected=not selected
        if not selected: continue
        is_protected=int(row["favorite"] or 0) or str(row["author"] or "").casefold() in favorite_creators or row["id"] in downloaded_ids or (str(row["source"] or "").lower(), str(row["model_key"] or "")) in downloaded_keys
        if is_protected and not include_protected: continue
        ids.append(row["id"])
    deleted=_delete_library_models(ids)
    return {"success":True,"deleted":deleted}

@app.route("/api/library/preview-cache/clean", methods=["POST"])
def clean_preview_cache_api():
    from preview_cache import clean_orphaned_previews
    result = clean_orphaned_previews()
    return {"success": True, **result}


@app.route("/api/library/preview-cache/repair", methods=["POST"])
def repair_preview_cache_api():
    from preview_cache import repair_missing_previews
    payload = request.json or {}
    print("Preview repair: checking for missing card previews...")
    result = repair_missing_previews(limit=payload.get("limit"))
    print(
        "Preview repair complete:",
        f"{result.get('repaired', 0)} restored,",
        f"{result.get('failed', 0)} unavailable,",
        f"{result.get('missing', 0)} missing checked"
    )
    return {"success": True, **result}


@app.route("/api/library/backfill-descriptions", methods=["POST"])
def backfill_descriptions():
    from description_backfill import fetch_description
    offset = max(0, int((request.json or {}).get("offset", 0)))
    batch = database.get_models_missing_description(50, offset=offset)
    updated = 0
    checked = 0
    for row in batch:
        checked += 1
        description = fetch_description(row)
        if description:
            database.update_description(row["id"], description)
            updated += 1
    remaining = database.count_models_missing_description()
    return {"success": True, "checked": checked, "updated": updated, "remaining": remaining}

@app.route("/api/library/cleanup-preview")
def library_cleanup_preview():
    try:
        days = int(request.args.get("days", 30))
    except (TypeError, ValueError):
        return {"success": False, "error": "Days must be a whole number."}, 400
    if days < 0 or days > 36500:
        return {"success": False, "error": "Days must be between 0 and 36500."}, 400
    architectures = [value for value in request.args.getlist("architecture") if value]
    include_unknown = str(request.args.get("include_unknown", "0")).lower() in ("1", "true", "yes")
    result = _library_cleanup_candidates(days, architectures=architectures, include_unknown=include_unknown)
    result.pop("matched_ids", None)
    result.pop("deletable_ids", None)
    result.pop("protected_ids", None)
    return {"success": True, **result}


@app.route("/api/library/cleanup", methods=["POST"])
def library_cleanup_delete():
    payload = request.get_json(silent=True) or {}
    try:
        days = int(payload.get("days", 30))
    except (TypeError, ValueError):
        return {"success": False, "error": "Days must be a whole number."}, 400
    if days < 0 or days > 36500:
        return {"success": False, "error": "Days must be between 0 and 36500."}, 400

    include_protected = payload.get("include_protected") is True
    include_unknown = payload.get("include_unknown") is True
    architectures = [str(value) for value in (payload.get("architectures") or []) if str(value).strip()]
    result = _library_cleanup_candidates(days, architectures=architectures, include_unknown=include_unknown)
    ids = result["matched_ids"] if include_protected else result["deletable_ids"]

    if ids:
        database.remember_retention_tombstones_for_model_ids(ids)

    _delete_library_models(ids)
    try:
        from preview_cache import clean_orphaned_previews
        clean_orphaned_previews()
    except Exception:
        pass

    # Reclaim pages only after an explicit cleanup, never during normal scans.
    if ids:
        maintenance = database.connect()
        try:
            maintenance.execute("PRAGMA optimize")
            maintenance.execute("VACUUM")
        finally:
            maintenance.close()

    return {
        "success": True,
        "deleted": len(ids),
        "protected_kept": 0 if include_protected else result["protected"],
        "included_protected": include_protected,
    }




OPTIONAL_UNIVERSAL_CREATOR_BLOCKS = {
    "maozhuoshushu": {
        "label": "maozhuoshushu",
        "reason": "Very high media volume (often thousands of images per model).",
    },
}


@app.route("/api/blocked-creators")
def blocked_creators_api():
    settings = load_settings()
    sources = settings.get("sources", {})
    rows = database.get_blocked_creators()
    result = []
    for row in rows:
        source = row.get("source", "")
        creator = row.get("creator", "")
        result.append({
            **row,
            "source_display": sources.get(source, {}).get("display", source),
            "model_count": database.blocked_creator_model_count(source, creator),
        })
    return {"success": True, "creators": result}


@app.route("/api/blocked-creators", methods=["POST"] )
def block_creator_api():
    payload = request.get_json(silent=True) or {}
    creator = str(payload.get("creator") or "").strip()
    sources = [str(x).strip().lower() for x in (payload.get("sources") or []) if str(x).strip()]
    remove_existing = payload.get("remove_existing") is True
    remove_favorites = payload.get("remove_favorites") is True
    if not creator or not sources:
        return {"success": False, "error": "Creator and at least one source are required."}, 400

    for source in sources:
        database.block_creator(source, creator)

    deleted = 0
    protected = 0
    if remove_existing:
        conn = database.connect(); conn.row_factory = sqlite3.Row
        placeholders = ",".join("?" for _ in sources)
        rows = conn.execute(
            f"SELECT DISTINCT m.id, m.favorite FROM models m LEFT JOIN model_sources ms ON ms.model_id=m.id "
            f"WHERE lower(m.author)=lower(?) AND (m.source IN ({placeholders}) OR ms.source IN ({placeholders}))",
            [creator, *sources, *sources]
        ).fetchall()
        conn.close()
        ids = []
        for row in rows:
            if bool(row["favorite"]) and not remove_favorites:
                protected += 1
            else:
                ids.append(row["id"])
        deleted = _delete_library_models(ids)
        try:
            from preview_cache import clean_orphaned_previews
            clean_orphaned_previews()
        except Exception:
            pass
    return {"success": True, "blocked": len(sources), "deleted": deleted, "protected": protected}


@app.route("/api/blocked-creators/universal", methods=["POST"])
def universal_block_creator_api():
    payload = request.get_json(silent=True) or {}
    creator = str(payload.get("creator") or "").strip()
    enabled = payload.get("enabled") is True
    key = creator.casefold()
    if key not in OPTIONAL_UNIVERSAL_CREATOR_BLOCKS:
        return {"success": False, "error": "Unknown optional universal creator exclusion."}, 400
    database.set_universal_creator_blocked(creator, enabled)
    return {"success": True, "creator": creator, "enabled": enabled}


@app.route("/api/blocked-creators/unblock", methods=["POST"])
def unblock_creator_api():
    payload = request.get_json(silent=True) or {}
    creator = str(payload.get("creator") or "").strip()
    source = str(payload.get("source") or "").strip().lower()
    if not creator or not source:
        return {"success": False, "error": "Creator and source are required."}, 400
    if database.is_hard_blocked_creator(source, creator):
        return {"success": False, "error": "This creator is a built-in AbyssBeacon exclusion and cannot be enabled."}, 403
    if database.is_universal_creator_blocked(creator):
        return {"success": False, "error": "This creator is blocked universally. Disable the universal exclusion in Settings → Blocked Creators first."}, 409
    database.unblock_creator(source, creator)
    return {"success": True}


@app.route("/settings/blocked-creators")
def blocked_creators_page():
    settings = load_settings()
    sources = settings.get("sources", {})
    rows = database.get_blocked_creators()
    creators = [{**row, "source_display": sources.get(row.get("source"), {}).get("display", row.get("source")), "model_count": database.blocked_creator_model_count(row.get("source"), row.get("creator"))} for row in rows]
    universal_enabled = {
        str(row.get("creator") or "").strip().casefold()
        for row in database.get_universal_blocked_creators()
    }
    universal_blocks = [
        {
            "creator": key,
            "label": meta.get("label") or key,
            "reason": meta.get("reason") or "",
            "enabled": key.casefold() in universal_enabled,
        }
        for key, meta in OPTIONAL_UNIVERSAL_CREATOR_BLOCKS.items()
    ]
    return render_template(
        "blocked_creators.html",
        creators=creators,
        sources=sources,
        universal_blocks=universal_blocks,
    )


@app.route("/api/favorite-creators")
def favorite_creators_api():
    conn = database.connect()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("""
            SELECT c.name,
                   COUNT(m.id) AS model_count,
                   GROUP_CONCAT(DISTINCT m.source) AS sources,
                   (SELECT finished FROM creator_scan_runs csr
                    WHERE lower(csr.creator) = lower(c.name)
                    ORDER BY csr.id DESC LIMIT 1) AS last_scan
            FROM creators c
            LEFT JOIN models m ON lower(m.author) = lower(c.name)
            WHERE c.favorite = 1
            GROUP BY c.id, c.name
            ORDER BY lower(c.name)
        """).fetchall()
    except sqlite3.OperationalError:
        rows = conn.execute("""
            SELECT c.name, COUNT(m.id) AS model_count,
                   GROUP_CONCAT(DISTINCT m.source) AS sources, NULL AS last_scan
            FROM creators c
            LEFT JOIN models m ON lower(m.author) = lower(c.name)
            WHERE c.favorite = 1
            GROUP BY c.id, c.name
            ORDER BY lower(c.name)
        """).fetchall()
    conn.close()
    return {"creators": [{
        "name": row["name"],
        "model_count": row["model_count"] or 0,
        "sources": [x for x in (row["sources"] or "").split(",") if x],
        "last_scan": time_since(row["last_scan"]) if row["last_scan"] else "never"
    } for row in rows]}


@app.route("/model/<int:model_id>/favorite", methods=["POST"])
def model_favorite(model_id):
    payload = request.get_json(silent=True) or {}
    favorite = bool(payload.get("favorite"))
    if not database.set_model_favorite(model_id, favorite):
        return {"success": False, "error": "Model not found"}, 404
    return {"success": True, "favorite": favorite}


@app.route("/creator/<path:author>/favorite", methods=["POST"])
def creator_favorite(author):
    author = unquote(author).strip()
    payload = request.get_json(silent=True) or {}
    favorite = bool(payload.get("favorite"))
    database.set_creator_favorite(author, favorite)
    return {"success": True, "favorite": favorite}



def _red_media_url_token(value):
    """Stable CivitAI CDN UUID used to match the same media across API shapes."""
    text = str(value or "")
    match = re.search(
        r"image\.civitai\.com/[^/]+/([0-9a-fA-F-]{20,})/",
        text,
        flags=re.IGNORECASE,
    )
    return match.group(1).lower() if match else ""


def _has_rich_generation_metadata(meta):
    if not isinstance(meta, dict):
        return False
    # Dimensions alone are not enough: Red's thin gallery records often have
    # width/height but no actual generation settings.
    strong = (
        "prompt", "negativePrompt", "negative_prompt", "seed", "steps",
        "sampler", "cfgScale", "cfg", "scheduler", "workflow", "comfy",
        "engine", "_generation_data_cached",
    )
    return any(meta.get(key) not in (None, "", [], {}) for key in strong)


@app.route("/api/model/<int:model_id>/version/<version_id>/media-metadata", methods=["GET"])
def lazy_red_version_media_metadata(model_id, version_id):
    """Resolve and cache the selected CivitAI Red version's own gallery."""
    version_id = str(version_id or "").strip()
    if not version_id:
        return {"ok": False, "error": "Version id is required."}, 400

    conn = database.connect()
    conn.row_factory = sqlite3.Row
    model_row = conn.execute(
        "SELECT id, source FROM models WHERE id=?",
        (model_id,),
    ).fetchone()
    existing_rows = conn.execute(
        """
        SELECT id, model_id, source, type, url, thumbnail, filename, path,
               position, metadata
        FROM model_media
        WHERE model_id=? AND lower(source)='civitaired'
        ORDER BY position, id
        """,
        (model_id,),
    ).fetchall()
    conn.close()

    if model_row is None:
        return {"ok": False, "error": "Model not found."}, 404

    existing_for_version = []
    for row in existing_rows:
        try:
            meta = json.loads(row["metadata"] or "{}")
            if not isinstance(meta, dict):
                meta = {}
        except Exception:
            meta = {}
        row_version = str(
            meta.get("civitai_red_model_version_id")
            or meta.get("civitai_model_version_id")
            or meta.get("model_version_id")
            or ""
        ).strip()
        if row_version == version_id:
            existing_for_version.append((row, meta))

    # A previously hydrated version can be returned entirely from SQLite.
    if existing_for_version and all(
        bool(meta.get("_version_gallery_hydrated"))
        for _, meta in existing_for_version
    ):
        media = []
        for row, meta in existing_for_version:
            media.append({
                "id": int(row["id"]),
                "source": row["source"],
                "type": row["type"],
                "url": row["url"],
                "thumbnail": row["thumbnail"],
                "filename": row["filename"],
                "path": row["path"],
                "position": row["position"],
                "metadata": meta,
                "metadata_obj": meta,
            })
        return {"ok": True, "cached": True, "version_id": version_id, "media": media}

    from scanners import civitaired as civitaired_scanner
    bundle = civitaired_scanner.fetch_version_gallery(version_id)
    fetched = bundle.get("media") or []
    version = bundle.get("version") or {}

    if not fetched:
        from scan_logging import verbose_print
        verbose_print(
            f"RED VERSION GALLERY: model={model_id} version={version_id} "
            "-> no media returned"
        )
        return {
            "ok": True,
            "cached": False,
            "version_id": version_id,
            "media": [],
        }

    def media_token(value):
        text = str(value or "")
        match = re.search(
            r"image\.civitai\.com/[^/]+/([0-9a-fA-F-]{20,})/",
            text,
            flags=re.IGNORECASE,
        )
        return match.group(1).lower() if match else ""

    # Match existing rows by stable CDN UUID first, then exact URL/path.
    existing_by_token = {}
    existing_by_url = {}
    existing_by_path = {}
    max_position = -1
    for row in existing_rows:
        max_position = max(max_position, int(row["position"] or 0))
        token = media_token(row["url"])
        if token:
            existing_by_token[token] = row
        if row["url"]:
            existing_by_url[str(row["url"])] = row
        if row["path"]:
            existing_by_path[str(row["path"]).casefold()] = row

    conn = database.connect()
    c = conn.cursor()
    returned = []

    for order, item in enumerate(fetched):
        meta = dict(item.get("metadata") or {})
        meta["civitai_red_model_version_id"] = version_id
        if version.get("name"):
            meta["civitai_red_model_version"] = version.get("name")
        meta["_version_gallery_hydrated"] = True

        token = media_token(item.get("url") or item.get("fallback_url"))
        row = (
            (existing_by_token.get(token) if token else None)
            or existing_by_url.get(str(item.get("url") or ""))
            or existing_by_path.get(str(item.get("path") or "").casefold())
        )

        if row is not None:
            media_id = int(row["id"])
            c.execute(
                """
                UPDATE model_media
                SET type=?, url=?, thumbnail=?, filename=?, path=?, metadata=?
                WHERE id=? AND model_id=?
                """,
                (
                    item.get("type") or row["type"],
                    item.get("url") or row["url"],
                    item.get("thumbnail") or row["thumbnail"],
                    item.get("filename") or row["filename"],
                    item.get("path") or row["path"],
                    json.dumps(meta, ensure_ascii=False),
                    media_id,
                    model_id,
                ),
            )
            position = int(row["position"] or 0)
        else:
            max_position += 1
            position = max_position
            c.execute(
                """
                INSERT INTO model_media
                    (model_id, source, type, url, thumbnail, filename, path, metadata, position)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    model_id,
                    "civitaired",
                    item.get("type") or "image",
                    item.get("url") or "",
                    item.get("thumbnail") or "",
                    item.get("filename") or f"preview-{order + 1}",
                    item.get("path") or "",
                    json.dumps(meta, ensure_ascii=False),
                    position,
                ),
            )
            media_id = int(c.lastrowid)

        returned.append({
            "id": media_id,
            "source": "civitaired",
            "type": item.get("type") or "image",
            "url": item.get("url") or "",
            "fallback_url": item.get("fallback_url") or "",
            "thumbnail": item.get("thumbnail") or "",
            "filename": item.get("filename") or f"preview-{order + 1}",
            "path": item.get("path") or "",
            "position": position,
            "metadata": meta,
            "metadata_obj": meta,
        })

    conn.commit()
    conn.close()

    from scan_logging import verbose_print
    verbose_print(
        f"RED VERSION GALLERY: model={model_id} version={version_id} "
        f"detail={bundle.get('detail_count', 0)} "
        f"list={bundle.get('listed_count', 0)} "
        f"gallery={len(returned)}"
    )

    return {
        "ok": True,
        "cached": False,
        "version_id": version_id,
        "version_name": version.get("name") or "",
        "media": returned,
    }



@app.route("/api/model/<int:model_id>/media/<int:media_id>/metadata", methods=["GET"])
def lazy_media_metadata(model_id, media_id):
    """Fetch missing rich Red media metadata once and cache it in SQLite."""
    conn = database.connect()
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT id,model_id,source,metadata FROM model_media WHERE id=? AND model_id=?",
        (media_id, model_id),
    ).fetchone()
    conn.close()
    if row is None:
        return {"ok": False, "error": "Media not found."}, 404
    try:
        stored=json.loads(row["metadata"] or "{}")
        if not isinstance(stored,dict): stored={}
    except Exception:
        stored={}
    if stored.get("_generation_data_cached"):
        print(f"RED METADATA: model={model_id} media={media_id} -> cache hit")
        return {"ok": True, "cached": True, "metadata": stored}
    if str(row["source"] or "").strip().lower()!="civitaired":
        return {"ok": True, "cached": True, "metadata": stored}
    red_media_id=stored.get("civitai_red_media_id")
    if not red_media_id:
        print(f"RED METADATA: model={model_id} media={media_id} -> missing Red media id")
        return {"ok": True, "cached": False, "metadata": stored}
    from scanners import civitaired as civitaired_scanner
    generation=civitaired_scanner.fetch_generation_data(red_media_id)
    if not generation:
        print(
            f"RED METADATA: model={model_id} media={media_id} "
            f"red_id={red_media_id} -> unavailable"
        )
        return {"ok": False, "error": "Generation metadata was unavailable.", "metadata": stored}, 404
    enriched=dict(stored)
    enriched.update(civitaired_scanner.generation_metadata_for_display(generation))
    database.update_media_metadata(media_id, model_id, enriched)
    print(
        f"RED METADATA: model={model_id} media={media_id} "
        f"red_id={red_media_id} -> fetched + cached"
    )
    return {"ok": True, "cached": False, "metadata": enriched}


@app.route("/model/<int:id>")
def model_details(id):

    conn = database.connect()

    conn.row_factory = sqlite3.Row


    model = conn.execute(
        """
        SELECT *
        FROM models
        WHERE id = ?
        """,
        (id,)
    ).fetchone()


    if model is None:

        conn.close()

        return "Model not found", 404


    model = dict(model)
    canonical_model = dict(model)
    maturity_mode = _normalize_maturity_mode(request.args.get("mature", "hide"))
    include_civitai_mature_media = _civitai_include_mature_media_enabled()

    detail_settings = load_settings()
    detail_preferences = detail_settings.get("preferences", {}) if isinstance(detail_settings.get("preferences", {}), dict) else {}
    detail_card_colors = detail_preferences.get("source_card_colors", {}) if isinstance(detail_preferences.get("source_card_colors", {}), dict) else {}
    source_colors = {
        name: detail_card_colors.get(name, data.get("color", "#00eaff"))
        for name, data in detail_settings.get("sources", {}).items()
    }

    raw_source_links = [dict(row) for row in database.get_model_sources(id)]
    raw_media_rows = [dict(row) for row in database.get_media(id)]
    source_snapshots = []
    for link in raw_source_links:
        snapshot = _decode_source_snapshot(link, canonical_model)
        snapshot["sensitive"] = bool(_source_snapshot_sensitive(snapshot, canonical_model))

        # Choose this source's preview using the current maturity preference.
        # This prevents a stored mature gallery item from becoming the visible
        # preview of an otherwise-safe source when Mature Content is hidden.
        source_name = str(snapshot.get("source") or "").strip().lower()
        source_media_rows = [
            row for row in raw_media_rows
            if str(row.get("source") or "").strip().lower() == source_name
        ]
        visible_source_media = [
            row for row in source_media_rows
            if _media_visible_for_maturity(
                row,
                maturity_mode,
                include_civitai_mature_media=include_civitai_mature_media,
            )
        ]
        first_image = next((
            row for row in visible_source_media
            if str(row.get("type") or "").strip().lower() == "image"
            and str(row.get("url") or "").strip()
        ), None)
        if first_image:
            snapshot["image"] = str(first_image.get("url") or "").strip()
            snapshot["has_media"] = 1
        elif source_media_rows and maturity_mode != "show":
            snapshot["image"] = ""
            snapshot["has_media"] = int(bool(visible_source_media))
        source_snapshots.append(snapshot)

    if not source_snapshots:
        fallback_link = {
            "source": canonical_model.get("source", ""),
            "url": canonical_model.get("url", ""),
            "model_key": canonical_model.get("model_key", ""),
            "source_data": "",
        }
        snapshot = _decode_source_snapshot(fallback_link, canonical_model)
        snapshot["sensitive"] = bool(_source_snapshot_sensitive(snapshot, canonical_model))
        source_snapshots = [snapshot]

    eligible_snapshots = (
        source_snapshots
        if maturity_mode == "show"
        else [snapshot for snapshot in source_snapshots if not snapshot.get("sensitive")]
    )
    if not eligible_snapshots:
        conn.close()
        return "Model hidden by Mature Content setting", 404

    presentation = _choose_presentation_snapshot(eligible_snapshots) or dict(eligible_snapshots[0])
    presentation_source = str(presentation.get("source") or "").strip().lower()
    canonical_source = str(canonical_model.get("source") or "").strip().lower()
    if presentation_source == canonical_source and str(canonical_model.get("image") or "").strip():
        canonical_media_rows = [
            row for row in raw_media_rows
            if str(row.get("source") or "").strip().lower() == canonical_source
            and str(row.get("type") or "").strip().lower() == "image"
        ]
        if maturity_mode == "show" or not canonical_media_rows:
            presentation["image"] = canonical_model.get("image")
    _apply_presentation_snapshot(model, presentation)

    # Only maturity-eligible source contexts are exposed to this detail view.
    # Source pills, downloads, galleries, and the source selector all derive
    # from this same list so hidden mature providers cannot bleed back in.
    source_priority_index = {source: index for index, source in enumerate(FEED_PRESENTATION_SOURCE_PRIORITY)}
    eligible_snapshots.sort(
        key=lambda snapshot: source_priority_index.get(str(snapshot.get("source") or "").strip().lower(), 999)
    )
    model["source_links"] = [dict(snapshot) for snapshot in eligible_snapshots]
    model["source_view_options"] = [
        {
            "source": str(snapshot.get("source") or "").strip().lower(),
            "label": SOURCE_VIEW_LABELS.get(str(snapshot.get("source") or "").strip().lower(), str(snapshot.get("source") or "").strip()),
        }
        for snapshot in eligible_snapshots
    ]

    # Installed-file records survive page reloads. Match them by source +
    # fingerprint so the path automatically disappears when the upstream file
    # changes and an update becomes available.
    installed_rows = conn.execute(
        """
        SELECT source, model_key, source_file_id, file_fingerprint,
               local_path, filename, version_id, version_name, installed_at
        FROM installed_files
        WHERE model_id=?
        ORDER BY installed_at DESC, id DESC
        """,
        (id,),
    ).fetchall()
    installed_rows = [dict(row) for row in installed_rows]

    # Build source-specific choices. Alternate sources get full download metadata
    # after they have been scanned once with this version of AbyssBeacon.
    model["download_sources"] = []
    for link in model["source_links"]:
        src = dict(link)
        src_files = src.get("files") or []
        for idx, f in enumerate(src_files):
            if isinstance(f, dict): f["_download_index"] = idx
        src["access_status"] = _source_access_status(src.get("source"), src.get("gated"), src.get("card_data"))
        if src["access_status"] == "public" and src_files:
            src["access_status"] = "downloadable"
        src["metadata_ready"] = bool(src_files)
        src["versions"] = _source_version_groups(src)

        installed_for_source = [
            row for row in installed_rows
            if str(row.get("source") or "").lower() == str(src.get("source") or "").lower()
        ]
        for version in src["versions"]:
            for file_data in version.get("files", []):
                if not isinstance(file_data, dict):
                    continue
                fingerprint = _download_file_fingerprint(src, file_data)
                match = next(
                    (
                        row for row in installed_for_source
                        if fingerprint
                        and str(row.get("file_fingerprint") or "") == fingerprint
                    ),
                    None,
                )
                if match:
                    file_data["_installed_path"] = str(match.get("local_path") or "")
                    file_data["_installed_at"] = str(match.get("installed_at") or "")

        # The drawer opens on the first/current version, so let the source-level
        # state describe that version. Older versions keep their own file
        # availability internally without duplicating Restricted/Unknown labels.
        if src["versions"]:
            current_state = str(src["versions"][0].get("access_status") or "").strip()
            if current_state in {"downloadable", "early_access", "paid_access", "gated", "unconfirmed"}:
                src["access_status"] = current_state
        model["download_sources"].append(src)

    model["queued_download_keys"] = database.get_download_queue_keys(id)
    model["watched_download_keys"] = database.get_download_watch_keys(id)

    # Deduplicated version choices for the detail page. Keep enough metadata to
    # make a version selection change the gallery/summary instead of merely
    # flattening every revision into one download list.
    version_choices = []
    version_map = {}
    for src in model["download_sources"]:
        for version in src.get("versions", []):
            label = str(version.get("name") or "Current version")
            key = label.casefold()
            entry = version_map.get(key)
            if not entry:
                raw_version_architecture = str(
                    version.get("architecture") or version.get("base_model") or ""
                ).strip()
                classified_version_architecture = processors.classify_architecture(raw_version_architecture)
                display_version_architecture = (
                    classified_version_architecture
                    if classified_version_architecture != "Other"
                    else raw_version_architecture
                )
                version_share_url = _model_version_share_url(
                    src.get("source"), src.get("url"), version
                )
                entry = {
                    "name": label,
                    "id": version.get("id"),
                    "status": version.get("access_status"),
                    "sources": [],
                    "share_url": version_share_url,
                    "share_source": src.get("source") if version_share_url else "",
                    "source_share_urls": {},
                    "source_metadata": {},
                    "description": version.get("description") or "",
                    "base_model": version.get("base_model") or "",
                    "architecture": display_version_architecture,
                    "base_model_type": version.get("base_model_type") or "",
                    "trained_words": version.get("trained_words") or [],
                    "early_access_deadline": version.get("early_access_deadline") or "",
                    "formats": [],
                }
                version_map[key] = entry
                version_choices.append(entry)
            elif entry.get("status") != "downloadable" and version.get("access_status") == "downloadable":
                entry["status"] = "downloadable"

            version_share_url = _model_version_share_url(
                src.get("source"), src.get("url"), version
            )
            canonical_source = str(model.get("source") or "").strip().lower()
            current_share_source = str(entry.get("share_source") or "").strip().lower()
            this_source = str(src.get("source") or "").strip().lower()
            if version_share_url:
                entry.setdefault("source_share_urls", {})[this_source] = version_share_url

            raw_source_architecture = str(
                version.get("architecture") or version.get("base_model") or src.get("architecture") or ""
            ).strip()
            classified_source_architecture = processors.classify_architecture(raw_source_architecture)
            source_architecture = (
                classified_source_architecture
                if classified_source_architecture != "Other"
                else raw_source_architecture
            )
            source_formats = []
            for source_file in version.get("files", []):
                if not isinstance(source_file, dict):
                    continue
                source_format = str(
                    source_file.get("fp") or source_file.get("format") or source_file.get("size_label") or ""
                ).strip()
                if source_format and source_format.casefold() not in {str(x).casefold() for x in source_formats}:
                    source_formats.append(source_format)
            entry.setdefault("source_metadata", {})[this_source] = {
                "name": label,
                "id": version.get("id"),
                "status": version.get("access_status"),
                "share_url": version_share_url,
                "description": version.get("description") or src.get("description") or "",
                "base_model": version.get("base_model") or src.get("base_model") or "",
                "architecture": source_architecture or src.get("architecture") or "",
                "base_model_type": version.get("base_model_type") or "",
                "trained_words": version.get("trained_words") or [],
                "early_access_deadline": version.get("early_access_deadline") or "",
                "formats": source_formats,
            }
            if version_share_url and (
                not entry.get("share_url")
                or (this_source == canonical_source and current_share_source != canonical_source)
            ):
                entry["share_url"] = version_share_url
                entry["share_source"] = src.get("source") or ""

            if not entry.get("description") and version.get("description"):
                entry["description"] = version.get("description")
            for field in ("base_model", "base_model_type", "early_access_deadline"):
                if not entry.get(field) and version.get(field):
                    entry[field] = version.get(field)
            if not entry.get("architecture"):
                raw_version_architecture = str(
                    version.get("architecture") or version.get("base_model") or ""
                ).strip()
                classified_version_architecture = processors.classify_architecture(raw_version_architecture)
                entry["architecture"] = (
                    classified_version_architecture
                    if classified_version_architecture != "Other"
                    else raw_version_architecture
                )
            if not entry.get("trained_words") and version.get("trained_words"):
                entry["trained_words"] = version.get("trained_words")
            for file_data in version.get("files", []):
                if not isinstance(file_data, dict):
                    continue
                fmt = str(file_data.get("fp") or file_data.get("format") or file_data.get("size_label") or "").strip()
                if fmt and fmt.casefold() not in {str(x).casefold() for x in entry["formats"]}:
                    entry["formats"].append(fmt)
            if src.get("source") not in entry["sources"]:
                entry["sources"].append(src.get("source"))
    model["version_choices"] = version_choices

    # Updates 2.0 foundation -------------------------------------------------
    # Treat installed history as a set, not one remembered download. Map each
    # local installed artifact back to the source's version metadata whenever
    # possible; newer records also persist version_id/version_name directly.
    history_for_model = []
    for history_key, rows in database.get_download_history_lookup().items():
        for row in rows:
            if any(
                str(row.get("source") or "").lower() == str(src.get("source") or "").lower()
                and str(row.get("model_key") or "") == str(src.get("model_key") or "")
                for src in model["download_sources"]
            ):
                history_for_model.append(row)

    history_by_fp = {
        str(row.get("file_fingerprint") or ""): row
        for row in history_for_model
        if str(row.get("file_fingerprint") or "")
    }
    history_by_file_id = {
        str(row.get("source_file_id") or ""): row
        for row in history_for_model
        if str(row.get("source_file_id") or "")
    }

    installed_versions = []
    seen_installed_keys = set()

    def _remember_installed_version(
        source, version_id, version_name, installed_at="", filename="",
        local_path="", source_rank=999999
    ):
        source = str(source or "").strip().lower()
        version_id = str(version_id or "").strip()
        version_name = str(version_name or "").strip()
        filename = str(filename or "").strip()
        label = version_name or filename or (f"Version {version_id}" if version_id else "Installed version")
        key = (source, version_id.casefold(), label.casefold())
        if key in seen_installed_keys:
            return None
        seen_installed_keys.add(key)
        item = {
            "source": source,
            "id": version_id,
            "name": label,
            "installed_at": str(installed_at or ""),
            "filename": filename,
            "local_path": str(local_path or ""),
            "source_rank": int(source_rank if source_rank is not None else 999999),
        }
        installed_versions.append(item)
        return item

    # Build a source/version lookup from the metadata currently on the card.
    version_lookup_by_fp = {}
    version_lookup_by_file_id = {}
    version_lookup_by_id = {}
    version_lookup_by_name = {}
    source_current_version = {}
    for src in model["download_sources"]:
        source_name = str(src.get("source") or "").strip().lower()
        versions = src.get("versions") or []
        if versions:
            source_current_version[source_name] = {
                "id": str(versions[0].get("id") or ""),
                "name": str(versions[0].get("name") or "Current version"),
            }
        for rank, version in enumerate(versions):
            version_id = str(version.get("id") or "")
            version_name = str(version.get("name") or "Current version")
            for file_data in version.get("files") or []:
                if not isinstance(file_data, dict):
                    continue
                fp = _download_file_fingerprint(src, file_data)
                file_id = str(
                    file_data.get("model_file_id")
                    or file_data.get("id")
                    or file_data.get("file_id")
                    or ""
                ).strip()
                meta = {
                    "source": source_name,
                    "id": version_id,
                    "name": version_name,
                    "rank": rank,
                }
                if fp:
                    version_lookup_by_fp[(source_name, fp)] = meta
                if file_id:
                    version_lookup_by_file_id[(source_name, file_id)] = meta
            if version_id:
                version_lookup_by_id[(source_name, version_id)] = meta
            if version_name:
                version_lookup_by_name[(source_name, version_name.casefold())] = meta

    for installed in installed_rows:
        source_name = str(installed.get("source") or "").strip().lower()
        fp = str(installed.get("file_fingerprint") or "").strip()
        file_id = str(installed.get("source_file_id") or "").strip()

        stored_version_id = str(installed.get("version_id") or "").strip()
        stored_version_name = str(installed.get("version_name") or "").strip()

        meta = (
            version_lookup_by_id.get((source_name, stored_version_id))
            if stored_version_id else None
        ) or (
            version_lookup_by_name.get((source_name, stored_version_name.casefold()))
            if stored_version_name else None
        ) or version_lookup_by_fp.get((source_name, fp)) \
          or version_lookup_by_file_id.get((source_name, file_id))

        if not stored_version_id and not stored_version_name:
            history_row = history_by_fp.get(fp) or history_by_file_id.get(file_id)
            if history_row:
                stored_version_id = str(history_row.get("version_id") or "").strip()
                stored_version_name = str(history_row.get("version_name") or "").strip()

        version_id = stored_version_id or (meta.get("id") if meta else "")
        version_name = stored_version_name or (meta.get("name") if meta else "")

        _remember_installed_version(
            source_name,
            version_id,
            version_name,
            installed.get("installed_at"),
            installed.get("filename"),
            installed.get("local_path"),
            int(meta.get("rank", 999999)) if meta else 999999,
        )

    # Browser-download history may exist without a local installed_files row.
    for row in history_for_model:
        fp = str(row.get("file_fingerprint") or "").strip()
        file_id = str(row.get("source_file_id") or "").strip()
        source_name = str(row.get("source") or "").strip().lower()
        row_version_id = str(row.get("version_id") or "").strip()
        row_version_name = str(row.get("version_name") or "").strip()
        meta = (
            version_lookup_by_id.get((source_name, row_version_id))
            if row_version_id else None
        ) or (
            version_lookup_by_name.get((source_name, row_version_name.casefold()))
            if row_version_name else None
        ) or version_lookup_by_fp.get((source_name, fp)) \
          or version_lookup_by_file_id.get((source_name, file_id))

        _remember_installed_version(
            source_name,
            row_version_id or (meta.get("id") if meta else ""),
            row_version_name or (meta.get("name") if meta else ""),
            row.get("downloaded_at"),
            row.get("filename"),
            "",
            int(meta.get("rank", 999999)) if meta else 999999,
        )

    # Prefer a version the source itself ranks newest; use installation time as
    # the tie-breaker. Unknown historical versions fall behind known versions.
    installed_versions.sort(
        key=lambda item: (
            int(item.get("source_rank", 999999)),
            str(item.get("installed_at") or ""),
        ),
        reverse=False,
    )

    newest_installed = installed_versions[0] if installed_versions else None
    current_version = None
    if newest_installed:
        current_version = source_current_version.get(newest_installed.get("source"))
    if not current_version and version_choices:
        current_version = {
            "id": str(version_choices[0].get("id") or ""),
            "name": str(version_choices[0].get("name") or "Current version"),
        }

    model["installed_versions"] = installed_versions
    model["installed_version_count"] = len(installed_versions)
    model["newest_installed_version"] = newest_installed or {}
    model["newest_installed_display"] = (
        str((newest_installed or {}).get("name") or "").strip()
        or str((newest_installed or {}).get("filename") or "").strip()
    )
    model["current_download_version"] = current_version or {}
    if model.get("update_available") and newest_installed and current_version:
        model["update_version_summary"] = {
            "installed": str(newest_installed.get("name") or newest_installed.get("filename") or "Installed"),
            "current": str(current_version.get("name") or "Current version"),
        }
    else:
        model["update_version_summary"] = {}

    # A SHA-merged card can use Red as the canonical row while regular CivitAI
    # owns the richer parent description. Prefer the longest non-empty source
    # description for display without changing canonical source priority.
    if not str(model.get("description") or "").strip():
        source_descriptions = [str(src.get("description") or "").strip() for src in model["download_sources"]]
        source_descriptions = [value for value in source_descriptions if value]
        if source_descriptions:
            model["description"] = max(source_descriptions, key=len)

    model["source_color"] = source_colors.get(model.get("source"), "#00eaff")

    # Preserve creator attribution per source on merged model detail pages.
    # Older alternate-source snapshots created before this feature may not yet
    # contain an author; their identity is filled automatically the next time
    # that source is scanned.
    author_sources = []
    for link in model["source_links"]:
        source = str(link.get("source") or "").strip().lower()
        snapshot = link if isinstance(link, dict) else {}
        author = str(snapshot.get("author") or "").strip()
        if not author:
            author = _infer_source_author(source, link.get("model_key", ""), link.get("url", ""))
        if not author:
            continue
        item = {
            "author": author,
            "source": source,
            "color": source_colors.get(source, "#00eaff"),
        }
        if not any(x["source"] == source and x["author"].casefold() == author.casefold() for x in author_sources):
            author_sources.append(item)

    canonical_author = str(model.get("author") or "").strip()
    canonical_source = str(model.get("source") or "").strip().lower()
    if canonical_author and not any(x["source"] == canonical_source and x["author"].casefold() == canonical_author.casefold() for x in author_sources):
        author_sources.insert(0, {
            "author": canonical_author,
            "source": canonical_source,
            "color": source_colors.get(canonical_source, "#00eaff"),
        })
    model["author_sources"] = author_sources or [{
        "author": canonical_author,
        "source": canonical_source,
        "color": model["source_color"],
    }]

    model["image"] = display_media_url(model.get("image"), model.get("source"))

    if model.get("files"):

        if isinstance(model.get("files"), str):
            try:
                model["files"] = json.loads(model["files"] or "[]")
            except Exception:
                model["files"] = []
        elif isinstance(model.get("files"), list):
            model["files"] = list(model["files"])
        else:
            model["files"] = []


        # Normalize the older string-only file format. Hugging Face
        # records scanned before download metadata was added can still be
        # downloaded immediately without requiring a database rescan.
        if model["files"] and isinstance(model["files"][0], str):

            normalized_files = []
            repo_id = (model.get("model_key") or "").strip()

            for filename in model["files"]:
                filename = str(filename)
                lower_name = filename.lower()
                primary = lower_name.endswith((
                    ".safetensors", ".ckpt", ".pt", ".pth",
                    ".bin", ".gguf"
                ))

                download_url = ""
                if model.get("source") == "huggingface" and repo_id:
                    encoded_path = quote(filename, safe="/")
                    download_url = (
                        f"https://huggingface.co/{repo_id}/resolve/main/"
                        f"{encoded_path}?download=true"
                    )

                normalized_files.append({
                    "name": filename.split("/")[-1],
                    "path": filename,
                    "primary": primary,
                    "size": "",
                    "download_url": download_url
                })

            model["files"] = normalized_files

    else:

        model["files"] = []

    for _download_index, file_data in enumerate(model["files"]):
        if isinstance(file_data, dict):
            file_data["_download_index"] = _download_index

    # Normalize file sizes to bytes for a consistent UI across providers.
    # Hugging Face/ModelScope expose bytes; CivitAI/Red commonly expose KB.
    for file_data in model["files"]:
        if not isinstance(file_data, dict):
            continue
        if file_data.get("size_bytes") not in (None, ""):
            continue
        raw_size = file_data.get("size")
        try:
            numeric = float(raw_size)
        except (TypeError, ValueError):
            numeric = 0
        if numeric > 0:
            if model.get("source") in ("civitai", "civitaired"):
                file_data["size_bytes"] = int(numeric * 1024)
            else:
                file_data["size_bytes"] = int(numeric)

        bytes_value = file_data.get("size_bytes") or 0
        try:
            bytes_value = float(bytes_value)
        except (TypeError, ValueError):
            bytes_value = 0
        if bytes_value > 0:
            units = ["B", "KB", "MB", "GB", "TB"]
            unit_index = 0
            while bytes_value >= 1024 and unit_index < len(units) - 1:
                bytes_value /= 1024
                unit_index += 1
            decimals = 0 if unit_index == 0 or bytes_value >= 100 else (1 if bytes_value >= 10 else 2)
            file_data["size_display"] = f"{bytes_value:.{decimals}f} {units[unit_index]}"
        else:
            file_data["size_display"] = str(file_data.get("size_label") or "").strip()

    if model.get("display_tags"):
        if isinstance(model.get("display_tags"), str):
            try:
                model["display_tags"] = json.loads(model["display_tags"] or "[]")
            except Exception:
                model["display_tags"] = []
        elif isinstance(model.get("display_tags"), list):
            model["display_tags"] = list(model["display_tags"])
        else:
            model["display_tags"] = []
    else:
        model["display_tags"] = []

    model["detail_tags"] = []
    _detail_tag_seen = set()
    for _snapshot in eligible_snapshots:
        for _tag in _normalized_model_tags(
            _snapshot.get("source"),
            _snapshot.get("tags"),
            _snapshot.get("card_data"),
        ):
            _tag_text = str(_tag or "").strip()
            if _tag_text and _tag_text.casefold() not in _detail_tag_seen:
                _detail_tag_seen.add(_tag_text.casefold())
                model["detail_tags"].append(_tag_text)

    model["access_status"] = _source_access_status(model.get("source"), bool(model.get("gated")), model.get("card_data"))
    if model["access_status"] == "public" and model.get("files"):
        model["access_status"] = "downloadable"
    if str(model.get("source") or "").lower() == "tensorhub":
        try:
            tensor_card = model.get("card_data") or {}
            if isinstance(tensor_card, str):
                tensor_card = json.loads(tensor_card or "{}")
            if not isinstance(tensor_card, dict):
                tensor_card = {}
            tensor_access = str(((tensor_card.get("tensorhub") or {}).get("download_access") or "")).strip().lower()
        except Exception:
            tensor_access = ""
        if tensor_access == "downloadable": model["access_status"] = "downloadable"
        elif tensor_access in {"paid_access", "paid", "buffet"}:
            model["access_status"] = "paid_access"; model["gated"] = True
        elif tensor_access in {"gated", "non_downloadable", "restricted", "disabled"}:
            model["access_status"] = "gated"; model["gated"] = True
        else: model["access_status"] = "unconfirmed"
        for file_data in model.get("files") or []:
            if isinstance(file_data, dict):
                file_data["download_url"] = ""
                file_data["source_url"] = model.get("url") or ""
                file_data["access_status"] = model["access_status"]


    if model is None:

        conn.close()

        return "Model not found", 404

    _detail_history = database.get_download_history_lookup() if detail_preferences.get("track_downloads", True) is not False else {}
    _annotate_download_state(
        model,
        _detail_history,
        detail_preferences,
        model.get("download_sources") or [],
    )

    # Detail views are intentionally local/cache-only.
    #
    # Older code tried to repair a missing description synchronously here by
    # calling Hugging Face, ModelScope, or CivitAI whenever the user opened a
    # card. A slow/unreachable source could therefore stall the entire model
    # viewer for several seconds (up to the request timeout). Description
    # enrichment belongs to scanning or an explicit Reload Model action, not
    # the ordinary browse/open path.
    #
    # If an older row has no stored description, render the detail immediately
    # with the locally cached metadata we have.


    conn.close()


    # Seen state is useful bookkeeping, but it must never be allowed to block
    # or fail the model viewer. database.mark_viewed() handles short contention
    # internally and can defer persistence when another writer is unusually
    # busy.
    database.mark_viewed(id)


    media_rows = raw_media_rows
    media = []
    eligible_source_names = {
        str(snapshot.get("source") or "").strip().lower()
        for snapshot in eligible_snapshots
    }
    _detail_fallback_assigned = False
    for row in media_rows:
        item = dict(row)
        item_source = str(item.get("source") or "").strip().lower()
        if item_source and item_source not in eligible_source_names:
            continue
        if not _media_visible_for_maturity(
            item,
            maturity_mode,
            include_civitai_mature_media=include_civitai_mature_media,
        ):
            continue
        raw_metadata = item.get("metadata") or ""
        if isinstance(raw_metadata, str):
            try:
                item["metadata_obj"] = json.loads(raw_metadata) if raw_metadata else {}
            except Exception:
                item["metadata_obj"] = {}
        else:
            item["metadata_obj"] = raw_metadata or {}

        # Older database rows predate filename/path columns. Recover a useful
        # repository-relative path from the media URL when possible.
        if not item.get("path"):
            parsed_path = unquote(urlparse(item.get("url") or "").path)
            marker = "/resolve/"
            if marker in parsed_path:
                tail = parsed_path.split(marker, 1)[1]
                parts = tail.split("/", 1)
                item["path"] = parts[1] if len(parts) > 1 else parts[0]
            else:
                item["path"] = parsed_path.rsplit("/", 1)[-1]
        if not item.get("filename"):
            item["filename"] = (item.get("path") or "").rsplit("/", 1)[-1]
        item["url"] = display_media_url(item.get("url"), item_source or model.get("source"))
        if item.get("thumbnail"):
            item["thumbnail"] = display_media_url(item.get("thumbnail"), item_source or model.get("source"))

        # The feed already has a canonical preview that may be cached locally
        # even when an older/source-specific media URL has expired. Let image
        # gallery items fall back to that known-good card preview on load error.
        if (
            str(item.get("type") or "").lower() != "video"
            and not _detail_fallback_assigned
            and (not item_source or item_source == str(model.get("source") or "").strip().lower())
        ):
            _fallback_image = str(model.get("image") or "").strip()
            if _fallback_image and _fallback_image != str(item.get("url") or ""):
                item["fallback_url"] = _fallback_image
                _detail_fallback_assigned = True
        if str(item.get("type") or "").lower() == "video" and item.get("thumbnail"):
            _thumb_clean = str(item.get("thumbnail") or "").split("?", 1)[0].split("#", 1)[0].lower()
            if (
                str(item.get("thumbnail") or "") == str(item.get("url") or "")
                or _thumb_clean.endswith((".mp4", ".webm", ".mov", ".m4v", ".avi", ".mkv"))
            ):
                item["thumbnail"] = ""
        item["metadata_obj"].setdefault("filename", item.get("filename", ""))
        if item.get("path"):
            item["metadata_obj"].setdefault("path", item["path"])
        media.append(item)


    # Ensure every eligible source has at least one source-owned preview in the
    # selector, even for older merged rows whose galleries predate per-source
    # media persistence. A normal rescan will populate the complete gallery.
    media_sources = {str(item.get("source") or "").strip().lower() for item in media}
    synthetic_media_id = -1
    for snapshot in eligible_snapshots:
        source_name = str(snapshot.get("source") or "").strip().lower()
        preview_url = str(snapshot.get("image") or "").strip()
        if not preview_url or source_name in media_sources:
            continue
        media.append({
            "id": synthetic_media_id,
            "model_id": id,
            "source": source_name,
            "type": "image",
            "url": display_media_url(preview_url, source_name),
            "thumbnail": "",
            "filename": "preview",
            "path": "preview",
            "position": 0,
            "metadata_obj": {"source": SOURCE_VIEW_LABELS.get(source_name, source_name)},
            "metadata": {"source": SOURCE_VIEW_LABELS.get(source_name, source_name)},
        })
        media_sources.add(source_name)
        synthetic_media_id -= 1

    # Combined view follows the same deterministic presentation-source order as
    # the feed. gallery.js deduplicates identical URLs across mirrors while each
    # source-specific view still retains its complete provider-owned gallery.
    media.sort(key=lambda item: (
        source_priority_index.get(str(item.get("source") or "").strip().lower(), 999),
        int(item.get("position") or 0),
        int(item.get("id") or 0),
    ))

    def _detail_source_context(snapshot):
        source_name = str(snapshot.get("source") or "").strip().lower()
        source_tags = _normalized_model_tags(
            source_name, snapshot.get("tags"), snapshot.get("card_data")
        )
        created_value = snapshot.get("created") or ""
        updated_value = snapshot.get("updated") or ""
        return {
            "source": source_name,
            "label": SOURCE_VIEW_LABELS.get(source_name, source_name),
            "color": source_colors.get(source_name, "#00eaff"),
            "url": str(snapshot.get("url") or ""),
            "name": str(snapshot.get("display_name") or snapshot.get("name") or model.get("display_name") or model.get("name") or ""),
            "author": str(snapshot.get("author") or ""),
            "architecture": str(snapshot.get("architecture") or ""),
            "model_type": str(snapshot.get("model_type") or ""),
            "downloads": snapshot.get("downloads", 0) or 0,
            "likes": snapshot.get("likes", 0) or 0,
            "created": format_date(created_value) if created_value else "",
            "updated": format_date(updated_value) if updated_value else "",
            "description": description_text(snapshot.get("description") or ""),
            "base_model": str(snapshot.get("base_model") or ""),
            "pipeline": str(snapshot.get("pipeline") or ""),
            "format": str(snapshot.get("format") or ""),
            "license": str(snapshot.get("license") or ""),
            "parameters": str(snapshot.get("parameters") or ""),
            "quantization": str(snapshot.get("quantization") or ""),
            "tags": source_tags,
            "sensitive": bool(snapshot.get("sensitive")),
        }

    model["source_contexts"] = {
        "combined": {
            "source": str(model.get("source") or "").strip().lower(),
            "label": "Combined",
            "color": model.get("source_color") or "#00eaff",
            "url": str(model.get("url") or ""),
            "name": str(model.get("display_name") or model.get("name") or ""),
            "author": str(model.get("author") or ""),
            "architecture": str(model.get("architecture") or ""),
            "model_type": str(model.get("model_type") or ""),
            "downloads": model.get("downloads", 0) or 0,
            "likes": model.get("likes", 0) or 0,
            "created": format_date(model.get("created")) if model.get("created") else "",
            "updated": format_date(model.get("updated")) if model.get("updated") else "",
            "description": description_text(model.get("description") or ""),
            "base_model": str(model.get("base_model") or ""),
            "pipeline": str(model.get("pipeline") or ""),
            "format": str(model.get("format") or ""),
            "license": str(model.get("license") or ""),
            "parameters": str(model.get("parameters") or ""),
            "quantization": str(model.get("quantization") or ""),
            "tags": list(model.get("detail_tags") or []),
            "sensitive": bool(model.get("sensitive")),
        }
    }
    for snapshot in eligible_snapshots:
        source_name = str(snapshot.get("source") or "").strip().lower()
        model["source_contexts"][source_name] = _detail_source_context(snapshot)

    # Mixed cards are not mature as a whole. In Show mode the mature source
    # remains selectable; Hide mode never receives that source context at all.
    model["sensitive"] = bool(eligible_snapshots) and all(
        bool(snapshot.get("sensitive")) for snapshot in eligible_snapshots
    )
    model["source_contexts"]["combined"]["sensitive"] = bool(model["sensitive"])

    return render_template(
        "components/model_detail.html",
        model=model,
        media=media,
        source_colors=source_colors,
        hide_scan=True
    )


def _start_missing_preview_repair_if_enabled():
    """Quietly repair missing card previews in the background when enabled."""
    try:
        preferences = load_settings().get("preferences", {})
        if preferences.get("download_missing_card_previews", False) is not True:
            return
    except Exception:
        return

    def _worker():
        try:
            from preview_cache import repair_missing_previews
            print("Preview repair: automatic missing-preview check started...")
            result = repair_missing_previews()
            print(
                "Preview repair automatic check complete:",
                f"{result.get('repaired', 0)} restored,",
                f"{result.get('failed', 0)} unavailable,",
                f"{result.get('missing', 0)} missing checked"
            )
        except Exception as exc:
            print(f"Preview repair failed: {exc}")

    threading.Thread(
        target=_worker,
        name="modelradar-preview-repair",
        daemon=True,
    ).start()




if __name__ == "__main__":

    host = "127.0.0.1"
    port = 5000
    url = f"http://{host}:{port}"

    print("Starting AbyssBeacon...")
    print(f"Open AbyssBeacon in your browser: {url}")
    print("Press Ctrl+C to stop AbyssBeacon.")

    # A threaded Werkzeug server keeps the UI/status endpoints responsive
    # while the scanner performs network and database work in its own thread.
    server = make_server(
        host,
        port,
        app,
        threaded=True
    )

    _start_missing_preview_repair_if_enabled()
    server.serve_forever()