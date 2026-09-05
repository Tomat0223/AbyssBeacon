from scan_logging import verbose_print as print
import builtins
NAME = "modelscope"
DISPLAY = "ModelScope"
ENABLED = True

# Developer-only scanner diagnostics. Keep False for normal AbyssBeacon use.
DEBUG_SCANNERS = False

def debug_print(*args, **kwargs):
    if DEBUG_SCANNERS:
        print(*args, **kwargs)

import requests
import json
from scanners.http_retry import get_with_backoff
import database
import time
import threading
import uuid
import urllib.parse

from datetime import datetime, timedelta, timezone

from scanners.common import processors
from scanners.common.repository_classifier import needs_repository_classification_refresh

import scan_control
import scan_status
from secrets_manager import get_source_token

MODELSCOPE_LIBRARY_REFRESH_KEY = "modelscope_library_refresh"
MODELSCOPE_LIBRARY_REFRESH_VERSION = 1


def _json_mapping(value):
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value or "{}")
            return dict(decoded) if isinstance(decoded, dict) else {}
        except Exception:
            return {}
    return {}


def library_refresh_marker(
    card_data,
    *,
    status="complete",
    reason="",
    files_checked=True,
    media_checked=True,
    tags_checked=True,
):
    """Mark a ModelScope snapshot as checked by the current metadata pass."""
    card = _json_mapping(card_data)
    card[MODELSCOPE_LIBRARY_REFRESH_KEY] = {
        "version": MODELSCOPE_LIBRARY_REFRESH_VERSION,
        "status": str(status or "complete"),
        "files_checked": bool(files_checked),
        "media_checked": bool(media_checked),
        "tags_checked": bool(tags_checked),
        "reason": str(reason or ""),
    }
    return card


def stored_library_refresh_version(value):
    card = _json_mapping(value)
    marker = card.get(MODELSCOPE_LIBRARY_REFRESH_KEY)
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



_thread_local = threading.local()


def _get_session():
    """Return one requests.Session per worker thread.

    ModelScope v2 can run several aliases concurrently. requests.Session is not
    documented as thread-safe, so each alias worker gets its own connection
    pool and auth/header state.
    """
    current = getattr(_thread_local, "session", None)
    if current is None:
        current = requests.Session()
        current.headers.update({
            "User-Agent": "AbyssBeacon/1.0",
            "Accept": "application/json",
        })
        _thread_local.session = current
    return current


def _apply_auth():
    current = _get_session()
    token = get_source_token("modelscope")
    if token:
        current.headers["Authorization"] = f"Bearer {token}"
    else:
        current.headers.pop("Authorization", None)
    return current



API = "https://modelscope.cn/openapi/v1/models"


def normalize_timestamp(value):
    """Normalize ModelScope timestamps to ISO-8601 UTC for SQLite sorting."""

    if value is None or value == "":
        return ""

    try:
        if isinstance(value, (int, float)):
            number = float(value)
        else:
            text = str(value).strip()
            if not text:
                return ""
            try:
                number = float(text)
            except ValueError:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                else:
                    parsed = parsed.astimezone(timezone.utc)
                return parsed.isoformat().replace("+00:00", "Z")

        # ModelScope may return Unix seconds or milliseconds.
        if abs(number) > 100000000000:
            number /= 1000.0
        parsed = datetime.fromtimestamp(number, timezone.utc)
        return parsed.isoformat().replace("+00:00", "Z")
    except Exception:
        return str(value).strip()

def timestamp_to_datetime(value):
    """Convert ISO or Unix second/millisecond timestamps to naive UTC."""

    if value is None or value == "":
        return None

    try:
        if isinstance(value, (int, float)):
            number = float(value)
        else:
            text = str(value).strip()
            if not text:
                return None
            try:
                number = float(text)
            except ValueError:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
                return parsed.replace(tzinfo=None)

        # ModelScope responses can use Unix milliseconds or seconds.
        if abs(number) > 100000000000:
            number /= 1000.0

        return datetime.utcfromtimestamp(number)

    except Exception:
        return None




def _payload_values(payload, keys):
    if not isinstance(payload, dict):
        return []

    values = []
    for key in keys:
        if key in payload:
            values.append(payload.get(key))

    # Be defensive about wrappers returned by ModelScope endpoints.
    for nested_key in ("Data", "data", "model", "Model"):
        nested = payload.get(nested_key)
        if isinstance(nested, dict):
            for key in keys:
                if key in nested:
                    values.append(nested.get(key))

    return values


def gated_signal(item=None, details=None):
    """Return True/False when ModelScope exposes a gated signal, else None."""

    payloads = [details or {}, item or {}]

    for payload in payloads:
        for protected in _payload_values(
            payload,
            ("ProtectedMode", "protected_mode", "protectedMode")
        ):
            if isinstance(protected, bool):
                return protected

            text = str(protected).strip().lower()
            if text in {"true", "yes", "gated", "on"}:
                return True
            if text in {"false", "no", "off"}:
                return False

            try:
                # Current ModelScope API payloads use 1 for gated and 2 for off.
                number = int(float(text))
                if number == 1:
                    return True
                if number == 2:
                    return False
            except (TypeError, ValueError):
                pass

        for value in _payload_values(
            payload,
            ("gated", "Gated", "is_gated", "IsGated")
        ):
            if isinstance(value, bool):
                return value
            text = str(value).strip().lower()
            if text in {"1", "true", "yes", "gated"}:
                return True
            if text in {"0", "false", "no", "off"}:
                return False

    return None


def model_is_private(item=None, details=None):
    for payload in (details or {}, item or {}):
        for value in _payload_values(
            payload,
            ("Visibility", "visibility", "ModelVisibility")
        ):
            text = str(value).strip().lower()
            if text in {"1", "private"}:
                return True
            if text in {"5", "public"}:
                return False
    return None


def detect_gated_model(item=None, details=None):
    return gated_signal(item, details) is True

