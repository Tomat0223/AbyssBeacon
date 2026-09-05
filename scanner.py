import database, scan_status, scan_control, time, json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from scanners import ALL_SCANNERS
from scanners.http_retry import reset_retry_stats, get_retry_stats, get_pacing_stats
from scan_logging import verbose_enabled
from settings_manager import load_settings, get_search_settings
from utils.loader import load_architectures

DEBUG_SCANNERS = False

# Network discovery is I/O-bound, so independent sources can safely search in
# parallel. Database writes remain serialized in the coordinator thread to keep
# SQLite predictable. Searches/aliases within one source stay sequential for
# now so ModelRadar does not accidentally hammer a single provider.
MAX_PARALLEL_SOURCES = 6

# v2: ModelScope is the current long pole, so only it gets bounded
# intra-source alias concurrency for now. This targets the measured bottleneck
# without multiplying request pressure across every provider at once.
MAX_PARALLEL_SEARCHES_PER_SOURCE = {
    "modelscope": 3,
}


def _source_display_name(source_name, source=None):
    """Return the scanner's user-facing source name for logs/status text."""
    source = source or ALL_SCANNERS.get(str(source_name or ""))
    display = str(getattr(source, "DISPLAY", "") or "").strip() if source else ""
    return display or str(source_name or "")



def _source_scan_summary(display_name, source_stats, duration):
    """Render persisted per-source results instead of the misleading processed count."""
    source_stats = source_stats or {}
    added = int(source_stats.get("added", 0) or 0)
    updated = int(source_stats.get("updated", 0) or 0)
    unchanged = int(source_stats.get("unchanged", 0) or 0)
    return (
        f"{display_name:<14}: {added} new · {updated} updated · "
        f"{unchanged} unchanged in {duration:.2f}s"
    )


def _make_source_progress_reporter(source_name, display_name, watch, *, terminal_enabled=True, browser_enabled=True):
    """Create one generic live-progress callback for provider scanners.

    Provider modules only report stage/current/total. The coordinator owns the
    user-facing formatting so every source behaves consistently. Normal
    multi-source scans keep progress in the browser only; a single sequential
    source may rewrite one terminal line in place. Verbose mode keeps completed
    stage markers as ordinary log lines instead of cursor rewriting.
    """
    source_name = str(source_name or "")
    display_name = str(display_name or source_name)
    watch = str(watch or "Models").strip()

    def report(current=0, total=0, stage="Scanning models", finalize=False):
        try:
            current_value = max(0, int(current or 0))
            total_value = max(0, int(total or 0))
        except (TypeError, ValueError):
            current_value, total_value = 0, 0
        stage_text = str(stage or "Scanning models").strip()

        if browser_enabled:
            scan_status.update_source_progress(
                source_name,
                status="scanning",
                progress_current=current_value,
                progress_total=total_value,
                progress_stage=stage_text,
                progress_label=watch,
            )

        text = f"{display_name} · {watch} · {stage_text}: {current_value}/{total_value}"
        if total_value:
            percent = int(round((current_value / total_value) * 100))
            percent = max(0, min(100, percent))
            text += f" ({percent}%)"

        if verbose_enabled():
            if finalize:
                print(text)
            return

        if not terminal_enabled or not scan_status.single_source_active(source_name):
            return
        scan_status.write_terminal_progress(text, finalize=bool(finalize))

    return report

def get_search_terms():
    settings = load_settings()
    value = settings.get("search_terms", [])
    if isinstance(value, dict):
        terms = []
        for group in value.values():
            if isinstance(group, list):
                terms.extend(group)
        return terms
    return value if isinstance(value, list) else []


def get_enabled_sources():
    settings = load_settings()
    sources = settings.get("sources", {})
    return [name for name, data in sources.items() if data.get("enabled")]


def _normalize_job_term(value):
    return " ".join(str(value or "").strip().casefold().split())


def _source_search_config(architecture_name, data, source_name):
    """Resolve one watch target into source-specific search behavior.

    Known sources may use exact structured base-model filters while text-based
    sources can retain multiple aliases. Unknown/custom watches always fall
    back to literal text searches so ModelRadar never needs a software update
    just to begin watching a newly released architecture.
    """
    searches = data.get("source_searches", {}) if isinstance(data, dict) else {}
    configured = searches.get(source_name) if isinstance(searches, dict) else None

    if isinstance(configured, list):
        return "text", [str(x).strip() for x in configured if str(x).strip()]
    if isinstance(configured, dict):
        mode = str(configured.get("mode") or "text").strip().lower()
        terms = configured.get("terms") or []
        if isinstance(terms, str):
            terms = [terms]
        terms = [str(x).strip() for x in terms if str(x).strip()]
        if terms:
            return ("base_model" if mode == "base_model" else "text"), terms

    keywords = data.get("keywords", []) if isinstance(data, dict) else []
    if isinstance(keywords, str):
        keywords = [keywords]
    fallback = [str(x).strip() for x in keywords if str(x).strip()]
    if not fallback:
        fallback = [architecture_name]
    return "text", fallback


def build_scan_plan(selected_sources, selected_architecture="", legacy_search_terms=None, selected_architectures=None):
    """Build and de-duplicate source-specific discovery jobs."""
    architectures = load_architectures()
    plan = {source: [] for source in selected_sources}
    seen = {source: set() for source in selected_sources}

    if selected_architectures:
        watches = [(name, architectures[name]) for name in selected_architectures if name in architectures]
    elif selected_architecture:
        watches = [(selected_architecture, architectures[selected_architecture])] if selected_architecture in architectures else []
    elif architectures:
        watches = list(architectures.items())
    else:
        watches = []

    # Backward compatibility for callers that still pass explicit search terms
    # (targeted/free-text scans). If no watch could be resolved, use them.
    if not watches and legacy_search_terms:
        watches = [(str(term), {"keywords": [str(term)]}) for term in legacy_search_terms]

    for architecture_name, data in watches:
        for source_name in selected_sources:
            mode, terms = _source_search_config(architecture_name, data, source_name)
            for term in terms:
                key = (mode, _normalize_job_term(term))
                if not key[1] or key in seen[source_name]:
                    continue
                seen[source_name].add(key)
                plan[source_name].append({
                    "watch": architecture_name,
                    "term": term,
                    "mode": mode,
                })

    return plan



