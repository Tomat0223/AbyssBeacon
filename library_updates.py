"""Manual, reusable library metadata upgrades.

Library updates are intentionally separate from database migrations. Migrations
keep the SQLite schema usable automatically; library updates may need to
re-evaluate thousands of already-saved source snapshots, so AbyssBeacon lets the
user run them explicitly from Options -> Library.
"""

import json
import re
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import database
from scanners.common import metadata as source_metadata
from scanners.common.repository_classifier import (
    REPOSITORY_CLASSIFIER_VERSION,
    REPOSITORY_CLASSIFIER_SOURCE_VERSIONS,
    classify_repository,
    repository_classifier_target_version,
    humanize_collection_family_name,
    synthesize_collection_title,
)

_SUPPORTED_REPOSITORY_SOURCES = ("huggingface", "modelscope")
_CIVITAI_COMPANION_NAMES = {"abyssbeacon info.txt", "modelradar info.txt"}
_LEGACY_COMPANION_MEDIA_SUFFIXES = {
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif",
    ".mp4", ".webm", ".mov",
}
_MODEL_WEIGHT_SUFFIXES = {
    ".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf",
}
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


def _normalized_source_name(value):
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def _is_civitai_companion_folder(source_dir, relative_path):
    """Confirm a legacy folder belongs to CivitAI/Red without guessing."""
    if any(
        _normalized_source_name(part) in {"civitai", "civitaired"}
        for part in relative_path.parts
    ):
        return True

    # The Simple install layout does not include a source directory, so use
    # AbyssBeacon's own info file as the source proof when it is available.
    for info_name in ("AbyssBeacon Info.txt", "ModelRadar Info.txt"):
        info_path = source_dir / info_name
        if not info_path.is_file():
            continue
        try:
            info_text = info_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in info_text.splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            if key.strip().casefold() != "source":
                continue
            return _normalized_source_name(value) in {"civitai", "civitaired"}
    return False


def _looks_like_companion_name(filename):
    """Return True for current sidecars or old per-weight companion names."""
    lower_name = str(filename or "").casefold()
    return (
        lower_name in _CIVITAI_COMPANION_NAMES
        or lower_name.startswith("preview.")
        or lower_name.endswith("_instructions.txt")
        or "_preview." in lower_name
    )


def _companion_matches_destination_weight(item, destination_weights):
    """Require legacy companions to share a moved destination weight's stem."""
    lower_name = item.name.casefold()
    if lower_name in _CIVITAI_COMPANION_NAMES or lower_name.startswith("preview."):
        return True

    for weight in destination_weights:
        weight_stem = weight.stem.casefold()
        if lower_name == f"{weight_stem}_instructions.txt":
            return True
        if (
            lower_name.startswith(f"{weight_stem}_preview.")
            and item.suffix.casefold() in _LEGACY_COMPANION_MEDIA_SUFFIXES
        ):
            return True
    return False