def get_preview_image(model_id):

    try:

        url = f"https://modelscope.cn/api/v1/models/{model_id}"

        r = get_with_backoff(
            _get_session(), url, provider="ModelScope",
            label=f"preview {model_id}", timeout=10
        )

        if r.status_code != 200:
            return ""

        data = r.json()

        if not isinstance(data, dict):
            return ""


        model = data.get("Data")

        if not isinstance(model, dict):
            return ""


        #
        # Try cover images first
        #
        muse = model.get("MuseInfo")

        if isinstance(muse, dict):

            versions = muse.get("versions")

            if isinstance(versions, list) and versions:

                version = versions[0]

                if isinstance(version, dict):

                    covers = version.get("coverImages")

                    if isinstance(covers, list) and covers:

                        cover = covers[0]

                        if isinstance(cover, dict):

                            image = cover.get("url")

                            if image:
                                return image


        #
        # Fallback to avatar
        #
        avatar = model.get("Avatar")

        if avatar:
            return avatar


    except Exception as e:

        print(
            "Preview error:",
            model_id,
            e
        )


    return ""


def _media_type_from_value(url, declared_type=""):

    text = str(declared_type or "").strip().lower()

    if "video" in text:
        return "video"

    if "image" in text:
        return "image"

    clean = str(url or "").lower().split("?", 1)[0].split("#", 1)[0]

    video_extensions = (
        ".mp4", ".webm", ".mov", ".m4v", ".avi", ".mkv"
    )

    if clean.endswith(video_extensions):
        return "video"

    return "image"



def extract_versions_from_details(details, model_id):
    """Return real ModelScope AIGC versions plus version-scoped files."""
    if not isinstance(details, dict):
        return [], []
    muse = details.get("MuseInfo")
    if not isinstance(muse, dict):
        return [], []
    raw_versions = muse.get("versions")
    if not isinstance(raw_versions, list) or not raw_versions:
        return [], []

    versions, files = [], []
    for raw in raw_versions:
        if not isinstance(raw, dict):
            continue
        mv = raw.get("modelVersion") if isinstance(raw.get("modelVersion"), dict) else {}
        revision = str(mv.get("versionName") or mv.get("revision") or raw.get("versionName") or "").strip()
        label = str(mv.get("showName") or mv.get("name") or raw.get("showName") or revision or "Version").strip()
        if not revision:
            continue

        stats = raw.get("stats") if isinstance(raw.get("stats"), dict) else {}
        if not stats:
            raw_stats = mv.get("stats")
            if isinstance(raw_stats, str):
                try: stats = json.loads(raw_stats or "{}")
                except Exception: stats = {}
            elif isinstance(raw_stats, dict):
                stats = dict(raw_stats)

        file_list = stats.get("fileList") or []
        file_sizes = stats.get("fileSizes") or []
        if not isinstance(file_list, list): file_list=[]
        if not isinstance(file_sizes, list): file_sizes=[]
        version_files=[]
        for idx, filename in enumerate(file_list):
            path=str(filename or "").strip()
            if not path: continue
            try: size=int(file_sizes[idx] if idx < len(file_sizes) else 0)
            except Exception: size=0
            primary=path.casefold().endswith((".safetensors",".ckpt",".pt",".pth",".bin",".gguf"))
            fd={
                "name":path.rsplit("/",1)[-1], "path":path,
                "size":size, "size_bytes":size, "sha256":"", "is_lfs":False,
                "revision":revision, "version_id":revision, "version":label,
                "download_url":f"https://modelscope.cn/models/{model_id}/resolve/{revision}/{path}",
                "primary":primary,
            }
            files.append(fd); version_files.append(dict(fd))

        versions.append({
            "id":revision, "name":label, "revision":revision,
            "files":version_files,
            "uploaded_at":normalize_timestamp(mv.get("gmtCreate") or mv.get("gmtModified") or ""),
            "access_status":"downloadable" if version_files else "unconfirmed",
        })
    return versions, files


def extract_media_from_details(details, model_id):

    if scan_control.should_stop():
        print("MEDIA STOP REQUESTED")
        return []

    debug_print("START MEDIA:", model_id)

    media = []
    seen_urls = set()

    def add_media(url, declared_type="", thumbnail=""):

        if not url:
            return

        url = clean_media_url(url)

        if not url or url in seen_urls:
            return

        thumbnail = clean_media_url(thumbnail) if thumbnail else ""
        media_type = _media_type_from_value(url, declared_type)

        # A video URL must never be used as its own image thumbnail.
        # If ModelScope does not provide a poster, the UI falls back to
        # static/images/video_preview.png on the card.
        if media_type == "video" and thumbnail == url:
            thumbnail = ""

        seen_urls.add(url)

        clean_path = url.split("?", 1)[0].rstrip("/")
        filename = clean_path.rsplit("/", 1)[-1] if "/" in clean_path else clean_path
        media.append({
            "type": media_type,
            "url": url,
            "thumbnail": thumbnail if thumbnail else (url if media_type == "image" else ""),
            "filename": filename,
            "path": filename,
            "metadata": {"filename": filename},
            "position": len(media)
        })

    def add_cover(cover, version_name="", version_id=""):

        if isinstance(cover, str):
            before = len(media)
            add_media(cover)
            if len(media) > before and (version_name or version_id):
                meta = dict(media[-1].get("metadata") or {})
                meta.update({"modelscope_version_name": version_name, "modelscope_version_id": version_id, "model_version": version_name, "model_version_id": version_id})
                media[-1]["metadata"] = meta
            return

        if not isinstance(cover, dict):
            return

        # Prefer an explicit video field before generic/image fields. Some
        # ModelScope AIGC entries use CDN URLs without a .mp4 suffix, so the
        # field name itself is an important media-type signal.
        explicit_video_url = (
            cover.get("VideoUrl")
            or cover.get("videoUrl")
            or cover.get("video")
            or cover.get("Video")
        )

        url = (
            explicit_video_url
            or cover.get("url")
            or cover.get("Url")
            or cover.get("CoverUrl")
            or cover.get("coverUrl")
            or cover.get("ImageUrl")
            or cover.get("imageUrl")
            or cover.get("src")
        )

        declared_type = (
            cover.get("type")
            or cover.get("Type")
            or cover.get("mediaType")
            or cover.get("MediaType")
            or cover.get("mimeType")
            or cover.get("MimeType")
            or ("video" if explicit_video_url else "")
        )

        # Additional video-only metadata is also a useful signal when the
        # media URL itself has no extension.
        if not declared_type and any(
            key in cover
            for key in (
                "duration", "Duration", "videoDuration", "VideoDuration",
                "poster", "Poster", "posterUrl", "PosterUrl"
            )
        ):
            declared_type = "video"

        thumbnail = (
            cover.get("thumbnail")
            or cover.get("Thumbnail")
            or cover.get("poster")
            or cover.get("Poster")
            or cover.get("posterUrl")
            or cover.get("PosterUrl")
            or cover.get("cover")
            or ""
        )

        before = len(media)
        add_media(url, declared_type, thumbnail)
        if len(media) > before and isinstance(cover, dict):
            useful = {}
            for key, value in cover.items():
                if value not in (None, "", [], {}):
                    useful[str(key)] = value
            if version_name or version_id:
                useful.update({"modelscope_version_name": version_name, "modelscope_version_id": version_id, "model_version": version_name, "model_version_id": version_id})
            media[-1]["metadata"] = useful

    if not isinstance(details, dict):
        return media

    # MuseInfo gallery media. ModelScope can place both images and videos
    # in coverImages, so do not assume every entry is an image.
    muse = details.get("MuseInfo")

    if isinstance(muse, dict):
        versions = muse.get("versions")

        if isinstance(versions, list):
            for version in versions:
                if not isinstance(version, dict):
                    continue
                mv = version.get("modelVersion") if isinstance(version.get("modelVersion"), dict) else {}
                version_name = str(mv.get("showName") or mv.get("name") or version.get("showName") or "").strip()
                version_id = str(mv.get("versionName") or mv.get("revision") or version.get("versionName") or "").strip()
                covers = version.get("coverImages")
                if isinstance(covers, list):
                    for cover in covers:
                        add_cover(cover, version_name, version_id)

    # ModelScope normal cover media.
    covers = details.get("CoverImages")

    if isinstance(covers, list):
        for cover in covers:
            add_cover(cover)

    debug_print("MEDIA FOUND:", model_id, len(media))

    return media