def _retention_datetime(value):
    value = str(value or "").strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _model_source_activity(model):
    dates = []
    for field in ("updated", "created"):
        parsed = _retention_datetime(getattr(model, field, ""))
        if parsed:
            dates.append(parsed)
    return max(dates) if dates else None


def _apply_shared_retention_tombstones(source_name, models, source_settings):
    """Prevent stale retention-deleted models from being immediately re-imported.

    A tombstone blocks only while BOTH are true:
      * its remembered source activity is still outside the user's CURRENT
        normal retention window, and
      * the source has not reported newer activity/metadata.

    Therefore widening 7 -> 90 -> 365 days automatically makes newly-in-window
    models eligible again. A genuine upstream update also clears the tombstone.

    TensorHub is excluded here because it has an earlier, cheaper listing-level
    retention preflight with the same current-window semantics.
    """
    if source_name == "tensorhub":
        return models, 0

    if source_settings.get("_external_search"):
        return models, 0

    if not source_settings.get("_normal_retention_enabled"):
        return models, 0

    try:
        days = max(0, min(36500, int(source_settings.get("_normal_retention_days", 7))))
    except (TypeError, ValueError):
        days = 7

    tombstones = database.get_retention_tombstones(source_name)
    if not tombstones:
        return models, 0

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    kept = []
    skipped = 0

    for model in models:
        model_key = str(getattr(model, "model_key", "") or "").strip()
        tombstone = tombstones.get(model_key)
        if not model_key or not tombstone:
            kept.append(model)
            continue

        old_activity = _retention_datetime(tombstone.get("activity_at"))
        current_activity = _model_source_activity(model)

        # If the old model is inside the CURRENT retention window now, an
        # earlier narrow policy must not suppress it anymore.
        if old_activity and old_activity >= cutoff:
            database.clear_retention_tombstone(source_name, model_key)
            kept.append(model)
            continue

        # A real upstream update is new information and is allowed back in.
        if current_activity and old_activity and current_activity > old_activity:
            database.clear_retention_tombstone(source_name, model_key)
            kept.append(model)
            continue

        # If the source gives a current activity date that itself falls inside
        # the active window, trust it and retire stale retention memory.
        if current_activity and current_activity >= cutoff:
            database.clear_retention_tombstone(source_name, model_key)
            kept.append(model)
            continue

        old_hash = str(tombstone.get("metadata_hash") or "").strip()
        current_hash = ""
        try:
            current_hash = str(database.model_metadata_hash(model) or "").strip()
        except Exception:
            current_hash = ""

        # Metadata changes can also represent a legitimate update on providers
        # with weak/missing timestamps.
        if old_hash and current_hash and old_hash != current_hash:
            database.clear_retention_tombstone(source_name, model_key)
            kept.append(model)
            continue

        # Tombstones produced by automatic retention normally have activity.
        # For legacy/no-date memory, only suppress when the fingerprint still
        # proves it is the same source record.
        if not old_activity and not (old_hash and current_hash and old_hash == current_hash):
            kept.append(model)
            continue

        skipped += 1

    return kept, skipped



