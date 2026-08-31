"""Manual, reusable library metadata upgrades.

Library updates are intentionally separate from database migrations. Migrations
keep the SQLite schema usable automatically; library updates may need to
re-evaluate thousands of already-saved source snapshots, so AbyssBeacon lets the
user run them explicitly from Options -> Library.
"""

import json
import threading
from datetime import datetime, timezone
from urllib.parse import quote

import database
from scanners.common.repository_classifier import (
    REPOSITORY_CLASSIFIER_VERSION,
    classify_repository,
    humanize_collection_family_name,
    synthesize_collection_title,
)

_SUPPORTED_REPOSITORY_SOURCES = ("huggingface", "modelscope")
_JOB_LOCK = threading.Lock()
_JOB_STATE = {
    "running": False,
    "started_at": "",
    "finished_at": "",
    "current": 0,
    "total": 0,
    "updated": 0,
    "checked": 0,
    "deferred": 0,
    "failed": 0,
    "source": "",
    "model_key": "",
    "error": "",
    "phase": "",
    "phase_current": 0,
    "phase_total": 0,
    "last_result": {},
}


def _json_object(value):
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value or "{}")
            return dict(parsed) if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _json_list(value):
    if isinstance(value, list):
        return list(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value or "[]")
            return list(parsed) if isinstance(parsed, list) else []
        except Exception:
            return []
    return []


def _classification_version(snapshot):
    card = _json_object(snapshot.get("card_data"))
    classification = card.get("repository_classification")
    if not isinstance(classification, dict):
        return 0
    try:
        return int(classification.get("version") or 0)
    except (TypeError, ValueError):
        return 0


def _checked_resolution(snapshot):
    """Return the saved repository-classifier check marker for this snapshot."""
    card = _json_object(snapshot.get("card_data"))
    checks = card.get("library_update_checks")
    if not isinstance(checks, dict):
        return {}
    resolution = checks.get("repository_classifier")
    return dict(resolution) if isinstance(resolution, dict) else {}


def _checked_resolution_version(snapshot):
    resolution = _checked_resolution(snapshot)
    try:
        return int(resolution.get("version") or 0)
    except (TypeError, ValueError):
        return 0


def _repository_metadata_version(snapshot):
    """Version satisfied either by classification or by an explicit source check.

    A legacy repository that no longer exists (or cannot return enough metadata)
    should not keep AbyssBeacon in a permanent update-needed state. A successful
    check marker resolves that repository for the current metadata version only;
    a future classifier-version bump makes it eligible for another check.
    """
    return max(_classification_version(snapshot), _checked_resolution_version(snapshot))


def _mark_checked_resolution(snapshot, source, model_key, reason, *, status="source_unavailable"):
    card = _json_object(snapshot.get("card_data"))
    checks = card.get("library_update_checks")
    checks = dict(checks) if isinstance(checks, dict) else {}
    checks["repository_classifier"] = {
        "version": int(REPOSITORY_CLASSIFIER_VERSION),
        "status": str(status or "checked"),
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "source": str(source or ""),
        "model_key": str(model_key or ""),
        "reason": str(reason or "Repository metadata unavailable"),
    }
    card["library_update_checks"] = checks
    snapshot["card_data"] = card


def _effective_snapshot(row):
    """Return source-owned data, filling only safe canonical legacy gaps."""
    snapshot = _json_object(row["source_data"])
    source = str(row["source"] or "").strip().lower()
    model_key = str(row["model_key"] or "").strip()
    canonical_matches = (
        str(row["canonical_source"] or "").strip().lower() == source
        and str(row["canonical_model_key"] or "").strip() == model_key
    )

    if canonical_matches:
        # Very old databases can have a seeded model_sources row whose source_data
        # is incomplete. The canonical row is the same provider identity, so it is
        # safe to use those values as a fallback without crossing source boundaries.
        for key in (
            "author", "name", "display_name", "tags", "display_tags", "files",
            "card_data", "description", "base_model", "architecture", "model_type",
            "format", "quantization", "parameters", "license", "pipeline", "gated",
            "updated", "created",
        ):
            if snapshot.get(key) not in (None, "", [], {}):
                continue
            canonical_value = row[key] if key in row.keys() else None
            if canonical_value not in (None, ""):
                snapshot[key] = canonical_value

    snapshot["files"] = _json_list(snapshot.get("files"))
    snapshot["card_data"] = _json_object(snapshot.get("card_data"))
    snapshot["display_tags"] = _json_list(snapshot.get("display_tags"))
    return snapshot


