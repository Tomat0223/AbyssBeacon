import copy
import json
import os
import threading


_SETTINGS_LOCK = threading.RLock()


DEFAULT_SEARCH_SETTINGS = {
    "huggingface": {
        "search_days": 7,
        "max_results": 100,
        "sort": "newest_updated"
    },
    "modelscope": {
        "search_days": 7,
        "max_results": 50,
        "sort": "newest_updated"
    },
    "civitai": {
        "search_days": 7,
        "max_results": 100,
        "sort": "newest",
        "include_mature_media": False
    },
    "civitaired": {
        "search_days": 7,
        "max_results": 100,
        "sort": "newest"
    },
    "tensorhub": {
        "search_days": 7,
        "max_results": 100,
        "sort": "newest",
        "creator_expansion_enabled": False,
        "creator_probe_results": 20,
        "creator_recheck_hours": 6,
        "creator_scan_max_results": 1000
    },
    "seaart": {
        "search_days": 7,
        "max_results": 100,
        "sort": "newest"
    }
}



SCAN_LIMIT_SOURCES = tuple(DEFAULT_SEARCH_SETTINGS.keys())
DEFAULT_SCAN_LIMITS = {
    "global_max_results": 150,
    "source_overrides": {source: None for source in SCAN_LIMIT_SOURCES},
}

SOURCE_RESULT_CAPS = {
    "huggingface": 5000,
    "modelscope": 3000,
    "civitai": 5000,
    "civitaired": 5000,
    "tensorhub": 5000,
    "seaart": 5000,
}


def normalize_scan_limits(scan_limits=None, legacy_search_settings=None):
    """Normalize centralized normal-scan result limits.

    global_max_results=None means Unlimited and is valid only when Automatic
    Retention supplies the normal scan's date boundary. Per-source overrides
    are optional finite lower ceilings; values at/above a finite global limit
    normalize back to Use Global (None).
    """
    scan_limits = scan_limits if isinstance(scan_limits, dict) else {}
    legacy_search_settings = legacy_search_settings if isinstance(legacy_search_settings, dict) else {}

    raw_global = scan_limits.get("global_max_results", 150)
    if raw_global in (None, "", "unlimited", "Unlimited"):
        global_limit = None
    else:
        global_limit = _int_value(raw_global, 150, minimum=1, maximum=5000)

    raw_overrides = scan_limits.get("source_overrides")
    if not isinstance(raw_overrides, dict):
        # One-time migration from the old scattered source maximums. The new
        # global default is 150; only old values below that become overrides.
        raw_overrides = {}
        if not scan_limits:
            for source in SCAN_LIMIT_SOURCES:
                old = legacy_search_settings.get(source, {})
                try:
                    old_limit = int(old.get("max_results"))
                except (TypeError, ValueError, AttributeError):
                    continue
                if old_limit > 0 and old_limit < 150:
                    raw_overrides[source] = old_limit

    overrides = {}
    for source in SCAN_LIMIT_SOURCES:
        raw = raw_overrides.get(source)
        if raw in (None, "", "global", "use_global"):
            overrides[source] = None
            continue
        cap = SOURCE_RESULT_CAPS[source]
        value = _int_value(raw, 1, minimum=1, maximum=cap)
        if global_limit is not None and value >= global_limit:
            overrides[source] = None
        else:
            overrides[source] = value

    return {
        "global_max_results": global_limit,
        "source_overrides": overrides,
    }


VALID_SORTS = {
    "huggingface": {
        "newest_updated",
        "newest_created",
        "downloads",
        "likes",
        "trending"
    },
    "modelscope": {
        "newest_updated",
        "downloads",
        "likes",
        "default"
    },
    "civitai": {
        "newest",
        "downloads",
        "highest_rated"
    },
    "civitaired": {
        "newest",
        "downloads",
        "highest_rated"
    },
    "tensorhub": {
        "newest",
        "newest_updated"
    },
    "seaart": {
        "newest"
    }
}


def _int_value(value, default, minimum=1, maximum=None):
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = default

    value = max(minimum, value)

    if maximum is not None:
        value = min(maximum, value)

    return value