def extract_media_from_files(files, model_id):

    media = []
    position = 0

    for file in files:

        if isinstance(file, dict):
            filename = (file.get("path") or file.get("name") or "")
            revision = file.get("revision") or "master"
        else:
            filename = file
            revision = "master"

        lower = str(filename).lower()

        debug_print("CHECKING FILE FOR MEDIA:", model_id, filename)

        image_extensions = (".png", ".jpg", ".jpeg", ".webp", ".gif")
        video_extensions = (".mp4", ".webm", ".mov", ".m4v", ".avi", ".mkv")

        media_type = None

        if lower.endswith(image_extensions):
            media_type = "image"
        elif lower.endswith(video_extensions):
            media_type = "video"

        if media_type:
            url = (
                "https://modelscope.cn/models/"
                + model_id
                + "/resolve/"
                + revision
                + "/"
                + filename
            )

            debug_print("FILE MEDIA FOUND:", model_id, filename, media_type)

            media.append({
                "type": media_type,
                "url": url,
                "thumbnail": url if media_type == "image" else "",
                "filename": str(filename).rsplit("/", 1)[-1],
                "path": str(filename),
                "metadata": {
                    "filename": str(filename).rsplit("/", 1)[-1],
                    "path": str(filename)
                },
                "position": position
            })

            position += 1

    return media


def clean_media_url(url):

    if not url:
        return ""

    url = str(url).strip()

    # Remove Markdown image/link wrappers.
    #
    # Example:
    # [https://example.com/image.png](https://example.com/image.png)
    #
    # becomes:
    # https://example.com/image.png

    if "](" in url and url.endswith(")"):
        url = url.split("](", 1)[1][:-1]

    # Handle Markdown image syntax:
    #
    # ![preview](https://example.com/image.png)

    if url.startswith("![") and "](" in url and url.endswith(")"):
        url = url.split("](", 1)[1][:-1]

    # Remove simple surrounding brackets.
    if url.startswith("[") and url.endswith("]"):
        url = url[1:-1].strip()

    # Fix known ModelScope typo.
    url = url.replace(
        "resouces.modelscope.cn",
        "resources.modelscope.cn"
    )

    return url.strip()


def get_files(model_id, revision):
    """
    Get ALL repository files from ModelScope.

    The download UI decides which files are shown normally
    and which are hidden under "Show All Downloads".
    """

    if scan_control.should_stop():
        print("FILE REQUEST STOPPED:", model_id)
        return []

    debug_print("START FILE REQUEST:", model_id)

    url = (
        f"https://modelscope.cn/api/v1/models/"
        f"{model_id}/repo/files"
    )

    try:

        response = get_with_backoff(
            _get_session(), url, provider="ModelScope",
            label=f"files {model_id}",
            params={
                "Revision": revision,
                "Root": ""
            },
            timeout=15
        )

        if response.status_code != 200:

            # Some valid ModelScope repositories do not expose this legacy
            # repo-files route. A 404 simply means file enumeration is not
            # available through this path; detail/gallery media can still be
            # used normally. Keep it visible only while developer debugging.
            if response.status_code == 404:
                debug_print(
                    "ModelScope files unavailable:",
                    model_id,
                    response.status_code
                )
            else:
                print(
                    "ModelScope files request failed:",
                    model_id,
                    response.status_code
                )

            return []


        data = response.json()

        debug_print(
            "MODELSCOPE DETAIL KEYS:",
            data.keys()
        )

        debug_print(
            "COVER IMAGES DEBUG:",
            model_id,
            data.get("CoverImages"),
            data.get("Avatar")
        )

        debug_print(
            "WIDGET DEBUG:",
            model_id,
            data.get("widgets")
        )

        debug_print(
            "MODELSCOPE FILE RESPONSE:",
            model_id
        )

        if not isinstance(data, dict):
            return []


        data_section = data.get(
            "Data",
            {}
        )

        if not isinstance(data_section, dict):
            return []


        debug_print(
            "FILES RESPONSE DATA:",
            data_section
        )


        repo_files = data_section.get(
            "Files",
            []
        )

        if not isinstance(repo_files, list):
            return []


        debug_print(
            "ModelScope files found:",
            model_id,
            len(repo_files)
        )


        files = []


        for item in repo_files:

            if scan_control.should_stop():

                print(
                    "FILE REQUEST STOPPED:",
                    model_id
                )

                break


            if not isinstance(item, dict):
                continue


            path = item.get(
                "Path",
                ""
            )

            if not path:
                continue


            name = item.get(
                "Name",
                path
            )


            lower_path = path.lower()


            # Files normally shown in the download panel.
            primary_file = lower_path.endswith(
                (
                    ".safetensors",
                    ".ckpt",
                    ".pt",
                    ".pth",
                    ".bin",
                    ".gguf"
                )
            )


            file_revision = item.get(
                "Revision",
                revision
            )


            download_url = (
                f"https://modelscope.cn/models/"
                f"{model_id}/resolve/"
                f"{file_revision}/"
                f"{path}"
            )


            files.append({

                "name": name,

                "path": path,

                "size":
                    item.get(
                        "Size",
                        0
                    ),

                "size_bytes":
                    item.get(
                        "Size",
                        0
                    ),

                "sha256":
                    item.get(
                        "Sha256",
                        ""
                    ),

                "is_lfs":
                    item.get(
                        "IsLFS",
                        False
                    ),

                "revision":
                    file_revision,

                "download_url":
                    download_url,

                "primary":
                    primary_file

            })


        debug_print(
            "ModelScope ALL files found:",
            model_id,
            len(files)
        )


        debug_print(
            "ModelScope primary files:",
            model_id,
            sum(
                1
                for file in files
                if file["primary"]
            )
        )


        debug_print(
            "ModelScope FILE SAMPLE:",
            model_id,
            files[:5]
        )


        image_files = [
            file["path"]
            for file in files
            if any(
                ext in file["path"].lower()
                for ext in (
                    ".png",
                    ".jpg",
                    ".jpeg",
                    ".webp",
                    ".gif"
                )
            )
        ]

        debug_print(
            "ModelScope IMAGE-LIKE FILES:",
            model_id,
            image_files
        )


        return files


    except Exception as e:

        print(
            "ModelScope files error:",
            model_id,
            e
        )

        return []