def _repository_rows(conn):
    return conn.execute(
        """
        SELECT
            ms.id AS source_link_id,
            ms.model_id,
            ms.source,
            ms.model_key,
            ms.url,
            ms.source_data,
            m.source AS canonical_source,
            m.model_key AS canonical_model_key,
            m.author,
            m.name,
            m.display_name,
            m.tags,
            m.display_tags,
            m.files,
            m.card_data,
            m.description,
            m.base_model,
            m.architecture,
            m.model_type,
            m.format,
            m.quantization,
            m.parameters,
            m.license,
            m.pipeline,
            m.gated,
            m.updated,
            m.created
        FROM model_sources ms
        JOIN models m ON m.id = ms.model_id
        WHERE lower(ms.source) IN ('huggingface', 'modelscope')
        ORDER BY ms.id
        """
    ).fetchall()


def _compute_update_status():
    conn = database.connect()
    try:
        rows = _repository_rows(conn)
    finally:
        conn.close()

    source_counts = {source: 0 for source in _SUPPORTED_REPOSITORY_SOURCES}
    checked_counts = {source: 0 for source in _SUPPORTED_REPOSITORY_SOURCES}
    current = 0
    checked_resolved = 0
    pending = 0
    for row in rows:
        snapshot = _effective_snapshot(row)
        source = str(row["source"] or "").strip().lower()
        if _repository_metadata_version(snapshot) >= REPOSITORY_CLASSIFIER_VERSION:
            current += 1
            if (
                _classification_version(snapshot) < REPOSITORY_CLASSIFIER_VERSION
                and _checked_resolution_version(snapshot) >= REPOSITORY_CLASSIFIER_VERSION
            ):
                checked_resolved += 1
                if source in checked_counts:
                    checked_counts[source] += 1
            continue
        pending += 1
        if source in source_counts:
            source_counts[source] += 1

    with _JOB_LOCK:
        job = dict(_JOB_STATE)

    return {
        "success": True,
        "needed": pending > 0,
        "pending": pending,
        "current": current,
        "total_repository_snapshots": len(rows),
        "classifier_version": REPOSITORY_CLASSIFIER_VERSION,
        "sources": source_counts,
        "checked_resolved": checked_resolved,
        "checked_sources": checked_counts,
        "job": job,
    }


def get_update_status():
    """Return pending metadata upgrades without changing the library."""
    with _JOB_LOCK:
        job = dict(_JOB_STATE)
    if job.get("running"):
        # Status polling happens several times per second while an update runs.
        # Do not repeatedly JSON-decode the entire library just to report progress.
        total = int(job.get("total") or 0)
        current = int(job.get("current") or 0)
        return {
            "success": True,
            "needed": True,
            "pending": max(0, total - current),
            "current": 0,
            "total_repository_snapshots": total,
            "classifier_version": REPOSITORY_CLASSIFIER_VERSION,
            "sources": {},
            "checked_resolved": 0,
            "checked_sources": {},
            "job": job,
        }
    return _compute_update_status()