def normalize_source_settings(source, values=None):
    values = values or {}
    defaults = DEFAULT_SEARCH_SETTINGS[source]

    result = copy.deepcopy(defaults)

    result["search_days"] = _int_value(
        values.get("search_days", defaults["search_days"]),
        defaults["search_days"]
    )

    sort_value = values.get("sort", defaults["sort"])
    if sort_value not in VALID_SORTS[source]:
        sort_value = defaults["sort"]
    result["sort"] = sort_value

    # Migrate older per-page settings automatically. Users now specify only
    # the maximum number of source results they want ModelRadar to inspect;
    # each scanner handles its own pagination and per-request limits.
    raw_max = values.get("max_results")
    if raw_max is None:
        if source == "huggingface":
            raw_max = values.get("result_limit", defaults["max_results"])
        else:
            old_size = values.get("page_size")
            old_count = values.get("page_count")
            if old_size is not None or old_count is not None:
                try:
                    raw_max = int(old_size or defaults["max_results"]) * int(old_count or 1)
                except (TypeError, ValueError):
                    raw_max = defaults["max_results"]
            else:
                raw_max = defaults["max_results"]

    max_caps = {
        "huggingface": 5000,
        "modelscope": 3000,
        "civitai": 5000,
        "civitaired": 5000,
        "tensorhub": 5000,
        "seaart": 5000,
    }
    result["max_results"] = _int_value(
        raw_max,
        defaults["max_results"],
        maximum=max_caps[source]
    )

    if source == "civitai":
        raw_include_mature = values.get("include_mature_media", defaults.get("include_mature_media", False))
        if isinstance(raw_include_mature, str):
            raw_include_mature = raw_include_mature.strip().lower() in {"1", "true", "yes", "on"}
        result["include_mature_media"] = bool(raw_include_mature)

    if source == "tensorhub":
        raw_creator_enabled = values.get("creator_expansion_enabled", defaults.get("creator_expansion_enabled", False))
        if isinstance(raw_creator_enabled, str):
            raw_creator_enabled = raw_creator_enabled.strip().lower() in {"1", "true", "yes", "on"}
        result["creator_expansion_enabled"] = bool(raw_creator_enabled)

        # Normal Expanded Creator Search intentionally checks only the newest
        # creator page. Deeper creator history belongs to the explicit Creator Scan.
        result["creator_probe_results"] = 20
        result["creator_recheck_hours"] = _int_value(
            values.get("creator_recheck_hours", defaults.get("creator_recheck_hours", 6)),
            defaults.get("creator_recheck_hours", 6),
            minimum=0,
            maximum=720,
        )
        result["creator_scan_max_results"] = _int_value(
            values.get("creator_scan_max_results", defaults.get("creator_scan_max_results", 1000)),
            defaults.get("creator_scan_max_results", 1000),
            minimum=1,
            maximum=5000,
        )

    return result

def normalize_search_settings(search_settings=None):
    search_settings = search_settings or {}

    return {
        source: normalize_source_settings(
            source,
            search_settings.get(source, {})
        )
        for source in DEFAULT_SEARCH_SETTINGS
    }


def _write_settings_unlocked(settings):
    """Write settings atomically so concurrent requests cannot corrupt JSON."""
    target = os.path.abspath("settings.json")
    temp = target + ".tmp"

    with open(temp, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=4)
        f.flush()
        os.fsync(f.fileno())

    os.replace(temp, target)


def _recover_first_json_object(text):
    """Recover a valid leading JSON object from a file with trailing duplicate data."""
    stripped = text.lstrip()
    if not stripped:
        return None

    try:
        value, end = json.JSONDecoder().raw_decode(stripped)
    except json.JSONDecodeError:
        return None

    return value if isinstance(value, dict) else None