def _run_one_source_job(source_name, source, job, source_seen_models, search_settings, terminal_progress_enabled=True, browser_progress_enabled=True):
    """Execute one source discovery job and return its models + timing.

    The shared seen-set is only an optimization. Returned models are also
    de-duplicated after all alias workers finish, so a rare race between two
    overlapping aliases cannot create duplicate commits.
    """
    watch = job["watch"]
    term = job["term"]
    mode = job["mode"]

    if scan_control.should_stop():
        return {"watch": watch, "term": term, "models": [], "count": 0, "duration": 0.0, "error": None}

    display_name = _source_display_name(source_name, source)
    if verbose_enabled():
        print(f"\nSEARCH WATCH: {watch}")
        print(f"Scanning {display_name}: {term}")
    scan_status.update_status(
        status="running",
        source=source_name,
        message=f"Scanning {display_name}: {watch}",
    )

    source_settings = dict(search_settings.get(source_name, {}))
    source_settings["_watch_architecture"] = watch
    source_settings["_architecture"] = term if mode == "base_model" else ""
    source_settings["_architecture_context"] = str(job.get("architecture_context") or "").strip()
    source_settings["_external_search"] = bool(job.get("external_search") or source_settings.get("_external_search"))
    source_settings["_external_intent"] = str(job.get("external_intent") or "anything").strip().lower()
    source_settings["_external_query"] = str(job.get("external_query") or term).strip()
    source_settings["_external_architectures"] = [str(value).strip() for value in (job.get("external_architectures") or []) if str(value).strip()]
    source_settings["_search_mode"] = mode
    source_settings["_blocked_creators"] = list(database.get_blocked_creator_set(source_name))
    source_settings["_progress_callback"] = _make_source_progress_reporter(
        source_name,
        display_name,
        watch,
        terminal_enabled=terminal_progress_enabled,
        browser_enabled=browser_progress_enabled,
    )
    current_settings = load_settings()
    prefs = current_settings.get("preferences", {})

    # Centralized Scan Limits apply only to normal source/architecture scans.
    # Creator, Discovery/tag, and Search Sources keep their established limits.
    if not source_settings.get("_external_search") and not job.get("creator"):
        scan_limits = current_settings.get("scan_limits", {}) or {}
        global_limit = scan_limits.get("global_max_results", 150)
        overrides = scan_limits.get("source_overrides", {}) or {}
        override = overrides.get(source_name)
        effective_limit = override if override not in (None, "") else global_limit
        source_settings["_normal_result_unlimited"] = effective_limit in (None, "")
        if source_settings["_normal_result_unlimited"]:
            # Scanner APIs still require a numeric pagination target. This is a
            # technical safety ceiling, not the user-facing stopping boundary;
            # Automatic Retention remains the semantic boundary.
            technical_caps = {
                "modelscope": 3000,
                "huggingface": 5000,
                "civitai": 5000,
                "civitaired": 5000,
                "tensorhub": 5000,
                "seaart": 5000,
            }
            source_settings["max_results"] = technical_caps.get(source_name, 5000)
        else:
            source_settings["max_results"] = max(1, int(effective_limit))

    # Normal scans should not spend time ingesting/detailing models that the
    # user's own retention policy will immediately remove. Explicit Creator
    # and Discovery scans use their separate retention clock and do not use
    # these normal-scan guards.
    source_settings["_normal_retention_enabled"] = bool(prefs.get("auto_cleanup_enabled", False))
    try:
        source_settings["_normal_retention_days"] = max(0, min(36500, int(prefs.get("auto_cleanup_days", 7))))
    except (TypeError, ValueError):
        source_settings["_normal_retention_days"] = 7

    # Normal source discovery and automatic retention use one shared time
    # window. Keeping a second per-source Search Days value was confusing: a
    # scanner could spend time importing 30-day-old models only for a 7-day
    # retention policy to remove them immediately. Explicit external/keyword
    # searches keep their own requested depth/window.
    if not source_settings.get("_external_search"):
        if source_settings["_normal_retention_enabled"]:
            source_settings["search_days"] = source_settings["_normal_retention_days"]
        else:
            # Retention OFF means there is no hidden normal-scan date boundary.
            # Existing scanners expect an integer Search Days value, so use a
            # deliberately remote horizon while the finite result limit is the
            # actual stopping boundary.
            source_settings["search_days"] = 36500

    raw_media_limit = prefs.get("media_per_model_limit", 100)
    try:
        media_limit = int(raw_media_limit)
    except (TypeError, ValueError):
        media_limit = 100
    source_settings["_media_limit"] = max(0, media_limit)

    if DEBUG_SCANNERS:
        print(f"{source_name} settings:", source_settings)

    job_start = time.perf_counter()
    try:
        models = source.scan(term, source_seen_models, source_settings, creator=job.get("creator"))

        models, retention_memory_skipped = _apply_shared_retention_tombstones(
            source_name,
            models,
            source_settings,
        )
        if retention_memory_skipped and verbose_enabled():
            print(
                f"{source_name}: retention memory skipped "
                f"{retention_memory_skipped} unchanged out-of-window model(s)"
            )

        # Enforce the user-selected media sanity limit before anything reaches
        # SQLite or the local preview cache. 0 means Unlimited.
        media_limit = int(source_settings.get("_media_limit") or 0)
        if media_limit > 0:
            for model in models:
                media = list(getattr(model, "media", []) or [])
                if len(media) > media_limit:
                    media = media[:media_limit]
                    model.media = media
                    model.preview_count = sum(1 for item in media if item.get("type", "image") == "image")
                    model.has_media = bool(media)
                    model.has_video = any(item.get("type") == "video" for item in media)

        # "Models" means the query must match model metadata, not merely the
        # uploader name. Anything mode intentionally keeps provider relevance.
        if source_settings.get("_external_search") and source_settings.get("_external_intent") == "models":
            query_text = str(source_settings.get("_external_query") or "").casefold().strip()
            query_terms = [term for term in query_text.split() if term]
            if query_terms:
                kept = []
                for model in models:
                    fields = [
                        getattr(model, "name", ""), getattr(model, "display_name", ""),
                        getattr(model, "description", ""), getattr(model, "tags", ""),
                        " ".join(getattr(model, "display_tags", []) or []),
                        getattr(model, "base_model", ""), getattr(model, "architecture", ""),
                        getattr(model, "model_type", ""), getattr(model, "pipeline", ""),
                    ]
                    haystack = " ".join(str(value or "") for value in fields).casefold()
                    # Treat a multi-word query like the normal feed search: all
                    # words must be present, but they need not form one exact
                    # contiguous phrase. Provider relevance still decides the
                    # candidate set before this final model-only guard.
                    if all(term in haystack for term in query_terms):
                        kept.append(model)
                models = kept

        # Search Sources architecture targeting is deliberately applied after
        # the provider's normal keyword search. This keeps arbitrary queries
        # such as "card" or "unicycle" from being mistaken for architecture
        # aliases while still letting the user narrow imported results.
        if source_settings.get("_external_search"):
            wanted_architectures = {
                str(value).casefold().strip()
                for value in (source_settings.get("_external_architectures") or [])
                if str(value).strip()
            }
            if wanted_architectures:
                before_architecture_filter = len(models)
                models = [
                    model for model in models
                    if str(getattr(model, "architecture", "") or "").casefold().strip() in wanted_architectures
                ]
                if verbose_enabled():
                    print(
                        f"{source_name} external architecture filter: "
                        f"kept {len(models)}/{before_architecture_filter}"
                    )

            # Search Sources can intentionally import old models. Give newly
            # inserted search results the same explicit-scan retention clock as
            # Creator / Discovery Scan results so a 7-day normal retention rule
            # cannot immediately delete a model the user just searched for.
            explicit_added_at = datetime.now(timezone.utc).isoformat()
            for model in models:
                model.retention_mode = "creator_added"
                model.creator_discovered_at = explicit_added_at

        duration = time.perf_counter() - job_start
        if verbose_enabled():
            print(f"{display_name}: {len(models)} models in {duration:.2f}s")
            print(f"{display_name}: processed {len(models)} models")
        return {
            "watch": watch,
            "term": term,
            "models": models,
            "count": len(models),
            "duration": duration,
            "error": None,
        }
    except Exception as exc:
        import traceback
        duration = time.perf_counter() - job_start
        scan_status.finish_terminal_progress()
        print(f"{source_name} failed while scanning {watch} / {term}:")
        traceback.print_exc()
        return {
            "watch": watch,
            "term": term,
            "models": [],
            "count": 0,
            "duration": duration,
            "error": repr(exc),
        }


