from __future__ import annotations

from scan_logging import verbose_print as print

import re
import builtins
import time
import json
import hashlib
import copy
from pathlib import Path
import html as html_lib
from datetime import datetime, timezone, timedelta

import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

from secrets_manager import get_source_token

import scan_control
import scan_status
from scanners.common.model import Model
from scanners.common import metadata as common_metadata

NAME = "tensorhub"

# Non-user-configurable, source-specific safety exclusions.
HARD_BLOCKED_CREATORS = {"e7g3", "kunjung"}
HARD_BLOCKED_OWNER_IDS = {
    "838872246360732333",  # Kunjung
    "893963469739538903",  # R
}


def _creator_identity_is_blocked(owner_id="", nickname="", blocked_creators=None):
    owner_id = str(owner_id or "").strip()
    nickname = str(nickname or "").casefold().strip()
    blocked_creators = blocked_creators or set()
    return owner_id in HARD_BLOCKED_OWNER_IDS or bool(nickname and nickname in blocked_creators)


def _owner_is_blocked(owner, blocked_creators):
    owner = owner if isinstance(owner, dict) else {}
    return _creator_identity_is_blocked(
        owner.get("id"),
        owner.get("nickname"),
        blocked_creators,
    )
DISPLAY = "TensorHub Art"
ENABLED = True

API = "https://api.tensorhub.art/community-web/v1/project/portal/list/v3"
CREATOR_API = "https://api.tensorhub.art/community-web/v1/project/user/list"
DETAIL_API = "https://api.tensorhub.art/community-web/v1/model/detail"
IMAGE_DETAIL_API = "https://api.tensorhub.art/community-web/v1/image/detail"
FILE_DETAIL_API = "https://api.tensorhub.art/community-web/v1/model/file/detail"
GENERAL_SEARCH_API = "https://api.tensorhub.art/community-web/v1/search/general/v2"
DETAIL_ENRICHMENT_VERSION = 5
SITE = "https://tensorhub.art"
PAGE_SIZE = 32

# Detail enrichment is network-bound. Stress testing showed 50 concurrent
# requests remained stable while 100 could saturate connections and cause
# ConnectTimeout failures. Recovery retries use a deliberately smaller pool.
DETAIL_WORKERS = 50
DETAIL_RETRY_WORKERS = 10

_DETAIL_DEBUG_LIMIT = 3
_DETAIL_DEBUG_SAVED = 0

_CREATOR_STATE_PATH = Path("app_config") / "tensorhub_creator_state.json"


def _load_creator_probe_state():
    try:
        if not _CREATOR_STATE_PATH.exists():
            return {}
        data = json.loads(_CREATOR_STATE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_creator_probe_state(state):
    try:
        _CREATOR_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        temp = _CREATOR_STATE_PATH.with_suffix(".json.tmp")
        temp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
        temp.replace(_CREATOR_STATE_PATH)
    except Exception as exc:
        print(f"TensorHub creator cooldown state warning: {exc}")


def _creator_probe_key(owner_id, base_model):
    return f"{str(owner_id).strip()}|{str(base_model).strip().casefold()}"


def _creator_probe_due(state, owner_id, base_model, cooldown_hours):
    if cooldown_hours <= 0:
        return True
    raw = str(state.get(_creator_probe_key(owner_id, base_model)) or "").strip()
    if not raw:
        return True
    try:
        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - stamp.astimezone(timezone.utc)
        return age.total_seconds() >= cooldown_hours * 3600
    except Exception:
        return True


def _status_print(*args, **kwargs):
    """Important TensorHub progress that should be visible even with verbose logs off."""
    builtins.print(*args, **kwargs)


# TensorHub exposes several partially independent discovery channels. The main
# /models feed does not reliably contain every project that appears in these
# channels, so AbyssBeacon scans all of them and de-duplicates by project ID.
CHANNELS = {
    "character": "107",
    "anime": "101",
    "realistic": "100",
    "illustration": "108",
    "sci_fi": "109",
    "visual_design": "110",
    "space_design": "111",
    "game_design": "112",
}

session = requests.Session()
session.headers.update({
    "User-Agent": "AbyssBeacon/1.0",
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Origin": SITE,
    "Referer": f"{SITE}/models",
})


def _apply_auth():
    """Apply the optional TensorHub Art website-session Bearer token to requests."""
    token = get_source_token("tensorhub")
    if token:
        session.headers["Authorization"] = f"Bearer {token}"
    else:
        session.headers.pop("Authorization", None)



def _post_with_backoff(payload, label, max_retries=3, url=API, public_catalog=False):
    """POST one TensorHub API page with bounded 429 handling.

    TensorHub's public /models catalog can differ when the request inherits an
    account Authorization header or does not look like the normal web client.
    Discovery lanes therefore opt into the same logged-out/public request
    identity used by tensorhub.art itself. Detail/download access remains free
    to use the configured account session.
    """
    waits = (3, 7, 15)
    attempt = 0
    while True:
        request_headers = None
        if public_catalog:
            # Requests removes session-level headers when the per-request value
            # is None, so Authorization cannot personalize/narrow discovery.
            request_headers = {
                "Authorization": None,
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:153.0) Gecko/20100101 Firefox/153.0",
                "Accept": "*/*",
                "Content-Type": "application/json",
                "Origin": SITE,
                "Referer": f"{SITE}/search" if url == GENERAL_SEARCH_API else f"{SITE}/models",
                "X-Request-Package-Id": "3023",
                "X-Request-Lang": "en-US",
            }
        response = session.post(
            url,
            json=payload,
            headers=request_headers,
            timeout=30,
        )
        if response.status_code != 429:
            return response
        if attempt >= max_retries:
            print(f"TensorHub rate limit persisted after {max_retries} retries: {label}")
            return response
        wait = waits[min(attempt, len(waits) - 1)]
        attempt += 1
        retry_after = str(response.headers.get("Retry-After") or "").strip()
        try:
            parsed = float(retry_after)
            if parsed > 0:
                wait = parsed
        except (TypeError, ValueError):
            pass
        shown = int(wait) if float(wait).is_integer() else round(wait, 1)
        print(f"TensorHub rate limited: {label} - waiting {shown}s (retry {attempt}/{max_retries})")
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if scan_control.should_stop():
                return response
            time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))



def _get_with_backoff(url, params, label, max_retries=3):
    """GET one TensorHub API resource with bounded 429 handling."""
    waits = (3, 7, 15)
    attempt = 0
    while True:
        response = session.get(url, params=params, timeout=30)
        if response.status_code != 429:
            return response
        if attempt >= max_retries:
            _status_print(f"TensorHub rate limit persisted after {max_retries} retries: {label}")
            return response
        wait = waits[min(attempt, len(waits) - 1)]
        attempt += 1
        retry_after = str(response.headers.get("Retry-After") or "").strip()
        try:
            parsed = float(retry_after)
            if parsed > 0:
                wait = parsed
        except (TypeError, ValueError):
            pass
        shown = int(wait) if float(wait).is_integer() else round(wait, 1)
        _status_print(f"TensorHub rate limited: {label} - waiting {shown}s (retry {attempt}/{max_retries})")
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if scan_control.should_stop():
                return response
            time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))


def _sort_value(value):
    value = str(value or "newest").strip().lower()
    if value in {"newest_updated", "latest_update", "updated"}:
        return "LATEST_UPDATE"
    return "NEWEST"


def _architecture_name(base_model):
    """Normalize TensorHub's source taxonomy into AbyssBeacon's canonical watches.

    TensorHub exposes internal IDs such as FLUX_2_KLEIN_9B_DISTILLED and
    WAN_2_2_A14B_HIGH_NOISE.  Do not collapse those to generic FLUX/Other when
    AbyssBeacon already has a precise architecture for the family.
    """
    text = str(base_model or "").strip()
    upper = text.upper().replace("-", "_").replace(" ", "_")
    upper = re.sub(r"_+", "_", upper).strip("_")

    if upper == "KREA_2":
        return "Krea 2"

    if "MINIMAX" in upper and "H3" in upper:
        return "MiniMax-H3"

    if upper in {
        "FLUX_2_KLEIN_4B",
        "FLUX_2_KLEIN_4B_BASE",
        "FLUX_2_KLEIN_4B_DISTILLED",
    }:
        return "FLUX.2 Klein 4B"

    if upper in {
        "FLUX_2_KLEIN_9B",
        "FLUX_2_KLEIN_9B_BASE",
        "FLUX_2_KLEIN_9B_DISTILLED",
    }:
        return "FLUX.2 Klein 9B"

    if upper in {
        "WAN_2_2",
        "WAN_2_2_5B",
        "WAN_2_2_A14B_LOW_NOISE",
        "WAN_2_2_A14B_HIGH_NOISE",
        "WAN_2_2_S2V_14B",
        "WAN_2_2_ANIMATE_14B",
    }:
        return "WAN 2.2"

    if upper in {"Z_IMAGE", "ZIMAGEBASE"}:
        return "Z-Image Base"

    if upper in {"Z_IMAGE_TURBO", "ZIMAGETURBO"}:
        return "Z-Image Turbo"

    if upper in {"LTX_2_5", "LTXV_2_5"}:
        return "LTX-2.5"

    # Preserve legacy fallbacks for architectures that are not yet part of the
    # curated V1 registry.  These can be tightened during the normalization pass.
    if upper.startswith("FLUX"):
        return "Other"
    if upper.startswith("LTX"):
        return "LTX"
    if upper.startswith("SCAIL"):
        return "SCAIL"
    return "Other"


def _matches_base_model(item, expected_base_model):
    """Return True only when TensorHub's structured nested base model matches."""
    expected = str(expected_base_model or "").strip().upper().replace("-", "_").replace(" ", "_")
    if not expected:
        return True

    nested = item.get("model") or {}
    actual_values = (
        nested.get("baseModel"),
        nested.get("baseModelDisplayName"),
    )

    for value in actual_values:
        actual = str(value or "").strip().upper().replace("-", "_").replace(" ", "_")
        if actual == expected:
            return True
    return False


def _model_type(value):
    normalized = str(value or "").strip().upper().replace("-", "_").replace(" ", "_")
    mapping = {
        "LORA": "LoRA",
        "LYCORIS": "LoRA",
        "LOHA": "LoRA",
        "CHECKPOINT": "Checkpoint",
        "WORKFLOW": "Workflow",
        "EMBEDDING": "Embedding",
        "TEXTUAL_INVERSION": "Embedding",
        "CONTROLNET": "ControlNet",
        "VAE": "VAE",
        "UPSCALER": "Upscaler",
    }
    return mapping.get(normalized, "Other")


def _safe_int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _cover_record(item):
    model_data = item.get("model") or {}
    cover = model_data.get("cover") or model_data.get("coverShowcase") or {}
    if not isinstance(cover, dict):
        cover = {}
    url = str(cover.get("url") or "").strip()
    snapshot = str(cover.get("snapshot") or "").strip()
    content_rating = str(cover.get("contentRating") or "").strip()
    lower = url.lower().split("?", 1)[0]
    is_video = lower.endswith((".mp4", ".webm", ".mov"))
    is_gif = lower.endswith(".gif")
    media_type = "video" if is_video else "image"
    image_url = snapshot if is_video and snapshot else ("" if is_video else url)
    return {
        "url": url,
        "image_url": image_url,
        "type": media_type,
        "thumbnail": snapshot if is_video else "",
        "content_rating": content_rating,
        "animated": is_gif,
    }


def _access_info(item):
    """Return a conservative *listing-level* TensorHub access state.

    Portal/list metadata is incomplete for some genuinely downloadable models.
    Therefore only explicit positive/negative signals are trusted here and an
    ambiguous listing stays ``unknown``. Public model-detail enrichment can
    later upgrade this to a definitive state.
    """
    model_data = item.get("model") or {}
    model_flags = model_data.get("flags") or {}
    project_flags = item.get("flags") or {}
    sponsor = item.get("sponsorInfo") or {}
    sponsored_download = sponsor.get("projectDownload") if isinstance(sponsor, dict) else None

    explicit_download = any((
        model_flags.get("freeDownload") is True,
        model_flags.get("allowDownload") is True,
        project_flags.get("freeDownload") is True,
        project_flags.get("allowDownload") is True,
    ))
    explicit_forbid = any((
        model_flags.get("forbidDownload") is True,
        project_flags.get("forbidDownload") is True,
    ))
    requires_grant = any((
        model_flags.get("grantDownload") is True,
        project_flags.get("grantDownload") is True,
        bool(sponsored_download),
    ))

    if explicit_download:
        status = "downloadable"
    elif explicit_forbid:
        status = "non_downloadable"
    elif requires_grant:
        status = "restricted"
    else:
        status = "unknown"

    return status, {
        "status": status,
        "downloadable": status == "downloadable",
        "authoritative": False,
        "model_flags": model_flags,
        "project_flags": project_flags,
        "sponsor_info": sponsor,
    }


def _tag_data(item):
    names = []
    raw = []
    for tag in item.get("projectTags") or []:
        if not isinstance(tag, dict):
            continue
        name = str(tag.get("name") or "").strip()
        if name:
            names.append(name)
        raw.append({
            "id": tag.get("id"),
            "name": name,
            "type": tag.get("type"),
        })
    return names, raw


def _matches_external_query(item, query):
    query = str(query or "").casefold().strip()
    if not query:
        return True
    model_data = item.get("model") or {}
    owner = item.get("owner") or {}
    tags = item.get("projectTags") or []
    fields = [
        item.get("name"), item.get("type"),
        model_data.get("name"), model_data.get("baseModel"), model_data.get("baseModelDisplayName"),
        owner.get("nickname"),
    ]
    fields.extend(tag.get("name") for tag in tags if isinstance(tag, dict))
    haystack = " ".join(str(v or "") for v in fields).casefold()
    return query in haystack


def _is_reprint_item(item):
    """Best-effort detection of TensorHub projects surfaced as Reprint.

    Reprints are publicly viewable but are not consistently represented in the
    portal discovery lanes. Once we observe a creator publishing reprints we
    can cheaply inspect that creator's newest catalog page for parity.
    """
    if not isinstance(item, dict):
        return False
    nested = item.get("model") if isinstance(item.get("model"), dict) else {}
    values = [
        item.get("status"), item.get("origin"), item.get("sourceType"),
        item.get("projectType"), item.get("publishType"),
        nested.get("status"), nested.get("origin"), nested.get("sourceType"),
    ]
    if item.get("reprint") is True or nested.get("reprint") is True:
        return True
    for value in values:
        normalized = str(value or "").strip().replace("-", "_").replace(" ", "_").upper()
        if normalized in {"REPRINT", "REPRINTED", "RE_POST", "REPOST"}:
            return True
    for tag in item.get("projectTags") or []:
        if isinstance(tag, dict) and str(tag.get("name") or "").strip().casefold() == "reprint":
            return True
    # TensorHub frequently stores the original-source URL only on reprints.
    text = " ".join(str(item.get(key) or "") for key in ("description", "sourceUrl", "originalUrl"))
    return "civitai.com/" in text.casefold() or "civitai.red/" in text.casefold()


def _version_direct_value(obj, names):
    if not isinstance(obj, dict):
        return None
    for name in names:
        value = obj.get(name)
        if value not in (None, "", [], {}):
            return value
    return None


def _tensorhub_meaningful_version_name(raw_name, project_name=""):
    """Return a human-useful TensorHub nested-version label when one exists.

    TensorHub may provide meaningful names for sibling versions, while other
    projects use generated timestamps or generic labels. Preserve descriptive
    version names and use epoch metadata when the source name is not useful.
    """
    raw = re.sub(r"\s+", " ", str(raw_name or "")).strip(" /\\|:-")
    if len(raw) < 3:
        return ""

    normalized = re.sub(r"[^a-z0-9]+", "", raw.casefold())
    project_normalized = re.sub(r"[^a-z0-9]+", "", str(project_name or "").casefold())

    # A nested model that merely repeats the project title does not add useful
    # version identity; epoch is clearer in that case.
    if project_normalized and normalized == project_normalized:
        return ""

    # TensorHub frequently uses timestamps as raw nested model names. Those were
    # the reason yula's versions were intentionally changed to Epoch 3/Epoch 5.
    if re.fullmatch(
        r"\d{4}[-/]\d{1,2}[-/]\d{1,2}(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?",
        raw,
        re.I,
    ):
        return ""

    # TensorHub's own UI uses explicit V1/V2/V3 labels as real sibling
    # version identities. Preserve those; generic training labels still fall
    # back to epoch where it is more useful.
    if re.fullmatch(r"v\d+(?:\.\d+)*", raw, re.I):
        return raw
    if re.fullmatch(
        r"(?:model|version|ver|epoch|checkpoint|lora)[ _-]*\d*(?:\.\d+)*",
        raw,
        re.I,
    ):
        return ""

    return raw