def scan(
    term,
    scan_seen_models=None,
    scan_settings=None,
    creator=None
):
    _apply_auth()

    scan_settings = scan_settings or {}
    progress_callback = scan_settings.get("_progress_callback")

    def _progress(current, total, stage="Scanning models", finalize=False):
        if callable(progress_callback):
            try:
                progress_callback(current, total, stage, finalize)
            except Exception:
                pass

    start_time = time.perf_counter()

    detail_fetches = 0
    file_fetches = 0
    processed_models = 0
    duplicates = 0
    old_models = 0
    media_files = 0

    SEARCH_DAYS = int(scan_settings.get("search_days", 7))
    # Users choose a total result ceiling; AbyssBeacon owns pagination.
    # ModelScope hard-limits each request to 50 results and its pagination
    # window to 3000 results.
    PAGE_SIZE = 50
    MAX_RESULTS = min(3000, max(1, int(scan_settings.get("max_results", 50))))
    SORT_MODE = scan_settings.get("sort", "newest_updated")

    sort_map = {
        "newest_updated": "last_modified",
        "downloads": "downloads",
        "likes": "likes",
        "default": "default"
    }
    api_sort = sort_map.get(SORT_MODE, "last_modified")

    max_page = 3000 // PAGE_SIZE
    target_results = 3000 if creator else MAX_RESULTS

    print("ModelScope creator:" if creator else "ModelScope search:", creator or term)
    if creator:
        print(f"ModelScope pagination: automatic, up to source end ({PAGE_SIZE} per request)")
    else:
        print(f"ModelScope maximum results: {MAX_RESULTS} ({PAGE_SIZE} per request, automatic pagination)")

    cutoff = datetime.utcnow() - timedelta(days=SEARCH_DAYS)


    results = []
    models = []

    if not creator:
        _progress(0, target_results, "Finding models")

    for page_number in range(1, max_page + 1):

        if scan_control.should_stop():
            print("SCAN STOP REQUESTED")
            break

        try:

            r = get_with_backoff(
                _get_session(), API, provider="ModelScope",
                label=f"search {creator or term} page {page_number}",
                params={
                    **({"owner": creator} if creator else {"search": term}),
                    "sort": api_sort,
                    "page_number": page_number,
                    "page_size": PAGE_SIZE
                },
                timeout=20
            )

        except Exception as e:

            print(
                "ModelScope request failed:",
                e
            )
            break

        print(
            f"ModelScope page {page_number} status:",
            r.status_code
        )

        if r.status_code != 200:
            print(r.text[:500])
            break

        try:
            data = r.json()
        except Exception:
            print("ModelScope returned non JSON")
            print(r.text[:500])
            break

        page_models = []

        if isinstance(data, list):
            page_models = data
        elif isinstance(data.get("data"), list):
            page_models = data["data"]
        elif isinstance(data.get("data"), dict):
            page_models = (
                data["data"].get("models")
                or data["data"].get("Models")
                or []
            )

        if not isinstance(page_models, list):
            page_models = []

        page_count = len(page_models)
        print(f"ModelScope page {page_number}: {page_count} results")

        remaining = target_results - len(models)
        if remaining <= 0:
            break
        models.extend(page_models[:remaining])
        if not creator:
            _progress(min(len(models), target_results), target_results, "Finding models")

        if len(models) >= target_results:
            break

        # A short page means the source has been exhausted. Creator scans keep
        # the historical 45-result tolerance so 46-49 visible entries still
        # trigger a follow-up page when ModelScope has more results available.
        if creator:
            if page_count < min(45, PAGE_SIZE):
                break
        elif page_count < PAGE_SIZE:
            break


    if not creator and not scan_control.should_stop():
        found_total = len(models)
        _progress(found_total, found_total if found_total < target_results else target_results, "Finding models", True)

    print(f"ModelScope results inspected: {len(models)}")

    if not creator and models:
        _progress(0, len(models), "Checking models")

    if models:
        debug_print("FIRST RESULT ID:")
        debug_print(models[0].get("id"))


    for item_index, item in enumerate(models, start=1):

        if not creator:
            _progress(item_index - 1, len(models), "Checking models")

        if scan_control.should_stop():
            print("SCAN STOP REQUESTED")
            break

        try:

            if not isinstance(item, dict):
                continue


            model_id = (
                item.get("id")
                or ""
            )

            if not model_id:
                continue


            if scan_seen_models is not None:

                seen_key = (
                    "modelscope",
                    model_id.lower()
                )

                if seen_key in scan_seen_models:
                    continue

                scan_seen_models.add(seen_key)


            scan_status.update_status(
                status="running",
                source="modelscope",
                current=model_id,
            )


            name = (
                item.get("display_name")
                or model_id.split("/")[-1]
            )


            if not name:

                continue


            author = ""

            if "/" in model_id:

                author = model_id.split("/")[0]

            blocked = {str(x).casefold() for x in (scan_settings.get("_blocked_creators") or [])}
            if author and author.casefold() in blocked:
                continue

            description = (
                item.get("description")
                or ""
            )


            tags = item.get(
                "tags",
                []
            )


            if not isinstance(tags, list):

                tags = []


            text = (
                model_id
                + " "
                + name
                + " "
                + description
                + " "
                + " ".join(tags)
                + " "
                + str(item.get("tasks", ""))
            )


            updated = (
                item.get("last_modified")
                or item.get("LastUpdatedTime")
                or ""
            )

            # Keep the search timestamp in exactly the same format
            # that is stored in SQLite.  SQLite's TEXT column returns
            # strings, so comparing an int to a string made every
            # unchanged ModelScope model look updated.
            updated = normalize_timestamp(updated)
            listing_updated = updated


            created = (
                item.get("created_at")
                or ""
            )

            # CHECK DATABASE BEFORE AGE CLASSIFICATION.
            # Existing unchanged models are duplicates even after they age past
            # the configured discovery window.  "Old" is reserved for models
            # we have not already stored.

            model_key = model_id.lower()

            # Prefer ModelScope's preserved source snapshot. On merged cards
            # another provider may own the canonical models row, while the
            # ModelScope state still lives in model_sources.
            existing_source = database.get_model_source_snapshot(
                "modelscope",
                model_key
            )

            existing_model = None
            if not existing_source:
                existing_model = database.get_model(
                    model_key,
                    "modelscope"
                )

            db_updated = ""
            stored_listing_updated = ""
            download_metadata_checked = False

            if existing_source:
                db_updated = str(existing_source.get("updated") or "")
                stored_card = existing_source.get("card_data") or {}
                if isinstance(stored_card, str):
                    try:
                        stored_card = json.loads(stored_card)
                    except Exception:
                        stored_card = {}
                if isinstance(stored_card, dict):
                    stored_ms = stored_card.get("modelscope") or {}
                    if isinstance(stored_ms, dict):
                        stored_listing_updated = str(
                            stored_ms.get("listing_updated") or ""
                        )
                        download_metadata_checked = bool(
                            stored_ms.get("download_metadata_checked")
                        )
            elif existing_model:
                db_updated = str(existing_model["updated"] or "")
                stored_card = existing_model["card_data"] or ""
                if isinstance(stored_card, str):
                    try:
                        stored_card = json.loads(stored_card)
                    except Exception:
                        stored_card = {}
                if isinstance(stored_card, dict):
                    stored_ms = stored_card.get("modelscope") or {}
                    if isinstance(stored_ms, dict):
                        stored_listing_updated = str(
                            stored_ms.get("listing_updated") or ""
                        )
                        download_metadata_checked = bool(
                            stored_ms.get("download_metadata_checked")
                        )

            compare_updated = stored_listing_updated or db_updated

            debug_print(
                "UPDATE CHECK:",
                model_id,
                "API:",
                repr(listing_updated),
                "DB:",
                repr(compare_updated or None)
            )

            if existing_source or existing_model:

                if listing_updated == compare_updated:

                    # Refresh access metadata cheaply from the listing. If a
                    # merged source snapshot disagrees with the listing, let
                    # the normal detail/build path refresh the whole snapshot
                    # instead of skipping it.
                    gate = gated_signal(item, None)
                    access_details = None

                    if gate is None and model_is_private(item, None) is True:
                        detail_fetches += 1
                        access_details = get_details(model_id)
                        gate = gated_signal(item, access_details)

                    stored_gate = None
                    if existing_source and existing_source.get("gated") is not None:
                        stored_gate = bool(existing_source.get("gated"))
                    elif existing_model:
                        stored_gate = bool(existing_model["gated"])

                    gate_changed = (
                        gate is not None
                        and stored_gate is not None
                        and gate != stored_gate
                    )

                    if gate_changed and existing_model:
                        # Canonical ModelScope rows can still use the lightweight
                        # gated-status update.
                        database.update_gated_status(
                            model_key,
                            "modelscope",
                            gate
                        )
                        print(
                            "ModelScope gated status updated:",
                            model_id,
                            gate
                        )
                        gate_changed = False

                    stored_files = (
                        existing_source.get("files")
                        if existing_source
                        else existing_model["files"]
                    ) or ""

                    if isinstance(stored_files, str):
                        try:
                            stored_files = json.loads(stored_files)
                        except Exception:
                            stored_files = []
                    if isinstance(stored_files, dict):
                        stored_files = list(stored_files.values())

                    has_stored_files = bool(
                        isinstance(stored_files, list) and stored_files
                    )
                    needs_download_metadata = (
                        not has_stored_files
                        and not download_metadata_checked
                    )
                    classification_card = (
                        existing_source.get("card_data")
                        if existing_source
                        else existing_model["card_data"]
                    )
                    needs_repository_classification = needs_repository_classification_refresh(
                        classification_card,
                        source="modelscope",
                    )

                    if not needs_download_metadata and not gate_changed and not needs_repository_classification:
                        duplicates += 1
                        debug_print("ModelScope unchanged:", model_id)
                        continue

                    if needs_repository_classification:
                        debug_print(
                            "ModelScope repository classification upgrade; forcing one detail/file refresh:",
                            model_id
                        )

                    if needs_download_metadata:
                        debug_print(
                            "ModelScope unchanged but missing download metadata; "
                            "forcing one detail/file refresh:",
                            model_id
                        )
                    elif gate_changed:
                        debug_print(
                            "ModelScope access changed on merged source; "
                            "forcing source snapshot refresh:",
                            model_id
                        )

            else:
                # Search Days applies to discovery of models not yet stored.
                # For newest-updated searches use last_modified; otherwise use
                # created_at when available.
                cutoff_value = updated if SORT_MODE == "newest_updated" else created
                model_date = timestamp_to_datetime(cutoff_value)

                if model_date and model_date < cutoff and not creator:
                    old_models += 1
                    continue


            # FETCH MEDIA ONLY FOR NEW OR CHANGED MODELS

            detail_fetches += 1

            details = get_details(model_id)

            # The listing often contains only a small tag subset. Preserve its
            # ordering, then append current detail-page tags for Collection and
            # normal model views.
            seen_tags = {str(value or "").strip().casefold() for value in tags if str(value or "").strip()}
            for value in _modelscope_tag_values(details):
                identity = value.casefold()
                if identity not in seen_tags:
                    seen_tags.add(identity)
                    tags.append(value)

            # Search listings often omit the prose shown on the model page.
            # Prefer description fields from the richer detail response.
            detail_description = (
                details.get("description")
                or details.get("Description")
                or details.get("ModelDescription")
                or details.get("model_description")
                or details.get("README")
                or details.get("Readme")
                or details.get("readme")
                or ""
            )
            if detail_description:
                description = str(detail_description)

            if details.get("LastUpdatedTime"):
                updated = normalize_timestamp(
                    details["LastUpdatedTime"]
                )

            debug_print(
                "MODELSCOPE DETAIL KEYS:",
                details.keys()
            )

            debug_print(
                "MODELSCOPE DETAIL VERSION DATA:",
                details.get("Revision"),
                details.get("revision"),
                details.get("Version"),
                details.get("version")
            )

            model_media = extract_media_from_details(
                details,
                model_id
            )


            versions_meta, version_files = extract_versions_from_details(details, model_id)

            revision = (
                details.get("Revision")
                or "master"
            )

            if version_files:
                model_files = version_files
            else:
                file_fetches += 1
                model_files = get_files(model_id, revision)

            file_media = extract_media_from_files(
                model_files,
                model_id
            )

            debug_print("FILE MEDIA FOUND:", model_id, len(file_media))


            #
            # Combine API media and repository media.
            # Avoid duplicates while preserving API media first.
            #
            existing_urls = {
                item.get("url")
                for item in model_media
                if item.get("url")
            }

            for item in file_media:

                url = item.get("url")

                if not url:
                    continue

                if url in existing_urls:
                    continue

                item["position"] = len(model_media)

                model_media.append(item)

                existing_urls.add(url)


            debug_print("TOTAL MODELSCOPE MEDIA:", model_id, len(model_media))


            has_media = 1 if model_media else 0

            raw_card_data = library_refresh_marker({
                "versions": versions_meta,
                "modelscope": {
                    "listing_updated": listing_updated,
                    "official_tags": details.get("OfficialTags") or [],
                    "all_tags": tags,
                    "versions": versions_meta,
                    "download_metadata_checked": True,
                },
            })

            raw_model = {

                "model_id": model_id,

                "model_key": model_id.lower(),

                "details": details,

                "created": created,

                "updated": updated,

                "tags": ",".join(tags),

                "files": model_files,

                "source": "modelscope",

                "url":
                    "https://modelscope.cn/models/" + model_id,

                # Cards expect image to be an actual image URL. If the
                # model only has video previews, leave this blank so the
                # existing video_preview.png placeholder is used.
                "image":
                    next((
                        item.get("thumbnail") or item.get("url")
                        for item in model_media
                        if item.get("type") == "image"
                    ), ""),

                "media":
                    model_media,

                "has_media":
                    has_media,

                "has_video":
                    any(
                        item.get("type") == "video"
                        for item in model_media
                    ),

                "preview_count":
                    len(model_media),

                "gated":
                    detect_gated_model(item, details),

                "card_data": raw_card_data,

                "sensitive":
                    False,

            }


            processed_model = processors.build_model(
                raw_model
            )

            if str(getattr(processed_model, "architecture", "") or "").casefold() == "other":
                processed_model.architecture = processors.classify_architecture_with_watch_fallback(
                    scan_settings.get("_watch_architecture"),
                    getattr(processed_model, "base_model", ""),
                    getattr(processed_model, "name", ""),
                    getattr(processed_model, "display_name", ""),
                    getattr(processed_model, "tags", ""),
                    getattr(processed_model, "description", ""),
                    raw_model.get("card_data"),
                )


            results.append(
                processed_model
            )

            processed_models += 1
            media_files += len(model_media)

        except Exception as e:

            print(
                "ModelScope item failed:",
                model_id,
                e
            )

            import traceback
            traceback.print_exc()

            continue            


    if not creator and models and not scan_control.should_stop():
        _progress(len(models), len(models), "Checking models", True)

    elapsed = time.perf_counter() - start_time

    print("\n========================================")
    print("ModelScope Scan Complete")
    print("========================================")
    print(f"Processed models : {processed_models}")
    print(f"Old models       : {old_models}")
    print(f"Duplicates       : {duplicates}")
    print(f"Detail fetches   : {detail_fetches}")
    print(f"File fetches     : {file_fetches}")
    print(f"Media files      : {media_files}")
    print(f"Time             : {elapsed:.2f} seconds")
    print("========================================")

    return results