def _dedupe_discovered_models(models):
    """Keep one returned model per source/model identity before DB commit."""
    unique = []
    seen = set()
    for model in models:
        source = str(getattr(model, "source", "") or "").casefold()
        model_key = str(getattr(model, "model_key", "") or "").casefold()
        url = str(getattr(model, "url", "") or "").casefold()
        key = (source, model_key or url)
        if not key[1]:
            # No reliable identity: keep it rather than accidentally drop data.
            unique.append(model)
            continue
        if key in seen:
            continue
        seen.add(key)
        unique.append(model)
    return unique


def _scan_source_jobs(source_name, source, jobs, search_settings):
    """Run discovery jobs for one source.

    Sources still run in parallel with one another. ModelScope additionally
    gets a bounded three-worker alias pool because it was the measured scan
    bottleneck. Other providers remain sequential in v2 to reduce rate-limit
    risk while we gather real timings.
    """
    source_seen_models = set()
    discovered = []
    job_results = []
    worker_start = time.perf_counter()

    if not jobs:
        return {
            "source_name": source_name,
            "models": [],
            "jobs": [],
            "duration": 0.0,
        }

    per_source_workers = min(
        MAX_PARALLEL_SEARCHES_PER_SOURCE.get(source_name, 1),
        len(jobs),
    )

    if per_source_workers <= 1:
        for job in jobs:
            if scan_control.should_stop():
                break
            result = _run_one_source_job(
                source_name, source, job, source_seen_models, search_settings,
                terminal_progress_enabled=True,
                browser_progress_enabled=True,
            )
            discovered.extend(result.pop("models"))
            job_results.append(result)
    else:
        if verbose_enabled():
            print(f"\n{_source_display_name(source_name, source)}: parallel aliases enabled ({per_source_workers} workers max)")
        with ThreadPoolExecutor(
            max_workers=per_source_workers,
            thread_name_prefix=f"modelradar-{source_name}-alias",
        ) as executor:
            future_map = {
                executor.submit(
                    _run_one_source_job,
                    source_name,
                    source,
                    job,
                    source_seen_models,
                    search_settings,
                    False,
                    False,
                ): job
                for job in jobs
                if not scan_control.should_stop()
            }

            for future in as_completed(future_map):
                result = future.result()
                discovered.extend(result.pop("models"))
                job_results.append(result)

    raw_count = len(discovered)
    discovered = _dedupe_discovered_models(discovered)
    collapsed = raw_count - len(discovered)
    if collapsed:
        print(f"{_source_display_name(source_name, source)}: collapsed {collapsed} overlapping alias result(s) before commit")

    return {
        "source_name": source_name,
        "models": discovered,
        "jobs": job_results,
        "duration": time.perf_counter() - worker_start,
    }


def _apply_scan_preflights(active_scanners, plan):
    """Run optional source-level checks before any per-architecture jobs launch.

    A source that fails preflight is skipped for this run without turning one
    connection problem into one traceback/error per architecture. Other selected
    sources remain runnable.
    """
    skipped = {}

    for source_name, source in active_scanners.items():
        if not plan.get(source_name):
            continue

        preflight = getattr(source, "scan_preflight", None)
        if not callable(preflight):
            continue

        try:
            ok, message = preflight()
        except Exception:
            ok = False
            display = str(getattr(source, "DISPLAY", source_name) or source_name)
            message = (
                f"{display} skipped: connection preflight failed. "
                f"Open Source Accounts to reconnect {display}."
            )

        if ok:
            continue

        message = str(message or f"{source_name} skipped: source is unavailable.").strip()
        print(message)
        plan[source_name] = []
        skipped[source_name] = message
        scan_status.update_source_health(source_name, "skipped", message)
        scan_status.update_source_progress(
            source_name,
            status="skipped",
            processed=0,
            added=0,
            updated=0,
            images=0,
            videos=0,
            message=message,
        )

    return skipped