def _tensorhub_version_summary(
    detail,
    fallback_id="",
    fallback_access="unconfirmed",
    project_name="",
):
    """Extract one TensorHub nested model/version without confusing file/image IDs."""
    if not isinstance(detail, dict):
        return None
    model_obj = detail.get("model") if isinstance(detail.get("model"), dict) else detail
    if not isinstance(model_obj, dict):
        return None
    version_id = str(model_obj.get("id") or fallback_id or "").strip()
    if not version_id:
        return None

    base_model = str(_version_direct_value(model_obj, {"baseModelDisplayName", "baseModel"}) or "").strip()
    epoch = _version_direct_value(model_obj, {"epoch", "epochs", "trainEpoch", "trainingEpoch"})
    if epoch in (None, ""):
        epoch = _first_deep_value(model_obj, {"epoch", "epochs", "trainEpoch", "trainingEpoch"})
    steps = _version_direct_value(model_obj, {"steps", "step", "trainSteps", "trainingSteps"})
    if steps in (None, ""):
        steps = _first_deep_value(model_obj, {"steps", "step", "trainSteps", "trainingSteps"})
    triggers = _version_direct_value(model_obj, {"triggerWords", "triggerWord", "trainedWords", "trigger_words"})
    if triggers in (None, ""):
        triggers = _first_deep_value(model_obj, {"triggerWords", "triggerWord", "trainedWords", "trigger_words"})
    if isinstance(triggers, str):
        triggers = [part.strip() for part in re.split(r"[,\\n]", triggers) if part.strip()]
    elif not isinstance(triggers, list):
        triggers = []

    uploaded = _version_direct_value(model_obj, {"uploadedAt", "uploadAt", "createdAt", "createAt"})
    uploaded_iso = _epoch_to_iso(uploaded) if uploaded not in (None, "") else ""
    if not uploaded_iso:
        uploaded_iso = _detail_timestamp(model_obj, {"uploadedAt", "uploadAt", "createdAt", "createAt"})

    raw_name = str(model_obj.get("name") or "").strip()
    source_version_name = _tensorhub_meaningful_version_name(
        raw_name,
        project_name,
    )

    # Prefer a real source-side version identity. Epoch remains the best label
    # for training-output projects whose raw version name is only a timestamp or
    # generic placeholder.
    if source_version_name:
        name = source_version_name
    elif epoch not in (None, ""):
        name = f"Epoch {epoch}"
    elif uploaded_iso:
        try:
            parsed = datetime.fromisoformat(uploaded_iso.replace("Z", "+00:00"))
            name = parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            name = uploaded_iso
    else:
        name = raw_name or f"Version {version_id}"

    return {
        "id": version_id,
        "name": name,
        "source_name": raw_name,
        "uploaded_at": uploaded_iso,
        "base_model": base_model,
        "epoch": epoch if epoch not in (None, "") else "",
        "steps": steps if steps not in (None, "") else "",
        "trigger_words": triggers,
        "access_status": fallback_access,
        "can_download": fallback_access == "downloadable",
    }


def _tensorhub_project_version_ids(detail, current_id=""):
    """Collect sibling nested model IDs explicitly attached to the project."""
    if not isinstance(detail, dict):
        return [str(current_id)] if str(current_id or "").strip() else []
    project = detail.get("project") if isinstance(detail.get("project"), dict) else {}
    ids = []
    def add(value):
        if isinstance(value, dict):
            value = value.get("id") or value.get("modelId")
        value = str(value or "").strip()
        if value and value not in ids:
            ids.append(value)
    add(current_id)
    for container in (project, detail):
        if not isinstance(container, dict):
            continue
        for key in (
            "relatedModels",
            "modelIds", "modelIdList", "models", "modelList", "modelVersions", "versions",
        ):
            value = container.get(key)
            if isinstance(value, list):
                for child in value:
                    add(child)
    return ids[:20]


def _fetch_tensorhub_sibling_details(detail, current_id):
    """Fetch sibling nested models only when the project explicitly lists them."""
    ids = _tensorhub_project_version_ids(detail, current_id)
    result = []
    for version_id in ids:
        if str(version_id) == str(current_id):
            result.append(detail)
            continue
        if scan_control.should_stop():
            break
        try:
            response = _get_with_backoff(
                DETAIL_API,
                {"modelId": version_id},
                f"sibling version {version_id}",
                max_retries=1,
            )
            if response.status_code != 200:
                continue
            payload = response.json() or {}
            sibling = payload.get("data") if str(payload.get("code", "0")) in {"", "0"} else None
            if isinstance(sibling, dict):
                result.append(sibling)
        except Exception:
            continue
    if not result:
        result=[detail]
    return result


def _attach_tensorhub_versions(model, version_details, access_status):
    """Persist TensorHub sibling versions and bind each artifact to its version."""
    summaries=[]
    files=[]
    seen_files=set()
    media=[]
    seen_media=set()
    for detail in version_details or []:
        detail_model=detail.get("model") if isinstance(detail.get("model"),dict) else detail
        version_id=str((detail_model or {}).get("id") or "").strip()
        summary=_tensorhub_version_summary(
            detail,
            version_id,
            access_status,
            str(getattr(model, "display_name", "") or getattr(model, "name", "") or ""),
        )
        if not summary:
            continue
        version_name=summary["name"]
        summaries.append(summary)
        for file_data in _detail_files(detail):
            fid=str(file_data.get("model_file_id") or "").strip()
            key=fid or str(file_data.get("name") or file_data.get("path") or "")
            if not key or key in seen_files:
                continue
            seen_files.add(key)
            file_data=dict(file_data)
            file_data["version_id"]=version_id
            file_data["version"]=version_name
            files.append(file_data)
        for media_data in _detail_media(detail, str(model.model_key or ""), version_id, "detail-version"):
            key=str(media_data.get("url") or "").strip()
            if not key or key in seen_media:
                continue
            seen_media.add(key)
            media_data=dict(media_data)
            meta=dict(media_data.get("metadata") or {})
            meta["tensorhub_version_id"]=version_id
            meta["tensorhub_version_name"]=version_name
            meta["model_version_id"]=version_id
            meta["model_version"]=version_name
            media_data["metadata"]=meta
            media.append(media_data)

    # Timestamp tabs can legitimately collide (TensorHub can publish multiple
    # epochs from the same training job at the same second). Disambiguate only
    # the duplicates so the user can tell Epoch 3 from Epoch 5.
    counts={}
    for summary in summaries:
        counts[summary["name"]]=counts.get(summary["name"],0)+1
    for summary in summaries:
        if counts.get(summary["name"], 0) <= 1:
            continue

        old = summary["name"]
        epoch_value = summary.get("epoch")
        source_name = _tensorhub_meaningful_version_name(
            summary.get("source_name"),
            str(getattr(model, "display_name", "") or getattr(model, "name", "") or ""),
        )

        if source_name and source_name.casefold() != old.casefold():
            new = source_name
        elif (
            epoch_value not in (None, "")
            and f"epoch {epoch_value}".casefold() not in old.casefold()
        ):
            new = f"{old} · Epoch {epoch_value}"
        else:
            # Rare true collision: preserve clarity without pretending two
            # identical Epoch labels are different metadata.
            short_id = str(summary.get("id") or "")[-6:]
            new = f"{old} · {short_id}" if short_id else old

        summary["name"] = new
        for file_data in files:
            if str(file_data.get("version_id") or "") == str(summary.get("id") or ""):
                file_data["version"] = new

    # Newest first, matching TensorHub's version selector.
    summaries.sort(key=lambda v: str(v.get("uploaded_at") or ""), reverse=True)
    model.card_data["versions"]=summaries
    th=(model.card_data or {}).setdefault("tensorhub",{})
    th["versions"]=summaries
    th["version_count"]=len(summaries)
    return files, media, summaries


def _stable_tensorhub_listing_value(value):
    """Normalize listing fragments that may contain rotating signed media URLs."""
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            key_text = str(key or "")
            if key_text.casefold() in {
                "expires", "expire", "expiration", "signature", "sig",
                "token", "auth", "authorization", "timestamp", "ts",
            }:
                continue
            result[key_text] = _stable_tensorhub_listing_value(child)
        return result
    if isinstance(value, list):
        return [_stable_tensorhub_listing_value(child) for child in value]
    if isinstance(value, str):
        text = value.strip()
        if text.startswith(("http://", "https://")):
            try:
                from urllib.parse import urlsplit, urlunsplit
                parts = urlsplit(text)
                return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
            except Exception:
                return text.split("?", 1)[0]
        return text
    return value


def _tensorhub_file_identity(files):
    """Stable TensorHub model-file IDs used to reuse definitive access probes."""
    identities = []
    for file_data in files or []:
        if not isinstance(file_data, dict):
            continue
        file_id = str(
            file_data.get("model_file_id")
            or file_data.get("file_id")
            or file_data.get("id")
            or ""
        ).strip()
        if file_id:
            identities.append(f"id:{file_id}")
            continue
        path = str(file_data.get("path") or file_data.get("name") or "").strip()
        if path:
            identities.append(f"path:{path.casefold()}")
    return tuple(sorted(set(identities)))