_AIGC_TAGS_URL = "https://www.modelscope.cn/api/v1/models/aigc/tags"
_AIGC_MODELS_URL = "https://modelscope.cn/api/v1/dolphin/models"
_official_tag_cache = {"loaded_at": 0.0, "items": []}


def _prepare_aigc_web_session(tag=""):
    """Prepare the public ModelScope web API session used by the AIGC models page.

    The normal scanner uses ModelScope's OpenAPI. Official-tag discovery is a
    separate website API (PUT /api/v1/dolphin/models) and expects browser-ish request
    state/headers. Prime the public models page so ModelScope can issue its
    ordinary guest cookies before the PUT request.
    """
    session = _apply_auth()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:153.0) Gecko/20100101 Firefox/153.0",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "x-modelscope-accept-language": "en_US",
        "Origin": "https://modelscope.cn",
        "bx-v": "2.5.37",
    })
    slug = str(tag or "").strip()
    referer = "https://modelscope.cn/models"
    if slug:
        referer += "?officialTags=" + urllib.parse.quote(slug) + "&tabKey=other"
    session.headers["Referer"] = referer
    session.headers["X-Modelscope-Trace-Id"] = str(uuid.uuid4())
    try:
        # This is intentionally best-effort. The endpoint is public, but the
        # page visit helps when ModelScope decides to issue guest/CSRF cookies.
        session.get(referer, timeout=12)
    except Exception:
        pass
    csrf = session.cookies.get("csrf_token")
    if csrf:
        session.headers["x-csrf-token"] = csrf
    return session


