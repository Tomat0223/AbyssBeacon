import json
import hashlib
import os
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests


WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

SOURCE_LABELS = {
    "huggingface": "Hugging Face",
    "modelscope": "ModelScope",
    "civitai": "CivitAI",
    "civitaired": "CivitAI Red",
    "tensorhub": "TensorHub Art",
    "seaart": "SeaArt",
}

APP_NAMESPACE = "AbyssBeacon"
UNKNOWN_CATEGORY = "AbyssBeacon-Other"
INFO_FILENAME = "AbyssBeacon Info.txt"
LEGACY_INFO_FILENAME = "ModelRadar Info.txt"


def safe_component(value, fallback="Unknown", max_length=120):
    value = str(value or "").strip()
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    if not value:
        value = fallback
    if value.upper() in WINDOWS_RESERVED:
        value = f"_{value}"
    if len(value) > max_length:
        value = value[:max_length].rstrip(" .")
    return value or fallback


def comfy_category(model_type, filename=""):
    value = str(model_type or "").strip().casefold()
    name = str(filename or "").casefold()
    if "lora" in value or "lycoris" in value:
        return "loras"
    if "checkpoint" in value or value in {"model", "full model"}:
        return "checkpoints"
    if "vae" in value:
        return "vae"
    if "control" in value:
        return "controlnet"
    if "embedding" in value or "textual inversion" in value:
        return "embeddings"
    if "upscale" in value or "upscaler" in value:
        return "upscale_models"
    if "text encoder" in value:
        return "text_encoders"
    if "diffusion" in value or "unet" in value:
        return "diffusion_models"
    if "workflow" in value or name.endswith(".json"):
        return "workflows"
    return UNKNOWN_CATEGORY


def comfy_category_for_model(model, filename="", file_data=None):
    """Choose the ComfyUI category, preferring the individual file's type.

    A single model version can contain a UNet, LoRA, text encoder, VAE, etc.
    Routing by the card's coarse model_type would put every artifact in the
    same folder. File-level metadata is therefore authoritative when present.
    """
    model = model or {}
    file_data = file_data or {}

    file_type = (
        file_data.get("file_type")
        or file_data.get("type")
        or file_data.get("kind")
        or ""
    )
    category = comfy_category(file_type, filename)
    if category != UNKNOWN_CATEGORY:
        return category

    category = comfy_category(model.get("model_type"), filename)
    if category != UNKNOWN_CATEGORY:
        return category

    path = str(file_data.get("path") or filename or "").replace("\\", "/").casefold()
    text = " ".join(
        str(model.get(key) or "")
        for key in (
            "model_type", "pipeline", "library", "tags", "format",
            "name", "display_name", "description",
        )
    ).casefold()

    # Hugging Face commonly tags complete diffusion repositories with
    # "diffusers" / text-to-image / image-to-image / video-generation while
    # AbyssBeacon's intentionally small UI model-type list may call them Other.
    diffusion_signals = (
        "diffusers",
        "diffusion",
        "text-to-image",
        "image-to-image",
        "video-generation",
        "image-to-video",
        "text-to-video",
        "unet",
        "transformer",
    )
    if any(signal in text for signal in diffusion_signals):
        return "diffusion_models"

    # Component paths from Diffusers-style repositories are another strong
    # signal even if old snapshots did not preserve pipeline/library tags.
    if any(
        segment in path.split("/")
        for segment in ("unet", "transformer", "diffusion_model", "diffusion_models")
    ):
        return "diffusion_models"

    return category


def original_filename(file_data, target_url=""):
    for key in ("name", "path", "filename"):
        raw = str((file_data or {}).get(key) or "").strip()
        if raw:
            return raw.replace("\\", "/").rsplit("/", 1)[-1]
    path = unquote(urlparse(str(target_url or "")).path)
    return path.rsplit("/", 1)[-1] or "model.safetensors"


def _stem_is_cryptic(stem):
    stem = str(stem or "").strip()
    if len(stem) < 4:
        return True
    # UUID / long hashes / machine IDs. TensorHub commonly prefixes an otherwise
    # generic training suffix with a UUID, e.g.
    # 1e91070c-...-12f649299dd9.TA_trained.safetensors.
    uuid_pattern = r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
    if re.fullmatch(r"[0-9a-fA-F]{16,}", stem):
        return True
    if re.fullmatch(uuid_pattern, stem):
        return True
    if re.match(rf"^{uuid_pattern}(?:[._ -].*)?$", stem):
        return True
    if re.fullmatch(r"[A-Za-z0-9_-]{20,}", stem) and not re.search(r"[aeiouAEIOU]{2}", stem):
        return True
    return False