def _tensorhub_metadata_hash(item):
    """Stable TensorHub metadata hash excluding volatile popularity counters."""
    nested = item.get("model") or {}
    owner = item.get("owner") or {}
    stable = {
        "project_id": item.get("id"),
        "project_name": item.get("name"),
        "project_type": item.get("type"),
        "owner_id": owner.get("id"),
        "owner_nickname": owner.get("nickname"),
        "tags": [
            (tag.get("id"), tag.get("name"), tag.get("type"))
            for tag in (item.get("projectTags") or [])
            if isinstance(tag, dict)
        ],
        "model_id": nested.get("id"),
        "model_name": nested.get("name"),
        "base_model": nested.get("baseModel"),
        "base_model_display": nested.get("baseModelDisplayName"),
        "cover": _stable_tensorhub_listing_value(nested.get("cover")),
        "cover_showcase": _stable_tensorhub_listing_value(nested.get("coverShowcase")),
        "nested_flags": nested.get("flags") or {},
        "project_flags": item.get("flags") or {},
        "status": item.get("status"),
        "modality": item.get("modality"),
        "vip_only_info": nested.get("viponlyInfo"),
        "sponsor_info": _stable_tensorhub_listing_value(item.get("sponsorInfo") or {}),
    }
    payload = json.dumps(
        stable, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _json_value(value, default=None):
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return default


def _existing_tensorhub_state():
    """Load TensorHub cache once so detail enrichment never erases rich data."""
    try:
        import database
        conn = database.connect()
        rows = conn.execute(
            """
            SELECT *
            FROM models
            WHERE source=?
            """,
            (NAME,),
        ).fetchall()
        media_rows = conn.execute(
            """
            SELECT mm.*
            FROM model_media mm
            JOIN models m ON m.id=mm.model_id
            WHERE m.source=?
            ORDER BY mm.model_id, mm.position, mm.id
            """,
            (NAME,),
        ).fetchall()
        conn.close()
    except Exception as exc:
        print(f"TensorHub detail cache unavailable: {type(exc).__name__}")
        return {}

    media_by_id = {}
    for row in media_rows:
        item = dict(row)
        raw_meta = item.get("metadata") or ""
        if isinstance(raw_meta, str):
            try:
                item["metadata"] = json.loads(raw_meta) if raw_meta else {}
            except Exception:
                item["metadata"] = {}
        media_by_id.setdefault(item.get("model_id"), []).append(item)

    result = {}
    for row in rows:
        item = dict(row)
        item["files_obj"] = _json_value(item.get("files"), []) or []
        item["card_obj"] = _json_value(item.get("card_data"), {}) or {}
        item["media_obj"] = media_by_id.get(item.get("id"), [])
        result[str(item.get("model_key") or "")] = item
    return result


def _extract_nuxt_array(page_html):
    match = re.search(
        r'<script[^>]+id=["\\\']__NUXT_DATA__["\\\'][^>]*>(.*?)</script>',
        page_html or "",
        flags=re.I | re.S,
    )
    if not match:
        return []
    raw = html_lib.unescape(match.group(1).strip())
    try:
        data = json.loads(raw)
    except Exception:
        return []
    return data if isinstance(data, list) else []


def _devalue_resolver(data):
    """Small decoder for the index-referenced Nuxt/devalue payload graph."""
    memo = {}
    resolving = set()

    def resolve_index(index):
        if type(index) is not int or index < 0 or index >= len(data):
            return None
        if index in memo:
            return memo[index]
        if index in resolving:
            return None
        resolving.add(index)
        node = data[index]
        if isinstance(node, dict):
            out = {}
            memo[index] = out
            for key, value in node.items():
                out[key] = resolve_ref(value)
        elif isinstance(node, list):
            if node and isinstance(node[0], str) and node[0] in {"Reactive", "ShallowReactive", "Ref", "EmptyRef"}:
                out = resolve_ref(node[1]) if len(node) > 1 else None
                memo[index] = out
            elif node and isinstance(node[0], str) and node[0] == "Set":
                out = [resolve_ref(value) for value in node[1:]]
                memo[index] = out
            elif node and isinstance(node[0], str) and node[0] == "Map":
                pairs = [resolve_ref(value) for value in node[1:]]
                out = {str(pairs[i]): pairs[i + 1] for i in range(0, len(pairs) - 1, 2)}
                memo[index] = out
            else:
                out = [resolve_ref(value) for value in node]
                memo[index] = out
        else:
            out = node
            memo[index] = out
        resolving.discard(index)
        return out

    def resolve_ref(value):
        if type(value) is int:
            if value < 0:
                return None
            return resolve_index(value)
        if isinstance(value, dict):
            return {key: resolve_ref(child) for key, child in value.items()}
        if isinstance(value, list):
            return [resolve_ref(child) for child in value]
        return value

    return resolve_index


def _walk_dicts(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def _detail_model_from_nuxt(data, route_model_id, project_id):
    if not data:
        return None
    resolve_index = _devalue_resolver(data)
    best = None
    best_score = -1
    for index, raw in enumerate(data):
        if not isinstance(raw, dict):
            continue
        keys = set(raw)
        if not ({"modelFileIds", "projectId", "baseModel", "flags", "cover", "coverShowcases"} & keys):
            continue
        try:
            obj = resolve_index(index)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        score = 0
        if str(obj.get("id") or "") == str(route_model_id or ""):
            score += 8
        if str(obj.get("projectId") or "") == str(project_id or ""):
            score += 7
        if "modelFileIds" in obj:
            score += 4
        if "baseModel" in obj:
            score += 2
        if "flags" in obj:
            score += 2
        if "cover" in obj or "coverShowcases" in obj:
            score += 2
        if score > best_score:
            best, best_score = obj, score
    return best if best_score >= 5 else None


def _format_bytes(value):
    try:
        size = int(value or 0)
    except (TypeError, ValueError):
        return ""
    if size <= 0:
        return ""
    units = ["B", "KB", "MB", "GB", "TB"]
    number = float(size)
    unit = 0
    while number >= 1024 and unit < len(units) - 1:
        number /= 1024.0
        unit += 1
    decimals = 0 if unit == 0 or number >= 100 else (1 if number >= 10 else 2)
    return f"{number:.{decimals}f} {units[unit]}"


def _hash_from_file(file_obj):
    hashes = file_obj.get("hash") or file_obj.get("hashes") or []
    if isinstance(hashes, dict):
        hashes = [hashes]
    if isinstance(hashes, str):
        return hashes, ""
    if not isinstance(hashes, list):
        return "", ""
    fallback = ("", "")
    for entry in hashes:
        if not isinstance(entry, dict):
            continue
        value = str(entry.get("hash") or entry.get("value") or "").strip()
        kind = str(entry.get("hashType") or entry.get("type") or "").strip()
        if value and not fallback[0]:
            fallback = (value, kind)
        if value and kind.upper() == "SHA256":
            return value, kind
    return fallback


def _detail_files(detail):
    files = []
    seen = set()
    for obj in _walk_dicts(detail):
        name = str(obj.get("name") or obj.get("filename") or "").strip()
        file_id = str(obj.get("id") or obj.get("modelFileId") or "").strip()
        file_type = str(obj.get("fileType") or obj.get("type") or "").strip()
        lower = name.lower()
        looks_file = bool(name and (lower.endswith((".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf")) or file_type in {"SAFE_TENSOR", "CKPT", "GGUF"}))
        if not looks_file or not file_id or file_id in seen:
            continue
        seen.add(file_id)
        sha, hash_type = _hash_from_file(obj)
        try:
            size_bytes = int(obj.get("size") or 0)
        except (TypeError, ValueError):
            size_bytes = 0
        model_size = str(obj.get("modelSize") or "").strip()
        files.append({
            "name": name,
            "path": name,
            "model_file_id": file_id,
            "size_bytes": size_bytes,
            "size_label": _format_bytes(size_bytes) or model_size,
            "sha256": sha if hash_type.upper() == "SHA256" or len(sha) == 64 else "",
            "hash": sha,
            "hash_type": hash_type,
            "file_type": file_type,
            "floating_point": obj.get("floatingPoint") or "",
            "model_size": obj.get("modelSize") or "",
            "primary": len(files) == 0,
        })
    return files


def _first_deep_value(value, names):
    names = set(names)
    for obj in _walk_dicts(value):
        for key, child in obj.items():
            if key in names and child not in (None, "", [], {}):
                return child
    return None


def _generation_metadata(generation):
    if not isinstance(generation, (dict, list)):
        return {}
    aliases = {
        "prompt": {"prompt", "positivePrompt", "positive_prompt", "displayPrompt"},
        "negative_prompt": {"negativePrompt", "negative_prompt"},
        "seed": {"seed"},
        "steps": {"steps", "step"},
        "cfg": {"cfg", "cfgScale", "guidanceScale", "guidance"},
        "sampler": {"sampler", "samplerName"},
        "scheduler": {"scheduler"},
        "width": {"width"},
        "height": {"height"},
        "model": {"modelFileName", "checkpoint"},
        "base_model": {"baseModel"},
        "model_file_id": {"modelFileId"},
        "model_id": {"modelId"},
        "weight": {"weight"},
        "clip_skip": {"clipSkip"},
        "strength": {"strength"},
    }
    result = {}
    for canonical, keys in aliases.items():
        value = _first_deep_value(generation, keys)
        if value not in (None, "", [], {}):
            result[canonical] = value
    gen_type = _first_deep_value(generation, {"type", "generateTaskType"})
    if gen_type not in (None, ""):
        result["generation_type"] = gen_type
    return result


def _detail_media(detail, project_id, route_model_id, discovery_lane):
    media = []
    seen = set()
    for obj in _walk_dicts(detail):
        url = str(obj.get("url") or "").strip()
        if not url or "model_showcase" not in url or url in seen:
            continue
        seen.add(url)
        snapshot = str(obj.get("snapshot") or "").strip()
        clean = url.lower().split("?", 1)[0]
        media_type = "video" if clean.endswith((".mp4", ".webm", ".mov")) else "image"
        filename = url.rsplit("/", 1)[-1].split("?", 1)[0] or f"tensorhub-preview-{len(media)+1}"
        image_id = str(obj.get("id") or obj.get("imageId") or "").strip()
        metadata = {
            "content_rating": obj.get("contentRating") or "",
            "project_id": project_id,
            "model_id": route_model_id,
            "image_id": image_id,
            "discovery_lane": discovery_lane,
        }
        created_at = obj.get("createdAt")
        if created_at not in (None, "", "0", 0):
            metadata["created_at"] = created_at
        generation = obj.get("generationData")
        metadata.update(_generation_metadata(generation))
        media.append({
            "type": media_type,
            "url": url,
            "thumbnail": snapshot if media_type == "video" else "",
            "filename": filename,
            "path": f"showcase/{len(media)+1}",
            "metadata": metadata,
            "position": len(media),
        })
    return media


def _tensorhub_paid_access_signal(*objects):
    """Return True only for explicit TensorHub paid/member download metadata.

    Prefer entitlement data from model/detail over presentation tags. TensorHub
    currently exposes paid local downloads as sponsorInfo.projectDownload and/or
    sponsorVersion with sponsorPlanType=PROJECT_DOWNLOAD. VIP metadata only counts
    when the actual boolean says the model is VIP-only. EXCLUSIVE remains a
    conservative fallback and is only used later to upgrade a confirmed gated
    probe; it never overrides a successful download probe.
    """
    def normalized(value):
        return str(value or "").strip().replace("-", "_").replace(" ", "_").upper()

    def is_exclusive(value):
        return normalized(value) == "EXCLUSIVE"

    def is_paid_download_plan(value):
        if not isinstance(value, dict):
            return False
        plan_type = normalized(value.get("sponsorPlanType") or value.get("planType") or value.get("type"))
        amount = value.get("amount")
        try:
            amount_value = float(amount or 0)
        except (TypeError, ValueError):
            amount_value = 0.0
        # PROJECT_DOWNLOAD itself is authoritative; amount > 0 is an extra
        # sanity signal but some account/API shapes may omit it.
        return plan_type == "PROJECT_DOWNLOAD" and (amount_value > 0 or amount in (None, ""))

    exclusive_seen = False

    for root in objects:
        if not isinstance(root, dict):
            continue

        for tags_key in ("project_tags", "projectTags", "tags", "relatedTags"):
            tags = root.get(tags_key)
            if isinstance(tags, list):
                for tag in tags:
                    if isinstance(tag, dict):
                        if is_exclusive(tag.get("name") or tag.get("label") or tag.get("value")):
                            exclusive_seen = True
                    elif is_exclusive(tag):
                        exclusive_seen = True

        for obj in _walk_dicts(root):
            # viponlyInfo is commonly present even when isViponly=false, so the
            # container object itself is NOT a paid signal.
            vip = obj.get("viponlyInfo")
            if not isinstance(vip, dict):
                vip = obj.get("vipOnlyInfo")
            if isinstance(vip, dict) and vip.get("isViponly") is True:
                return True

            if obj.get("vipOnly") is True or obj.get("subscriberOnly") is True:
                return True

            flags = obj.get("flags")
            if isinstance(flags, dict) and (flags.get("vipOnly") is True or flags.get("subscriberOnly") is True):
                return True

            sponsor = obj.get("sponsorInfo")
            if isinstance(sponsor, dict):
                project_download = sponsor.get("projectDownload")
                if isinstance(project_download, dict):
                    # A concrete projectDownload object means TensorHub has a
                    # purchase/unlock plan for local model download.
                    if is_paid_download_plan(project_download) or project_download.get("amount") not in (None, "", 0, "0"):
                        return True

            sponsor_version = obj.get("sponsorVersion")
            if isinstance(sponsor_version, dict) and is_paid_download_plan(sponsor_version):
                return True

            # When walking detail['data'], sponsorVersion itself appears as an
            # object rather than under a sponsorVersion key.
            if is_paid_download_plan(obj):
                return True

            for key in ("projectType", "publishType", "accessType", "downloadType"):
                if is_exclusive(obj.get(key)):
                    exclusive_seen = True

    return exclusive_seen


def _detail_access(detail):
    """Read authoritative per-version download flags from model detail state."""
    flag_objects = []
    for obj in _walk_dicts(detail):
        flags = obj.get("flags")
        if isinstance(flags, dict):
            flag_objects.append(flags)
    # The model/version's own flags are normally on the selected detail object.
    if isinstance(detail.get("flags"), dict):
        flag_objects.insert(0, detail.get("flags"))

    for flags in flag_objects:
        if flags.get("freeDownload") is True or flags.get("allowDownload") is True:
            return "downloadable", flags
    for flags in flag_objects:
        if flags.get("forbidDownload") is True:
            return "non_downloadable", flags
    for flags in flag_objects:
        if flags.get("grantDownload") is True:
            return "restricted", flags
    return "unknown", (flag_objects[0] if flag_objects else {})



def _response_data(response, label):
    """Return TensorHub's `data` object or a short failure reason."""
    if response.status_code != 200:
        return None, f"HTTP {response.status_code}"
    try:
        payload = response.json() or {}
    except Exception as exc:
        return None, f"invalid JSON: {type(exc).__name__}"
    if str(payload.get("code", "0")) not in {"", "0"}:
        return None, f"API code {payload.get('code')}: {payload.get('message') or ''}".strip()
    data = payload.get("data")
    if data is None:
        return None, f"{label} data missing"
    return data, ""


def _image_metadata_is_rich(metadata):
    """The model-detail response sometimes already includes complete generation data."""
    if not isinstance(metadata, dict):
        return False
    useful = ("prompt", "negative_prompt", "seed", "sampler", "steps", "cfg")
    # A prompt is the best signal; otherwise require at least two useful generation fields.
    if str(metadata.get("prompt") or "").strip():
        return True
    count = sum(1 for key in useful if metadata.get(key) not in (None, "", [], {}))
    return count >= 2


def _fetch_image_detail(image_id, model_id):
    if not image_id or not model_id:
        return None, "missing image/model id"
    try:
        response = _get_with_backoff(
            IMAGE_DETAIL_API,
            {"id": image_id, "modelId": model_id},
            f"image detail {image_id}",
        )
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"

    data, reason = _response_data(response, "image detail")
    if reason:
        return None, reason

    # TensorHub's frontend reads response.data.data.image.
    if isinstance(data, dict) and isinstance(data.get("image"), dict):
        return data.get("image"), ""
    if isinstance(data, dict):
        return data, ""
    return None, "image detail object missing"


def _enrich_media_details(media, route_model_id):
    """Fetch sparse image detail and count actual rich generation metadata."""
    if not media:
        return media, 0, 0, 0

    fetched = 0
    failed = 0
    result = []

    for item in media:
        item = dict(item)
        metadata = dict(item.get("metadata") or {})
        image_id = str(metadata.get("image_id") or "").strip()

        if item.get("type") != "image" or not image_id:
            item["metadata"] = metadata
            result.append(item)
            continue

        if not _image_metadata_is_rich(metadata):
            detail, reason = _fetch_image_detail(image_id, route_model_id)
            if isinstance(detail, dict):
                fetched += 1
                metadata.update(_generation_metadata(detail))
                for source_key, target_key in (("contentRating","content_rating"),("createdAt","created_at"),("width","width"),("height","height")):
                    value = detail.get(source_key)
                    if value not in (None, "", [], {}): metadata[target_key] = value
                metadata["image_detail_fetched"] = True
                metadata.pop("image_detail_error", None)
            else:
                metadata["image_detail_fetched"] = False
                metadata["image_detail_error"] = str(reason or "unknown")[:160]
                failed += 1

        metadata["rich_generation_metadata"] = _image_metadata_is_rich(metadata)
        item["metadata"] = metadata
        result.append(item)

    rich_count = sum(1 for item in result if item.get("type") == "image" and _image_metadata_is_rich(item.get("metadata") or {}))
    return result, fetched, failed, rich_count


def _fetch_file_detail(model_file_id):
    """Fetch TensorHub file detail using the file id expected by the API."""
    if not model_file_id:
        return None, "missing model file id"
    try:
        response = _get_with_backoff(
            FILE_DETAIL_API,
            {"id": model_file_id},
            f"file detail {model_file_id}",
        )
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"

    data, reason = _response_data(response, "file detail")
    if reason:
        return None, reason
    if isinstance(data, dict):
        return data, ""
    return None, "file detail object missing"


def _merge_file_detail(file_record, detail):
    merged = dict(file_record or {})
    if not isinstance(detail, dict):
        return merged

    # Find the object that actually represents this file. TensorHub may wrap it.
    target_id = str(merged.get("model_file_id") or "").strip()
    candidates = []
    for obj in _walk_dicts(detail):
        obj_id = str(obj.get("id") or obj.get("modelFileId") or "").strip()
        name = str(obj.get("name") or obj.get("filename") or "").strip()
        if (target_id and obj_id == target_id) or (
            name and name == str(merged.get("name") or "")
        ):
            candidates.append(obj)
    source = candidates[0] if candidates else detail

    name = str(source.get("name") or source.get("filename") or "").strip()
    if name:
        merged["name"] = name
        merged["path"] = name

    try:
        size_bytes = int(source.get("size") or merged.get("size_bytes") or 0)
    except (TypeError, ValueError):
        size_bytes = int(merged.get("size_bytes") or 0)
    merged["size_bytes"] = size_bytes
    model_size_label = str(source.get("modelSize") or merged.get("model_size") or "").strip()
    merged["size_label"] = _format_bytes(size_bytes) or model_size_label

    sha, hash_type = _hash_from_file(source)
    if sha:
        merged["hash"] = sha
        merged["hash_type"] = hash_type
        if hash_type.upper() == "SHA256" or len(sha) == 64:
            merged["sha256"] = sha

    merged["file_type"] = source.get("fileType") or merged.get("file_type") or ""
    merged["floating_point"] = source.get("floatingPoint") or merged.get("floating_point") or ""
    merged["model_size"] = source.get("modelSize") or merged.get("model_size") or ""

    # Authenticated file-detail responses may include a direct/signed URL.
    # Keep only explicit HTTP(S) URLs; otherwise AbyssBeacon falls back to the
    # Tensor.Art model page and lets the browser complete sign-in/download.
    direct_url = str(
        source.get("downloadUrl")
        or source.get("downloadURL")
        or source.get("signedUrl")
        or source.get("fileUrl")
        or source.get("fileURL")
        or ""
    ).strip()
    if direct_url.startswith(("https://", "http://")):
        merged["download_url"] = direct_url

    merged["detail_enriched"] = True
    return merged


def _enrich_files(files):
    """Fetch file-detail objects and return both merged files and raw detail objects."""
    merged_files = []
    raw_details = []
    failures = 0

    for file_record in files or []:
        file_id = str(file_record.get("model_file_id") or "").strip()
        detail, reason = _fetch_file_detail(file_id)
        if isinstance(detail, dict):
            merged_files.append(_merge_file_detail(file_record, detail))
            raw_details.append(detail)
        else:
            fallback = dict(file_record)
            fallback["detail_enriched"] = False
            fallback["detail_error"] = str(reason or "unknown")[:160]
            merged_files.append(fallback)
            failures += 1

    return merged_files, raw_details, failures


def _normalize_access_label(access):
    access = str(access or "unknown").strip().lower()
    if access == "unknown":
        return "unconfirmed"
    return access


def _probe_download_access(files, referer=""):
    """Confirm TensorHub download access without saving a file.

    The signed-URL request is the authoritative gate. If TensorHub issues a URL,
    request only byte 0 from it and close the streamed response immediately.
    Nothing is written to disk or download history.
    """
    token = get_source_token("tensorhub")
    if not token:
        return "unconfirmed", {
            "reason": "no saved TensorHub session",
            "signed_url_http": None,
            "probe_http": None,
        }

    candidate = next(
        (
            item for item in (files or [])
            if isinstance(item, dict) and str(item.get("model_file_id") or "").strip()
        ),
        None,
    )
    if not candidate:
        return "unconfirmed", {
            "reason": "no TensorHub downloadable model file ID available",
            "signed_url_http": None,
            "probe_http": None,
        }

    file_id = str(candidate.get("model_file_id") or "").strip()
    headers = {
        "Authorization": f"Bearer {token}",
        "Cookie": f"ta_token_prod={token}",
        "Accept": "*/*",
        "Origin": SITE,
        "Referer": str(referer or f"{SITE}/models").strip() or f"{SITE}/models",
        "X-Request-Package-Id": "3023",
        "X-Request-Lang": "en-US",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:153.0) Gecko/20100101 Firefox/153.0",
    }

    try:
        response = requests.get(
            "https://api.tensorhub.art/community-web/v1/model/file/url",
            params={"modelFileId": file_id, "useTcdn": "true"},
            headers=headers,
            timeout=15,
        )
    except Exception as exc:
        return "unconfirmed", {
            "reason": f"signed URL request failed: {type(exc).__name__}",
            "signed_url_http": None,
            "probe_http": None,
            "model_file_id": file_id,
        }

    signed_http = response.status_code
    if signed_http in (401, 403):
        return "unconfirmed", {
            "reason": f"TensorHub session rejected (HTTP {signed_http})",
            "signed_url_http": signed_http,
            "probe_http": None,
            "model_file_id": file_id,
        }
    if signed_http != 200:
        return "unconfirmed", {
            "reason": f"signed URL HTTP {signed_http}",
            "signed_url_http": signed_http,
            "probe_http": None,
            "model_file_id": file_id,
        }

    try:
        payload = response.json() if response.content else {}
    except Exception:
        return "unconfirmed", {
            "reason": "invalid signed URL response",
            "signed_url_http": signed_http,
            "probe_http": None,
            "model_file_id": file_id,
        }

    data = payload.get("data") if isinstance(payload, dict) else {}
    signed_url = str((data or {}).get("url") or "").strip() if isinstance(data, dict) else ""
    api_code = str(payload.get("code", "")).strip() if isinstance(payload, dict) else ""
    api_message = str(payload.get("message", "")).strip() if isinstance(payload, dict) else ""

    # A successful signed-URL API response with no URL is TensorHub's normal
    # "you cannot download this file" outcome. This is definitive restriction,
    # not an ambiguous metadata state.
    if not signed_url.startswith(("https://", "http://")):
        reason = " / ".join(
            value for value in (
                f"code {api_code}" if api_code else "",
                api_message,
            ) if value
        ) or "signed URL not issued"
        return "gated", {
            "reason": reason,
            "signed_url_http": signed_http,
            "probe_http": None,
            "model_file_id": file_id,
        }

    probe = None
    try:
        probe = requests.get(
            signed_url,
            headers={"Range": "bytes=0-0", "User-Agent": headers["User-Agent"]},
            stream=True,
            allow_redirects=True,
            timeout=15,
        )
        probe_http = probe.status_code
        if probe_http in (200, 206):
            return "downloadable", {
                "reason": "byte-range access confirmed",
                "signed_url_http": signed_http,
                "probe_http": probe_http,
                "model_file_id": file_id,
            }
        if probe_http in (401, 403):
            return "gated", {
                "reason": f"file probe HTTP {probe_http}",
                "signed_url_http": signed_http,
                "probe_http": probe_http,
                "model_file_id": file_id,
            }
        return "unconfirmed", {
            "reason": f"unexpected file probe HTTP {probe_http}",
            "signed_url_http": signed_http,
            "probe_http": probe_http,
            "model_file_id": file_id,
        }
    except Exception as exc:
        return "unconfirmed", {
            "reason": f"file probe failed: {type(exc).__name__}",
            "signed_url_http": signed_http,
            "probe_http": None,
            "model_file_id": file_id,
        }
    finally:
        if probe is not None:
            probe.close()


def _apply_access_state(model, access_status):
    """Apply TensorHub access state without polluting source/model tags."""
    access_status = _normalize_access_label(access_status)

    removable = {
        "DOWNLOADABLE",
        "GATED",
        "DOWNLOAD UNCONFIRMED",
        "ACCESS UNCONFIRMED",
    }
    display = [
        tag for tag in list(model.display_tags or [])
        if str(tag).upper() not in removable
    ]
    plain = [
        tag.strip() for tag in str(model.tags or "").split(",")
        if tag.strip() and str(tag).upper() not in removable
    ]
    # Structured display tags preserve multi-word TensorHub labels in older
    # cached rows whose plain tag string used spaces as a delimiter.
    if display:
        plain = list(display)

    model.gated = access_status in {
        "gated", "paid_access", "non_downloadable", "restricted", "paid", "buffet", "disabled"
    }
    model.display_tags = list(dict.fromkeys(display))[:12]
    model.tags = ",".join(dict.fromkeys(plain))
    return access_status


def _epoch_to_iso(value):
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        number = float(text)
    except (TypeError, ValueError):
        # Newer TensorHub detail responses can return ISO timestamps while
        # older responses use epoch seconds/milliseconds. Normalize both.
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).isoformat()
        except Exception:
            return ""
    if number <= 0:
        return ""
    if number > 10_000_000_000:
        number /= 1000.0
    try:
        return datetime.fromtimestamp(number, tz=timezone.utc).isoformat()
    except Exception:
        return ""


