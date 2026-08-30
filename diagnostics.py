"""Safe, paste-ready AbyssBeacon support diagnostics.

This module never serializes credential values or raw scanner/download
objects. Account configuration is queried only through boolean helpers. It
collects support-relevant state, then runs all free-form text through a final
redaction pass before returning the report.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from importlib import metadata as importlib_metadata
import json
import os
from pathlib import Path
import platform
import re
import sqlite3
import subprocess
import sys

import active_downloads
import scan_status
from config import DATABASE
from secrets_manager import (
    civitai_search_configured,
    configured_sources,
    seaart_connection_status,
)
from settings_manager import load_settings
from seaart_browser import browser_session_saved as seaart_browser_session_saved
from version import ABYSSBEACON_VERSION


REPORT_FORMAT_VERSION = 1
_ROOT = Path(__file__).resolve().parent

_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(authorization|proxy-authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"session[_-]?token|device[_-]?token|auth[_-]?token|token|password|passwd|cookie|secret|"
    r"x-device-id|x-browser-id|x-page-id|x-gray-tag)"
    r"[\"']?\s*([:=])\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_URL_SECRET = re.compile(
    r"(?i)([?&](?:token|access_token|refresh_token|api_key|apikey|key|auth|authorization|"
    r"session|session_token|device_token|signature|sig|credential|security-token|"
    r"x-amz-signature|x-amz-credential|x-amz-security-token|policy|key-pair-id)=)([^&#\s]+)"
)
_BEARER = re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_WINDOWS_USER_PATH = re.compile(r"(?i)\b([A-Z]:\\Users\\)[^\\/\s]+")


def _safe_text(value) -> str:
    text = str(value or "")
    text = _URL_SECRET.sub(lambda m: m.group(1) + "<redacted>", text)
    # Remove authorization payloads before the generic key/value pass so a
    # value like "Bearer abc..." cannot leave the token behind.
    text = _BEARER.sub(lambda m: f"{m.group(1)} <redacted>", text)
    text = _SECRET_ASSIGNMENT.sub(lambda m: f"{m.group(1)}{m.group(2)}<redacted>", text)
    text = _JWT.sub("<redacted-jwt>", text)
    text = _WINDOWS_USER_PATH.sub(lambda m: m.group(1) + "<user>", text)

    # Hide the current home/profile directory if it occurs in diagnostic text.
    candidates = {
        str(Path.home()),
        str(os.environ.get("USERPROFILE") or ""),
        str(os.environ.get("HOME") or ""),
    }
    for raw in sorted((p for p in candidates if p), key=len, reverse=True):
        text = text.replace(raw, "~")
        text = text.replace(raw.replace("/", "\\"), "~")
    return text


def _safe_path(value) -> str:
    value = str(value or "").strip()
    return _safe_text(value) if value else "not configured"


def _yes_no(value) -> str:
    return "yes" if bool(value) else "no"


def _package_version(name: str) -> str:
    try:
        return importlib_metadata.version(name)
    except Exception:
        return "unknown"


def _git_value(*args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(_ROOT),
            capture_output=True,
            text=True,
            timeout=0.75,
            check=False,
        )
        if result.returncode == 0:
            return _safe_text(result.stdout.strip())
    except Exception:
        pass
    return ""


def _build_identity() -> dict:
    # The application version is declared centrally in version.py. Keep the
    # environment override for development/packaging scenarios, but normal
    # releases should always report the committed AbyssBeacon version.
    declared = str(os.environ.get("ABYSSBEACON_VERSION") or ABYSSBEACON_VERSION).strip()

    commit = _git_value("rev-parse", "--short", "HEAD")
    branch = _git_value("rev-parse", "--abbrev-ref", "HEAD")
    return {
        "version": declared or "not declared by this build",
        "git_commit": commit or "unavailable",
        "git_branch": branch or "unavailable",
    }


def _table_exists(conn, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table,),
    ).fetchone()
    return bool(row)


def _count(conn, table: str, where: str = "", params=()) -> int | None:
    if not _table_exists(conn, table):
        return None
    sql = f'SELECT COUNT(*) FROM "{table}"'
    if where:
        sql += " WHERE " + where
    try:
        return int(conn.execute(sql, tuple(params)).fetchone()[0])
    except Exception:
        return None


def _status_counts(conn, table: str) -> dict:
    if not _table_exists(conn, table):
        return {}
    try:
        rows = conn.execute(
            f'SELECT COALESCE(NULLIF(TRIM(status), \'\'), \'unknown\') AS status, COUNT(*) '
            f'FROM "{table}" GROUP BY status ORDER BY status'
        ).fetchall()
        return {str(row[0]): int(row[1]) for row in rows}
    except Exception:
        return {}


def _database_snapshot() -> dict:
    path = Path(DATABASE)
    result = {
        "path": _safe_path(path.resolve() if path.exists() else path),
        "exists": path.exists(),
        "size_bytes": path.stat().st_size if path.exists() else 0,
        "quick_check": "not run",
        "journal_mode": "unknown",
        "user_version": "unknown",
        "schema_version": "unknown",
        "counts": {},
        "queue_status": {},
        "watch_status": {},
        "recent_scans": [],
        "error": "",
    }
    if not path.exists():
        result["error"] = "Database file does not exist."
        return result

    try:
        conn = sqlite3.connect(str(path), timeout=2.0)
        conn.row_factory = sqlite3.Row
        result["quick_check"] = str(conn.execute("PRAGMA quick_check").fetchone()[0])
        result["journal_mode"] = str(conn.execute("PRAGMA journal_mode").fetchone()[0])
        result["user_version"] = int(conn.execute("PRAGMA user_version").fetchone()[0])
        result["schema_version"] = int(conn.execute("PRAGMA schema_version").fetchone()[0])

        for table in (
            "models",
            "model_sources",
            "model_media",
            "creators",
            "creator_sources",
            "installed_files",
            "download_history",
            "download_queue",
            "download_watchlist",
            "scan_runs",
            "blocked_creators",
            "universal_blocked_creators",
        ):
            value = _count(conn, table)
            if value is not None:
                result["counts"][table] = value

        if _table_exists(conn, "models"):
            result["counts"]["models_new"] = _count(conn, "models", "COALESCE(viewed, 0)=0")
            result["counts"]["models_favorite"] = _count(conn, "models", "COALESCE(favorite, 0)=1")
            result["counts"]["models_with_media"] = _count(conn, "models", "COALESCE(has_media, 0)=1")

        result["queue_status"] = _status_counts(conn, "download_queue")
        result["watch_status"] = _status_counts(conn, "download_watchlist")

        if _table_exists(conn, "scan_runs"):
            rows = conn.execute(
                """
                SELECT id, started, finished, duration, total_processed, total_added,
                       total_updated, total_media, total_images, total_videos
                FROM scan_runs
                ORDER BY id DESC
                LIMIT 5
                """
            ).fetchall()
            for row in rows:
                item = dict(row)
                if _table_exists(conn, "scan_results"):
                    source_rows = conn.execute(
                        """
                        SELECT source, processed, added, updated, media, images, videos
                        FROM scan_results WHERE scan_id=? ORDER BY id
                        """,
                        (row["id"],),
                    ).fetchall()
                    item["sources"] = [dict(source_row) for source_row in source_rows]
                else:
                    item["sources"] = []
                result["recent_scans"].append(item)
        conn.close()
    except Exception as exc:
        result["error"] = _safe_text(f"{type(exc).__name__}: {exc}")

    return result


def _download_snapshot() -> dict:
    try:
        snapshot = active_downloads.snapshot()
    except Exception as exc:
        return {"active": 0, "paused": 0, "failed": 0, "other": 0, "errors": [_safe_text(exc)]}

    items = snapshot.get("items", []) if isinstance(snapshot, dict) else []
    counts = Counter(str(item.get("status") or "unknown") for item in items if isinstance(item, dict))
    active_states = {"starting", "downloading", "installing", "canceling", "pausing"}
    errors = []
    for item in items:
        if not isinstance(item, dict) or item.get("status") != "failed":
            continue
        message = _safe_text(item.get("error") or "Download failed.")
        source = _safe_text(item.get("source") or "unknown")
        errors.append(f"{source}: {message}")
        if len(errors) >= 5:
            break

    active = sum(counts.get(state, 0) for state in active_states)
    known = active + counts.get("paused", 0) + counts.get("failed", 0) + counts.get("complete", 0) + counts.get("canceled", 0)
    return {
        "active": active,
        "paused": counts.get("paused", 0),
        "failed": counts.get("failed", 0),
        "other": max(0, sum(counts.values()) - known),
        "errors": errors,
    }


def _source_snapshot(settings: dict) -> dict:
    source_settings = settings.get("sources", {}) if isinstance(settings.get("sources"), dict) else {}
    try:
        account_config = configured_sources()
    except Exception:
        account_config = {}
    try:
        seaart = seaart_connection_status()
    except Exception:
        seaart = {"scan": False, "download": False}
    try:
        seaart_browser_saved = bool(seaart_browser_session_saved())
    except Exception:
        seaart_browser_saved = False

    health = scan_status.get_source_health()
    sources = []
    known = ["civitai", "civitaired", "huggingface", "modelscope", "seaart", "tensorhub"]
    for source in known:
        conf = source_settings.get(source, {}) if isinstance(source_settings.get(source), dict) else {}
        health_item = health.get(source, {}) if isinstance(health.get(source), dict) else {}
        item = {
            "source": source,
            "display": conf.get("display") or source,
            "enabled": bool(conf.get("enabled")),
            "account_configured": bool(account_config.get(source)),
            "health": str(health_item.get("status") or "not checked"),
            "message": _safe_text(health_item.get("message") or ""),
        }
        if source == "seaart":
            # SeaArt's normal release flow now uses the isolated browser profile.
            # The legacy secrets-manager booleans only describe manually imported
            # cURL sessions, so relying on them alone makes diagnostics report
            # "not connected" even when Source Accounts has a verified saved
            # browser session. A saved browser marker is written only after
            # Finish Connection verifies the signed-in SeaArt account.
            legacy_scan = bool(seaart.get("scan"))
            legacy_download = bool(seaart.get("download"))
            item["browser_session"] = seaart_browser_saved
            item["scan_session"] = seaart_browser_saved or legacy_scan
            item["download_session"] = seaart_browser_saved or legacy_download
            item["account_configured"] = bool(
                item["scan_session"] or item["download_session"]
            )
        if source == "civitai":
            try:
                item["search_session"] = bool(civitai_search_configured())
            except Exception:
                item["search_session"] = False
        sources.append(item)
    return {"sources": sources}


def _scan_snapshot() -> dict:
    try:
        status = scan_status.get_status()
    except Exception:
        status = {}
    safe_sources = {}
    for source, value in (status.get("sources") or {}).items():
        if not isinstance(value, dict):
            continue
        safe_sources[str(source)] = {
            "status": _safe_text(value.get("status") or ""),
            "processed": int(value.get("processed") or 0),
            "added": int(value.get("added") or 0),
            "updated": int(value.get("updated") or 0),
            "images": int(value.get("images") or 0),
            "videos": int(value.get("videos") or 0),
            "message": _safe_text(value.get("message") or ""),
        }
    return {
        "status": _safe_text(status.get("status") or "idle"),
        "source": _safe_text(status.get("source") or ""),
        "current": _safe_text(status.get("current") or ""),
        "processed": int(status.get("processed") or 0),
        "added": int(status.get("added") or 0),
        "updated": int(status.get("updated") or 0),
        "media": int(status.get("media") or 0),
        "images": int(status.get("images") or 0),
        "videos": int(status.get("videos") or 0),
        "message": _safe_text(status.get("message") or ""),
        "sources": safe_sources,
    }


def _json_inline(value) -> str:
    return _safe_text(json.dumps(value, ensure_ascii=False, sort_keys=True))


def generate_diagnostic_report() -> str:
    settings = load_settings()
    prefs = settings.get("preferences", {}) if isinstance(settings.get("preferences"), dict) else {}
    search_settings = settings.get("search_settings", {}) if isinstance(settings.get("search_settings"), dict) else {}
    scan_limits = settings.get("scan_limits", {}) if isinstance(settings.get("scan_limits"), dict) else {}

    identity = _build_identity()
    db = _database_snapshot()
    downloads = _download_snapshot()
    sources = _source_snapshot(settings)["sources"]
    scan = _scan_snapshot()

    enabled_sources = [item["source"] for item in sources if item["enabled"]]
    selected_sources = prefs.get("selected_scan_sources") or prefs.get("scan_sources") or []
    enabled_architectures = prefs.get("enabled_architectures") or prefs.get("scan_architectures") or []

    source_sort = {}
    for source, values in search_settings.items():
        if isinstance(values, dict):
            source_sort[source] = values.get("sort")

    lines = [
        "AbyssBeacon Diagnostic Report",
        "============================",
        f"Generated UTC: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"Report format: {REPORT_FORMAT_VERSION}",
        "",
        "[Build]",
        f"AbyssBeacon version: {identity['version']}",
        f"Git branch: {identity['git_branch']}",
        f"Git commit: {identity['git_commit']}",
        "",
        "[Runtime]",
        f"OS: {_safe_text(platform.platform())}",
        f"Python: {_safe_text(platform.python_version())}",
        f"Python executable: {_safe_path(sys.executable)}",
        f"Flask: {_package_version('Flask')}",
        f"Requests: {_package_version('requests')}",
        f"Pillow: {_package_version('Pillow')}",
        "",
        "[Library / Preferences]",
        f"ComfyUI root: {_safe_path(prefs.get('local_comfy_root'))}",
        f"Download behavior: {_safe_text(prefs.get('download_behavior') or 'browser')}",
        f"Install layout: {_safe_text(prefs.get('install_layout') or 'unknown')}",
        f"Existing-file behavior: {_safe_text(prefs.get('existing_file_behavior') or 'unknown')}",
        f"Friendly filenames: {_safe_text(prefs.get('friendly_filenames') or 'unknown')}",
        f"Save model info sidecar: {_yes_no(prefs.get('save_model_info'))}",
        f"Save preview sidecar: {_yes_no(prefs.get('save_model_preview'))}",
        f"Media per model limit: {_safe_text(prefs.get('media_per_model_limit', 'unknown'))}",
        f"Automatic retention: {_yes_no(prefs.get('auto_cleanup_enabled'))}",
        f"Retention days: {_safe_text(prefs.get('auto_cleanup_days', 'unknown'))}",
        f"Verbose scan logging: {_yes_no(prefs.get('verbose_scan_logging'))}",
        "",
        "[Scanner Configuration]",
        f"Enabled sources: {_json_inline(enabled_sources)}",
        f"Last selected scan sources: {_json_inline(selected_sources)}",
        f"Enabled architectures: {_json_inline(enabled_architectures)}",
        f"Global scan result limit: {_safe_text(scan_limits.get('global_max_results', 'not configured'))}",
        f"Per-source limit overrides: {_json_inline(scan_limits.get('source_overrides', {}))}",
        f"Source sort modes: {_json_inline(source_sort)}",
    ]

    civitai = search_settings.get("civitai", {}) if isinstance(search_settings.get("civitai"), dict) else {}
    if civitai:
        lines.append(f"CivitAI expanded media scan: {_yes_no(civitai.get('include_mature_media'))}")

    tensor = search_settings.get("tensorhub", {}) if isinstance(search_settings.get("tensorhub"), dict) else {}
    if tensor:
        lines.extend([
            f"TensorHub expanded creator search: {_yes_no(tensor.get('creator_expansion_enabled'))}",
            f"TensorHub creator recheck hours: {_safe_text(tensor.get('creator_recheck_hours', 'unknown'))}",
            f"TensorHub creator scan max: {_safe_text(tensor.get('creator_scan_max_results', 'unknown'))}",
        ])

    lines.extend(["", "[Source Status]"])
    for item in sources:
        extra = []
        if item["source"] == "seaart":
            extra.append(f"browser-session={_yes_no(item.get('browser_session'))}")
            extra.append(f"scan-session={_yes_no(item.get('scan_session'))}")
            extra.append(f"download-session={_yes_no(item.get('download_session'))}")
        if item["source"] == "civitai":
            extra.append(f"search-session={_yes_no(item.get('search_session'))}")
        suffix = ("; " + "; ".join(extra)) if extra else ""
        line = (
            f"{item['display']}: enabled={_yes_no(item['enabled'])}; "
            f"credential/session configured={_yes_no(item['account_configured'])}; "
            f"last health={item['health']}{suffix}"
        )
        if item["message"]:
            line += f"; message={item['message']}"
        lines.append(_safe_text(line))

    lines.extend([
        "",
        "[Current Scan]",
        f"Status: {scan['status']}",
        f"Source: {scan['source'] or 'none'}",
        f"Current item: {scan['current'] or 'none'}",
        f"Processed/New/Updated: {scan['processed']}/{scan['added']}/{scan['updated']}",
        f"Media/Images/Videos: {scan['media']}/{scan['images']}/{scan['videos']}",
    ])
    if scan["message"]:
        lines.append(f"Message: {scan['message']}")
    for source, item in scan["sources"].items():
        line = (
            f"  {source}: {item['status'] or 'unknown'}; processed={item['processed']}; "
            f"new={item['added']}; updated={item['updated']}; images={item['images']}; videos={item['videos']}"
        )
        if item["message"]:
            line += f"; message={item['message']}"
        lines.append(_safe_text(line))

    lines.extend([
        "",
        "[Database]",
        f"Path: {db['path']}",
        f"Exists: {_yes_no(db['exists'])}",
        f"Size bytes: {db['size_bytes']}",
        f"SQLite quick_check: {db['quick_check']}",
        f"Journal mode: {db['journal_mode']}",
        f"SQLite user_version: {db['user_version']}",
        f"SQLite schema_version: {db['schema_version']}",
    ])
    if db["error"]:
        lines.append(f"Database error: {db['error']}")
    for key, value in db["counts"].items():
        lines.append(f"{key}: {value}")
    if db["queue_status"]:
        lines.append(f"Download queue states: {_json_inline(db['queue_status'])}")
    if db["watch_status"]:
        lines.append(f"Paid/waiting watch states: {_json_inline(db['watch_status'])}")

    lines.extend([
        "",
        "[Active Downloads]",
        f"Active: {downloads['active']}",
        f"Paused: {downloads['paused']}",
        f"Failed: {downloads['failed']}",
    ])
    for message in downloads["errors"]:
        lines.append(f"Recent download error: {_safe_text(message)}")

    lines.extend(["", "[Recent Scan Timing]"])
    if not db["recent_scans"]:
        lines.append("No scan history recorded.")
    for run in db["recent_scans"]:
        duration = run.get("duration")
        duration_text = f"{float(duration):.2f}s" if duration is not None else "running/unknown"
        lines.append(
            _safe_text(
                f"Scan #{run.get('id')}: started={run.get('started') or 'unknown'}; "
                f"duration={duration_text}; processed={run.get('total_processed') or 0}; "
                f"new={run.get('total_added') or 0}; updated={run.get('total_updated') or 0}; "
                f"media={run.get('total_media') or 0}"
            )
        )
        for source_result in run.get("sources", []):
            lines.append(
                _safe_text(
                    f"  {source_result.get('source')}: processed={source_result.get('processed') or 0}; "
                    f"new={source_result.get('added') or 0}; updated={source_result.get('updated') or 0}; "
                    f"media={source_result.get('media') or 0}"
                )
            )

    failures = [
        item for item in sources
        if str(item.get("health") or "").casefold() in {"error", "skipped", "stopped"}
        or item.get("message")
    ]
    lines.extend(["", "[Recent Warnings / Source Failures]"])
    if not failures and not downloads["errors"]:
        lines.append("No in-memory source/download failures are currently recorded.")
    else:
        for item in failures:
            message = item.get("message") or "No additional message."
            lines.append(_safe_text(f"{item['display']}: {item['health']} — {message}"))
        for message in downloads["errors"]:
            lines.append(_safe_text(f"Download: {message}"))

    lines.extend([
        "",
        "[Privacy]",
        "Credential values are not read into this report. Free-form diagnostic text is redacted again before output.",
        "Do not attach secrets.json or browser profile files to a public issue.",
    ])

    # Defense in depth: redact the complete finished document, even though all
    # potentially free-form fields were already sanitized as they were added.
    return _safe_text("\n".join(lines).rstrip() + "\n")
