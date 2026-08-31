import hashlib
import re
from collections import defaultdict

_WEIGHT_EXTENSIONS = (".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf")
_CONFIG_FILES = {"model_index.json", "config.json", "adapter_config.json", "adapter_model.safetensors"}

# Increment this whenever stored repository classification needs to be rebuilt.
# Update Library uses this value to detect older HF/ModelScope snapshots.
REPOSITORY_CLASSIFIER_VERSION = 4


def _text(value):
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        return " ".join(_text(item) for item in value)
    if isinstance(value, dict):
        return " ".join(f"{_text(k)} {_text(v)}" for k, v in value.items())
    return str(value)


def _tokens(value):
    text = _text(value).casefold()
    return {part for part in re.split(r"[\s,;|]+", text) if part}


def _file_path(item):
    if isinstance(item, dict):
        return str(item.get("path") or item.get("name") or "").strip()
    return str(item or "").strip()


def _file_size(item):
    if not isinstance(item, dict):
        return 0
    value = item.get("size_bytes", item.get("size", 0))
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _normalize_lora_family(path):
    """Collapse rank/epoch/step/checkpoint/shard variants to one logical artifact family."""
    name = _file_path(path).replace("\\", "/").rsplit("/", 1)[-1]
    stem = re.sub(r"\.(?:safetensors|ckpt|pt|pth|bin|gguf)$", "", name, flags=re.I)
    stem = stem.casefold().strip()

    # Hugging Face shard suffixes represent one weight set, not independent artifacts.
    stem = re.sub(r"[-_.]?\d{4,6}-of-\d{4,6}$", "", stem)

    # Common training checkpoints/epochs/steps, LoRA rank variants, and
    # zero-padded training-step suffixes used by some HF/ModelScope uploaders.
    # The bare-number rule is intentionally strict: it must start with zero and
    # contain at least six digits, so ordinary model names ending in v1/v2/250
    # are not collapsed accidentally.
    suffixes = (
        r"(?:[-_. ](?:epoch|ep|step|steps|checkpoint|ckpt)[-_. ]?\d+)$",
        r"(?:[-_. ]r(?:ank)?[-_. ]?\d+)$",
        r"(?:[-_. ](?:rank)[-_. ]?\d+)$",
        r"(?:[-_. ]0\d{5,})$",
        r"(?:[-_. ](?:fp16|fp32|bf16|fp8|int8|int4))$",
    )
    changed = True
    while changed and stem:
        changed = False
        for pattern in suffixes:
            updated = re.sub(pattern, "", stem, flags=re.I).strip("-_. ")
            if updated != stem:
                stem = updated
                changed = True

    # Normalise punctuation only after semantic suffix removal.
    stem = re.sub(r"[^a-z0-9]+", " ", stem).strip()
    return stem


def _likely_workflow_json(path):
    value = _file_path(path).casefold().replace("\\", "/")
    if not value.endswith(".json"):
        return False
    name = value.rsplit("/", 1)[-1]
    if name in {"config.json", "model_index.json", "adapter_config.json", "tokenizer_config.json", "generation_config.json"}:
        return False
    if any(marker in value for marker in ("workflow", "comfy", "graph", "prompt")):
        return True
    return False


def _component_full_model_evidence(paths):
    lowered = [path.casefold().replace("\\", "/") for path in paths]
    components = {
        "transformer": any("/transformer/" in f"/{p}" or p.startswith("transformer/") for p in lowered),
        "unet": any("/unet/" in f"/{p}" or p.startswith("unet/") for p in lowered),
        "vae": any("/vae/" in f"/{p}" or p.startswith("vae/") for p in lowered),
        "text_encoder": any("/text_encoder" in f"/{p}" or p.startswith("text_encoder") for p in lowered),
        "tokenizer": any("/tokenizer" in f"/{p}" or p.startswith("tokenizer") for p in lowered),
    }
    return sum(1 for value in components.values() if value), components