def _stem_is_generic(stem):
    stem = str(stem or "").strip()
    return bool(re.fullmatch(r"(?:v|ver|version|model|checkpoint|lora|final|latest)[-_ ]?\d*(?:\.\d+)*", stem, re.I))


def _normalized_name_tokens(value):
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def library_model_name(model):
    """Return a concise model identity for local folders/fallback filenames.

    The architecture already has its own parent directory in AbyssBeacon's
    organized layout, so a trailing architecture suffix such as
    "Lipples - Minimax H3" is redundant and becomes simply "Lipples".
    """
    raw = str(model.get("display_name") or model.get("name") or "").strip()
    if not raw:
        return "Model"

    architecture = str(model.get("architecture") or model.get("base_model") or "").strip()
    architecture_key = _normalized_name_tokens(architecture)
    if not architecture_key:
        return raw

    # Common title patterns: "Name - Architecture", "Name | Architecture",
    # "Name: Architecture", and "Name (Architecture)". Only strip when the
    # entire trailing segment normalizes to the architecture value.
    paren = re.match(r"^(.*?)\s*\(([^()]*)\)\s*$", raw)
    if paren and _normalized_name_tokens(paren.group(2)) == architecture_key:
        candidate = paren.group(1).strip(" -–—|:")
        if candidate:
            return candidate

    parts = re.split(r"\s+(?:[-–—|:])\s+", raw)
    if len(parts) > 1 and _normalized_name_tokens(parts[-1]) == architecture_key:
        candidate = " - ".join(parts[:-1]).strip(" -–—|:")
        if candidate:
            return candidate

    return raw


def _stem_has_model_signal(stem, model_name):
    """Return True when the source filename already contains useful model identity."""
    generic = {
        "model", "checkpoint", "lora", "final", "latest", "trained", "train",
        "safe", "safetensor", "safetensors", "fp16", "bf16", "fp8", "int8",
        "v1", "v2", "main",
    }

    def tokens(value):
        return {
            token
            for token in re.findall(r"[A-Za-z0-9]+", str(value or "").casefold())
            if len(token) >= 3 and token not in generic and not token.isdigit()
        }

    stem_tokens = tokens(stem)
    model_tokens = tokens(model_name)
    return bool(stem_tokens & model_tokens)


def _stem_has_descriptive_signal(stem, model=None):
    """Return True when a source filename is already useful on its own.

    A source filename does not need to repeat AbyssBeacon's card title to be
    worth preserving. Creators often use a different but perfectly descriptive
    local filename, such as ``Hana - Krea 2.safetensors``.
    """
    raw_tokens = re.findall(r"[A-Za-z0-9]+", str(stem or "").casefold())
    if not raw_tokens:
        return False

    technical = {
        "model", "checkpoint", "lora", "locon", "lokr", "lycoris",
        "final", "latest", "trained", "train", "training",
        "safe", "safetensor", "safetensors", "weights", "weight",
        "epoch", "epochs", "step", "steps", "main",
        "fp16", "bf16", "fp8", "int8", "gguf", "ckpt",
    }

    architecture = ""
    if isinstance(model, dict):
        architecture = str(model.get("architecture") or model.get("base_model") or "")
    architecture_tokens = {
        token
        for token in re.findall(r"[A-Za-z0-9]+", architecture.casefold())
        if token
    }

    useful = []
    for token in raw_tokens:
        if token in technical or token in architecture_tokens or token.isdigit():
            continue
        if re.fullmatch(r"v\d+(?:\d+)?", token):
            continue
        if re.fullmatch(r"(?:epoch|step)\d+", token):
            continue
        if token in {"krea", "krea2", "kr2", "k2"}:
            continue
        useful.append(token)

    if any(len(token) >= 4 for token in useful):
        return True

    alpha = [token for token in useful if token.isalpha()]
    return len(alpha) >= 2 and sum(len(token) for token in alpha) >= 6