def get_official_tags(force=False):
    """Return ModelScope's official AIGC tag taxonomy.

    Entries retain the machine slug (`Tag`), friendly English label (`Name`),
    localized label, and semantic TagType. Cached briefly to make autocomplete
    cheap while the user types.
    """
    now = time.time()
    if not force and _official_tag_cache["items"] and now - _official_tag_cache["loaded_at"] < 900:
        return list(_official_tag_cache["items"])
    session = _prepare_aigc_web_session("")
    try:
        response = session.get(_AIGC_TAGS_URL, timeout=15)
        response.raise_for_status()
        payload = response.json()
        data = payload.get("Data") or payload.get("data") or {}
        items = data.get("data") if isinstance(data, dict) else []
        if not isinstance(items, list):
            items = []
        cleaned = []
        for item in items:
            if not isinstance(item, dict):
                continue
            slug = str(item.get("Tag") or "").strip()
            name = str(item.get("Name") or slug).strip()
            if not slug:
                continue
            cleaned.append({
                "id": slug,
                "name": name or slug,
                "slug": slug,
                "type": str(item.get("TagType") or "").strip(),
                "localized_name": str(item.get("ChineseName") or "").strip(),
            })
        if cleaned:
            _official_tag_cache["items"] = cleaned
            _official_tag_cache["loaded_at"] = now
        return cleaned
    except Exception as exc:
        debug_print(f"ModelScope official-tag catalog failed: {exc!r}")
        return list(_official_tag_cache["items"])