def classify_repository(raw):
    """Classify HF/ModelScope repositories from weighted evidence.

    The output deliberately separates artifact type from container shape. A
    repository containing several distinct LoRA families becomes a Collection,
    while rank/epoch/checkpoint variants of one LoRA remain one logical family.
    """
    source = str(raw.get("source") or "").casefold()
    if source not in {"huggingface", "modelscope"}:
        return None

    details = raw.get("details") if isinstance(raw.get("details"), dict) else {}
    files = raw.get("files") or []
    paths = [_file_path(item) for item in files if _file_path(item)]
    lowered_paths = [path.casefold().replace("\\", "/") for path in paths]

    model_id = str(raw.get("model_id") or "")
    repo_name = model_id.rsplit("/", 1)[-1]
    tags = raw.get("tags") or details.get("tags") or []
    tag_tokens = _tokens(tags)
    library = str(
        raw.get("library")
        or details.get("library_name")
        or details.get("libraryName")
        or details.get("Library")
        or details.get("library")
        or ""
    ).casefold()
    description = _text(
        details.get("readme")
        or details.get("description")
        or details.get("Description")
        or details.get("ModelDescription")
        or details.get("model_description")
        or ""
    ).casefold()
    repo_text = f"{repo_name} {_text(tags)} {library} {description}".casefold()

    scores = defaultdict(int)
    reasons = defaultdict(list)

    def add(kind, points, reason):
        scores[kind] += int(points)
        reasons[kind].append(reason)

    # Strong source metadata. HF/ModelScope tags saying LoRA/adapter are much
    # more meaningful than a random training filename containing "ckpt".
    exact_lora_tags = {
        "lora", "diffusion-lora", "template:diffusion-lora", "adapter", "adapters",
        "peft", "lycoris", "loha", "lokr", "locon", "dora"
    }
    if tag_tokens & exact_lora_tags:
        add("LoRA", 120, "source metadata identifies LoRA/adapter content")
    if any(token.endswith(":diffusion-lora") for token in tag_tokens):
        add("LoRA", 120, "source template identifies diffusion LoRA content")
    if library in {"lora", "peft", "adapters"}:
        add("LoRA", 120, f"source library is {library}")

    if any(path.endswith("adapter_config.json") for path in lowered_paths):
        add("LoRA", 100, "adapter_config.json present")
    if any("adapter_model" in path and path.endswith(_WEIGHT_EXTENSIONS) for path in lowered_paths):
        add("LoRA", 90, "adapter model weights present")

    # Workflow evidence. A workflow-looking JSON is strong; a repo/readme that
    # explicitly says workflow strengthens it further. Plain config JSON does not.
    workflow_named_files = [path for path in paths if _likely_workflow_json(path)]
    generic_json_files = []
    for path in paths:
        lower = path.casefold().replace("\\", "/")
        name = lower.rsplit("/", 1)[-1]
        if lower.endswith(".json") and name not in _CONFIG_FILES and not name.endswith("config.json"):
            generic_json_files.append(path)

    repo_says_workflow = "workflow" in repo_name.casefold() or "workflows" in repo_name.casefold()
    description_says_workflow = bool(re.search(r"\bworkflows?\b", description))
    workflow_files = list(workflow_named_files)
    if (repo_says_workflow or description_says_workflow) and generic_json_files:
        workflow_files = list(dict.fromkeys(workflow_files + generic_json_files))

    if workflow_named_files:
        add("Workflow", 90, f"{len(workflow_named_files)} workflow-named JSON file(s)")
    elif generic_json_files and not any(path.casefold().endswith(_WEIGHT_EXTENSIONS) for path in paths):
        add("Workflow", 55, f"{len(generic_json_files)} user JSON file(s) with no model weights")
    if repo_says_workflow:
        add("Workflow", 55, "repository name identifies workflows")
    if description_says_workflow:
        add("Workflow", 45, "README/description identifies workflows")

    # File-level LoRA evidence. Keep this below explicit source tags, but well
    # above weak words buried in unrelated training checkpoints.
    lora_named_weights = [
        path for path in paths
        if path.casefold().endswith(_WEIGHT_EXTENSIONS)
        and re.search(r"(?:^|[/_. -])(lora|lycoris|loha|lokr|locon|dora)(?:[/_. -]|$)", path.casefold())
    ]
    if lora_named_weights:
        add("LoRA", min(70, 35 + len(lora_named_weights) * 5), "LoRA-named weight files present")

    # Full-model/checkpoint evidence should be structural. The word ckpt in a
    # training filename is deliberately only weak evidence below.
    component_count, components = _component_full_model_evidence(paths)
    has_model_index = any(path.casefold().endswith("model_index.json") for path in paths)
    has_config = any(path.casefold().endswith("config.json") for path in paths)
    weight_files = [item for item in files if _file_path(item).casefold().endswith(_WEIGHT_EXTENSIONS)]
    large_weights = [item for item in weight_files if _file_size(item) >= 1_500_000_000]
    gguf_weights = [item for item in weight_files if _file_path(item).casefold().endswith(".gguf")]

    if has_model_index and component_count >= 2:
        add("Checkpoint", 130, "model_index plus multi-component model structure")
    elif component_count >= 3:
        add("Checkpoint", 115, "multi-component model structure")
    if gguf_weights and not scores["LoRA"]:
        add("Checkpoint", 90, "GGUF model weights present")
    if large_weights and has_config and not scores["LoRA"]:
        add("Checkpoint", 80, "large primary weights plus config present")
    if large_weights and len(weight_files) <= 4 and not scores["LoRA"]:
        add("Checkpoint", 60, "small set of very large model weight files")

    # Weak naming hints. These can break ties, but can never overpower strong
    # source/file evidence. In particular ckpt500 must not turn a LoRA repo into
    # a Checkpoint repository.
    if re.search(r"\blora\b", repo_name.casefold().replace("_", " ").replace("-", " ")):
        add("LoRA", 25, "repository name says LoRA")
    if re.search(r"\b(checkpoint|checkpoints|ckpt)\b", repo_name.casefold().replace("_", " ").replace("-", " ")):
        add("Checkpoint", 20, "repository name says checkpoint")
    if any(re.search(r"(?:checkpoint|ckpt)[-_. ]?\d+", path.casefold()) for path in paths):
        add("Checkpoint", 4, "training checkpoint filename present")

    # Determine logical LoRA families. Source-level LoRA evidence allows generic
    # safetensors names to participate; otherwise require LoRA-like filenames.
    lora_candidate_paths = []
    strong_lora_source = scores["LoRA"] >= 100
    for path in paths:
        lower = path.casefold().replace("\\", "/")
        # Collection detection intentionally uses deployable safetensors only.
        # Training .pt/.bin snapshots can otherwise make one LoRA look like a bundle.
        if not lower.endswith(".safetensors"):
            continue
        if any(marker in lower for marker in (
            "/optimizer", "optimizer.", "training_state", "scheduler",
            "/checkpoint-", "/checkpoints/", "global_step", "mp_rank", "zero_pp_rank"
        )):
            continue
        if strong_lora_source or re.search(r"(?:^|[/_. -])(lora|lycoris|loha|lokr|locon|dora)(?:[/_. -]|$)", lower):
            family = _normalize_lora_family(path)
            if family:
                lora_candidate_paths.append((path, family))

    families = []
    seen_families = set()
    family_paths = defaultdict(list)
    for path, family in lora_candidate_paths:
        family_paths[family].append(path)
        if family not in seen_families:
            seen_families.add(family)
            families.append(family)

    # A repository can contain one LoRA family but still be effectively a
    # training-series bundle: many zero-padded snapshots of that same family.
    # Those are painful in the normal model drawer, so route them through the
    # Collection family UI without confusing ordinary rank variants (r8/r16/...).
    single_family_training_series = False
    single_family_name = ""
    if len(families) == 1:
        family = families[0]
        numbered_steps = set()
        for path in family_paths.get(family, []):
            name = _file_path(path).replace("\\", "/").rsplit("/", 1)[-1]
            stem = re.sub(r"\.(?:safetensors|ckpt|pt|pth|bin|gguf)$", "", name, flags=re.I)
            match = re.search(r"[-_. ](0\d{5,})$", stem, flags=re.I)
            if match:
                try:
                    numbered_steps.add(int(match.group(1)))
                except (TypeError, ValueError):
                    pass
        if len(numbered_steps) >= 4:
            single_family_training_series = True
            sample_paths = family_paths.get(family, [])
            if sample_paths:
                single_family_name = _display_lora_family(sample_paths[0])

    # Workflow-only repositories with no model weights should strongly prefer
    # Workflow even when incidental prose mentions LoRA nodes/models.
    if workflow_files and not weight_files:
        add("Workflow", 100, "workflow repository contains no model weights")

    ranked = sorted(
        ((kind, score) for kind, score in scores.items() if score > 0),
        key=lambda pair: (-pair[1], pair[0]),
    )
    primary = ranked[0][0] if ranked else "Other"

    # If source metadata confidently says LoRA, a structural full-model score
    # from unrelated training snapshots must not overturn it.
    if scores["LoRA"] >= 100 and scores["LoRA"] >= scores["Checkpoint"] - 20:
        primary = "LoRA"

    detected = [kind for kind, score in ranked if score >= 45]
    if primary not in detected and primary != "Other":
        detected.insert(0, primary)

    container = "single"
    collection_type = ""
    collection_shape = ""
    if primary == "LoRA" and (len(families) > 1 or single_family_training_series):
        container = "collection"
        collection_type = "LoRA"
        collection_shape = "training_series" if single_family_training_series and len(families) == 1 else "multi_family"
        display_type = "Collection"
    else:
        display_type = primary

    return {
        "version": REPOSITORY_CLASSIFIER_VERSION,
        "display_type": display_type,
        "primary_artifact_type": primary,
        "container": container,
        "collection_type": collection_type,
        "collection_shape": collection_shape,
        "single_family_training_series": bool(single_family_training_series),
        "single_family_name": single_family_name,
        "detected_types": detected,
        "lora_family_count": len(families),
        "lora_families": families[:40],
        # Compatibility aliases for classifier-v2 snapshots/diagnostics.
        "logical_lora_family_count": len(families),
        "logical_lora_families": families[:40],
        "scores": {kind: int(score) for kind, score in ranked},
        "evidence": {kind: values[:8] for kind, values in reasons.items() if values},
        "workflow_file_count": len(workflow_files),
        "weight_file_count": len(weight_files),
        "component_evidence": components,
    }