def friendly_filename(model, file_data, target_url, mode="obvious"):
    """TEST MODE: preserve the provider's original filename.

    Friendly/descriptive renaming is intentionally disabled while we isolate
    source filename metadata.  ``mode`` is accepted so the existing settings UI
    and call sites do not need to change for this test.

    The only transformation left is Windows-safe component sanitization; no
    model name, version, precision, architecture, or generic-name replacement is
    added.
    """
    original = original_filename(file_data, target_url)
    suffix = Path(original).suffix
    fallback = "model" + (suffix or ".safetensors")
    return safe_component(original, fallback, 180)

def resolve_comfy_root(configured_root):
    """Return the ComfyUI application root.

    v1.4.0 asked for the `models` directory. Accept that old value too and
    transparently step back to the ComfyUI root so existing settings continue
    working after the UI changes to a single ComfyUI-folder field.
    """
    root = Path(str(configured_root or "")).expanduser()

    # Backward compatibility: .../ComfyUI/models -> .../ComfyUI
    if root.name.casefold() == "models":
        return root.parent

    return root


def _unknown_category_directory(comfy_root, folder_name):
    comfy_root = resolve_comfy_root(comfy_root)
    parent = comfy_root.parent
    if (
        comfy_root.name.casefold() == "comfyui"
        and "comfyui-easy-install" in parent.name.casefold()
    ):
        return parent / folder_name
    return comfy_root / folder_name


def category_base_directory(comfy_root, category):
    """Map an AbyssBeacon category to its real ComfyUI location."""
    comfy_root = resolve_comfy_root(comfy_root)
    if category == "workflows":
        return comfy_root / "user" / "default" / "workflows"
    if category == UNKNOWN_CATEGORY:
        return _unknown_category_directory(comfy_root, UNKNOWN_CATEGORY)
    return comfy_root / "models" / category


def _namespace_install_path(base, namespace, layout, model_name, source_label, architecture):
    if layout == "simple":
        return base / namespace / model_name
    if layout == "organized":
        return base / namespace / source_label / model_name
    return base / namespace / architecture / source_label / model_name

def install_directory(comfy_root, model, source, prefs, filename="", file_data=None):
    root = resolve_comfy_root(comfy_root)
    category = comfy_category_for_model(model, filename, file_data)
    layout = str(prefs.get("install_layout") or "simple").lower()
    architecture = safe_component(
        model.get("architecture") or model.get("base_model") or "Other",
        "Other",
    )
    source_label = safe_component(
        SOURCE_LABELS.get(str(source).lower(), source),
        "Source",
    )
    model_name = safe_component(
        library_model_name(model),
        "Model",
    )

    current_base = category_base_directory(root, category)
    return _namespace_install_path(
        current_base, APP_NAMESPACE, layout, model_name, source_label, architecture
    )

def _version_metadata(model, file_data):
    card = model.get("card_data") or {}
    if isinstance(card, str):
        try: card = json.loads(card or "{}")
        except Exception: card = {}
    versions = card.get("versions") if isinstance(card, dict) else []
    if not isinstance(versions, list): versions = []
    wanted_id = str(file_data.get("version_id") or "")
    wanted_name = str(file_data.get("version") or "").casefold()
    for version in versions:
        if not isinstance(version, dict): continue
        if wanted_id and str(version.get("id") or "") == wanted_id:
            return version
        if wanted_name and str(version.get("name") or "").casefold() == wanted_name:
            return version
    return {}


def _list_value(value):
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        return [v.strip() for v in re.split(r"[,\n]", value) if v.strip()]
    return []