def _apply_classification(snapshot, source, model_key, *, details_override=None, files_verified=False):
    files = _json_list(snapshot.get("files"))
    if not files and not files_verified:
        # Legacy snapshots sometimes predate repository file persistence. Do not
        # guess whether they are Collections. The Update Library job will make a
        # targeted source request for only these repositories, then retry.
        return None, "missing stored repository file metadata"

    card = _json_object(snapshot.get("card_data"))
    details = dict(card)
    if isinstance(details_override, dict):
        # Fresh source detail is authoritative for classification while the
        # stored card still contributes AbyssBeacon-specific metadata.
        details.update(details_override)
    if snapshot.get("description") and not details.get("description"):
        details["description"] = snapshot.get("description")
    if snapshot.get("base_model") and not details.get("base_model"):
        details["base_model"] = snapshot.get("base_model")

    fresh = details_override if isinstance(details_override, dict) else {}
    classification = classify_repository({
        "source": source,
        "model_id": model_key,
        "details": details,
        "files": files,
        "tags": (
            fresh.get("tags")
            or fresh.get("Tags")
            or fresh.get("tag")
            or snapshot.get("tags")
            or []
        ),
        "library": (
            fresh.get("library_name")
            or fresh.get("libraryName")
            or fresh.get("library")
            or fresh.get("Library")
            or details.get("library_name")
            or details.get("libraryName")
            or details.get("library")
            or details.get("Library")
            or ""
        ),
    })
    if not isinstance(classification, dict):
        return None, "repository classifier did not return a result"

    classified_type = str(classification.get("display_type") or "").strip()
    if classified_type and classified_type != "Other":
        snapshot["model_type"] = classified_type

    card["repository_classification"] = classification
    checks = card.get("library_update_checks")
    if isinstance(checks, dict) and "repository_classifier" in checks:
        checks = dict(checks)
        checks.pop("repository_classifier", None)
        if checks:
            card["library_update_checks"] = checks
        else:
            card.pop("library_update_checks", None)
    snapshot["card_data"] = card

    display_tags = _json_list(snapshot.get("display_tags"))
    display_tags = [
        tag for tag in display_tags
        if not str(tag or "").strip().casefold().endswith(" collection")
        and str(tag or "").strip().casefold() != "collection"
    ]

    if classification.get("container") == "collection":
        primary = str(classification.get("primary_artifact_type") or "").strip()
        label = f"{primary} Collection" if primary else "Collection"
        display_tags.insert(0, label)
        if classification.get("collection_shape") == "training_series":
            family_name = str(classification.get("single_family_name") or "").strip()
            title = humanize_collection_family_name(family_name)
        else:
            title = synthesize_collection_title(
                snapshot.get("author"),
                snapshot.get("architecture"),
                primary,
                model_key.rsplit("/", 1)[-1],
            )
        if title:
            snapshot["display_name"] = title

    snapshot["display_tags"] = display_tags[:5]
    return classification, ""