def humanize_collection_family_name(value):
    """Return a readable display label without changing family identity.

    Repository/file identity remains case-folded internally for grouping,
    favorites, and future update tracking. This helper is display-only.
    When the uploader already used mixed casing we preserve that token casing;
    for all-lowercase repository filenames we apply conservative product/model
    casing so titles do not look accidentally normalized to lowercase.
    """
    text = re.sub(r"[_-]+", " ", str(value or "").strip())
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""

    known = {
        "lora": "LoRA",
        "lokr": "LoKr",
        "locon": "LoCon",
        "loha": "LoHa",
        "dora": "DoRA",
        "lycoris": "LyCORIS",
        "flux": "FLUX",
        "ltx": "LTX",
        "scail": "SCAIL",
        "sdxl": "SDXL",
        "sd3": "SD3",
        "krea2": "Krea2",
    }

    parts = []
    for token in text.split(" "):
        lower = token.casefold()
        if lower in known:
            parts.append(known[lower])
            continue
        # Preserve meaningful uploader casing such as artyPROJECTArt.
        if any(ch.isupper() for ch in token[1:]) or (token and token[0].isupper()):
            parts.append(token)
            continue
        # Keep compact version/rank/step tokens conventional.
        if re.fullmatch(r"[vr]\d+(?:\.\d+)*", lower):
            parts.append(lower)
            continue
        if re.fullmatch(r"\d+(?:\.\d+)*", lower):
            parts.append(token)
            continue
        parts.append(token[:1].upper() + token[1:] if token else token)
    return " ".join(parts)