def search_official_tags(query="", limit=30):
    q = str(query or "").strip().casefold()
    try:
        limit = max(1, min(100, int(limit or 30)))
    except (TypeError, ValueError):
        limit = 30
    matches = []
    for item in get_official_tags():
        haystack = " ".join([
            str(item.get("name") or ""),
            str(item.get("slug") or ""),
            str(item.get("localized_name") or ""),
            str(item.get("type") or ""),
        ]).casefold()
        if q and q not in haystack:
            continue
        matches.append({
            "id": item["slug"],
            "name": item["name"],
            "count": 0,
            "type": item.get("type", ""),
        })
    matches.sort(key=lambda x: (0 if str(x["name"]).casefold().startswith(q) else 1, str(x["name"]).casefold()))
    return matches[:limit]


def resolve_official_tag(value):
    """Resolve a friendly label/localized label/slug to the official slug."""
    text = str(value or "").strip()
    if not text:
        return "", ""
    folded = text.casefold()
    catalog = get_official_tags()
    for item in catalog:
        if folded == str(item.get("slug") or "").casefold():
            return item["slug"], item["name"]
    for item in catalog:
        if folded in {
            str(item.get("name") or "").casefold(),
            str(item.get("localized_name") or "").casefold(),
        }:
            return item["slug"], item["name"]
    # Keep manual slugs usable if the taxonomy request is temporarily down.
    return text, text


def _modelscope_tag_values(item):
    """Return normalized ModelScope tags while preserving official English labels."""
    values = []
    seen = set()
    if not isinstance(item, dict):
        return values

    for tag in item.get("OfficialTags") or []:
        if isinstance(tag, dict):
            value = str(tag.get("Name") or tag.get("Tag") or "").strip()
        else:
            value = str(tag or "").strip()
        if value and value.casefold() not in seen:
            seen.add(value.casefold()); values.append(value)

    attrs = item.get("AigcAttributes")
    if isinstance(attrs, str) and attrs.strip():
        try:
            attrs = json.loads(attrs)
        except Exception:
            attrs = {}
    if isinstance(attrs, dict):
        for value in attrs.get("OfficialTags") or []:
            text = str(value or "").strip()
            if text and text.casefold() not in seen:
                seen.add(text.casefold()); values.append(text)

    for value in item.get("Tags") or item.get("tags") or []:
        text = str(value or "").strip()
        if text and text.casefold() not in seen:
            seen.add(text.casefold()); values.append(text)
    return values


def _build_tag_discovery_model(item, requested_tag=""):
    if not isinstance(item, dict):
        return None
    model_id = str(item.get("model_id") or item.get("ModelId") or item.get("BackendSupport", {}).get("model_id") or "").strip()
    if not model_id:
        path = str(item.get("Path") or item.get("CreatedBy") or "").strip()
        name = str(item.get("Name") or "").strip()
        if path and name:
            model_id = f"{path}/{name}"
    if not model_id:
        return None

    details = get_details(model_id)
    if not isinstance(details, dict) or not details:
        details = item
    tags = _modelscope_tag_values(item)
    for value in _modelscope_tag_values(details):
        if value.casefold() not in {x.casefold() for x in tags}:
            tags.append(value)
    requested = str(requested_tag or "").strip()
    if requested and requested.casefold() not in {x.casefold() for x in tags}:
        tags.append(requested)

    model_media = extract_media_from_details(details, model_id)
    versions_meta, version_files = extract_versions_from_details(details, model_id)
    revision = details.get("Revision") or details.get("revision") or "master"
    model_files = version_files or get_files(model_id, revision)
    existing_urls = {x.get("url") for x in model_media if x.get("url")}
    for media in extract_media_from_files(model_files, model_id):
        if media.get("url") and media.get("url") not in existing_urls:
            media["position"] = len(model_media)
            model_media.append(media)
            existing_urls.add(media.get("url"))

    raw_model = {
        "model_id": model_id,
        "model_key": model_id.lower(),
        "details": details,
        "created": normalize_timestamp(item.get("CreatedTime") or item.get("created_at") or details.get("CreatedTime") or ""),
        "updated": normalize_timestamp(item.get("LastUpdatedTime") or item.get("last_modified") or details.get("LastUpdatedTime") or ""),
        "tags": ",".join(tags),
        "files": model_files,
        "source": NAME,
        "url": "https://modelscope.cn/models/" + model_id,
        "image": next((x.get("thumbnail") or x.get("url") for x in model_media if x.get("type") == "image"), ""),
        "media": model_media,
        "has_media": 1 if model_media else 0,
        "has_video": any(x.get("type") == "video" for x in model_media),
        "preview_count": len(model_media),
        "gated": detect_gated_model(item, details),
        "card_data": library_refresh_marker({
            "versions": versions_meta,
            "modelscope": {
                "official_tags": item.get("OfficialTags") or [],
                "all_tags": tags,
                "versions": versions_meta,
            }
        }),
        "sensitive": False,
    }
    model = processors.build_model(raw_model)
    # processors derives useful display fields from the tag text; explicitly
    # retain the human-readable union too for local tag: search/autocomplete.
    model.tags = ",".join(tags)
    display = list(getattr(model, "display_tags", []) or [])
    for value in tags:
        if value.casefold() not in {str(x).casefold() for x in display}:
            display.append(value)
    model.display_tags = display[:24]
    return model