def _commit_source_models(scan_id, source_name, models, stats):
    """Serialize database writes for one completed source worker."""
    source_stats = {
        "added": 0,
        "updated": 0,
        "changed": 0,
        "unchanged": 0,
        "media": 0,
        "images": 0,
        "videos": 0,
        "processed": 0,
    }

    # Cache only small card covers locally. Full gallery media stays remote.
    try:
        from preview_cache import cache_model_previews
        cached_previews = cache_model_previews(models)
        if cached_previews and verbose_enabled():
            print(f"{source_name}: cached {cached_previews} card preview(s)")
    except Exception as exc:
        if verbose_enabled():
            print(f"{source_name}: preview cache skipped: {exc}")

    commit_started = time.perf_counter()
    for model in models:
        media_items = getattr(model, "media", []) or []

        model_save_started = time.perf_counter()
        save_result = database.add_model(model)
        model_save_elapsed = time.perf_counter() - model_save_started

        state = save_result.get("state", "unchanged") if isinstance(save_result, dict) else "unchanged"
        if state == "new":
            stats["added"] += 1
            source_stats["added"] += 1
            if isinstance(save_result, dict):
                database.record_scan_model_change(scan_id, save_result.get("model_id"), "new")
        elif state == "changed":
            # "updated" now means an existing database row actually changed.
            # Merely re-seeing an unchanged model is counted separately below.
            stats["updated"] += 1
            source_stats["updated"] += 1
            stats["changed"] += 1
            source_stats["changed"] += 1
            if isinstance(save_result, dict):
                database.record_scan_model_change(scan_id, save_result.get("model_id"), "updated")
        else:
            stats["unchanged"] += 1
            source_stats["unchanged"] += 1

        media_changed = bool(save_result.get("media_changed")) if isinstance(save_result, dict) else bool(media_items)
        if media_changed and media_items:
            count = len(media_items)
            stats["media"] += count
            source_stats["media"] += count

            for item in media_items:
                media_type = item.get("type", "image")
                if media_type == "image":
                    stats["images"] += 1
                    source_stats["images"] += 1
                elif media_type == "video":
                    stats["videos"] += 1
                    source_stats["videos"] += 1

    source_stats["processed"] = len(models)
    database.add_scan_result(scan_id, source_name, source_stats)
    commit_elapsed = time.perf_counter() - commit_started
    source_stats["commit_seconds"] = commit_elapsed
    if verbose_enabled():
        print(
            f"DATABASE COMMIT: {source_name} - {len(models)} models, "
            f"{source_stats['media']} media in {commit_elapsed:.2f}s"
        )
    return source_stats