def _detail_timestamp(detail, names):
    value = _first_deep_value(detail, set(names))
    return _epoch_to_iso(value)


def _parse_iso_datetime(value):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def _listing_datetime(value):
    """Parse TensorHub listing timestamps without requiring model/detail."""
    if value in (None, "", 0, "0"):
        return None
    # TensorHub commonly uses epoch milliseconds, but tolerate seconds and ISO.
    try:
        number = float(value)
        if number > 0:
            if number > 10_000_000_000:
                number /= 1000.0
            return datetime.fromtimestamp(number, tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        pass
    return _parse_iso_datetime(value)


def _listing_source_dates(item):
    """Return canonical TensorHub model/project source dates.

    Only the public project record and its direct ``model`` object participate.
    Earlier builds walked every nested dictionary and accidentally picked up
    timestamps from covers, attachments, owner metadata, and other unrelated
    objects. That made stale projects look recent during the pre-retention guard
    even though cleanup later used the actual model dates.
    """
    if not isinstance(item, dict):
        return None, None, None

    model_obj = item.get("model") if isinstance(item.get("model"), dict) else {}
    scopes = (model_obj, item)
    created_keys = (
        "createdAt", "createAt", "uploadedAt", "uploadAt",
        "publishedAt", "publishAt", "created", "createTime",
    )
    updated_keys = (
        "updatedAt", "updateAt", "modifiedAt", "lastUpdateAt",
        "updated", "updateTime",
    )

    def collect(keys):
        values = []
        for obj in scopes:
            for key in keys:
                if key not in obj:
                    continue
                dt = _listing_datetime(obj.get(key))
                if dt:
                    values.append(dt)
        return max(values) if values else None

    created = collect(created_keys)
    updated = collect(updated_keys)
    activity_values = [dt for dt in (created, updated) if dt]
    activity = max(activity_values) if activity_values else None
    return created, updated, activity


def _outside_normal_retention(item, days):
    _created, _updated, activity = _listing_source_dates(item)
    if activity is None:
        # Never guess. If TensorHub omits a usable date, keep the model and let
        # normal detail/cleanup logic decide later.
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(0, int(days)))
    return activity < cutoff


def _retention_activity_probe(item):
    """Fetch only model/detail long enough to learn the authoritative source age.

    TensorHub's portal listing often omits the model's real created/updated date.
    This lightweight preflight deliberately stops before file/image enrichment.
    """
    if not isinstance(item, dict):
        return None
    nested = item.get("model") if isinstance(item.get("model"), dict) else {}
    route_model_id = str(nested.get("id") or "").strip()
    if not route_model_id:
        return None
    try:
        response = _get_with_backoff(
            DETAIL_API,
            {"modelId": route_model_id},
            f"retention preflight {route_model_id}",
        )
        if response.status_code != 200:
            return None
        payload = response.json() or {}
        if str(payload.get("code", "0")) not in {"", "0"}:
            return None
        detail = payload.get("data")
        if not isinstance(detail, dict):
            return None
        created = _parse_iso_datetime(_detail_timestamp(detail, {"createdAt", "createAt", "uploadedAt", "uploadAt"}))
        updated = _parse_iso_datetime(_detail_timestamp(detail, {"updatedAt", "updateAt", "modifiedAt"}))
        values = [value for value in (created, updated) if value]
        return max(values) if values else None
    except Exception:
        return None


def _detail_retry_due(old_th, cooldown_hours=12):
    """Return True when a previously failed detail request may be retried.

    A failed/unsupported model must not monopolize the front of the enrichment
    queue every scan. We move past it and retry later.
    """
    if not isinstance(old_th, dict):
        return True
    if old_th.get("detail_enriched"):
        return False

    state = str(old_th.get("detail_enrichment_state") or "").strip().lower()
    if state not in {"failed", "unavailable"}:
        return True

    attempted = _parse_iso_datetime(old_th.get("detail_attempted_at"))
    if attempted is None:
        return True

    elapsed = datetime.now(timezone.utc) - attempted
    return elapsed.total_seconds() >= max(1, int(cooldown_hours)) * 3600


def _mark_detail_attempt(model, ok, reason=""):
    th = (model.card_data or {}).setdefault("tensorhub", {})
    previous_attempts = 0
    try:
        previous_attempts = int(th.get("detail_attempts") or 0)
    except (TypeError, ValueError):
        previous_attempts = 0

    th["detail_attempts"] = previous_attempts + 1
    th["detail_attempted_at"] = datetime.now(timezone.utc).isoformat()

    if ok:
        th["detail_enrichment_state"] = "enriched"
        th["detail_last_error"] = ""
    else:
        th["detail_enrichment_state"] = "failed"
        th["detail_last_error"] = str(reason or "unknown")[:240]
    return model