def _hydrate_repository_snapshot(snapshot, source, model_key):
    """Fetch only the missing repository inventory for one legacy snapshot.

    This is intentionally not a discovery scan. It requests the exact repository
    already present in the user's library, just like Reload Model, so Update
    Library can finish old rows that predate stored file metadata.
    """
    if source == "huggingface":
        from scanners import huggingface as huggingface_scanner

        huggingface_scanner._apply_auth()
        response = huggingface_scanner.get_with_backoff(
            huggingface_scanner.session,
            f"https://huggingface.co/api/models/{model_key}",
            provider="Hugging Face",
            label=f"library update {model_key}",
            params={"blobs": "true"},
            timeout=15,
        )
        if response.status_code != 200:
            return None, False, f"Hugging Face returned HTTP {response.status_code}"

        details = response.json()
        if not isinstance(details, dict):
            return None, False, "Hugging Face returned invalid repository metadata"

        files = []
        for sibling in details.get("siblings", []) or []:
            if not isinstance(sibling, dict):
                continue
            filename = str(sibling.get("rfilename") or "").strip()
            if not filename:
                continue
            lower_name = filename.casefold()
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

        snapshot["files"] = files
        snapshot["gated"] = int(bool(details.get("gated")))
        if details.get("sha"):
            snapshot["sha"] = str(details.get("sha"))
        if details.get("lastModified"):
            snapshot["updated"] = str(details.get("lastModified"))

        # Merge source card metadata without discarding AbyssBeacon-owned keys.
        source_card = details.get("cardData") or {}
        if isinstance(source_card, dict):
            card = _json_object(snapshot.get("card_data"))
            for key, value in source_card.items():
                if key not in card or card.get(key) in (None, "", [], {}):
                    card[key] = value
            card["gated"] = bool(details.get("gated"))
            snapshot["card_data"] = card

        # A successful source response verifies the inventory even when the repo
        # currently contains zero files, so it is safe to stamp classification.
        return details, True, ""

    if source == "modelscope":
        from scanners import modelscope as modelscope_scanner

        modelscope_scanner._apply_auth()
        details = modelscope_scanner.get_details(model_key)
        if not isinstance(details, dict) or not details:
            return None, False, "ModelScope returned no repository metadata"

        revision = str(details.get("Revision") or details.get("revision") or "master").strip() or "master"
        versions_meta, version_files = modelscope_scanner.extract_versions_from_details(details, model_key)
        files = version_files or modelscope_scanner.get_files(model_key, revision)
        if not isinstance(files, list):
            files = []
        snapshot["files"] = files

        card = _json_object(snapshot.get("card_data"))
        card["versions"] = versions_meta
        ms_card = card.get("modelscope")
        ms_card = dict(ms_card) if isinstance(ms_card, dict) else {}
        ms_card["versions"] = versions_meta
        card["modelscope"] = ms_card
        snapshot["card_data"] = card

        try:
            snapshot["gated"] = int(bool(modelscope_scanner.detect_gated_model({}, details)))
        except Exception:
            snapshot["gated"] = int(bool(details.get("Private") or details.get("private")))

        updated = details.get("LastUpdatedTime") or details.get("last_modified") or details.get("updated_at") or ""
        if updated:
            try:
                updated = modelscope_scanner.normalize_timestamp(updated)
            except Exception:
                updated = str(updated)
            snapshot["updated"] = updated

        description = details.get("description") or details.get("Description") or details.get("ModelDescription") or details.get("model_description") or ""
        if description:
            snapshot["description"] = str(description)

        return details, True, ""

    return None, False, f"targeted repository refresh is not supported for {source}"


def _persist_snapshot_update(conn, row, snapshot):
    source = str(row["source"] or "").strip().lower()
    model_key = str(row["model_key"] or "").strip()
    source_data = json.dumps(snapshot, ensure_ascii=False)
    conn.execute(
        "UPDATE model_sources SET source_data=? WHERE id=?",
        (source_data, int(row["source_link_id"])),
    )

    canonical_matches = (
        str(row["canonical_source"] or "").strip().lower() == source
        and str(row["canonical_model_key"] or "").strip() == model_key
    )
    if not canonical_matches:
        return

    model_type = str(snapshot.get("model_type") or "").strip()
    display_name = str(snapshot.get("display_name") or "").strip()
    display_tags = json.dumps(_json_list(snapshot.get("display_tags")), ensure_ascii=False)
    card_data = json.dumps(_json_object(snapshot.get("card_data")), ensure_ascii=False)
    files = json.dumps(_json_list(snapshot.get("files")), ensure_ascii=False)
    is_collection = str(model_type).casefold() == "collection"
    conn.execute(
        """
        UPDATE models
        SET card_data=?,
            files=?,
            model_type=CASE WHEN ?<>'' THEN ? ELSE model_type END,
            display_name=CASE WHEN ? AND ?<>'' THEN ? ELSE display_name END,
            display_tags=?
        WHERE id=?
        """,
        (
            card_data,
            files,
            model_type,
            model_type,
            int(is_collection),
            display_name,
            display_name,
            display_tags,
            int(row["model_id"]),
        ),
    )

def _set_job(**values):
    with _JOB_LOCK:
        _JOB_STATE.update(values)