def synthesize_collection_title(author, architecture="", collection_type="", repo_name=""):
    """Return a stable human-facing title for a repository collection."""
    author = str(author or "").strip()
    architecture = str(architecture or "").strip()
    collection_type = str(collection_type or "").strip()
    repo_name = str(repo_name or "").strip()

    if author:
        possessive = f"{author}'" if author.casefold().endswith("s") else f"{author}'s"
        if architecture and architecture.casefold() != "other":
            return f"{possessive} {architecture} Collection"
        if collection_type:
            return f"{possessive} {collection_type} Collection"
        return f"{possessive} Collection"

    if repo_name:
        cleaned = re.sub(r"[_-]+", " ", repo_name).strip()
        cleaned = re.sub(r"\s+", " ", cleaned)
        if cleaned:
            return cleaned
    if collection_type:
        return f"{collection_type} Collection"
    return "Model Collection"


def _display_lora_family(path):
    name = _file_path(path).replace("\\", "/").rsplit("/", 1)[-1]
    stem = re.sub(r"\.(?:safetensors|ckpt|pt|pth|bin|gguf)$", "", name, flags=re.I).strip()
    stem = re.sub(r"[-_.]?\d{4,6}-of-\d{4,6}$", "", stem)
    suffixes = (
        r"(?:[-_. ](?:epoch|ep|step|steps|checkpoint|ckpt)[-_. ]?\d+)$",
        r"(?:[-_. ]r(?:ank)?[-_. ]?\d+)$",
        r"(?:[-_. ](?:rank)[-_. ]?\d+)$",
        r"(?:[-_. ]0\d{5,})$",
        r"(?:[-_. ](?:fp16|fp32|bf16|fp8|int8|int4))$",
    )
    changed = True
    while changed and stem:
        changed = False
        for pattern in suffixes:
            updated = re.sub(pattern, "", stem, flags=re.I).strip("-_. ")
            if updated != stem:
                stem = updated
                changed = True
    return stem or name