def _detail_signature(model):
    th = (model.card_data or {}).get("tensorhub") or {}
    stable = {
        "listing_hash": th.get("listing_hash", ""),
        "download_access": th.get("download_access", "unknown"),
        "files": model.files or [],
        "sha": model.sha or "",
        "media": [
            {
                "url": item.get("url", ""),
                "type": item.get("type", ""),
                "thumbnail": item.get("thumbnail", ""),
                "metadata": item.get("metadata", {}),
            }
            for item in (model.media or [])
        ],
        "description": model.description or "",
        "created": model.created or "",
        "updated": model.updated or "",
    }
    payload = json.dumps(stable, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _apply_cached_detail(model, cached):
    if not cached:
        return model
    card = cached.get("card_obj") or {}
    old_th = card.get("tensorhub") or {}
    if not old_th.get("detail_enriched"):
        return model

    model.sha = str(cached.get("sha") or model.sha or "")
    model.files = cached.get("files_obj") or []
    model.description = str(cached.get("description") or model.description or "")
    model.created = str(cached.get("created") or model.created or "")
    model.updated = str(cached.get("updated") or model.updated or "")
    model.format = str(cached.get("format") or model.format or "")
    model.quantization = str(cached.get("quantization") or model.quantization or "")
    if cached.get("media_obj"):
        model.media = [
            {
                "type": item.get("type") or "image",
                "url": item.get("url") or "",
                "thumbnail": item.get("thumbnail") or "",
                "filename": item.get("filename") or "",
                "path": item.get("path") or "",
                "metadata": item.get("metadata") or {},
                "position": item.get("position") or 0,
            }
            for item in cached.get("media_obj")
        ]
        model.has_media = bool(model.media)
        model.has_video = any(item.get("type") == "video" for item in model.media)
        model.preview_count = sum(1 for item in model.media if item.get("type") == "image")
        first_image = next((item.get("url") for item in model.media if item.get("type") == "image" and item.get("url")), "")
        if first_image:
            model.image = first_image

    # Preserve sibling version metadata learned by detail enrichment. Listing
    # payloads expose only one selected nested model and would otherwise erase
    # the version selector on every later unchanged scan.
    cached_versions = card.get("versions") if isinstance(card, dict) else None
    if isinstance(cached_versions, list) and cached_versions:
        model.card_data["versions"] = copy.deepcopy(cached_versions)

    fresh_th = (model.card_data or {}).get("tensorhub") or {}
    merged_th = dict(old_th)
    merged_th.update(fresh_th)
    # Preserve authoritative detail fields over weaker listing access data.
    for key in ("detail_enriched", "detail_enriched_at", "detail_enrichment_version", "detail_enrichment_state", "detail_attempts", "detail_attempted_at", "detail_last_error", "download_access", "detail_flags", "model_files", "image_detail_fetched_count", "image_detail_enriched_count", "image_detail_failed_count", "file_detail_failed_count"):
        if key in old_th:
            merged_th[key] = old_th[key]
    model.card_data["tensorhub"] = merged_th
    access = _apply_access_state(model, merged_th.get("download_access") or "unconfirmed")
    model.metadata_hash = _detail_signature(model)
    return model


def _fetch_public_detail(model, discovery_lane="", force_access_probe=False):
    """Enrich one TensorHub model through TensorHub's public JSON model-detail API.

    TensorHub's generated web client maps `communityWebV1ServiceModelDetail`
    to GET /community-web/v1/model/detail?modelId=<nested model/version id>.
    This avoids the Cloudflare-protected HTML document route entirely.
    """
    debug_model_name = str(
        getattr(model, "display_name", "")
        or getattr(model, "name", "")
        or model.model_key
        or ""
    )
    th = (model.card_data or {}).setdefault("tensorhub", {})
    cached_access_status = _normalize_access_label(th.get("download_access") or "unconfirmed")
    cached_access_files = _tensorhub_file_identity(th.get("model_files") or model.files or [])
    project_id = str(model.model_key or th.get("project_id") or "").strip()
    route_model_id = str(
        th.get("route_model_id")
        or th.get("model_id")
        or ""
    ).strip()

    if not route_model_id:
        reason = "missing nested model id"
        _mark_detail_attempt(model, False, reason)
        print("\nTensorHub detail FAILED")
        print(f"  Model     : {debug_model_name}")
        print(f"  Project ID: {project_id}")
        print(f"  Reason    : {reason}")
        return model, False, reason

    try:
        response = _get_with_backoff(
            DETAIL_API,
            {"modelId": route_model_id},
            f"detail {route_model_id}",
        )
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
        _mark_detail_attempt(model, False, reason)
        print("\nTensorHub detail FAILED")
        print(f"  Model     : {debug_model_name}")
        print(f"  Project ID: {project_id}")
        print(f"  Model ID  : {route_model_id}")
        print(f"  Reason    : {reason}")
        return model, False, reason

    if response.status_code != 200:
        reason = f"HTTP {response.status_code}"
        _mark_detail_attempt(model, False, reason)
        print("\nTensorHub detail FAILED")
        print(f"  Model     : {debug_model_name}")
        print(f"  Project ID: {project_id}")
        print(f"  Model ID  : {route_model_id}")
        print(f"  HTTP      : {response.status_code}")
        print(f"  Reason    : {reason}")
        return model, False, reason

    try:
        payload = response.json() or {}
    except Exception as exc:
        reason = f"invalid JSON: {type(exc).__name__}"
        _mark_detail_attempt(model, False, reason)
        print("\nTensorHub detail FAILED")
        print(f"  Model     : {debug_model_name}")
        print(f"  Project ID: {project_id}")
        print(f"  Model ID  : {route_model_id}")
        print(f"  HTTP      : {response.status_code}")
        print(f"  Reason    : {reason}")
        return model, False, reason

    if str(payload.get("code", "0")) not in {"", "0"}:
        reason = f"API code {payload.get('code')}: {payload.get('message') or ''}".strip()
        _mark_detail_attempt(model, False, reason)
        print("\nTensorHub detail FAILED")
        print(f"  Model     : {debug_model_name}")
        print(f"  Project ID: {project_id}")
        print(f"  Model ID  : {route_model_id}")
        print(f"  Reason    : {reason}")
        return model, False, reason

    detail = payload.get("data")
    if not isinstance(detail, dict):
        reason = "detail data missing"
        _mark_detail_attempt(model, False, reason)
        print("\nTensorHub detail FAILED")
        print(f"  Model     : {debug_model_name}")
        print(f"  Project ID: {project_id}")
        print(f"  Model ID  : {route_model_id}")
        print(f"  HTTP      : {response.status_code}")
        print(f"  Keys      : {sorted(payload.keys())}")
        print(f"  Reason    : {reason}")
        return model, False, reason

    # The service returns a rich object containing (at minimum) project + model.
    detail_model = detail.get("model") if isinstance(detail.get("model"), dict) else detail
    detail_project = detail.get("project") if isinstance(detail.get("project"), dict) else {}

    # TensorHub projects can expose multiple nested model/version IDs (for
    # example multiple epochs from one training run). Preserve those siblings
    # instead of flattening every artifact into one synthetic Current version.
    version_details = _fetch_tensorhub_sibling_details(detail, route_model_id)
    files, media, version_summaries = _attach_tensorhub_versions(
        model, version_details, "unconfirmed"
    )
    if not files:
        files = _detail_files(detail)
    if not media:
        media = _detail_media(detail, project_id, route_model_id, discovery_lane)

    # File detail can contain access/file fields omitted by model/detail.
    files, file_detail_objects, file_detail_failures = _enrich_files(files)

    # Some model-detail media records carry full generationData; others are sparse.
    # Fill only the sparse ones through TensorHub's dedicated image-detail API.
    media, image_detail_fetched, image_detail_failed, rich_image_metadata = _enrich_media_details(
        media,
        route_model_id,
    )

    access_probe = {
        "model": detail_model,
        "project": detail_project,
        "file_details": file_detail_objects,
    }
    access_status, detail_flags = _detail_access(access_probe)
    access_status = _normalize_access_label(access_status)
    paid_access_signal = _tensorhub_paid_access_signal(
        th,
        detail,
        detail_model,
        detail_project,
        *(version_details or []),
    )
    if paid_access_signal and access_status in {"gated", "restricted", "non_downloadable", "unconfirmed"}:
        access_status = "paid_access"

    # Byte-range probing is authoritative, but a normal scan does not need to
    # repeat it when the same TensorHub file IDs already have a definitive
    # cached result. Explicit Reload Model still forces a new probe so newly
    # granted account access is detected immediately.
    access_probe_result = {}
    current_access_files = _tensorhub_file_identity(files)
    detail_access_status = _normalize_access_label(access_status)
    cached_definitive = cached_access_status in {"downloadable", "gated", "paid_access"}
    same_access_files = bool(
        cached_access_files
        and current_access_files
        and cached_access_files == current_access_files
    )
    detail_disagrees = (
        detail_access_status in {"downloadable", "gated"}
        and cached_definitive
        and detail_access_status != cached_access_status
    )
    should_probe_access = bool(
        force_access_probe
        or not cached_definitive
        or not same_access_files
        or detail_disagrees
    )

    if should_probe_access:
        probed_status, access_probe_result = _probe_download_access(files, model.url)
        if probed_status in {"downloadable", "gated"}:
            access_status = (
                "paid_access"
                if probed_status == "gated" and paid_access_signal
                else probed_status
            )
            print(
                "TensorHub access probe: "
                f"{debug_model_name} -> {access_status} "
                f"(signed={access_probe_result.get('signed_url_http')}, "
                f"file={access_probe_result.get('probe_http')})"
            )
        else:
            if access_status not in {"downloadable", "gated", "paid_access"}:
                access_status = "unconfirmed"
            print(
                "TensorHub access probe unresolved: "
                f"{debug_model_name} -> {access_probe_result.get('reason') or 'unknown reason'}"
            )
    else:
        access_status = cached_access_status
        access_probe_result = dict(th.get("access_probe") or {})
        print(f"TensorHub access probe cached: {debug_model_name} -> {access_status}")

    if files:
        model.files = files
        sha256 = next(
            (str(file.get("sha256") or "") for file in files if file.get("sha256")),
            "",
        )
        if sha256:
            model.sha = sha256
        first_type = str(files[0].get("file_type") or "").upper()
        if first_type == "SAFE_TENSOR":
            model.format = "safetensors"
        precision = str(files[0].get("floating_point") or "").strip()
        if precision:
            model.quantization = precision

    if media:
        model.media = media
        model.has_media = True
        model.has_video = any(item.get("type") == "video" for item in media)
        model.preview_count = sum(1 for item in media if item.get("type") == "image")
        first_image = next(
            (
                item.get("url")
                for item in media
                if item.get("type") == "image" and item.get("url")
            ),
            "",
        )
        if first_image:
            model.image = first_image

    description = (
        detail_project.get("description")
        or detail_model.get("description")
        or _first_deep_value(detail, {"description", "modelDescription", "projectDescription"})
    )
    if isinstance(description, str) and description.strip():
        cleaned_description = common_metadata.extract_description({"description": description})
        if cleaned_description:
            model.description = cleaned_description

    # TensorHub's project/container can be much older than the nested model
    # currently surfaced by NEWEST. Retention must follow the actual nested
    # model/version activity or a freshly-added version can be discovered,
    # inserted, and then deleted immediately by AbyssBeacon's global cleanup.
    #
    # Prefer the newest explicit version upload timestamp collected above.
    # Fall back to the selected model's direct timestamps, and only then to the
    # broader detail response for older TensorHub response shapes.
    version_dates = []
    for version in version_summaries or []:
        if not isinstance(version, dict):
            continue
        version_dt = _parse_iso_datetime(version.get("uploaded_at"))
        if version_dt:
            version_dates.append(version_dt)

    selected_created = _detail_timestamp(
        detail_model,
        {"createdAt", "createAt", "uploadedAt", "uploadAt"},
    )
    selected_updated = _detail_timestamp(
        detail_model,
        {"updatedAt", "updateAt", "modifiedAt"},
    )

    if version_dates:
        newest_version = max(version_dates).isoformat()
        oldest_version = min(version_dates).isoformat()

        # `created` remains the earliest nested version we know about, while
        # `updated` represents the most recent nested-version activity. Cleanup
        # uses max(created, updated), so updated is the important retention key.
        model.created = oldest_version
        model.updated = newest_version
    else:
        created = selected_created or _detail_timestamp(
            detail,
            {"createdAt", "createAt", "uploadedAt", "uploadAt"},
        )
        updated = selected_updated or _detail_timestamp(
            detail,
            {"updatedAt", "updateAt", "modifiedAt"},
        )
        if created:
            model.created = created
        if updated:
            model.updated = updated

    th["retention_activity"] = {
        "created": model.created or "",
        "updated": model.updated or "",
        "basis": "nested_versions" if version_dates else "selected_model",
    }

    # Preserve three states: confirmed downloadable, confirmed restricted,
    # and unconfirmed. Unknown is never silently promoted to downloadable/gated.
    access_status = _apply_access_state(model, access_status)

    # Keep TensorHub's parent access metadata consistent with the final
    # enriched access state. The discovery/listing payload can call a paid
    # project merely "restricted", while model/detail exposes the actual
    # PROJECT_DOWNLOAD entitlement. If the paid signal has already been
    # confirmed above, do not leave the parent access object behind as
    # restricted or the detail UI will render the generic lock state.
    parent_access = th.get("access")
    if access_status == "paid_access" and isinstance(parent_access, dict):
        parent_access["status"] = "paid_access"
        parent_access["downloadable"] = False
        parent_access["authoritative"] = True

    for version in (model.card_data or {}).get("versions") or []:
        if isinstance(version, dict):
            version["access_status"] = access_status
            version["can_download"] = access_status == "downloadable"
    th["versions"] = list((model.card_data or {}).get("versions") or [])

    # TensorHub exposes useful model labels such as `yula` as triggerWords on
    # the nested versions rather than projectTags. Preserve those as actual
    # model metadata; access state belongs in the access UI, not Tags.
    trigger_tags = []
    for version in th["versions"]:
        if not isinstance(version, dict):
            continue
        for trigger in version.get("trigger_words") or []:
            trigger = str(trigger or "").strip()
            if trigger and trigger.casefold() not in {x.casefold() for x in trigger_tags}:
                trigger_tags.append(trigger)
    if trigger_tags:
        existing_display = [str(x) for x in (model.display_tags or []) if str(x).strip()]
        existing_plain = [x.strip() for x in str(model.tags or "").split(",") if x.strip()]
        for trigger in trigger_tags:
            if trigger.casefold() not in {x.casefold() for x in existing_display}:
                existing_display.append(trigger)
            if trigger.casefold() not in {x.casefold() for x in existing_plain}:
                existing_plain.append(trigger)
        model.display_tags = existing_display[:12]
        model.tags = ",".join(existing_plain)

    detail_model_id = str(detail_model.get("id") or route_model_id or "").strip()
    detail_project_id = str(
        detail_project.get("id")
        or detail_model.get("projectId")
        or project_id
        or ""
    ).strip()

    th["detail_enriched"] = True
    th["detail_enriched_at"] = datetime.now(timezone.utc).isoformat()
    th["detail_enrichment_version"] = DETAIL_ENRICHMENT_VERSION
    th["download_access"] = access_status
    th["access_probe"] = access_probe_result
    th["access_probe_at"] = datetime.now(timezone.utc).isoformat() if access_probe_result else ""
    th["detail_flags"] = detail_flags
    th["model_files"] = files
    th["image_detail_fetched_count"] = image_detail_fetched
    th["image_detail_enriched_count"] = rich_image_metadata
    th["image_detail_failed_count"] = image_detail_failed
    th["file_detail_failed_count"] = file_detail_failures
    th["detail_model_id"] = detail_model_id
    th["detail_project_id"] = detail_project_id
    th["detail_source"] = "community-web/v1/model/detail"

    model.metadata_hash = _detail_signature(model)
    _mark_detail_attempt(model, True, "")

    # Successful per-model detail blocks are intentionally verbose-only now.
    # Failures above remain visible in normal logging.
    print("\nTensorHub detail API OK")
    print(f"  Model     : {debug_model_name}")
    print(f"  Project ID: {project_id}")
    print(f"  Model ID  : {route_model_id}")
    print(f"  HTTP      : {response.status_code}")
    print(f"  Versions  : {len((model.card_data or {}).get('versions') or [])}")
    print(f"  Files     : {len(files)}")
    print(f"  Media     : {len(media)}")
    print(f"  Image API : {image_detail_fetched} fetched / {image_detail_failed} failed")
    print(f"  Generation metadata : {rich_image_metadata} / {sum(1 for item in media if item.get('type') == 'image')} available")
    print(f"  File details: {max(0, len(files) - file_detail_failures)} fetched / {file_detail_failures} failed")
    print(f"  Access    : {access_status}")

    return model, True, ""


def _detail_failure_retryable(reason):
    """Return whether a failed detail request deserves one recovery attempt."""
    text = str(reason or "").strip().casefold()
    if not text:
        return True
    if "404" in text:
        return False
    if "missing nested model id" in text:
        return False
    return True


def _retry_detail_failures(failures, model_by_id, label="TensorHub detail"):
    """Retry failed detail requests once with a smaller recovery pool.

    failures contains (project_id, failed_model, discovery_lane, reason).
    Definite 404s and structural missing-ID failures are not retried.
    """
    failures = list(failures or [])
    retryable = [
        (project_id, model, lane, reason)
        for project_id, model, lane, reason in failures
        if _detail_failure_retryable(reason)
    ]
    skipped = len(failures) - len(retryable)

    if skipped:
        print(
            f"  Retry skipped : {skipped} definite/non-retryable failure(s)"
        )

    if not retryable or scan_control.should_stop():
        return 0, len(failures)

    workers = min(DETAIL_RETRY_WORKERS, len(retryable))
    print(
        f"  Recovery retry: {len(retryable)} detail(s) with {workers} worker(s)"
    )

    recovered = 0
    still_failed = 0

    with ThreadPoolExecutor(
        max_workers=workers,
        thread_name_prefix="tensorhub-detail-retry",
    ) as executor:
        futures = {
            executor.submit(_fetch_public_detail, model, lane): (
                project_id,
                model,
                lane,
            )
            for project_id, model, lane, _reason in retryable
        }

        for future in as_completed(futures):
            project_id, original, lane = futures[future]
            if scan_control.should_stop():
                break

            try:
                retry_model, ok, reason = future.result()
            except Exception as exc:
                retry_model, ok, reason = (
                    original,
                    False,
                    f"{type(exc).__name__}: {exc}",
                )

            model_by_id[project_id] = retry_model

            if ok:
                recovered += 1
            else:
                still_failed += 1
                print(f"  Recovery FAILED {project_id}: {reason}")

    remaining = still_failed + skipped
    print(
        f"  Recovery result: {recovered} recovered, {remaining} still failed"
    )
    return recovered, remaining


def _build_model(item, discovery_lane="main"):
    project_id = str(item.get("id") or "").strip()
    if not project_id:
        return None

    nested = item.get("model") or {}
    owner = item.get("owner") or {}
    stats = item.get("statisticInfo") or {}
    tags, raw_tags = _tag_data(item)
    cover = _cover_record(item)
    access_status, access_data = _access_info(item)

    project_name = str(item.get("name") or nested.get("name") or f"TensorHub {project_id}").strip()
    version_name = str(nested.get("name") or "").strip()
    base_model = str(nested.get("baseModelDisplayName") or nested.get("baseModel") or "").strip()
    author = str(owner.get("nickname") or owner.get("id") or "").strip()

    model = Model()
    model.name = project_name
    model.display_name = project_name
    model.author = author
    model.source = NAME
    # TensorHub's portal payload has two IDs:
    #   item.id          = project/container ID used for discovery/deduplication
    #   item.model.id    = routable public model/version ID used by /models/<id>
    #
    # Using project_id here produces a TensorHub 404 for some projects.
    route_model_id = str(nested.get("id") or project_id).strip()
    model.url = f"{SITE}/models/{route_model_id}"
    model.model_key = project_id
    listing_hash = _tensorhub_metadata_hash(item)
    model.metadata_hash = listing_hash
    model.base_model = base_model
    model.architecture = _architecture_name(base_model)
    model.model_type = _model_type(item.get("type"))

    model.tags = ",".join(tags)
    model.display_tags = tags[:12]
    model.downloads = _safe_int(stats.get("downloadCount"))
    model.likes = _safe_int(stats.get("likeCount"))
    listing_created, listing_updated, _listing_activity = _listing_source_dates(item)
    model.created = listing_created.isoformat() if listing_created else ""
    model.updated = listing_updated.isoformat() if listing_updated else ""
    model._preserved_first_seen = datetime.now(timezone.utc).isoformat()
    # Listing metadata is not authoritative enough to call an unknown model
    # gated. Explicit restrictions remain gated; unknown is enriched later.
    model.gated = access_status in {"non_downloadable", "restricted"}

    content_rating = cover.get("content_rating", "").upper()
    model.sensitive = content_rating in {"MATURE", "ADULT", "NSFW", "EXPLICIT"} or common_metadata.detect_sensitive(
        project_name, tags, content_rating
    )

    if cover["url"]:
        media = {
            "type": cover["type"],
            "url": cover["url"],
            "thumbnail": cover["thumbnail"],
            "filename": f"tensorhub-cover-{cover['url'].rsplit('/', 1)[-1].split('?', 1)[0]}",
            "path": "cover",
            "metadata": {
                "content_rating": cover.get("content_rating", ""),
                "project_id": project_id,
                "model_id": nested.get("id"),
                "discovery_lane": discovery_lane,
            },
            "position": 0,
        }
        model.media = [media]
        model.has_media = True
        model.has_video = cover["type"] == "video"
        model.preview_count = 0 if model.has_video else 1
        model.image = cover["image_url"]

    model.card_data = {
        "tensorhub": {
            "project_id": project_id,
            "model_id": nested.get("id"),
            "route_model_id": route_model_id,
            "version_name": version_name,
            "owner_id": owner.get("id"),
            "owner_nickname": owner.get("nickname"),
            "project_type": item.get("type"),
            "project_status": item.get("status"),
            "modality": item.get("modality"),
            "base_model": nested.get("baseModel"),
            "base_model_display_name": nested.get("baseModelDisplayName"),
            "is_model_run_supported": nested.get("isModelRunSupported"),
            "run_count": _safe_int(stats.get("runCount")),
            "comment_count": _safe_int(stats.get("commentCount")),
            "recent_update": bool((stats.get("attachInfo") or {}).get("RecentUpdate")),
            "access": access_data,
            "listing_hash": listing_hash,
            "project_tags": raw_tags,
            "vip_only_info": nested.get("viponlyInfo"),
            "discovery_lane": discovery_lane,
        }
    }
    return model


def _fetch_lane(
    base_model,
    channel_id,
    lane_name,
    max_results,
    sort_value,
    blocked_creators,
    external_query="",
    visibility="FLAW",
):
    """Fetch one TensorHub discovery lane with 32-item cursor pagination."""
    collected = []
    cursor = ""
    seen_cursors = set()
    page = 0

    while len(collected) < max_results and not scan_control.should_stop():
        page += 1
        remaining = max_results - len(collected)
        size = min(PAGE_SIZE, remaining)
        payload = {
            "size": str(size),
            "visibility": str(visibility or "FLAW"),
            "filter": {"baseModels": [base_model]},
            "sort": sort_value,
        }
        if lane_name in {"main", "ordinary"}:
            payload["refererPath"] = "/models"
        if channel_id:
            payload["channelId"] = str(channel_id)
        if cursor:
            payload["cursor"] = str(cursor)
            # NEWEST/LATEST_UPDATE use an empty mix cursor. TensorHub's HOT
            # ranking has a compound cursor, but AbyssBeacon intentionally does
            # not use that recommendation order for discovery scans.
            payload["mixCursor"] = ""

        response = _post_with_backoff(
            payload,
            f"{lane_name} page {page}",
            public_catalog=True,
        )
        if response.status_code != 200:
            print(f"TensorHub {lane_name} error: HTTP {response.status_code}")
            break
        try:
            data = (response.json() or {}).get("data") or {}
        except Exception:
            print(f"TensorHub {lane_name} returned invalid JSON")
            break

        items = data.get("items") or []
        if not isinstance(items, list):
            items = []
        if not items:
            break

        accepted = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            owner = item.get("owner") or {}
            if _owner_is_blocked(owner, blocked_creators):
                continue
            if external_query and not _matches_external_query(item, external_query):
                continue
            collected.append(item)
            accepted += 1
            if len(collected) >= max_results:
                break

        next_cursor = str(data.get("cursor") or "").strip()
        print(f"TensorHub {lane_name} page {page}: {len(items)} results, {accepted} kept")

        # TensorHub can return fewer items than requested while still providing
        # a valid next cursor. Do NOT treat a short page as exhaustion.
        # Continue until the cursor disappears, repeats, or we hit max_results.
        if not next_cursor or next_cursor == cursor or next_cursor in seen_cursors:
            break
        seen_cursors.add(next_cursor)
        cursor = next_cursor

    return collected




def _general_search_dicts(value):
    """Yield dictionaries from TensorHub's general-search response, regardless of wrapper shape."""
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _general_search_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _general_search_dicts(child)


def _unwrap_general_search_item(value):
    """Peel common search-result wrappers while preserving normal portal cards."""
    if not isinstance(value, dict):
        return None
    current = value
    for key in ("project", "modelProject", "target", "resource", "content", "item"):
        nested = current.get(key)
        if isinstance(nested, dict) and nested is not current:
            # Only unwrap when the nested object looks materially more like an entity.
            if nested.get("id") or nested.get("owner") or nested.get("model"):
                current = nested
                break
    return current




def _extract_general_search_models(body, blocked_creators):
    """Return de-duplicated MODEL projects from a general-search response."""
    model_items = {}
    root = (body.get("data") if isinstance(body, dict) else body) or body
    for raw in _general_search_dicts(root):
        kind = str(raw.get("type") or raw.get("resultType") or raw.get("resourceType") or "").upper()
        candidate = _unwrap_general_search_item(raw)
        if not isinstance(candidate, dict):
            continue
        nested_model = candidate.get("model")
        if kind != "MODEL" and not (isinstance(nested_model, dict) and candidate.get("id")):
            continue
        project_id = str(candidate.get("id") or "").strip()
        owner = candidate.get("owner") or {}
        if project_id and not _owner_is_blocked(owner, blocked_creators):
            model_items.setdefault(project_id, candidate)
    return list(model_items.values())


def _fetch_architecture_search(base_model, blocked_creators, retention_enabled, retention_days, max_results, result_unlimited=False):
    """Fetch TensorHub's real search page for one structured base model.

    Automatic Retention supplies the date boundary. A finite centralized
    result limit is an additional ceiling; Unlimited exhausts the date window.
    With retention disabled, the finite result limit is the required boundary.
    """
    page_size = 20  # TensorHub's website search uses 20-item offset pages.
    offset = 0
    collected = {}
    page = 0

    now = datetime.now(timezone.utc)
    start = now - timedelta(days=max(0, int(retention_days or 0)))

    while not scan_control.should_stop():
        page += 1
        if retention_enabled and result_unlimited:
            limit = page_size
        else:
            remaining = max(0, int(max_results or 0) - len(collected))
            if remaining <= 0:
                break
            limit = min(page_size, remaining)

        payload = {
            "query": "",
            "sort": "NEWEST",
            "visibility": "ORDINARY",
            "filter": {"baseModels": [base_model]},
            "types": ["MODEL"],
            "limit": limit,
        }
        if retention_enabled:
            payload["createdAtBegin"] = str(int(start.timestamp() * 1000))
            payload["createdAtEnd"] = str(int(now.timestamp() * 1000))
        if offset:
            payload["offset"] = str(offset)

        response = _post_with_backoff(
            payload,
            f"architecture search page {page}",
            url=GENERAL_SEARCH_API,
            public_catalog=True,
        )
        if response.status_code != 200:
            _status_print(f"TensorHub architecture search error: HTTP {response.status_code}")
            break
        try:
            body = response.json() or {}
        except Exception:
            _status_print("TensorHub architecture search returned invalid JSON")
            break

        page_items = _extract_general_search_models(body, blocked_creators)
        if not page_items:
            break

        before = len(collected)
        for item in page_items:
            project_id = str(item.get("id") or "").strip()
            if not project_id:
                continue
            # Keep the source's structured filter honest. This also protects us
            # if TensorHub changes the search endpoint's filter semantics later.
            if not _matches_base_model(item, base_model):
                continue
            collected.setdefault(project_id, item)

        kept = len(collected) - before
        print(
            f"TensorHub search page {page}: {len(page_items)} result(s), "
            f"{kept} new matching project(s)"
        )

        # Website pagination advances by the number requested, not by how many
        # AbyssBeacon kept after local verification/de-duplication.
        offset += limit

        # Do NOT treat a short *filtered* page as end-of-results. The search
        # response can contain entries that AbyssBeacon intentionally excludes
        # (or duplicates), so a requested 20-row page may yield only 19 usable
        # MODEL projects even though later offsets still contain results. Keep
        # paging until the endpoint yields no usable model rows, or until the
        # configured finite result boundary is reached.
        if not result_unlimited and len(collected) >= int(max_results or 0):
            break

    return list(collected.values()), page

def _fetch_general_search(query, intent, max_results, blocked_creators):
    """Use TensorHub's real free-text search for AbyssBeacon Search Sources."""
    intent = str(intent or "anything").strip().lower()
    if intent == "models":
        types = ["MODEL"]
    elif intent == "creators":
        types = ["USER"]
    else:
        # Posts/articles/tags are useful on TensorHub's website but are not
        # importable AbyssBeacon records. Anything means models + creators here.
        types = ["MODEL", "USER"]

    payload = {
        "query": str(query or "").strip(),
        "visibility": "ORDINARY",
        "filter": {},
        "types": types,
        "limit": max(1, int(max_results or 100)),
    }
    response = _post_with_backoff(payload, "general search", url=GENERAL_SEARCH_API)
    if response.status_code != 200:
        _status_print(f"TensorHub keyword search error: HTTP {response.status_code}")
        return [], []
    try:
        body = response.json() or {}
    except Exception:
        _status_print("TensorHub keyword search returned invalid JSON")
        return [], []

    model_items = {}
    users = {}
    # The API has changed wrapper names before, so classify recursively instead
    # of coupling AbyssBeacon to one response envelope.
    for raw in _general_search_dicts((body.get("data") if isinstance(body, dict) else body) or body):
        kind = str(raw.get("type") or raw.get("resultType") or raw.get("resourceType") or "").upper()
        candidate = _unwrap_general_search_item(raw)
        if not isinstance(candidate, dict):
            continue

        # MODEL results use the same project/model/owner vocabulary as portal cards.
        nested_model = candidate.get("model")
        owner = candidate.get("owner") or {}
        if kind == "MODEL" or (isinstance(nested_model, dict) and candidate.get("id")):
            project_id = str(candidate.get("id") or "").strip()
            if project_id and not _owner_is_blocked(owner, blocked_creators):
                model_items.setdefault(project_id, candidate)
            continue

        # USER results may be wrapped or returned directly.
        if kind == "USER" or (candidate.get("nickname") and candidate.get("id") and not nested_model):
            owner_id = str(candidate.get("id") or candidate.get("userId") or "").strip()
            nickname = str(candidate.get("nickname") or candidate.get("name") or owner_id).strip()
            if owner_id and not _creator_identity_is_blocked(owner_id, nickname, blocked_creators):
                users.setdefault(owner_id, nickname)

    _status_print(
        f"TensorHub keyword search: {len(model_items)} model result(s), "
        f"{len(users)} creator result(s)"
    )
    return list(model_items.values()), list(users.items())


def _creator_items(data):
    """TensorHub uses `projects` on creator lists and `items` on portal lists."""
    values = data.get("projects")
    if values is None:
        values = data.get("items")
    return values if isinstance(values, list) else []


def _fetch_creator_catalog(owner_id, max_results, blocked_creators, owner_name="", single_page=False, expected_base_model=""):
    """Fetch the broader creator catalog exposed by TensorHub creator pages.

    TensorHub labels this request visibilityGte=PRIVATE, but models returned by
    it can still be publicly viewable/downloadable. AbyssBeacon therefore treats
    PRIVATE as discovery metadata, not as an access restriction.
    """
    owner_id = str(owner_id or "").strip()
    if not owner_id:
        return []
    if _creator_identity_is_blocked(owner_id, owner_name, blocked_creators):
        return []

    collected = []
    cursor = ""
    seen_cursors = set()
    page = 0
    while len(collected) < max_results and not scan_control.should_stop():
        page += 1
        remaining = max_results - len(collected)
        limit = min(20, remaining)
        payload = {
            "limit": limit,
            "sort": "NEWEST",
            "subscriberOnly": False,
            "visibilityGte": "PRIVATE",
            "userId": owner_id,
        }
        if cursor:
            payload["cursor"] = str(cursor)
        response = _post_with_backoff(
            payload,
            f"creator {owner_name or owner_id} page {page}",
            url=CREATOR_API,
        )
        if response.status_code != 200:
            print(f"TensorHub creator {owner_name or owner_id} error: HTTP {response.status_code}")
            break
        try:
            data = (response.json() or {}).get("data") or {}
        except Exception:
            print(f"TensorHub creator {owner_name or owner_id} returned invalid JSON")
            break
        items = _creator_items(data)
        if not items:
            break
        for item in items:
            if not isinstance(item, dict):
                continue
            item_owner = item.get("owner") or {}
            if _owner_is_blocked(item_owner, blocked_creators):
                continue
            if expected_base_model and not _matches_base_model(item, expected_base_model):
                continue
            collected.append(item)
            if len(collected) >= max_results:
                break
        next_cursor = str(data.get("cursor") or "").strip()
        if single_page or not next_cursor or next_cursor == cursor or next_cursor in seen_cursors:
            break
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    return collected


def _remember_creator_identities(items, discovered_via="observed"):
    """Persist TensorHub owner IDs independently from short-lived model rows."""
    try:
        import database
    except Exception:
        return 0
    remembered = 0
    for item in items or []:
        if not isinstance(item, dict):
            continue
        owner = item.get("owner") if isinstance(item.get("owner"), dict) else {}
        owner_id = str(owner.get("id") or "").strip()
        nickname = str(owner.get("nickname") or owner_id).strip()
        if not owner_id or not nickname:
            continue
        try:
            if database.remember_creator_source_identity(
                nickname, NAME, owner_id,
                profile_url=f"https://tensorhub.art/u/{owner_id}",
                discovered_via=discovered_via,
            ):
                remembered += 1
        except Exception:
            continue
    return remembered


def _known_creator_map():
    """Return every persisted TensorHub owner ID, even if their models aged out."""
    try:
        import database
        rows = database.get_creator_source_identities(NAME)
    except Exception:
        return {}
    result = {}
    for row in rows:
        owner_id = str(row.get("source_creator_id") or "").strip()
        nickname = str(row.get("creator_name") or owner_id).strip()
        if owner_id and nickname:
            result.setdefault(owner_id, nickname)
    return result


def _local_owner_ids_for_creator(creator):
    """Resolve a creator from persistent identity memory, then legacy model rows."""
    creator = str(creator or "").strip()
    try:
        import database
        identities = database.get_creator_source_identities(NAME, creator_name=creator)
        ids = []
        for row in identities:
            owner_id = str(row.get("source_creator_id") or "").strip()
            if owner_id and owner_id not in ids:
                ids.append(owner_id)
        if ids:
            return ids

        # Migration fallback for an older DB before creator_sources was seeded.
        conn = database.connect()
        rows = conn.execute(
            "SELECT card_data FROM models WHERE source=? AND lower(author)=lower(?)",
            (NAME, creator),
        ).fetchall()
        conn.close()
    except Exception:
        return []
    ids = []
    for row in rows:
        try:
            card = json.loads(row[0] or "{}")
            owner_id = str(((card.get("tensorhub") or {}).get("owner_id")) or "").strip()
            if owner_id and owner_id not in ids:
                ids.append(owner_id)
        except Exception:
            continue
    return ids



def _fetch_tag_catalog(tag_id, max_results, sort_value, blocked_creators):
    """Fetch TensorHub models for one public tag/category.

    TensorHub's tag pages use the same portal/list/v3 endpoint as normal
    discovery, with tagIds in the request body. Cursor state is provider-owned
    and is carried forward verbatim so NEWEST, LATEST_UPDATE, and HOT_TODAY can
    all paginate safely.
    """
    tag_id = str(tag_id or "").strip()
    if not tag_id:
        return []

    collected = []
    cursor = ""
    mix_cursor = ""
    seen_states = set()
    page = 0

    while len(collected) < max_results and not scan_control.should_stop():
        page += 1
        remaining = max_results - len(collected)
        size = min(PAGE_SIZE, remaining)
        payload = {
            "size": str(size),
            "tagIds": [tag_id],
            "filter": {},
            "sort": sort_value,
        }
        if cursor:
            payload["cursor"] = str(cursor)
        if mix_cursor:
            payload["mixCursor"] = str(mix_cursor)

        response = _post_with_backoff(payload, f"tag {tag_id} page {page}")
        if response.status_code != 200:
            print(f"TensorHub tag {tag_id} error: HTTP {response.status_code}")
            break
        try:
            data = (response.json() or {}).get("data") or {}
        except Exception:
            print(f"TensorHub tag {tag_id} returned invalid JSON")
            break

        items = data.get("items") or []
        if not isinstance(items, list) or not items:
            break

        accepted = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            owner = item.get("owner") or {}
            if _owner_is_blocked(owner, blocked_creators):
                continue
            collected.append(item)
            accepted += 1
            if len(collected) >= max_results:
                break

        next_cursor = str(data.get("cursor") or "").strip()
        next_mix = str(data.get("mixCursor") or "").strip()
        print(f"TensorHub tag {tag_id} page {page}: {len(items)} results, {accepted} kept")

        state = (next_cursor, next_mix)
        if not next_cursor or state in seen_states or (next_cursor == cursor and next_mix == mix_cursor):
            break
        seen_states.add(state)
        cursor, mix_cursor = next_cursor, next_mix

    return collected


def scan_tag(tag_id, max_results=100, sort="NEWEST", tag_name=""):
    """Explicit TensorHub tag/category discovery used by AbyssBeacon Discovery Scan."""
    _apply_auth()
    try:
        max_results = max(1, int(max_results))
    except (TypeError, ValueError):
        max_results = 100

    sort_value = str(sort or "NEWEST").strip().upper()
    if sort_value not in {"NEWEST", "LATEST_UPDATE", "HOT_TODAY"}:
        sort_value = "NEWEST"

    try:
        import database
        blocked_creators = set(database.get_blocked_creator_set(NAME))
    except Exception:
        blocked_creators = set()
    blocked_creators.update(HARD_BLOCKED_CREATORS)

    label = str(tag_name or tag_id).strip()
    print("\n================================")
    print("TENSORHUB DISCOVERY SCAN")
    print("================================")
    print(f"Tag        : {label} ({tag_id})")
    print(f"Sort       : {sort_value}")
    print(f"Max results: {max_results}")

    items = _fetch_tag_catalog(tag_id, max_results, sort_value, blocked_creators)
    unique_items = {}
    for item in items:
        project_id = str(item.get("id") or "").strip()
        if project_id:
            unique_items.setdefault(project_id, item)

    _remember_creator_identities(unique_items.values(), discovered_via="discovery")

    existing_cache = _existing_tensorhub_state()
    try:
        import database
        retention_tombstones = database.get_retention_tombstones(NAME)
    except Exception:
        retention_tombstones = {}
    models = []
    detail_candidates = []
    lane = f"tag:{tag_id}"

    for project_id, item in unique_items.items():
        model = _build_model(item, discovery_lane=lane)
        if not model:
            continue

        cached = existing_cache.get(project_id)
        model = _apply_cached_detail(model, cached)
        old_th = ((cached or {}).get("card_obj") or {}).get("tensorhub") or {}
        fresh_th = (model.card_data or {}).get("tensorhub") or {}
        listing_changed = bool(cached and old_th.get("listing_hash") and old_th.get("listing_hash") != fresh_th.get("listing_hash"))
        already_detailed = bool(old_th.get("detail_enriched"))
        try:
            detail_version = int(old_th.get("detail_enrichment_version") or 0)
        except (TypeError, ValueError):
            detail_version = 0
        upgrade_needed = already_detailed and detail_version < DETAIL_ENRICHMENT_VERSION
        retry_due = _detail_retry_due(old_th)
        if (not cached) or listing_changed or upgrade_needed or ((not already_detailed) and retry_due):
            priority = 0 if not cached else (1 if listing_changed or upgrade_needed else 2)
            detail_candidates.append((priority, project_id, model))
        models.append(model)

    if detail_candidates and not scan_control.should_stop():
        chosen = sorted(detail_candidates, key=lambda entry: entry[0])
        model_by_id = {str(model.model_key or ""): model for model in models}
        workers = min(DETAIL_WORKERS, len(chosen))
        _status_print(f"TensorHub Discovery details: {len(chosen)} model(s), {workers} worker(s)")
        detailed = 0
        failures = []
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="tensorhub-discovery-detail") as executor:
            futures = {
                executor.submit(_fetch_public_detail, model, lane): (project_id, model)
                for _, project_id, model in chosen
            }
            for future in as_completed(futures):
                project_id, original = futures[future]
                if scan_control.should_stop():
                    break
                try:
                    detailed_model, ok, reason = future.result()
                except Exception as exc:
                    detailed_model, ok, reason = original, False, f"{type(exc).__name__}: {exc}"
                model_by_id[project_id] = detailed_model
                if ok:
                    detailed += 1
                else:
                    failures.append((project_id, detailed_model, lane, reason))
                    _status_print(f"TensorHub Discovery detail FAILED {project_id}: {reason}")

        recovered, remaining = _retry_detail_failures(
            failures,
            model_by_id,
            "TensorHub Discovery detail",
        )
        detailed += recovered
        models = [model_by_id.get(str(model.model_key or ""), model) for model in models]
        _status_print(f"TensorHub Discovery detailed: {detailed}, failed: {remaining}")

    print(f"TensorHub Discovery: {len(models)} unique model(s)")
    return models