def scan_tag(tag_value, max_results=100, sort="NEWEST", tag_name="", allowed_architectures=None):
    """Explicit ModelScope official-tag Discovery Scan.

    The current website sends a PUT /api/v1/dolphin/models query with an
    `aigc_official_tags contains` criterion. ModelScope does not expose the
    same clear newest sort control here, so AbyssBeacon keeps the source's
    default result order and still applies its own architecture target after
    retrieval.
    """
    requested = str(tag_value or tag_name or "").strip()
    tag, resolved_label = resolve_official_tag(requested)
    if not tag:
        return []
    try:
        max_results = max(1, min(3000, int(max_results)))
    except (TypeError, ValueError):
        max_results = 100

    total_started = time.perf_counter()
    query_started = time.perf_counter()
    session = _prepare_aigc_web_session(tag)
    endpoint = _AIGC_MODELS_URL
    results = []
    seen = set()
    page = 1
    while len(results) < max_results and not scan_control.should_stop():
        page_size = min(30, max_results - len(results))
        payload = {
            "PageSize": page_size,
            "PageNumber": page,
            "SortBy": "Default",
            "Target": "",
            "Criterion": [{
                "category": "aigc_official_tags",
                "predicate": "contains",
                "values": [tag],
            }],
            "SingleCriterion": [],
        }
        response = session.put(endpoint, json=payload, timeout=20)
        if response.status_code != 200:
            raise RuntimeError(f"ModelScope tag discovery HTTP {response.status_code}: {response.text[:300]}")
        data = response.json()
        wrapper = data.get("Data") or data.get("data") or {}
        model_block = wrapper.get("Model") if isinstance(wrapper, dict) else {}
        items = model_block.get("Models") if isinstance(model_block, dict) else []
        if not isinstance(items, list):
            items = []

        # The website API can answer 200 with an empty set when the request has
        # not acquired the current guest web state yet. Re-prime once on page 1
        # before concluding the tag truly has no results.
        if not items and page == 1:
            session = _prepare_aigc_web_session(tag)
            response = session.put(endpoint, json=payload, timeout=20)
            if response.status_code == 200:
                data = response.json()
                wrapper = data.get("Data") or data.get("data") or {}
                model_block = wrapper.get("Model") if isinstance(wrapper, dict) else {}
                items = model_block.get("Models") if isinstance(model_block, dict) else []
                if not isinstance(items, list):
                    items = []
        if not items:
            message = ""
            if isinstance(data, dict):
                message = str(data.get("Message") or data.get("message") or data.get("Code") or data.get("code") or "")
            print(f"ModelScope tag query returned 0 models: {resolved_label or tag} [{tag}]" + (f" | {message}" if message else ""))
            break
        added = 0
        for item in items:
            key = str(item.get("Id") or item.get("Name") or item.get("BackendSupport", {}).get("model_id") or "").strip()
            if not key or key in seen:
                continue
            seen.add(key)
            results.append(item)
            added += 1
            if len(results) >= max_results:
                break
        if not added or len(items) < page_size:
            break
        page += 1

    query_seconds = time.perf_counter() - query_started
    allowed = {str(x).casefold() for x in (allowed_architectures or []) if str(x).strip()}
    candidates_before_arch = len(results)
    if allowed:
        early_kept = []
        for item in results:
            # Dolphin already carries enough naming/tag/base-model context for
            # AbyssBeacon's configured keyword classifier. Reject irrelevant
            # architectures before the expensive detail/files/media requests.
            text = " ".join([
                str(item.get("Name") or ""),
                str(item.get("Path") or ""),
                str(item.get("BaseModel") or item.get("base_model") or ""),
                " ".join(_modelscope_tag_values(item)),
                json.dumps(item.get("AigcAttributes") or {}, ensure_ascii=False, default=str),
            ])
            architecture = processors.classify_architecture(text)
            if str(architecture or "Other").casefold() in allowed:
                early_kept.append(item)
        results = early_kept

    blocked = {str(x).casefold() for x in database.get_blocked_creator_set(NAME)}
    models = []
    detail_started = time.perf_counter()
    for item in results:
        if scan_control.should_stop():
            break
        try:
            model = _build_tag_discovery_model(item, requested_tag=resolved_label or tag)
        except Exception as exc:
            debug_print(f"ModelScope tag detail failed: {exc!r}")
            continue
        if model and str(model.author or "").casefold() not in blocked:
            models.append(model)
    detail_seconds = time.perf_counter() - detail_started
    total_seconds = time.perf_counter() - total_started
    # Discovery timing is intentionally always visible. Normal provider debug
    # logging is gated by verbose_scan_logging, but this compact breakdown is
    # part of the user-facing scan summary and is useful for spotting future
    # ModelScope slowdowns without turning noisy scanner logging back on.
    builtins.print("\nModelScope Discovery timing")
    builtins.print(f"  Tag query          : {query_seconds:.2f}s")
    builtins.print(f"  Candidates         : {candidates_before_arch}")
    if allowed:
        builtins.print(f"  Architecture kept  : {len(results)}")
        builtins.print(f"  Rejected early     : {candidates_before_arch - len(results)}")
    builtins.print(f"  Detailed           : {len(models)}")
    builtins.print(f"  Detail/media/files : {detail_seconds:.2f}s")
    builtins.print(f"  Total              : {total_seconds:.2f}s")
    return models

def get_details(model_id):

    try:

        url = f"https://modelscope.cn/api/v1/models/{model_id}"

        r = get_with_backoff(
            _get_session(), url, provider="ModelScope",
            label=f"details {model_id}", timeout=10
        )

        if r.status_code != 200:
            return {}

        data = r.json()

        model = data.get("Data")

        if isinstance(model, dict):
            return model

    except Exception as e:

        print(
            "ModelScope details error:",
            model_id,
            e
        )

    return {}