def _repair_stranded_civitai_companions(checkpoint_root, lora_root):
    """Move AbyssBeacon sidecars after their LoRA weight has already moved.

    This intentionally does not classify or move model weights. A source folder
    is eligible only when it has no weight remaining and the exact parallel
    LoRA folder already contains a weight. Conflicts are never overwritten.
    """
    checkpoint_root = Path(checkpoint_root)
    lora_root = Path(lora_root)
    result = {"moved": 0, "folders_removed": 0, "conflicts": 0, "failed": 0}
    if not checkpoint_root.is_dir() or not lora_root.is_dir():
        return result

    candidate_dirs = set()
    try:
        for item in checkpoint_root.rglob("*"):
            if not item.is_file():
                continue
            if _looks_like_companion_name(item.name):
                candidate_dirs.add(item.parent)
    except OSError:
        result["failed"] += 1
        return result

    for source_dir in sorted(candidate_dirs, key=lambda path: str(path).casefold()):
        try:
            relative_path = source_dir.relative_to(checkpoint_root)
        except ValueError:
            continue
        if not _is_civitai_companion_folder(source_dir, relative_path):
            continue

        destination_dir = lora_root / relative_path
        try:
            source_weights = [
                item for item in source_dir.iterdir()
                if item.is_file() and item.suffix.casefold() in _MODEL_WEIGHT_SUFFIXES
            ]
            destination_weights = [
                item for item in destination_dir.iterdir()
                if item.is_file() and item.suffix.casefold() in _MODEL_WEIGHT_SUFFIXES
            ] if destination_dir.is_dir() else []
        except OSError:
            result["failed"] += 1
            continue

        if source_weights or not destination_weights:
            continue

        for item in list(source_dir.iterdir()):
            if not item.is_file():
                continue
            if not _companion_matches_destination_weight(item, destination_weights):
                continue
            target = destination_dir / item.name
            if target.exists():
                result["conflicts"] += 1
                continue
            try:
                shutil.move(str(item), str(target))
                result["moved"] += 1
            except OSError:
                result["failed"] += 1

        try:
            source_dir.rmdir()
            result["folders_removed"] += 1
        except OSError:
            # The folder may contain an unrelated file or a conflict. Preserve
            # it exactly as-is rather than broadening cleanup behavior.
            pass

    return result


def repair_stranded_civitai_companions():
    """Repair companions left behind by a manual/legacy LoRA relocation."""
    try:
        from installer import APP_NAMESPACE, category_base_directory, resolve_comfy_root
        from settings_manager import load_settings

        settings = load_settings() or {}
        preferences = settings.get("preferences") if isinstance(settings, dict) else {}
        preferences = preferences if isinstance(preferences, dict) else {}
        configured_root = str(
            preferences.get("local_comfy_root")
            or preferences.get("local_models_root")
            or ""
        ).strip()
        if not configured_root:
            return {"moved": 0, "folders_removed": 0, "conflicts": 0, "failed": 0}

        comfy_root = resolve_comfy_root(configured_root)
        checkpoint_root = category_base_directory(comfy_root, "checkpoints") / APP_NAMESPACE
        lora_root = category_base_directory(comfy_root, "loras") / APP_NAMESPACE
        return _repair_stranded_civitai_companions(checkpoint_root, lora_root)
    except Exception:
        # Startup must never fail because a local library path is unavailable.
        return {"moved": 0, "folders_removed": 0, "conflicts": 0, "failed": 1}


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


def _huggingface_library_refresh_version(snapshot):
    card = _json_object(snapshot.get("card_data"))
    marker = card.get("hf_library_refresh")
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


def _huggingface_library_refresh_target():
    # Kept source-specific so media/inventory maintenance does not force a fake
    # repository-classifier version bump. The scanner owns the canonical value.
    try:
        from scanners import huggingface as huggingface_scanner
        return int(huggingface_scanner.HF_LIBRARY_REFRESH_VERSION)
    except Exception:
        return 1


def _repository_update_current(snapshot, source):
    source = str(source or "").strip().casefold()
    if _repository_metadata_version(snapshot) < repository_classifier_target_version(source):
        return False
    if source == "huggingface":
        return _huggingface_library_refresh_version(snapshot) >= _huggingface_library_refresh_target()
    return True


def _mark_huggingface_library_refresh(snapshot, *, status="complete", reason="", inventory_complete=True, readme_checked=True):
    card = _json_object(snapshot.get("card_data"))
    card["hf_library_refresh"] = {
        "version": int(_huggingface_library_refresh_target()),
        "status": str(status or "checked"),
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "inventory_complete": bool(inventory_complete),
        "readme_checked": bool(readme_checked),
        "reason": str(reason or ""),
    }
    snapshot["card_data"] = card