def build_info_text(model, file_data, source, installed_filename):
    version = _version_metadata(model, file_data)
    triggers = []
    for obj in (file_data, version):
        if not isinstance(obj, dict): continue
        for key in ("trained_words", "trigger_words", "triggerWords", "activation_text"):
            triggers.extend(_list_value(obj.get(key)))
    triggers = list(dict.fromkeys(triggers))

    strengths = []
    for obj in (file_data, version):
        if not isinstance(obj, dict): continue
        for key in ("recommended_strength", "strength", "weight", "recommended_weight"):
            value = obj.get(key)
            if value not in (None, ""):
                strengths.append(str(value))
    strengths = list(dict.fromkeys(strengths))

    lines = [
        "AbyssBeacon Library Information",
        "=" * 30,
        "",
        f"Model: {model.get('display_name') or model.get('name') or ''}",
        f"Creator: {model.get('author') or ''}",
        f"Source: {SOURCE_LABELS.get(str(source).lower(), source)}",
        f"Source URL: {model.get('url') or ''}",
        f"Architecture: {model.get('architecture') or model.get('base_model') or ''}",
        f"Model type: {model.get('model_type') or ''}",
        f"Version: {file_data.get('version') or version.get('name') or ''}",
        f"Installed file: {installed_filename}",
        f"Installed: {datetime.now(timezone.utc).isoformat()}",
    ]
    if triggers:
        lines += ["", "Trigger words", "-------------", ", ".join(triggers)]
    if strengths:
        lines += ["", "Recommended strength / weight", "-----------------------------", ", ".join(strengths)]
    description = str(model.get("description") or "").strip()
    if description:
        lines += ["", "Description / usage notes", "-------------------------", description]
    lines += ["", "Generated locally by AbyssBeacon."]
    return "\n".join(lines).strip() + "\n"


class DownloadCancelled(Exception):
    """Raised when the user cancels and discards a managed local download."""


class DownloadPaused(Exception):
    """Raised when the user pauses while intentionally keeping the .part file."""


class DownloadResumeUnavailable(Exception):
    """Raised when a server refuses a Range resume; existing .part is preserved."""


def _download_stream(url, destination, referer="", extra_headers=None, progress_callback=None, resume_existing=True):
    """Download to .part and resume an interrupted transfer when supported.

    A normal network/process interruption intentionally keeps the .part file.
    An explicit AbyssBeacon Cancel removes it, because Cancel means discard while
    a restart/failure should remain resumable.
    """
    destination = Path(destination)
    temp = destination.with_suffix(destination.suffix + ".part")
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:153.0) "
            "Gecko/20100101 Firefox/153.0"
        ),
        "Accept": "*/*",
    }
    if str(referer or "").startswith(("http://", "https://")):
        headers["Referer"] = str(referer)
    if isinstance(extra_headers, dict):
        for key, value in extra_headers.items():
            if value not in (None, ""):
                headers[str(key)] = str(value)

    destination.parent.mkdir(parents=True, exist_ok=True)
    # A normal click on a download button means "start fresh". Resume is an
    # explicit action from Download Manager. This also cleans up orphaned .part
    # files left by older builds where Dismiss/Cancel only forgot the UI job.
    if not resume_existing and temp.exists():
        try:
            temp.unlink()
        except OSError as exc:
            raise RuntimeError(f"Could not discard the previous partial download: {exc}") from exc
    existing = temp.stat().st_size if temp.exists() else 0
    request_headers = dict(headers)
    if existing > 0:
        request_headers["Range"] = f"bytes={existing}-"

    # Publish the exact partial path before the network request. This lets a
    # restarted AbyssBeacon recover the real byte count from disk even if the
    # process is stopped between progress callbacks.
    if callable(progress_callback):
        progress_callback(
            stage="Connecting",
            downloaded_bytes=existing,
            total_bytes=None,
            part_path=str(temp),
        )

    try:
        with requests.get(url, stream=True, allow_redirects=True, timeout=(20, 600), headers=request_headers) as response:
            if response.status_code >= 400:
                host = urlparse(str(response.url or url)).netloc or "remote server"
                raise RuntimeError(f"{host} rejected the file request (HTTP {response.status_code}).")

            resumed = existing > 0 and response.status_code == 206
            if existing > 0 and not resumed:
                # NEVER truncate a saved partial file just because a server
                # ignored Range. Preserve the bytes and let the user retry.
                raise DownloadResumeUnavailable(
                    f"Resume was not accepted by the download server (HTTP {response.status_code}). "
                    f"The existing {existing} byte partial file was preserved on disk."
                )

            if resumed:
                content_range = str(response.headers.get("Content-Range") or "")
                start_match = re.match(r"bytes\s+(\d+)-", content_range, flags=re.I)
                if not start_match or int(start_match.group(1)) != existing:
                    raise DownloadResumeUnavailable(
                        "The download server returned an unexpected resume range. "
                        "The existing partial file was preserved on disk."
                    )

            content_length = 0
            try:
                content_length = max(0, int(response.headers.get("Content-Length") or 0))
            except (TypeError, ValueError):
                content_length = 0

            total_bytes = existing + content_length if resumed else content_length
            content_range = str(response.headers.get("Content-Range") or "")
            match = re.search(r"/(\d+)$", content_range)
            if match:
                try: total_bytes = int(match.group(1))
                except ValueError: pass

            downloaded_bytes = existing
            if callable(progress_callback):
                progress_callback(stage="Downloading", downloaded_bytes=downloaded_bytes, total_bytes=total_bytes, part_path=str(temp))

            with open(temp, "ab" if resumed else "wb") as handle:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        handle.write(chunk)
                        downloaded_bytes += len(chunk)
                        if callable(progress_callback):
                            progress_callback(stage="Downloading", downloaded_bytes=downloaded_bytes, total_bytes=total_bytes, part_path=str(temp))
        os.replace(temp, destination)
    except DownloadPaused:
        # Pause means keep every byte already written so Resume can use Range.
        raise
    except DownloadCancelled:
        try: temp.unlink(missing_ok=True)
        except Exception: pass
        raise
    except Exception:
        # Keep partial bytes for Retry/Resume, including across app restarts.
        raise
    return destination