def load_settings():
    repaired = False

    with _SETTINGS_LOCK:
        try:
            with open("settings.json", encoding="utf-8") as f:
                text = f.read()
            settings = json.loads(text)
        except json.JSONDecodeError as e:
            print("ERROR LOADING SETTINGS.JSON")
            print(e)

            # The most common failure mode was multiple preference saves writing
            # settings.json at the same time, leaving a valid object followed by
            # extra JSON. Recover the first complete object automatically.
            settings = _recover_first_json_object(text) if 'text' in locals() else None
            if settings is not None:
                print("Recovered settings.json and removed trailing/corrupt data.")
                repaired = True
            else:
                settings = {}
        except Exception as e:
            print("ERROR LOADING SETTINGS.JSON")
            print(e)
            settings = {}

        if not isinstance(settings, dict):
            settings = {}

        if not isinstance(settings.get("preferences"), dict):
            settings["preferences"] = {}

        # CivitAI shipped disabled while its scanner was a placeholder. Enable
        # it once when upgrading to the real integration; after this marker is
        # written, a user's later source choice is respected.
        migrations = settings.setdefault("migrations", {})
        if not migrations.get("civitai_scanner_v1"):
            sources = settings.setdefault("sources", {})
            civitai = sources.setdefault("civitai", {
                "display": "CivitAI", "enabled": True, "color": "#ff4d88"
            })
            civitai["enabled"] = True
            migrations["civitai_scanner_v1"] = True
            repaired = True

        # Add CivitAI Red as a distinct authenticated source. It is enabled in
        # the source registry but is not forced into an existing user's scan
        # selection; selecting it without credentials simply shows a clear
        # connection message.
        if not migrations.get("civitaired_scanner_v1"):
            sources = settings.setdefault("sources", {})
            sources.setdefault("civitaired", {
                "display": "CivitAI Red", "enabled": True, "color": "#ff2d55"
            })
            migrations["civitaired_scanner_v1"] = True
            repaired = True

        # TensorHub Art is a separate provider from tensor.art. Add it without
        # forcing it into an existing user's active source selection.
        if not migrations.get("tensorhub_scanner_v1"):
            sources = settings.setdefault("sources", {})
            sources.setdefault("tensorhub", {
                "display": "TensorHub Art", "enabled": True, "color": "#ff7a45"
            })
            migrations["tensorhub_scanner_v1"] = True
            repaired = True

        # SeaArt public discovery/detail integration. Add the source without
        # changing an existing user's active scan-source selection.
        if not migrations.get("seaart_scanner_v1"):
            sources = settings.setdefault("sources", {})
            sources.setdefault("seaart", {
                "display": "SeaArt", "enabled": True, "color": "#35c7ff"
            })
            migrations["seaart_scanner_v1"] = True
            repaired = True

        # Backward compatibility with the short-lived global scanner settings.
        legacy_scanner = settings.get("scanner", {})
        source_settings = settings.get("search_settings", {})

        if not source_settings and legacy_scanner:
            source_settings = {}
            for source in DEFAULT_SEARCH_SETTINGS:
                migrated = {}
                if "search_days" in legacy_scanner:
                    migrated["search_days"] = legacy_scanner["search_days"]
                if "result_limit" in legacy_scanner:
                    migrated["max_results"] = legacy_scanner["result_limit"]
                source_settings[source] = migrated

        settings["search_settings"] = normalize_search_settings(source_settings)
        normalized_limits = normalize_scan_limits(
            settings.get("scan_limits"),
            legacy_search_settings=settings["search_settings"],
        )
        if settings.get("scan_limits") != normalized_limits:
            settings["scan_limits"] = normalized_limits
            repaired = True

        if repaired:
            _write_settings_unlocked(settings)

        return settings


def get_search_settings():
    settings = load_settings()
    return settings["search_settings"]


def get_source_search_settings(source):
    return get_search_settings().get(
        source,
        copy.deepcopy(DEFAULT_SEARCH_SETTINGS.get(source, {}))
    )


def save_settings(settings):
    with _SETTINGS_LOCK:
        if not isinstance(settings, dict):
            settings = {}

        if not isinstance(settings.get("preferences"), dict):
            settings["preferences"] = {}

        settings["search_settings"] = normalize_search_settings(
            settings.get("search_settings", {})
        )
        settings["scan_limits"] = normalize_scan_limits(
            settings.get("scan_limits"),
            legacy_search_settings=settings["search_settings"],
        )

        # The global scanner block is obsolete now that each source owns its
        # search controls.
        settings.pop("scanner", None)

        _write_settings_unlocked(settings)