def run_scan(selected_sources=None, search_terms=None, selected_architecture="", explicit_plan=None, search_overrides=None, selected_architectures=None):
    if not selected_sources:
        selected_sources = get_enabled_sources()

    database.initialize()
    scan_id = database.start_scan()
    start_time = time.perf_counter()
    total = 0
    stats = {
        "added": 0,
        "updated": 0,
        "changed": 0,
        "unchanged": 0,
        "duplicates": 0,
        "media": 0,
        "images": 0,
        "videos": 0,
    }
    search_settings = get_search_settings()
    if search_overrides:
        for source_name in list(search_settings):
            search_settings[source_name] = {**search_settings[source_name], **search_overrides}

    if verbose_enabled():
        print("SEARCH SETTINGS:", search_settings)
    scan_status.reset_status()
    scan_status.reset_source_health()
    # scan_control.reset() is intentionally owned by the caller before the
    # background worker starts. Clearing it here can erase a user's Stop
    # request made during scan initialization.
    reset_retry_stats()
    scan_status.update_status(status="running", message="Initializing scan...")

    active_scanners = {
        name: ALL_SCANNERS[name]
        for name in selected_sources
        if name in ALL_SCANNERS and ALL_SCANNERS[name].ENABLED
    }
    architecture_labels = list(selected_architectures or [])
    if not architecture_labels and selected_architecture:
        architecture_labels = [selected_architecture]
    print("SCAN ARCHITECTURES:", ", ".join(architecture_labels) or "all configured")

    plan = explicit_plan if explicit_plan is not None else build_scan_plan(list(active_scanners), selected_architecture, search_terms, selected_architectures)
    if verbose_enabled():
        print("SEARCH PLAN:")
        for source_name, jobs in plan.items():
            readable = [f"{job['watch']} -> {job['term']} ({job['mode']})" for job in jobs]
            print(f"  {source_name}: {readable}")

    scan_status.initialize_sources(list(active_scanners))
    _apply_scan_preflights(active_scanners, plan)

    runnable = [
        (source_name, source, plan.get(source_name, []))
        for source_name, source in active_scanners.items()
        if plan.get(source_name, [])
    ]

    if not runnable:
        print("No scanner jobs were resolved.")
    elif len(runnable) == 1:
        source_name, source, jobs = runnable[0]
        result = _scan_source_jobs(source_name, source, jobs, search_settings)
        if scan_control.should_stop():
            source_stats = {"processed": 0}
        else:
            source_stats = _commit_source_models(scan_id, source_name, result["models"], stats)
        source_state = "stopped" if scan_control.should_stop() else ("error" if any(j.get("error") for j in result.get("jobs", [])) else "complete")
        scan_status.update_source_health(source_name, "stopped" if scan_control.should_stop() else ("error" if source_state == "error" else "ok"), "")
        scan_status.update_source_progress(
            source_name,
            status=source_state,
            processed=source_stats.get("processed", 0),
            added=source_stats.get("added", 0),
            updated=source_stats.get("updated", 0),
            images=source_stats.get("images", 0),
            videos=source_stats.get("videos", 0),
        )
        total += source_stats["processed"]
        if not verbose_enabled():
            scan_status.finish_terminal_progress()
            display_name = _source_display_name(source_name, source)
            print(_source_scan_summary(display_name, source_stats, result["duration"]))
        scan_status.update_status(
            status="stopping" if scan_control.should_stop() else "running",
            source=source_name,
            processed=total,
            added=stats["added"],
            updated=stats["updated"],
            media=stats["media"],
            images=stats["images"],
            videos=stats["videos"],
            message=(
                f"{_source_display_name(source_name, source)}: "
                f"{source_stats.get('added', 0)} new, "
                f"{source_stats.get('updated', 0)} updated, "
                f"{source_stats.get('unchanged', 0)} unchanged"
            ),
        )
    else:
        workers = min(MAX_PARALLEL_SOURCES, len(runnable))
        source_names = ", ".join(_source_display_name(item[0], item[1]) for item in runnable)
        if verbose_enabled():
            print("\n========================================")
            print("PARALLEL SOURCE SCAN")
            print("========================================")
            print(f"Workers : {workers}")
            print(f"Sources : {source_names}")
            print("ModelScope aliases : up to 3 concurrent\nOther source aliases: sequential")
            print("========================================")
        scan_status.update_status(
            status="running",
            source="multiple",
            message=f"Scanning {len(runnable)} sources in parallel...",
        )

        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="modelradar-scan") as executor:
            futures = {
                executor.submit(_scan_source_jobs, source_name, source, jobs, search_settings): source_name
                for source_name, source, jobs in runnable
            }

            for future in as_completed(futures):
                source_name = futures[future]

                try:
                    result = future.result()
                except Exception:
                    import traceback
                    print(f"{source_name} worker failed:")
                    traceback.print_exc()
                    scan_status.update_source_health(source_name, "error", "Source worker failed")
                    scan_status.update_source_progress(source_name, status="error", message="Source worker failed")
                    database.add_scan_result(
                        scan_id,
                        source_name,
                        {"added": 0, "updated": 0, "media": 0, "images": 0, "videos": 0, "processed": 0},
                    )
                    continue

                if scan_control.should_stop():
                    source_stats = {"processed": 0}
                else:
                    source_stats = _commit_source_models(
                        scan_id, source_name, result["models"], stats
                    )
                had_error = any(j.get("error") for j in result.get("jobs", []))
                source_state = "stopped" if scan_control.should_stop() else ("error" if had_error else "complete")
                scan_status.update_source_health(source_name, "stopped" if scan_control.should_stop() else ("error" if had_error else "ok"), "")
                scan_status.update_source_progress(
                    source_name,
                    status=source_state,
                    processed=source_stats.get("processed", 0),
                    added=source_stats.get("added", 0),
                    updated=source_stats.get("updated", 0),
                    images=source_stats.get("images", 0),
                    videos=source_stats.get("videos", 0),
                )
                total += source_stats["processed"]

                display_name = _source_display_name(source_name, active_scanners.get(source_name))
                print(_source_scan_summary(display_name, source_stats, result["duration"]))
                scan_status.update_status(
                    status="stopping" if scan_control.should_stop() else "running",
                    source=source_name,
                    processed=total,
                    added=stats["added"],
                    updated=stats["updated"],
                    media=stats["media"],
                    images=stats["images"],
                    videos=stats["videos"],
                    message=(
                        f"{display_name}: {source_stats.get('added', 0)} new, "
                        f"{source_stats.get('updated', 0)} updated, "
                        f"{source_stats.get('unchanged', 0)} unchanged"
                    ),
                )

    # A selected source with no resolved jobs should still get a zero row in the
    # scan history so the run remains easy to account for.
    runnable_names = {item[0] for item in runnable}
    for source_name in active_scanners:
        if source_name not in runnable_names:
            scan_status.update_source_progress(source_name, status="skipped", processed=0, added=0)
            database.add_scan_result(
                scan_id,
                source_name,
                {"added": 0, "updated": 0, "media": 0, "images": 0, "videos": 0, "processed": 0},
            )

    was_stopped = scan_control.should_stop()
    print("\n================================")
    print("SCAN STOPPED" if was_stopped else "SCAN COMPLETE")
    print("================================")
    print(f"New models     : {stats['added']}")
    print(f"Updated models : {stats['updated']}")
    print(f"Unchanged      : {stats.get('unchanged', 0)}")
    print("================================")
    retry_stats = get_retry_stats()
    retry_parts = []
    for provider, values in retry_stats.items():
        if values.get("retries"):
            state = "recovered" if not values.get("failed") else f"{values.get('failed')} failed"
            retry_parts.append(f"{provider}: {values.get('retries')} retries, {state}")
    if retry_parts:
        print("Rate-limit retries: " + "; ".join(retry_parts))


    failed_jobs = []
    # Source health is populated as workers finish; summarize partial failures.
    for source_name, health in scan_status.get_source_health().items():
        if health.get("status") == "error":
            failed_jobs.append(source_name)
    retry_provider_to_source = {
        "Hugging Face": "huggingface", "ModelScope": "modelscope",
        "CivitAI": "civitai", "CivitAI Red": "civitaired",
    }
    for provider, values in retry_stats.items():
        if values.get("failed"):
            source_name = retry_provider_to_source.get(provider, provider)
            if source_name not in failed_jobs:
                failed_jobs.append(source_name)
            scan_status.update_source_health(source_name, "error", "Rate limit retries exhausted")
    if failed_jobs:
        print("Partial failures : " + ", ".join(sorted(failed_jobs)))

    scan_status.update_status(
        status="stopped" if was_stopped else ("complete_with_errors" if failed_jobs else "complete"),
        added=stats["added"],
        updated=stats["updated"],
        media=stats["media"],
        images=stats["images"],
        videos=stats["videos"],
        processed=total,
        message=(f"Scan stopped. Processed {total} models before stopping." if was_stopped else (f"Scan complete with source errors. Processed {total} models." if failed_jobs else f"Scan complete. Processed {total} models.")),
    )
    duration = time.perf_counter() - start_time
    print(f"Total scan time : {duration:.2f}s")
    database.finish_scan(scan_id, duration, {**stats, "processed": total})
    return total