def _save_preview(model, folder, installed_filename, download_headers=None):
    """Save a useful full-resolution companion preview.

    AbyssBeacon keeps tiny WebP card thumbnails for fast browsing, but the
    installed library should receive a source-quality image when one exists.
    The already-cached card thumbnail remains the offline fallback.
    """
    image = str(model.get("image") or "").strip()
    high_res = str(model.get("_install_preview_url") or "").strip()
    cached_hint = str(model.get("_cached_preview") or "").strip()
    preview_video = str(model.get("_install_preview_video_url") or "").strip()
    # Sidecars belong to the model folder, not to an individual variant.
    # A bf16/int8/convrot set therefore shares one preview and one info file.
    existing_previews = sorted(Path(folder).glob("preview.*"))
    for existing in existing_previews:
        if existing.is_file() and existing.stat().st_size > 0:
            return str(existing)
    allowed_exts = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"}
    video_exts = {".mp4", ".webm", ".mov"}
    cache_dir = Path(__file__).resolve().parent / "static" / "cache" / "previews"

    def preview_destination(url, fallback=".jpg"):
        parsed = Path(unquote(urlparse(str(url or "")).path))
        ext = parsed.suffix.lower()
        if ext not in allowed_exts:
            ext = fallback
        return Path(folder) / f"preview{ext}"

    def copy_cached(public_or_name):
        value = str(public_or_name or "").replace("\\", "/").strip()
        if not value:
            return ""
        filename = Path(value).name
        cached = cache_dir / filename
        if not cached.is_file() or cached.stat().st_size <= 0:
            return ""
        ext = cached.suffix.lower()
        if ext not in allowed_exts:
            ext = ".webp"
        dest = Path(folder) / f"preview{ext}"
        shutil.copy2(cached, dest)
        return str(dest)

    try:
        # 1. Download the full-resolution gallery image AbyssBeacon already knows.
        if high_res.startswith(("http://", "https://")):
            dest = preview_destination(high_res)
            try:
                _download_stream(
                    high_res,
                    dest,
                    model.get("url") or "",
                    download_headers,
                )
                if dest.is_file() and dest.stat().st_size > 0:
                    return str(dest)
            except requests.exceptions.SSLError as exc:
                # Hugging Face occasionally terminates a resolve-image TLS
                # connection early. Retry once with a clean request before
                # accepting the cached thumbnail fallback.
                print(
                    "AbyssBeacon full-resolution preview TLS retry:",
                    type(exc).__name__,
                    exc,
                )
                try:
                    time.sleep(0.35)
                    _download_stream(
                        high_res,
                        dest,
                        model.get("url") or "",
                        None,
                    )
                    if dest.is_file() and dest.stat().st_size > 0:
                        return str(dest)
                except Exception as retry_exc:
                    print(
                        "AbyssBeacon full-resolution preview retry failed; "
                        "falling back to card cache:",
                        type(retry_exc).__name__,
                        retry_exc,
                    )
            except Exception as exc:
                print(
                    "AbyssBeacon full-resolution preview download failed; "
                    "falling back to card cache:",
                    type(exc).__name__,
                    exc,
                )

        # 2. Canonical card cache.
        copied = copy_cached(cached_hint)
        if copied:
            return copied

        # 3. image itself may already be /static/cache/previews/<hash>.webp.
        normalized = image.replace("\\", "/")
        if "/static/cache/previews/" in normalized:
            copied = copy_cached(normalized)
            if copied:
                return copied

        # 4. Snapshot may still contain the original image URL.
        if image.startswith(("http://", "https://")):
            # First see whether that exact source URL has already been cached.
            cache_name = hashlib.sha256(
                image.encode("utf-8", errors="ignore")
            ).hexdigest() + ".webp"
            copied = copy_cached(cache_name)
            if copied:
                return copied

            # Last-resort network image.
            dest = preview_destination(image)
            _download_stream(
                image,
                dest,
                model.get("url") or "",
                download_headers,
            )
            if dest.is_file() and dest.stat().st_size > 0:
                return str(dest)

        # 5. Video-only models still get one useful visual companion. We save
        # exactly one source preview video and never add it to download history.
        if preview_video.startswith(("http://", "https://")):
            parsed = Path(unquote(urlparse(preview_video).path))
            ext = parsed.suffix.lower()
            if ext not in video_exts:
                ext = ".mp4"
            dest = Path(folder) / f"preview{ext}"
            _download_stream(
                preview_video,
                dest,
                model.get("url") or "",
                download_headers,
            )
            if dest.is_file() and dest.stat().st_size > 0:
                return str(dest)

    except Exception as exc:
        print(
            "AbyssBeacon preview companion skipped:",
            model.get("display_name") or model.get("name") or "",
            type(exc).__name__,
            exc,
        )
        return ""

    return ""