def _mark_checked_resolution(snapshot, source, model_key, reason, *, status="source_unavailable"):
    card = _json_object(snapshot.get("card_data"))
    checks = card.get("library_update_checks")
    checks = dict(checks) if isinstance(checks, dict) else {}
    checks["repository_classifier"] = {
        "version": int(repository_classifier_target_version(source)),
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
        target_version = repository_classifier_target_version(source)
        if _repository_update_current(snapshot, source):
            current += 1
            if (
                _classification_version(snapshot) < target_version
                and _checked_resolution_version(snapshot) >= target_version
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
        "source_versions": dict(REPOSITORY_CLASSIFIER_SOURCE_VERSIONS),
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
            "source_versions": dict(REPOSITORY_CLASSIFIER_SOURCE_VERSIONS),
            "sources": {},
            "checked_resolved": 0,
            "checked_sources": {},
            "job": job,
        }
    return _compute_update_status()


def _needs_huggingface_source_refresh(snapshot, source, classification, details_override=None):
    """Return True when v5 needs one exact-repository HF metadata refresh.

    Most repository upgrades can be completed entirely from saved files/tags.
    Metadata-light Hugging Face archives are the exception: several independent
    safetensors can look like an ordinary/unknown repository until README prose
    identifies them as a LoRA bundle. Refresh only those ambiguous multi-weight
    repositories, never the whole source.
    """
    if isinstance(details_override, dict) and details_override.get("_library_source_metadata_checked"):
        return False
    if str(source or "").strip().casefold() != "huggingface":
        return False
    if not isinstance(classification, dict) or classification.get("container") == "collection":
        return False

    files = _json_list(snapshot.get("files"))
    deployable = []
    for item in files:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or item.get("name") or "").strip().casefold().replace("\\", "/")
        if not path.endswith(".safetensors"):
            continue
        if any(marker in path for marker in (
            "/optimizer", "optimizer.", "training_state", "scheduler",
            "/checkpoint-", "/checkpoints/", "global_step", "mp_rank", "zero_pp_rank",
        )):
            continue
        deployable.append(path)
    if len(deployable) < 4:
        return False

    primary = str(classification.get("primary_artifact_type") or "").strip().casefold()
    existing_type = str(snapshot.get("model_type") or "").strip().casefold()
    components = classification.get("component_evidence")
    has_structural_full_model = isinstance(components, dict) and any(bool(value) for value in components.values())

    # Unknown/misleading legacy rows are the main v5 target. Existing LoRA rows
    # also get one source check when they contain many weights but failed to
    # resolve into a Collection from saved metadata. Strong multi-component full
    # models are left alone.
    if has_structural_full_model:
        return False
    return primary == "other" or existing_type == "lora"


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

    if _needs_huggingface_source_refresh(snapshot, source, classification, details_override):
        return None, "Hugging Face repository needs source metadata/README verification"

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
            label=f"library refresh {model_key}",
            params={"blobs": "true"},
            timeout=15,
        )
        if response.status_code != 200:
            return None, False, f"Hugging Face returned HTTP {response.status_code}"

        details = response.json()
        if not isinstance(details, dict):
            return None, False, "Hugging Face returned invalid repository metadata"

        revision = str(details.get("sha") or "main").strip() or "main"
        files, inventory_complete, inventory_method = huggingface_scanner.repository_files_with_status(
            model_key,
            details,
            force_recursive=True,
        )
        if not isinstance(files, list):
            files = []
        snapshot["files"] = files
        snapshot["gated"] = int(bool(details.get("gated")))
        if details.get("sha"):
            snapshot["sha"] = str(details.get("sha"))
        if details.get("lastModified"):
            snapshot["updated"] = str(details.get("lastModified"))

        card = _json_object(snapshot.get("card_data"))
        source_card = details.get("cardData") or {}
        if isinstance(source_card, dict):
            for key, value in source_card.items():
                if key not in card or card.get(key) in (None, "", [], {}):
                    card[key] = value
        card["gated"] = bool(details.get("gated"))
        card = huggingface_scanner.repository_inventory_marker(
            card,
            complete=inventory_complete,
            revision=revision,
            method=inventory_method,
            file_count=len(files),
        )

        readme_text = ""
        readme_checked = False
        readme_reason = ""
        try:
            readme_response = huggingface_scanner.get_with_backoff(
                huggingface_scanner.session,
                f"https://huggingface.co/{model_key}/raw/main/README.md",
                provider="Hugging Face",
                label=f"library refresh README {model_key}",
                timeout=10,
            )
            readme_checked = True
            if readme_response.status_code == 200:
                readme_text = str(readme_response.text or "")
                details["readme"] = readme_text
            else:
                readme_reason = f"README HTTP {readme_response.status_code}"
        except Exception as exc:
            readme_reason = f"README check failed: {type(exc).__name__}: {exc}"

        description = source_metadata.extract_description(details)
        if description:
            snapshot["description"] = str(description)

        repository_media_data = huggingface_scanner.media.extract_media(
            files,
            f"https://huggingface.co/{model_key}/resolve/main",
        )
        readme_media = (
            huggingface_scanner.extract_readme_media(readme_text, model_key)
            if readme_text
            else []
        )
        card = huggingface_scanner.readme_media_marker(card, readme_media)
        model_media = huggingface_scanner.merge_media_items(
            repository_media_data.get("media") or [],
            readme_media,
        )
        media_data = huggingface_scanner.media_summary(model_media)
        snapshot["media"] = model_media
        snapshot["image"] = media_data.get("image") or ""
        snapshot["preview_count"] = int(media_data.get("preview_count") or 0)
        snapshot["has_media"] = int(bool(media_data.get("has_media")))
        snapshot["has_video"] = int(bool(media_data.get("has_video")))

        refresh_status = "complete" if inventory_complete and readme_checked else "checked"
        refresh_reason_parts = []
        if not inventory_complete:
            refresh_reason_parts.append("recursive inventory incomplete")
        if readme_reason:
            refresh_reason_parts.append(readme_reason)
        card = huggingface_scanner.library_refresh_marker(
            card,
            status=refresh_status,
            reason="; ".join(refresh_reason_parts),
            inventory_complete=inventory_complete,
            readme_checked=readme_checked,
        )
        snapshot["card_data"] = card
        details["_library_source_metadata_checked"] = True
        details["_library_inventory_complete"] = bool(inventory_complete)
        details["_library_readme_checked"] = bool(readme_checked)

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
            display_tags=?,
            gated=?,
            description=CASE WHEN ?<>'' THEN ? ELSE description END,
            updated=CASE WHEN ?<>'' THEN ? ELSE updated END,
            sha=CASE WHEN ?<>'' THEN ? ELSE sha END
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
            int(bool(snapshot.get("gated", 0))),
            str(snapshot.get("description") or ""),
            str(snapshot.get("description") or ""),
            str(snapshot.get("updated") or ""),
            str(snapshot.get("updated") or ""),
            str(snapshot.get("sha") or ""),
            str(snapshot.get("sha") or ""),
            int(row["model_id"]),
        ),
    )