def run_discovery_scan(source_name, discovery_type, value, *, label="", sort="NEWEST", max_results=100, allowed_architectures=None):
    """Run an explicit provider discovery scan without altering normal scan history.

    Explicit discovery results use the same retention window as Creator Scan
    results: the retention clock begins when ModelRadar first imports them.
    """
    source_name = str(source_name or "").strip().lower()
    discovery_type = str(discovery_type or "").strip().lower()
    value = str(value or "").strip()
    label = str(label or value).strip()
    if not source_name or not value:
        return 0

    source = ALL_SCANNERS.get(source_name)
    if not source or not getattr(source, "ENABLED", False):
        raise ValueError(f"Discovery source is unavailable: {source_name}")

    if discovery_type not in {"tag", "category"} or not hasattr(source, "scan_tag"):
        raise ValueError(f"{source_name} does not support {discovery_type} discovery yet")

    database.initialize()
    scan_status.reset_status()
    scan_control.reset()
    reset_retry_stats()
    scan_status.update_status(status="running", source=source_name, message=(f"Scanning ModelScope tag: {label}…" if source_name == "modelscope" else f"Discovering {label} on {source_name}..."))

    start_time = time.perf_counter()
    print("\n================================")
    print("DISCOVERY SCAN")
    print("================================")
    print("Source :", source_name)
    print("Type   :", discovery_type)
    print("Value  :", label)
    print("Sort   :", sort)
    print("Limit  :", max_results)

    allowed_architectures = {str(name).casefold() for name in (allowed_architectures or []) if str(name).strip()}
    if source_name == "modelscope":
        models = source.scan_tag(value, max_results=max_results, sort=sort, tag_name=label, allowed_architectures=allowed_architectures)
    else:
        models = source.scan_tag(value, max_results=max_results, sort=sort, tag_name=label)
    if allowed_architectures:
        before = len(models)
        models = [model for model in models if str(getattr(model, "architecture", "Other") or "Other").casefold() in allowed_architectures]
        print(f"Architecture target: kept {len(models)}/{before} discovery result(s)")

    # Respect the global media sanity limit used by normal scans.
    prefs = load_settings().get("preferences", {})
    try:
        media_limit = int(prefs.get("media_per_model_limit", 100) or 0)
    except (TypeError, ValueError):
        media_limit = 100
    if media_limit > 0:
        for model in models:
            media = list(getattr(model, "media", []) or [])
            if len(media) > media_limit:
                media = media[:media_limit]
                model.media = media
                model.preview_count = sum(1 for item in media if item.get("type", "image") == "image")
                model.has_media = bool(media)
                model.has_video = any(item.get("type") == "video" for item in media)

    try:
        from preview_cache import cache_model_previews
        cache_model_previews(models)
    except Exception as exc:
        if verbose_enabled():
            print(f"{source_name}: discovery preview cache skipped: {exc}")

    stats = {"added": 0, "updated": 0, "unchanged": 0, "media": 0, "images": 0, "videos": 0}
    explicit_added_at = datetime.now(timezone.utc).isoformat()

    for model in models:
        if scan_control.should_stop():
            break
        existing = database.model_exists(model.model_key, model.source)
        if not existing:
            # Reuse the established explicit-scan retention marker. The column
            # name is historical, but the policy intentionally covers both
            # Creator Scan and Discovery Scan imports.
            model.retention_mode = "creator_added"
            model.creator_discovered_at = explicit_added_at

        save_result = database.add_model(model)
        state = save_result.get("state", "unchanged") if isinstance(save_result, dict) else "unchanged"
        if state == "new":
            stats["added"] += 1
        elif state == "changed":
            stats["updated"] += 1
        else:
            stats["unchanged"] += 1

        media_items = getattr(model, "media", []) or []
        media_changed = bool(save_result.get("media_changed")) if isinstance(save_result, dict) else bool(media_items)
        if media_changed:
            stats["media"] += len(media_items)
            for item in media_items:
                if item.get("type", "image") == "video":
                    stats["videos"] += 1
                else:
                    stats["images"] += 1

        processed = stats["added"] + stats["updated"] + stats["unchanged"]
        scan_status.update_status(
            status="running",
            source=source_name,
            processed=processed,
            added=stats["added"],
            updated=stats["updated"],
            media=stats["media"],
            images=stats["images"],
            videos=stats["videos"],
            message=f"Discovery Scan: {processed}/{len(models)} models processed",
        )

    duration = time.perf_counter() - start_time
    stopped = scan_control.should_stop()
    processed = stats["added"] + stats["updated"] + stats["unchanged"]
    scan_status.update_status(
        status="stopped" if stopped else "complete",
        source=source_name,
        processed=processed,
        added=stats["added"],
        updated=stats["updated"],
        media=stats["media"],
        images=stats["images"],
        videos=stats["videos"],
        message=("Discovery Scan stopped." if stopped else f"Discovery Scan complete — {stats['added']} new, {stats['updated']} updated."),
    )

    print("\n================================")
    print("DISCOVERY SCAN STOPPED" if stopped else "DISCOVERY SCAN COMPLETE")
    print("================================")
    print(f"New models     : {stats['added']}")
    print(f"Updated models : {stats['updated']}")
    print(f"Unchanged      : {stats['unchanged']}")
    print(f"Media changed  : {stats['media']}")
    print(f"Images         : {stats['images']}")
    print(f"Videos         : {stats['videos']}")
    print(f"Total time     : {duration:.2f}s")
    return processed

if __name__ == "__main__":
    run_scan()