def _run_repository_update():
    conn = database.connect()
    updated = 0
    checked = 0
    deferred = 0
    failed = 0
    source_refreshes = 0
    try:
        rows = _repository_rows(conn)
        pending_rows = []
        for row in rows:
            snapshot = _effective_snapshot(row)
            if _repository_metadata_version(snapshot) < REPOSITORY_CLASSIFIER_VERSION:
                pending_rows.append((row, snapshot))

        _set_job(
            total=len(pending_rows), current=0, updated=0, checked=0, deferred=0, failed=0,
            phase="saved_data", phase_current=0, phase_total=len(pending_rows),
        )
        print("\nLIBRARY UPDATE")
        print(f"  Repository classifier target: v{REPOSITORY_CLASSIFIER_VERSION}")
        print(f"  Repository snapshots pending: {len(pending_rows)}")

        needs_source_refresh = []
        for index, (row, snapshot) in enumerate(pending_rows, 1):
            source = str(row["source"] or "").strip().lower()
            model_key = str(row["model_key"] or "").strip()
            _set_job(
                current=index, source=source, model_key=model_key,
                phase_current=index,
            )

            try:
                classification, reason = _apply_classification(snapshot, source, model_key)
                if classification is None:
                    if reason == "missing stored repository file metadata":
                        needs_source_refresh.append((row, snapshot))
                    else:
                        _mark_checked_resolution(
                            snapshot, source, model_key, reason,
                            status="classification_unavailable",
                        )
                        _persist_snapshot_update(conn, row, snapshot)
                        checked += 1
                        _set_job(checked=checked, error=reason)
                    continue

                _persist_snapshot_update(conn, row, snapshot)
                updated += 1
                _set_job(updated=updated)
                if index % 100 == 0:
                    conn.commit()
                    print(f"  Processed {index}/{len(pending_rows)} repository snapshots...")
            except Exception as exc:
                reason = f"{type(exc).__name__}: {exc}"
                try:
                    _mark_checked_resolution(
                        snapshot, source, model_key, reason,
                        status="classification_error",
                    )
                    _persist_snapshot_update(conn, row, snapshot)
                    checked += 1
                    _set_job(checked=checked, error=reason)
                except Exception:
                    failed += 1
                    _set_job(failed=failed, error=reason)

        conn.commit()

        if needs_source_refresh:
            legacy_count = len(needs_source_refresh)
            print(
                f"  {legacy_count} legacy repositor{'y is' if legacy_count == 1 else 'ies are'} "
                "missing file metadata; refreshing only those saved repositories..."
            )
            _set_job(
                phase="source_refresh", phase_current=0,
                phase_total=len(needs_source_refresh), source="", model_key="",
            )

            for refresh_index, (row, snapshot) in enumerate(needs_source_refresh, 1):
                source = str(row["source"] or "").strip().lower()
                model_key = str(row["model_key"] or "").strip()
                _set_job(
                    phase_current=refresh_index, source=source, model_key=model_key
                )
                try:
                    details, verified, reason = _hydrate_repository_snapshot(snapshot, source, model_key)
                    source_refreshes += 1
                    if not verified:
                        _mark_checked_resolution(
                            snapshot, source, model_key, reason,
                            status="source_unavailable",
                        )
                        _persist_snapshot_update(conn, row, snapshot)
                        conn.commit()
                        checked += 1
                        _set_job(checked=checked, error=reason)
                    else:
                        classification, reason = _apply_classification(
                            snapshot, source, model_key,
                            details_override=details, files_verified=True,
                        )
                        if classification is None:
                            _mark_checked_resolution(
                                snapshot, source, model_key, reason,
                                status="classification_unavailable",
                            )
                            _persist_snapshot_update(conn, row, snapshot)
                            conn.commit()
                            checked += 1
                            _set_job(checked=checked, error=reason)
                        else:
                            _persist_snapshot_update(conn, row, snapshot)
                            conn.commit()
                            updated += 1
                            _set_job(updated=updated)

                    if refresh_index % 10 == 0 or refresh_index == len(needs_source_refresh):
                        print(
                            f"  Checked {refresh_index}/{len(needs_source_refresh)} "
                            "legacy repository inventories..."
                        )
                except Exception as exc:
                    reason = f"{type(exc).__name__}: {exc}"
                    try:
                        _mark_checked_resolution(
                            snapshot, source, model_key, reason,
                            status="refresh_error",
                        )
                        _persist_snapshot_update(conn, row, snapshot)
                        conn.commit()
                        checked += 1
                        _set_job(checked=checked, error=reason)
                    except Exception:
                        deferred += 1
                        _set_job(deferred=deferred, error=reason)

        conn.commit()
        remaining = _compute_update_status()
        result = {
            "updated": updated,
            "checked": checked,
            "deferred": deferred,
            "failed": failed,
            "source_refreshes": source_refreshes,
            "remaining": int(remaining.get("pending") or 0),
            "sources_remaining": remaining.get("sources") or {},
        }
        print(
            "Library update complete: "
            f"{updated} updated, {checked} checked/resolved, "
            f"{deferred} deferred, {failed} failed, "
            f"{result['remaining']} still pending."
        )
        if source_refreshes:
            print(
                f"  Targeted repository refreshes used: {source_refreshes} "
                "(no broad source scan)."
            )
        if checked:
            print(
                f"  {checked} repositor{'y was' if checked == 1 else 'ies were'} checked but could not "
                f"supply usable metadata and {'was' if checked == 1 else 'were'} marked resolved for v{REPOSITORY_CLASSIFIER_VERSION}."
            )
            print("  They will be eligible for another check after a future metadata-version change.")
        if result["remaining"]:
            print(
                "Library update notice remains active only for entries that could not be saved "
                "or marked checked."
            )
        else:
            print("Library metadata is current.")

        _set_job(
            running=False,
            finished_at=datetime.now(timezone.utc).isoformat(),
            source="",
            model_key="",
            phase="",
            phase_current=0,
            phase_total=0,
            last_result=result,
        )
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        print(f"Library update failed: {type(exc).__name__}: {exc}")
        _set_job(
            running=False,
            finished_at=datetime.now(timezone.utc).isoformat(),
            error=str(exc),
            phase="",
            phase_current=0,
            phase_total=0,
            last_result={"error": str(exc)},
        )
    finally:
        conn.close()