def install_model_file(model, file_data, source, target_url, prefs, download_headers=None, progress_callback=None, resume_existing=True):
    configured_root = str(
        prefs.get("local_comfy_root")
        or prefs.get("local_models_root")
        or ""
    ).strip()
    if not configured_root:
        raise ValueError(
            "Local installer is enabled, but no ComfyUI folder is configured in "
            "Settings → Library → Local Installer."
        )

    root = resolve_comfy_root(configured_root)
    if not root.exists() or not root.is_dir():
        raise ValueError(f"Configured ComfyUI folder does not exist: {root}")

    filename = friendly_filename(
        model,
        file_data,
        target_url,
        prefs.get("friendly_filenames", "off"),
    )
    folder = install_directory(root, model, source, prefs, filename, file_data)
    folder.mkdir(parents=True, exist_ok=True)
    destination = folder / filename

    if destination.exists():
        behavior = str(prefs.get("existing_file_behavior") or "keep_both").lower()
        if behavior == "skip":
            return {"path": str(destination), "filename": filename, "skipped": True, "folder": str(folder)}
        if behavior == "replace":
            pass
        else:
            counter = 2
            stem, suffix = destination.stem, destination.suffix
            while destination.exists():
                destination = folder / f"{stem} ({counter}){suffix}"
                counter += 1
            filename = destination.name

    _download_stream(
        target_url,
        destination,
        model.get("url") or "",
        download_headers,
        progress_callback=progress_callback,
        resume_existing=resume_existing,
    )

    if callable(progress_callback):
        progress_callback(stage="Installing")

    info_path = ""
    preview_path = ""
    if prefs.get("save_model_info", True):
        info = build_info_text(model, file_data, source, filename)
        current_info = folder / INFO_FILENAME
        legacy_info = folder / LEGACY_INFO_FILENAME
        # A manually renamed legacy install folder can still contain the old
        # sidecar filename. Migrate it in place, then use only AbyssBeacon's
        # sidecar name going forward so the folder never accumulates duplicates.
        if legacy_info.is_file() and not current_info.exists():
            try:
                legacy_info.replace(current_info)
            except OSError:
                pass
        current_info.write_text(info, encoding="utf-8")
        if legacy_info.is_file() and legacy_info != current_info:
            try:
                legacy_info.unlink()
            except OSError:
                pass
        info_path = str(current_info)
    if prefs.get("save_model_preview", True):
        if callable(progress_callback):
            progress_callback(stage="Saving preview")
        preview_path = _save_preview(model, folder, filename, download_headers)

    if callable(progress_callback):
        progress_callback(stage="Finalizing")

    return {
        "path": str(destination),
        "filename": filename,
        "folder": str(folder),
        "info_path": info_path,
        "preview_path": preview_path,
        "skipped": False,
    }