def run_creator_scan(
    author,
    selected_sources=None,
    mode="targeted",
    architecture="",
    architectures=None,
    model_type=""
):
    """Scan exact creator/owner names across supported enabled sources.

    Cross-source username matches are stored independently by source; ModelRadar
    does not assume identical usernames belong to the same real-world creator.
    """
    author = (author or "").strip()
    if not author:
        return 0

    if not selected_sources:
        selected_sources = get_enabled_sources()

    blocked_sources = [source for source in selected_sources if database.is_creator_blocked(source, author)]
    if blocked_sources:
        print(f"Creator scan skipped: {author} is blocked on {', '.join(blocked_sources)}")
        selected_sources = [source for source in selected_sources if source not in blocked_sources]
        if not selected_sources:
            return 0

    database.initialize()
    start_time = time.perf_counter()
    creator_started = datetime.now(timezone.utc).isoformat()
    search_settings = get_search_settings()

    # Creator scans have their own history. They intentionally never write to
    # scan_runs, which keeps the navbar Last scan reserved for the main SCAN.
    conn = database.connect()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS creator_scan_runs (
            id INTEGER PRIMARY KEY,
            creator TEXT NOT NULL,
            started TEXT,
            finished TEXT,
            duration REAL DEFAULT 0,
            mode TEXT,
            architecture TEXT,
            model_type TEXT,
            sources TEXT,
            processed INTEGER DEFAULT 0,
            added INTEGER DEFAULT 0,
            updated INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()
    scan_seen_models = set()

    stats = {
        "added": 0,
        "updated": 0,
        "duplicates": 0,
        "media": 0,
        "images": 0,
        "videos": 0,
    }
    total = 0

    scan_control.reset()
    reset_retry_stats()
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
        message=f"Scanning creator {author}...",
        sources={},
    )

    print("\n================================")
    print("CREATOR SCAN")
    print("================================")
    print("Creator:", author)
    print("Sources:", selected_sources)

    matching_architectures = {
        str(value or "").strip().casefold()
        for value in (architectures or [])
        if str(value or "").strip()
    }
    if mode == "matching":
        print("Architectures:", list(architectures or []))

    for source_name in selected_sources:
        if scan_control.should_stop():
            break

        source = ALL_SCANNERS.get(source_name)
        if not source or not getattr(source, "ENABLED", False):
            continue

        # Only sources with exact-owner scanning support participate. This is
        # deliberately explicit so fuzzy username searches never get mistaken
        # for verified creator identity matches.
        if source_name not in {"huggingface", "modelscope", "civitai", "civitaired", "tensorhub", "seaart"}:
            print(f"{source_name}: creator scan not supported yet; skipping")
            continue

        source_stats = {
            "added": 0,
            "updated": 0,
            "media": 0,
            "images": 0,
            "videos": 0,
            "processed": 0,
        }

        scan_status.update_status(
            status="running",
            source=source_name,
            current=author,
            message=f"Scanning {author} on {source_name}"
        )

        try:
            source_settings = search_settings.get(source_name, {})
            source_start = time.perf_counter()
            models = source.scan(
                "",
                scan_seen_models,
                source_settings,
                creator=author
            )
            elapsed = time.perf_counter() - source_start

            # Apply creator-requested filters only to what gets stored. The
            # source scan still walks the creator catalog so a targeted or
            # matching scan cannot miss models that happen to be on later pages.
            if mode in {"targeted", "matching"}:
                wanted_arch = (architecture or "").strip().casefold()
                wanted_type = (model_type or "").strip().casefold()
                filtered_models = []
                for model in models:
                    model_arch = str(getattr(model, "architecture", "") or "").strip().casefold()
                    model_kind = str(getattr(model, "model_type", "") or "").strip().casefold()

                    if mode == "matching":
                        if matching_architectures and model_arch not in matching_architectures:
                            continue
                    else:
                        if wanted_arch and model_arch != wanted_arch:
                            continue
                        if wanted_type and model_kind != wanted_type:
                            continue

                    filtered_models.append(model)
                models = filtered_models

            # Store only the requested subset. Creator scans intentionally do
            # NOT create scan-history rows; the navbar Last scan belongs only
            # to the main discovery SCAN button.
            for model in models:
                existing = database.model_exists(model.model_key, model.source)
                # Models first discovered through an explicit Creator Scan can be
                # much older than the normal source-search window. Keep their
                # source created/updated dates intact, but use the date ModelRadar
                # added them for retention and card activity so a deep historical
                # find is not immediately treated as stale. Existing normal-scan
                # rows keep their original retention behavior.
                if not existing:
                    model.retention_mode = "creator_added"
                    model.creator_discovered_at = datetime.now(timezone.utc).isoformat()
                database.add_model(model)

                if existing:
                    stats["updated"] += 1
                    source_stats["updated"] += 1
                else:
                    stats["added"] += 1
                    source_stats["added"] += 1

                media_items = getattr(model, "media", []) or []
                stats["media"] += len(media_items)
                source_stats["media"] += len(media_items)
                for media_item in media_items:
                    kind = media_item.get("type", "image")
                    if kind == "video":
                        stats["videos"] += 1
                        source_stats["videos"] += 1
                    else:
                        stats["images"] += 1
                        source_stats["images"] += 1

            total += len(models)
            source_stats["processed"] = len(models)

            print(
                f"{source_name}: creator scan processed {len(models)} "
                f"changed/new models in {elapsed:.2f}s"
            )

            scan_status.update_status(
                status="running",
                source=source_name,
                processed=total,
                added=stats["added"],
                updated=stats["updated"],
                media=stats["media"],
                images=stats["images"],
                videos=stats["videos"],
                message=f"{source_name}: creator scan complete"
            )

        except Exception:
            import traceback
            print(f"{source_name} creator scan failed:")
            traceback.print_exc()

    duration = time.perf_counter() - start_time
    scan_status.update_status(
        status="complete",
        processed=total,
        added=stats["added"],
        updated=stats["updated"],
        media=stats["media"],
        images=stats["images"],
        videos=stats["videos"],
        message=f"Creator scan complete for {author}."
    )

    creator_finished = datetime.now(timezone.utc).isoformat()
    conn = database.connect()
    conn.execute("""
        INSERT INTO creator_scan_runs (
            creator, started, finished, duration, mode, architecture,
            model_type, sources, processed, added, updated
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        author, creator_started, creator_finished, duration, mode,
        (
            ", ".join(str(value) for value in (architectures or []))
            if mode == "matching"
            else (architecture or "")
        ),
        model_type or "", json.dumps(selected_sources),
        total, stats["added"], stats["updated"]
    ))
    conn.commit()
    conn.close()

    print("Creator scan complete:", author, f"({duration:.2f}s)")
    return total