def _variant_label(path, family_display):
    name = _file_path(path).replace("\\", "/").rsplit("/", 1)[-1]
    stem = re.sub(r"\.(?:safetensors|ckpt|pt|pth|bin|gguf)$", "", name, flags=re.I).strip()
    family = str(family_display or "").strip()
    remainder = stem[len(family):] if family and stem.casefold().startswith(family.casefold()) else ""
    remainder = remainder.strip("-_. ")
    if remainder:
        if re.fullmatch(r"0\d{5,}", remainder):
            return f"step {int(remainder)}"
        return remainder.replace("_", " ")
    for pattern, prefix in (
        (r"(?:^|[-_. ])r(?:ank)?[-_. ]?(\d+)(?:$|[-_. ])", "r"),
        (r"(?:^|[-_. ])(?:epoch|ep)[-_. ]?(\d+)(?:$|[-_. ])", "epoch "),
        (r"(?:^|[-_. ])(?:step|steps)[-_. ]?(\d+)(?:$|[-_. ])", "step "),
        (r"(?:^|[-_. ])(?:checkpoint|ckpt)[-_. ]?(\d+)(?:$|[-_. ])", "checkpoint "),
    ):
        match = re.search(pattern, stem, flags=re.I)
        if match:
            return f"{prefix}{match.group(1)}"
    return ""


def collection_family_id(source, model_key, collection_type, family_key):
    """Return a stable source/repository-scoped identifier for one model family."""
    # Do not include display/artifact type in the identity. A classifier can
    # improve from LoRA -> LyCORIS (or similar) without invalidating a user's
    # favorite/update history for the same repository family.
    parts = (
        str(source or "").strip().casefold(),
        str(model_key or "").strip().casefold(),
        str(family_key or "").strip().casefold(),
    )
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:24]
    return f"cf_{digest}"


def build_collection_groups(files, collection_type="LoRA", source="", model_key=""):
    """Group repository files into virtual model families for Collection view.

    This deliberately does not create top-level model rows. File indexes are
    retained so existing tracked source-download routes can be reused safely.
    """
    collection_type = str(collection_type or "LoRA").strip() or "LoRA"
    grouped = {}

    for index, item in enumerate(files or []):
        if not isinstance(item, dict):
            continue
        path = _file_path(item)
        lower = path.casefold().replace("\\", "/")
        if not path or not lower.endswith(".safetensors"):
            continue
        if any(marker in lower for marker in (
            "/optimizer", "optimizer.", "training_state", "scheduler",
            "/checkpoint-", "/checkpoints/", "global_step", "mp_rank", "zero_pp_rank"
        )):
            continue

        if collection_type.casefold() == "lora":
            key = _normalize_lora_family(path)
            display = _display_lora_family(path)
        else:
            display = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
            key = re.sub(r"[^a-z0-9]+", " ", display.casefold()).strip()
        if not key:
            continue

        group = grouped.setdefault(key, {
            "key": key,
            "name": display,
            "artifact_type": collection_type,
            "files": [],
            "total_size_bytes": 0,
        })
        size = _file_size(item)
        variant = _variant_label(path, group["name"])
        group["files"].append({
            **dict(item),
            "_download_index": index,
            "variant_label": variant,
        })
        group["total_size_bytes"] += size

    def natural_key(value):
        return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", str(value or ""))]

    groups = list(grouped.values())
    for group in groups:
        group["files"].sort(key=lambda file_data: (natural_key(file_data.get("variant_label")), natural_key(file_data.get("name") or file_data.get("path"))))
        variants = []
        for file_data in group["files"]:
            label = str(file_data.get("variant_label") or "").strip()
            if label and label not in variants:
                variants.append(label)
        group["variants"] = variants
        group["file_count"] = len(group["files"])
        group["family_id"] = collection_family_id(
            source, model_key, collection_type, group.get("key") or ""
        )
        search_parts = [
            group.get("name") or "",
            group.get("artifact_type") or "",
            " ".join(variants),
        ]
        search_parts.extend(
            str(file_data.get("name") or file_data.get("path") or "")
            for file_data in group["files"]
        )
        group["search_text"] = " ".join(search_parts).casefold()

    groups.sort(key=lambda group: str(group.get("name") or "").casefold())
    return groups

def needs_repository_classification_refresh(card_data, minimum_version=None):
    """Return True when a stored HF/ModelScope snapshot predates this classifier."""
    if minimum_version is None:
        minimum_version = REPOSITORY_CLASSIFIER_VERSION

    value = card_data
    if isinstance(value, str):
        try:
            import json
            value = json.loads(value or "{}")
        except Exception:
            value = {}
    if not isinstance(value, dict):
        return True
    classification = value.get("repository_classification")
    if not isinstance(classification, dict):
        return True
    try:
        return int(classification.get("version") or 0) < int(minimum_version)
    except (TypeError, ValueError):
        return True