def scan(term, scan_seen_models=None, scan_settings=None, creator=None):
    """Discover TensorHub projects from public feeds plus creator catalog probes."""
    _apply_auth()
    scan_seen_models = scan_seen_models if scan_seen_models is not None else set()
    scan_settings = scan_settings or {}

    try:
        max_results = max(1, int(scan_settings.get("max_results", 100)))
    except (TypeError, ValueError):
        max_results = 100
    # Expanded Creator Search is intentionally one newest page only. TensorHub
    # returns at most 20 creator projects on that page; deeper history belongs
    # to the explicit Creator Scan path.
    creator_probe_results = 20
    try:
        creator_recheck_hours = max(0, min(720, int(scan_settings.get("creator_recheck_hours", 6))))
    except (TypeError, ValueError):
        creator_recheck_hours = 6
    creator_expansion_enabled = bool(scan_settings.get("creator_expansion_enabled", False))
    print("TensorHub scan mode: " + ("Deep creator discovery" if creator_expansion_enabled else "Standard"))
    normal_retention_enabled = bool(scan_settings.get("_normal_retention_enabled", False))
    normal_result_unlimited = bool(scan_settings.get("_normal_result_unlimited", False))
    try:
        normal_retention_days = max(0, min(36500, int(scan_settings.get("_normal_retention_days", 7))))
    except (TypeError, ValueError):
        normal_retention_days = 7
    normal_retention_cutoff = datetime.now(timezone.utc) - timedelta(
        days=max(0, int(normal_retention_days))
    )

    # Persistent exclusions for stale TensorHub projects removed by cleanup.
    # This must be initialized in the normal scan() path (not only scan_tag),
    # otherwise retention preflight cannot consult remembered stale projects.
    try:
        import database
        retention_tombstones = database.get_retention_tombstones(NAME)
    except Exception:
        retention_tombstones = {}
    try:
        creator_scan_max = max(1, int(scan_settings.get("creator_scan_max_results", max_results)))
    except (TypeError, ValueError):
        creator_scan_max = max_results

    blocked_creators = {
        str(value or "").casefold().strip()
        for value in (scan_settings.get("_blocked_creators") or [])
        if str(value or "").strip()
    }
    blocked_creators.update(HARD_BLOCKED_CREATORS)

    search_mode = str(scan_settings.get("_search_mode") or "text").strip().lower()
    base_model = str(scan_settings.get("_architecture") or "").strip()
    architecture_context = str(scan_settings.get("_architecture_context") or "").strip()

    if search_mode == "base_model" and base_model:
        structured_base = base_model
    elif architecture_context:
        structured_base = architecture_context
    else:
        structured_base = ""

    # Manual creator-page scan: enumerate every accessible project for owner IDs
    # AbyssBeacon has already learned for this creator, bounded by the setting.
    if creator and not scan_settings.get("_external_search"):
        owner_ids = _local_owner_ids_for_creator(creator)
        if not owner_ids:
            print(f"TensorHub creator scan: no stored owner ID for {creator}")
            return []
        creator_scan_started = time.perf_counter()
        _status_print("")
        _status_print("TensorHub creator scan")
        _status_print(f"  Creator       : {creator}")
        _status_print(f"  Owner IDs     : {len(owner_ids)}")
        _status_print(f"  Catalog limit : {creator_scan_max}")

        scan_status.update_status(
            status="running",
            source=NAME,
            current=creator,
            message="TensorHub: loading creator catalog..."
        )

        unique_items = {}
        for owner_index, owner_id in enumerate(owner_ids, start=1):
            owner_started = time.perf_counter()
            owner_items = _fetch_creator_catalog(
                owner_id,
                creator_scan_max,
                blocked_creators,
                owner_name=creator,
                expected_base_model=structured_base,
            )
            owner_elapsed = time.perf_counter() - owner_started
            _status_print(
                f"  Catalog {owner_index}/{len(owner_ids)}: "
                f"{len(owner_items)} project(s) in {owner_elapsed:.2f}s"
            )
            for item in owner_items:
                project_id = str(item.get("id") or "").strip()
                if project_id:
                    unique_items.setdefault(project_id, item)

        _status_print(f"  Unique catalog: {len(unique_items)} project(s)")
        scan_status.update_status(
            status="running",
            source=NAME,
            current=creator,
            message=f"TensorHub: catalog loaded ({len(unique_items)} projects)"
        )

        cache_started = time.perf_counter()
        existing_cache = _existing_tensorhub_state()
        _status_print(
            f"  Cache loaded  : {len(existing_cache)} TensorHub row(s) "
            f"in {time.perf_counter() - cache_started:.2f}s"
        )
        models = []
        detail_candidates = []
        lane = f"creator:{','.join(owner_ids)}"
        for project_id, item in unique_items.items():
            if project_id in scan_seen_models:
                continue
            model = _build_model(item, discovery_lane=lane)
            if model:
                cached = existing_cache.get(project_id)
                model = _apply_cached_detail(model, cached)
                old_th = ((cached or {}).get("card_obj") or {}).get("tensorhub") or {}
                fresh_th = (model.card_data or {}).get("tensorhub") or {}
                listing_changed = bool(cached and old_th.get("listing_hash") and old_th.get("listing_hash") != fresh_th.get("listing_hash"))
                already_enriched = bool(old_th.get("detail_enriched"))
                try:
                    detail_version = int(old_th.get("detail_enrichment_version") or 0)
                except (TypeError, ValueError):
                    detail_version = 0
                upgrade_needed = already_enriched and detail_version < DETAIL_ENRICHMENT_VERSION
                retry_due = _detail_retry_due(old_th)
                if (not cached) or listing_changed or upgrade_needed or ((not already_enriched) and retry_due):
                    priority = 0 if not cached else (1 if listing_changed or upgrade_needed else 2)
                    detail_candidates.append((priority, project_id, model))
                scan_seen_models.add(project_id)
                models.append(model)
        cached_complete = max(0, len(models) - len(detail_candidates))
        _status_print(f"  Cached/ready  : {cached_complete}")
        _status_print(f"  Needs detail  : {len(detail_candidates)}")

        if detail_candidates:
            chosen = sorted(detail_candidates, key=lambda entry: entry[0])
            model_by_id = {str(model.model_key or ""): model for model in models}
            # Creator scans can involve hundreds of detail requests. Use a
            # larger pool than normal discovery so deep creator scans do not
            # spend most of their time waiting on serial network latency.
            # TensorHub request helpers retain their existing retry/backoff
            # behavior if the service starts returning rate-limit responses.
            # Stress-tested deep creator enrichment ceiling. This only
            # affects explicit TensorHub creator scans; normal source scans
            # retain their lower concurrency.
            # 100-way concurrency was fast but began producing connection
            # timeouts during stress testing. Test a midpoint that should retain
            # much of the throughput while staying below connection saturation.
            workers = min(DETAIL_WORKERS, len(chosen))
            _status_print(f"  Detail workers: {workers}")

            scan_status.update_status(
                status="running",
                source=NAME,
                current=creator,
                message=f"TensorHub: enriching details 0/{len(chosen)}"
            )

            detail_started = time.perf_counter()
            detail_done = 0
            detail_ok = 0
            detail_failed = 0
            detail_failures = []

            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="tensorhub-detail") as executor:
                futures = {
                    executor.submit(_fetch_public_detail, model, lane): (project_id, model)
                    for _, project_id, model in chosen
                }
                for future in as_completed(futures):
                    project_id, original = futures[future]
                    try:
                        enriched_model, ok, _reason = future.result()
                    except Exception:
                        enriched_model, ok = original, False

                    detail_done += 1
                    model_by_id[project_id] = enriched_model
                    if ok:
                        detail_ok += 1
                    else:
                        detail_failed += 1
                        detail_failures.append(
                            (project_id, enriched_model, lane, _reason)
                        )

                    scan_status.update_status(
                        status="running",
                        source=NAME,
                        current=creator,
                        message=f"TensorHub: enriching details {detail_done}/{len(chosen)}"
                    )

                    if detail_done == 1 or detail_done == len(chosen) or detail_done % 10 == 0:
                        elapsed = time.perf_counter() - detail_started
                        _status_print(
                            f"  Detail progress: {detail_done}/{len(chosen)} "
                            f"({detail_ok} ok, {detail_failed} failed) "
                            f"in {elapsed:.1f}s"
                        )

            recovered, remaining = _retry_detail_failures(
                detail_failures,
                model_by_id,
                "TensorHub creator detail",
            )
            if recovered:
                detail_ok += recovered
            detail_failed = remaining

            _status_print(f"  Detail total  : {time.perf_counter() - detail_started:.2f}s")
            models = [model_by_id.get(str(model.model_key or ""), model) for model in models]

        _remember_creator_identities(unique_items.values(), discovered_via="explicit")
        total_elapsed = time.perf_counter() - creator_scan_started
        _status_print(f"  Returned      : {len(models)} unique project(s)")
        _status_print(f"  Creator total : {total_elapsed:.2f}s")
        return models

    external_search = bool(scan_settings.get("_external_search"))
    external_query = str(scan_settings.get("_external_query") or term).strip() if external_search else ""
    external_intent = str(scan_settings.get("_external_intent") or "anything").strip().lower()

    # Search Sources is a genuine free-text search. It must never require the
    # keyword itself to map to a TensorHub base model first.
    if external_search and external_query:
        search_started = time.perf_counter()
        search_items, matched_users = _fetch_general_search(
            external_query, external_intent, max_results, blocked_creators
        )

        # Creator hits are leads: expand their TensorHub catalogs, then dedupe
        # against direct MODEL hits. Architecture context, when selected, is a
        # result filter rather than permission to perform the keyword search.
        unique_items = {}
        for item in search_items:
            project_id = str(item.get("id") or "").strip()
            if project_id:
                unique_items.setdefault(project_id, item)

        if external_intent in {"anything", "creators"}:
            remaining = max(1, max_results - len(unique_items))
            per_creator = max(1, min(100, remaining))
            for owner_id, nickname in matched_users:
                if scan_control.should_stop() or len(unique_items) >= max_results:
                    break
                for item in _fetch_creator_catalog(
                    owner_id, per_creator, blocked_creators, owner_name=nickname,
                    expected_base_model=structured_base,
                ):
                    project_id = str(item.get("id") or "").strip()
                    if project_id:
                        unique_items.setdefault(project_id, item)
                    if len(unique_items) >= max_results:
                        break

        # Optional architecture selection filters results after free-text search.
        if structured_base:
            wanted = str(structured_base).casefold().strip()
            unique_items = {
                project_id: item for project_id, item in unique_items.items()
                if wanted in {
                    str(((item.get("model") or {}).get("baseModel") or "")).casefold().strip(),
                    str(((item.get("model") or {}).get("baseModelDisplayName") or "")).casefold().strip(),
                }
                or _architecture_name(str(((item.get("model") or {}).get("baseModelDisplayName") or (item.get("model") or {}).get("baseModel") or ""))).casefold()
                   == _architecture_name(structured_base).casefold()
            }

        existing_cache = _existing_tensorhub_state()
        models = []
        detail_candidates = []
        for project_id, item in unique_items.items():
            if project_id in scan_seen_models:
                continue
            model = _build_model(item, discovery_lane="keyword-search")
            if not model:
                continue
            cached = existing_cache.get(project_id)
            model = _apply_cached_detail(model, cached)
            old_th = ((cached or {}).get("card_obj") or {}).get("tensorhub") or {}
            fresh_th = (model.card_data or {}).get("tensorhub") or {}
            listing_changed = bool(cached and old_th.get("listing_hash") and old_th.get("listing_hash") != fresh_th.get("listing_hash"))
            already_enriched = bool(old_th.get("detail_enriched"))
            try:
                detail_version = int(old_th.get("detail_enrichment_version") or 0)
            except (TypeError, ValueError):
                detail_version = 0
            retry_due = _detail_retry_due(old_th)
            if (not cached) or listing_changed or detail_version < DETAIL_ENRICHMENT_VERSION or ((not already_enriched) and retry_due):
                detail_candidates.append((project_id, model))
            scan_seen_models.add(project_id)
            models.append(model)

        if detail_candidates:
            model_by_id = {str(model.model_key or ""): model for model in models}
            workers = min(DETAIL_WORKERS, len(detail_candidates))
            failures = []
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="tensorhub-search-detail") as executor:
                futures = {
                    executor.submit(_fetch_public_detail, model, "keyword-search"): (project_id, model)
                    for project_id, model in detail_candidates
                }
                for future in as_completed(futures):
                    project_id, original = futures[future]
                    try:
                        enriched_model, ok, reason = future.result()
                    except Exception as exc:
                        enriched_model, ok, reason = original, False, f"{type(exc).__name__}: {exc}"
                    model_by_id[project_id] = enriched_model
                    if not ok:
                        failures.append(
                            (project_id, enriched_model, "keyword-search", reason)
                        )

            _retry_detail_failures(
                failures,
                model_by_id,
                "TensorHub keyword detail",
            )
            models = [model_by_id.get(str(model.model_key or ""), model) for model in models]

        _remember_creator_identities(unique_items.values(), discovered_via="external-search")
        _status_print(f"TensorHub external search: {len(models)} model(s) in {time.perf_counter() - search_started:.2f}s")
        return models

    if not structured_base:
        print(f"TensorHub has no structured mapping for search term '{term}'; skipping")
        return []

    # Normal TensorHub discovery now mirrors the website's real search page
    # instead of crawling main + category portal lanes. The API supports exact
    # base-model filtering, NEWEST ordering, created-date bounds, and 20-item
    # offset pagination.
    print("\nTensorHub settings")
    print(f"  Base model       : {structured_base}")
    print("  Discovery        : search/general/v2")
    print("  Sort             : NEWEST")
    if normal_retention_enabled and normal_result_unlimited:
        print(f"  Search window    : {normal_retention_days} day(s), exhaustive (Unlimited results)")
    elif normal_retention_enabled:
        print(f"  Search window    : {normal_retention_days} day(s) OR {max_results} result(s), whichever comes first")
    else:
        print(f"  Search window    : retention OFF, capped at {max_results} result(s)")
    print(f"  Creator expansion: {'ON' if creator_expansion_enabled else 'OFF'}")
    if creator_expansion_enabled:
        print(f"  Creator probe    : up to 20 newest project(s) per eligible creator")
        print(f"  Creator cooldown : {creator_recheck_hours} hour(s)")

    scan_started_at = time.perf_counter()
    discovery_started_at = time.perf_counter()
    unique_items = {}
    discovery_lanes = {}

    search_items, search_pages = _fetch_architecture_search(
        structured_base,
        blocked_creators,
        normal_retention_enabled,
        normal_retention_days,
        max_results,
        normal_result_unlimited,
    )
    for item in search_items:
        project_id = str(item.get("id") or "").strip()
        if not project_id:
            continue
        unique_items.setdefault(project_id, item)
        discovery_lanes.setdefault(project_id, []).append("architecture-search")

    recent_update_discovered = sum(
        1
        for item in unique_items.values()
        if isinstance(item, dict)
        and isinstance(item.get("statisticInfo"), dict)
        and isinstance((item.get("statisticInfo") or {}).get("attachInfo"), dict)
        and ((item.get("statisticInfo") or {}).get("attachInfo") or {}).get("RecentUpdate") is True
    )

    public_discovery_seconds = time.perf_counter() - discovery_started_at
    public_unique = len(unique_items)
    owner_map = {}
    for item in unique_items.values():
        owner = item.get("owner") or {}
        owner_id = str(owner.get("id") or "").strip()
        nickname = str(owner.get("nickname") or owner_id).strip()
        if owner_id and not _creator_identity_is_blocked(owner_id, nickname, blocked_creators):
            owner_map.setdefault(owner_id, nickname)

    # Creator identity is long-lived even though model cards are intentionally
    # short-lived under retention. Newly observed creators refresh the registry,
    # and Expanded Creator Search can later operate from that registry even after
    # all of the creator's models have aged out of AbyssBeacon.
    _remember_creator_identities(unique_items.values(), discovered_via="observed")
    known_creator_map = _known_creator_map()
    for owner_id, nickname in known_creator_map.items():
        if not _creator_identity_is_blocked(owner_id, nickname, blocked_creators):
            owner_map.setdefault(owner_id, nickname)

    creator_new = 0
    creator_checked = 0
    creator_skipped_cooldown = 0
    creator_seconds = 0.0
    creator_state = _load_creator_probe_state()
    eligible_owner_map = owner_map
    if creator_expansion_enabled and owner_map:
        eligible_owner_map = {
            owner_id: nickname
            for owner_id, nickname in owner_map.items()
            if _creator_probe_due(creator_state, owner_id, structured_base, creator_recheck_hours)
        }
        creator_skipped_cooldown = len(owner_map) - len(eligible_owner_map)

    if creator_expansion_enabled and creator_probe_results > 0 and eligible_owner_map and not scan_control.should_stop():
        creator_started_at = time.perf_counter()
        workers = min(3, len(eligible_owner_map))
        print(
            f"TensorHub creator expansion: {len(eligible_owner_map)}/{len(owner_map)} creators eligible, "
            f"{workers} workers, 1 page/creator, {creator_recheck_hours}h cooldown"
        )
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="tensorhub-creator") as executor:
            futures = {
                executor.submit(
                    _fetch_creator_catalog,
                    owner_id,
                    creator_probe_results,
                    blocked_creators,
                    nickname,
                    True,
                    structured_base,
                ): (owner_id, nickname)
                for owner_id, nickname in eligible_owner_map.items()
            }
            for future in as_completed(futures):
                if scan_control.should_stop():
                    break
                owner_id, nickname = futures[future]
                creator_checked += 1
                if creator_checked == 1 or creator_checked % 25 == 0 or creator_checked == len(eligible_owner_map):
                    print(f"TensorHub creator expansion progress: {creator_checked}/{len(eligible_owner_map)}")
                try:
                    items = future.result()
                    creator_state[_creator_probe_key(owner_id, structured_base)] = datetime.now(timezone.utc).isoformat()
                except Exception as exc:
                    print(f"TensorHub creator {nickname} failed: {exc}")
                    continue
                for item in items:
                    project_id = str(item.get("id") or "").strip()
                    if not project_id:
                        continue
                    if project_id not in unique_items:
                        unique_items[project_id] = item
                        discovery_lanes[project_id] = [f"creator:{owner_id}"]
                        creator_new += 1
                    else:
                        discovery_lanes.setdefault(project_id, []).append(f"creator:{owner_id}")
        creator_seconds = time.perf_counter() - creator_started_at
        _save_creator_probe_state(creator_state)

    print("TensorHub discovery summary")
    print(f"  Search pages     : {search_pages}")
    print(f"  Search unique    : {public_unique}")
    print(f"  RecentUpdate rows: {recent_update_discovered}")
    print(f"  Creators probed : {creator_checked}")
    if creator_expansion_enabled:
        print(f"  Cooldown skipped: {creator_skipped_cooldown}")
    print(f"  Creator-only    : {creator_new}")
    print(f"  Final unique    : {len(unique_items)}")

    build_started_at = time.perf_counter()
    existing_cache = _existing_tensorhub_state()
    models = []
    architecture_rejected = 0
    detail_candidates = []
    retention_rejected = 0
    retention_memory_hits = 0
    retention_preflight_rejects = 0
    retention_listing_rejects = 0
    retention_recent_update_bypasses = 0
    retention_rejected_samples = []

    # TensorHub's portal list often omits the authoritative source dates that
    # model/detail later exposes. Before doing expensive file/image enrichment,
    # cheaply preflight only the unseen rows whose age cannot be decided from
    # the listing or retention memory. The resulting tombstones make later
    # scans skip unchanged stale projects immediately.
    retention_preflight = {}
    retention_preflight_candidates = {}
    if normal_retention_enabled:
        for project_id, item in unique_items.items():
            if project_id in existing_cache or project_id in scan_seen_models:
                continue
            if not _matches_base_model(item, structured_base):
                continue
            listing_hash = _tensorhub_metadata_hash(item)

            # TensorHub's NEWEST feed explicitly marks older project containers
            # when a fresh nested model/version has been added. In that case the
            # project-level timestamp can be stale by months even though the
            # selected model is genuinely new. Let the row through to normal
            # detail enrichment instead of rejecting it on project age.
            stats = item.get("statisticInfo") if isinstance(item.get("statisticInfo"), dict) else {}
            attach = stats.get("attachInfo") if isinstance(stats.get("attachInfo"), dict) else {}
            if attach.get("RecentUpdate") is True:
                continue

            tombstone = retention_tombstones.get(project_id)
            old_hash = str((tombstone or {}).get("metadata_hash") or "").strip()
            tombstone_activity = _parse_iso_datetime((tombstone or {}).get("activity_at"))
            tombstone_still_outside_window = bool(
                tombstone_activity and tombstone_activity < normal_retention_cutoff
            )
            if (
                old_hash
                and listing_hash
                and old_hash == listing_hash
                and tombstone_still_outside_window
            ):
                continue

            # Creator-only expansion is intentionally allowed to surface an old
            # historical model once. It receives creator/discovery retention when
            # imported, then automatic cleanup writes a tombstone so an unchanged
            # project is not resurrected every creator-probe cycle. Public-lane
            # discoveries keep the normal source-date preflight.
            lanes = list(dict.fromkeys(discovery_lanes.get(project_id, [])))
            creator_only = bool(lanes) and all(str(lane).startswith("creator:") for lane in lanes)
            if creator_only:
                continue

            _created, _updated, activity = _listing_source_dates(item)
            if activity is None:
                retention_preflight_candidates[project_id] = item

    if retention_preflight_candidates and not scan_control.should_stop():
        workers = min(8, len(retention_preflight_candidates))
        print(f"\nTensorHub retention preflight")
        print(f"  Missing source date : {len(retention_preflight_candidates)}")
        print(f"  Probing             : {len(retention_preflight_candidates)} with {workers} workers")
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="tensorhub-retention") as executor:
            futures = {
                executor.submit(_retention_activity_probe, item): project_id
                for project_id, item in retention_preflight_candidates.items()
            }
            checked = 0
            for future in as_completed(futures):
                project_id = futures[future]
                try:
                    retention_preflight[project_id] = future.result()
                except Exception:
                    retention_preflight[project_id] = None
                checked += 1
                if checked == len(futures) or checked % 100 == 0:
                    print(f"  Progress            : {checked}/{len(futures)}")

    for project_id, item in unique_items.items():
        if scan_control.should_stop():
            break
        if not _matches_base_model(item, structured_base):
            architecture_rejected += 1
            continue
        if project_id in scan_seen_models:
            continue

        # Normal discovery must not re-import a model that retention already
        # removed. TensorHub's portal/list response frequently omits the same
        # authoritative source timestamp that model/detail later supplies, so a
        # date-only prefilter cannot reliably catch these rows before expensive
        # detail/media work. Retention tombstones solve that gap: cleanup stores
        # the deleted project ID + listing hash, and an unchanged project is
        # rejected immediately on later normal scans. If TensorHub changes the
        # listing hash, we allow one refresh so a genuinely updated model can be
        # reconsidered. Explicit Creator/Discovery scans bypass this normal path.
        current_listing_hash = _tensorhub_metadata_hash(item)
        tombstone = retention_tombstones.get(project_id) if project_id not in existing_cache else None
        tombstone_match = False
        if tombstone:
            old_hash = str(tombstone.get("metadata_hash") or "").strip()
            tombstone_match = bool(old_hash and current_listing_hash and old_hash == current_listing_hash)

        lanes = list(dict.fromkeys(discovery_lanes.get(project_id, [])))
        creator_only = bool(lanes) and all(str(lane).startswith("creator:") for lane in lanes)

        stats = item.get("statisticInfo") if isinstance(item.get("statisticInfo"), dict) else {}
        attach = stats.get("attachInfo") if isinstance(stats.get("attachInfo"), dict) else {}
        recent_nested_update = attach.get("RecentUpdate") is True

        _created, _updated, listing_activity = _listing_source_dates(item)
        probed_activity = retention_preflight.get(project_id)
        authoritative_activity = listing_activity or probed_activity
        outside_by_source_date = bool(
            authoritative_activity and authoritative_activity < normal_retention_cutoff
        )

        tombstone_activity = _parse_iso_datetime((tombstone or {}).get("activity_at"))
        tombstone_still_outside_window = bool(
            tombstone_activity and tombstone_activity < normal_retention_cutoff
        )
        tombstone_memory_block = bool(
            tombstone_match and (creator_only or tombstone_still_outside_window)
        )

        # Compatibility for tombstones written before creator-only retention began:
        # if the old tombstone stored the database metadata hash instead of the
        # TensorHub listing hash, matching source activity still proves this is the
        # same stale project. A genuinely newer source timestamp is allowed back in.
        tombstone_same_activity = False
        if creator_only and tombstone and not tombstone_match and authoritative_activity:
            old_activity = _parse_iso_datetime(tombstone.get("activity_at"))
            if old_activity:
                tombstone_same_activity = authoritative_activity <= old_activity

        # A creator-only hit gets one chance even when its source date is old.
        # Once creator/discovery retention removes it, the tombstone becomes the
        # gate. A changed listing hash/newer activity is allowed through again as
        # real upstream activity; unchanged retention memory stays suppressed.
        raw_retention_block = (
            tombstone_memory_block
            or tombstone_same_activity
            or (outside_by_source_date and not creator_only)
        )
        if normal_retention_enabled and project_id not in existing_cache and raw_retention_block and recent_nested_update:
            retention_recent_update_bypasses += 1
        retention_blocks = raw_retention_block and not recent_nested_update

        if normal_retention_enabled and project_id not in existing_cache and retention_blocks:
            retention_rejected += 1
            reason = "retention memory" if tombstone_memory_block else ("detail date preflight" if probed_activity and not listing_activity else "listing source date")
            if tombstone_memory_block: retention_memory_hits += 1
            elif probed_activity and not listing_activity: retention_preflight_rejects += 1
            else: retention_listing_rejects += 1
            activity_text = (
                authoritative_activity.isoformat()
                if authoritative_activity
                else str((tombstone or {}).get("activity_at") or "unknown")
            )
            if len(retention_rejected_samples) < 8:
                nested = item.get("model") if isinstance(item.get("model"), dict) else {}
                retention_rejected_samples.append({
                    "name": str(item.get("name") or nested.get("name") or project_id).strip(),
                    "activity": activity_text,
                    "reason": reason,
                })
            if not tombstone_memory_block:
                try:
                    import database
                    database.remember_retention_tombstone(
                        NAME,
                        project_id,
                        current_listing_hash,
                        activity_text,
                    )
                    retention_tombstones[project_id] = {
                        "metadata_hash": current_listing_hash,
                        "activity_at": activity_text,
                    }
                except Exception:
                    pass
            scan_seen_models.add(project_id)
            continue

        lane = ",".join(dict.fromkeys(discovery_lanes.get(project_id, [])))
        model = _build_model(item, discovery_lane=lane)
        if not model:
            continue

        # Automatic Expanded Creator Search may intentionally discover projects
        # far outside normal source retention. Give a newly imported creator-only
        # result the explicit-discovery clock once; do not refresh that clock on
        # later probes while the row still exists.
        if creator_only and project_id not in existing_cache:
            model.retention_mode = "creator_added"
            model.creator_discovered_at = datetime.now(timezone.utc).isoformat()

        cached = existing_cache.get(project_id)
        model = _apply_cached_detail(model, cached)
        fresh_th = (model.card_data or {}).get("tensorhub") or {}
        old_th = ((cached or {}).get("card_obj") or {}).get("tensorhub") or {}
        listing_changed = bool(cached and old_th.get("listing_hash") and old_th.get("listing_hash") != fresh_th.get("listing_hash"))
        already_enriched = bool(old_th.get("detail_enriched"))
        try:
            detail_version = int(old_th.get("detail_enrichment_version") or 0)
        except (TypeError, ValueError):
            detail_version = 0
        upgrade_needed = already_enriched and detail_version < DETAIL_ENRICHMENT_VERSION
        retry_due = _detail_retry_due(old_th)

        # Successful records are normally skipped. A detail-version bump is a
        # deliberate one-time backfill when AbyssBeacon learns richer TensorHub data.
        needs_detail = (
            (not cached)
            or listing_changed
            or upgrade_needed
            or ((not already_enriched) and retry_due)
        )
        if needs_detail:
            priority = 0 if not cached else (1 if listing_changed or upgrade_needed else 2)
            detail_candidates.append((priority, project_id, model, lane))
        scan_seen_models.add(project_id)
        models.append(model)

    # Summarize persistent detail state from the DB cache.
    cached_enriched = 0
    cached_failed_cooldown = 0
    for cached in existing_cache.values():
        old_th = (cached.get("card_obj") or {}).get("tensorhub") or {}
        if old_th.get("detail_enriched"):
            cached_enriched += 1
        elif not _detail_retry_due(old_th):
            cached_failed_cooldown += 1

    build_seconds = time.perf_counter() - build_started_at
    enrichment_started_at = time.perf_counter()
    enriched = 0
    failed = 0
    print("\nTensorHub detail refresh")
    print(f"  Cached complete : {cached_enriched}")
    print(f"  Retry cooldown  : {cached_failed_cooldown}")
    print(f"  Needs detail    : {len(detail_candidates)}")

    if detail_candidates and not scan_control.should_stop():
        chosen = sorted(detail_candidates, key=lambda entry: entry[0])
        model_by_id = {str(model.model_key or ""): model for model in models}
        workers = min(DETAIL_WORKERS, len(chosen))
        print(f"  Processing       : {len(chosen)} with {workers} workers")
        failures = []
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="tensorhub-detail") as executor:
            futures = {
                executor.submit(_fetch_public_detail, model, lane): (project_id, model, lane)
                for _, project_id, model, lane in chosen
            }
            for future in as_completed(futures):
                project_id, original, lane = futures[future]
                if scan_control.should_stop():
                    break
                try:
                    enriched_model, ok, reason = future.result()
                except Exception as exc:
                    enriched_model, ok, reason = original, False, f"{type(exc).__name__}: {exc}"
                model_by_id[project_id] = enriched_model
                if ok:
                    enriched += 1
                else:
                    failures.append((project_id, enriched_model, lane, reason))
                    print(f"TensorHub detail FAILED {project_id}: {reason}")

        recovered, failed = _retry_detail_failures(
            failures,
            model_by_id,
            "TensorHub detail",
        )
        enriched += recovered

        models = [model_by_id.get(str(model.model_key or ""), model) for model in models]
        print(f"  Detailed this run: {enriched}")
        print(f"  Failed this run  : {failed}")

    enrichment_seconds = time.perf_counter() - enrichment_started_at
    total_seconds = time.perf_counter() - scan_started_at

    # Verbose-only diagnostics retained for future scanner audits.
    print("\nTensorHub timing")
    print(f"  Public discovery : {public_discovery_seconds:.2f}s")
    if creator_expansion_enabled:
        print(f"  Creator expansion: {creator_seconds:.2f}s ({creator_checked} checked, {creator_skipped_cooldown} cooldown skipped)")
    else:
        print("  Creator expansion: disabled")
    print(f"  Build/cache work : {build_seconds:.2f}s")
    print(f"  Model details    : {enrichment_seconds:.2f}s")
    print(f"  Scanner total    : {total_seconds:.2f}s")
    if architecture_rejected:
        print(f"  Architecture rejected: {architecture_rejected}")
    if retention_rejected:
        print(f"  Retention rejected   : {retention_rejected} old new/reintroduced project(s) (> {normal_retention_days} day window)")
        print(f"    Memory hits        : {retention_memory_hits}")
        print(f"    Preflight rejects  : {retention_preflight_rejects}")
        if retention_listing_rejects:
            print(f"    Listing-date rejects: {retention_listing_rejects}")
        if retention_recent_update_bypasses:
            print(f"    RecentUpdate kept  : {retention_recent_update_bypasses}")
        print("  Rejected sample      :")
        for sample in retention_rejected_samples:
            print(f"    {sample['name']} | source activity={sample['activity']} | via={sample.get('reason','retention')}")
    print(f"TensorHub: {len(models)} unique {structured_base} project(s) processed")
    return models