def _refresh_huggingface_media(row, snapshot, model_key=""):
    try:
        media_items = _json_list(snapshot.get("media"))
        database.refresh_canonical_model_media(
            int(row["model_id"]),
            "huggingface",
            media_items,
            fallback_image=str(snapshot.get("image") or ""),
        )
        return True
    except Exception as media_exc:
        print(f"  Hugging Face media refresh skipped [{model_key}]: {media_exc}")
        return False


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
            source = str(row["source"] or "").strip().lower()
            if not _repository_update_current(snapshot, source):
                pending_rows.append((row, snapshot))

        _set_job(
            total=len(pending_rows), current=0, updated=0, checked=0, deferred=0, failed=0,
            phase="saved_data", phase_current=0, phase_total=len(pending_rows),
        )
        print("\nLIBRARY UPDATE")
        print(
            "  Repository metadata targets: "
            f"Hugging Face v{repository_classifier_target_version('huggingface')} · "
            f"ModelScope v{repository_classifier_target_version('modelscope')}"
        )
        print(f"  Hugging Face library inventory/media refresh: v{_huggingface_library_refresh_target()}")
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
                if (
                    source == "huggingface"
                    and _huggingface_library_refresh_version(snapshot) < _huggingface_library_refresh_target()
                ):
                    needs_source_refresh.append((row, snapshot))
                    continue

                classification, reason = _apply_classification(snapshot, source, model_key)
                if classification is None:
                    if reason in {
                        "missing stored repository file metadata",
                        "Hugging Face repository needs source metadata/README verification",
                    }:
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
                f"  {legacy_count} repositor{'y needs' if legacy_count == 1 else 'ies need'} "
                "targeted source refresh; checking only repositories already saved in the library..."
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
                        if source == "huggingface":
                            _mark_huggingface_library_refresh(
                                snapshot,
                                status="source_unavailable",
                                reason=reason,
                                inventory_complete=False,
                                readme_checked=False,
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
                            if source == "huggingface":
                                _refresh_huggingface_media(row, snapshot, model_key)
                            checked += 1
                            _set_job(checked=checked, error=reason)
                        else:
                            if source == "huggingface":
                                # _hydrate_repository_snapshot records the full refresh
                                # marker in card_data. Keep a defensive marker here for
                                # older/custom scanner paths that still return verified.
                                if _huggingface_library_refresh_version(snapshot) < _huggingface_library_refresh_target():
                                    _mark_huggingface_library_refresh(
                                        snapshot,
                                        status="checked",
                                        reason="Targeted Hugging Face library refresh completed",
                                        inventory_complete=bool(details.get("_library_inventory_complete")),
                                        readme_checked=bool(details.get("_library_readme_checked")),
                                    )
                            _persist_snapshot_update(conn, row, snapshot)
                            conn.commit()
                            if source == "huggingface":
                                _refresh_huggingface_media(row, snapshot, model_key)
                            updated += 1
                            _set_job(updated=updated)

                    if refresh_index % 10 == 0 or refresh_index == len(needs_source_refresh):
                        print(
                            f"  Checked {refresh_index}/{len(needs_source_refresh)} "
                            "repository metadata checks..."
                        )
                except Exception as exc:
                    reason = f"{type(exc).__name__}: {exc}"
                    try:
                        _mark_checked_resolution(
                            snapshot, source, model_key, reason,
                            status="refresh_error",
                        )
                        if source == "huggingface":
                            _mark_huggingface_library_refresh(
                                snapshot,
                                status="source_unavailable",
                                reason=reason,
                                inventory_complete=False,
                                readme_checked=False,
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
                f"  Targeted library repository refreshes used: {source_refreshes} "
                "(only repositories already saved in the library; no discovery scan)."
            )
        if checked:
            print(
                f"  {checked} repositor{'y was' if checked == 1 else 'ies were'} checked but could not "
                f"supply usable metadata and {'was' if checked == 1 else 'were'} marked resolved for the current source metadata version."
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
    companion_result = repair_stranded_civitai_companions()
    moved = int(companion_result.get("moved") or 0)
    folders_removed = int(companion_result.get("folders_removed") or 0)
    conflicts = int(companion_result.get("conflicts") or 0)
    failed = int(companion_result.get("failed") or 0)
    if moved or folders_removed or conflicts or failed:
        print("\nCIVITAI LORA COMPANION CLEANUP")
        print(f"  Companion files moved : {moved}")
        print(f"  Empty folders removed : {folders_removed}")
        if conflicts:
            print(f"  Existing-file conflicts: {conflicts} left untouched")
        if failed:
            print(f"  Filesystem errors       : {failed} item(s) left untouched")

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
        print(f"  Hugging Face library metadata/inventory needs refresh: {hf} repository entr{'y' if hf == 1 else 'ies'}")
    if ms:
        print(f"  ModelScope repository metadata is out of date: {ms} repository entr{'y' if ms == 1 else 'ies'}")
    print("  Open Options > Library > Update Library to apply the current repository metadata rules.")
    if hf:
        print("  Hugging Face entries will be checked only from the saved library: full repository tree + README media.")
    print("  Legacy entries missing file metadata will be refreshed individually from their source.")
    print("  This notice will remain until all applicable library entries are current.")