def start_update():
    with _JOB_LOCK:
        if _JOB_STATE.get("running"):
            return False, dict(_JOB_STATE)
        _JOB_STATE.update({
            "running": True,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": "",
            "current": 0,
            "total": 0,
            "updated": 0,
            "checked": 0,
            "deferred": 0,
            "failed": 0,
            "source": "",
            "model_key": "",
            "error": "",
            "phase": "",
            "phase_current": 0,
            "phase_total": 0,
            "last_result": {},
        })

    thread = threading.Thread(
        target=_run_repository_update,
        name="abyssbeacon-library-update",
        daemon=True,
    )
    thread.start()
    return True, dict(_JOB_STATE)


def print_startup_notice():
    """Print a persistent startup reminder until all applicable rows are current."""
    try:
        status = get_update_status()
    except Exception as exc:
        print(f"Library update check skipped: {exc}")
        return

    if not status.get("needed"):
        return

    sources = status.get("sources") or {}
    hf = int(sources.get("huggingface") or 0)
    ms = int(sources.get("modelscope") or 0)
    print("\nLIBRARY UPDATE AVAILABLE")
    if hf:
        print(f"  Hugging Face repository metadata is out of date: {hf} repository entr{'y' if hf == 1 else 'ies'}")
    if ms:
        print(f"  ModelScope repository metadata is out of date: {ms} repository entr{'y' if ms == 1 else 'ies'}")
    print("  Open Options > Library > Update Library to apply the current repository metadata rules.")
    print("  Legacy entries missing file metadata will be refreshed individually from their source.")
    print("  This notice will remain until all applicable library entries are current.")
