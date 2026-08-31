import re
from scanners.common.model import Model

# Developer-only scanner diagnostics. Keep False for normal ModelRadar use.
DEBUG_SCANNERS = False

def debug_print(*args, **kwargs):
    if DEBUG_SCANNERS:
        print(*args, **kwargs)


from utils.loader import (
    load_architectures,
    load_model_types
)


from scanners.common import metadata
from scanners.common.repository_classifier import classify_repository, synthesize_collection_title, humanize_collection_family_name



def classify(text, rules):

    text = text.lower()

    for name, data in rules.items():

        for keyword in data.get("keywords", []):

            if keyword.lower() in text:
                return name

    return "Other"



def _normalize_architecture_text(value):
    """Normalize source punctuation/separators without losing model numbers."""
    text = str(value or "").casefold()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _architecture_aliases():
    """Build one canonical alias vocabulary directly from architectures.json."""
    aliases = []
    for name, data in load_architectures().items():
        values = [name, data.get("label", "")]
        values.extend(data.get("keywords", []) or [])
        for source_data in (data.get("source_searches", {}) or {}).values():
            if isinstance(source_data, dict):
                values.extend(source_data.get("terms", []) or [])
        for value in values:
            normalized = _normalize_architecture_text(value)
            if normalized:
                aliases.append((normalized, name))

    # Specific variants must win over broad aliases (Z Image Turbo before
    # Z Image, FLUX.2 Klein 9B before any shorter spelling, etc.).
    aliases.sort(key=lambda pair: len(pair[0]), reverse=True)
    return aliases


def classify_architecture(*values):
    """Resolve heterogeneous source metadata to a canonical ModelRadar label."""
    text = _normalize_architecture_text(" ".join(
        str(value or "") for value in values if value is not None
    ))
    if not text:
        return "Other"

    # Common repository/source spellings sometimes put BASE before the size.
    if re.search(r"\bflux\s*2\s*klein\s*base\s*4b\b", text):
        return "FLUX.2 Klein 4B"
    if re.search(r"\bflux\s*2\s*klein\s*base\s*9b\b", text):
        return "FLUX.2 Klein 9B"

    padded = f" {text} "
    compact = text.replace(" ", "")

    for alias, name in _architecture_aliases():
        if f" {alias} " in padded:
            return name
        alias_compact = alias.replace(" ", "")
        if len(alias_compact) >= 6 and alias_compact in compact:
            return name

    return "Other"



def classify_model_type(text):

    return classify(
        text,
        load_model_types()
    )




def classify_architecture_with_watch_fallback(watch, *values):
    """Use the exact scan watch only after direct metadata classification fails."""
    classified = classify_architecture(*values)
    if classified != "Other":
        return classified

    watch_class = classify_architecture(watch)
    if watch_class == "Other":
        return "Other"

    evidence = _normalize_architecture_text(" ".join(
        str(value or "") for value in values if value is not None
    ))

    # Explicit same-family contradictions must win over search provenance.
    if watch_class == "WAN 2.2":
        if re.search(r"\bwan\s*2\s*1\b", evidence) or "wan21" in evidence.replace(" ", ""):
            return "Other"

    if watch_class == "FLUX.2 Klein 4B" and re.search(r"\b9b\b", evidence):
        return "Other"
    if watch_class == "FLUX.2 Klein 9B" and re.search(r"\b4b\b", evidence):
        return "Other"

    if watch_class == "Z-Image Turbo" and re.search(r"\bz\s*image\s*base\b", evidence):
        return "Other"
    if watch_class == "Z-Image Base" and re.search(r"\bz\s*image\s*turbo\b", evidence):
        return "Other"

    if watch_class == "LTX-2.5" and re.search(r"\bltxv?\s*2\s*3\b", evidence):
        return "Other"

    return watch_class



def build_model(raw):

    details = raw.get(
        "details",
        {}
    )

    model_id = raw["model_id"]

    tags = raw.get(
        "tags",
        ""
    )

    if isinstance(tags, list):
        tags = " ".join(tags)

    files = raw.get(
        "files",
        []
    )

    base_model = metadata.extract_base_model(details)


    file_text = " ".join(
        file.get("path", "")
        if isinstance(file, dict)
        else str(file)
        for file in files
    )

    text = (
        model_id
        + " "
        + tags
        + " "
        + file_text
        + " "
        + base_model
    )


    model = Model()


    model.name = model_id.split("/")[-1]


    repo_name = model_id.split("/")[-1]

    model.name = repo_name

    model.display_name = metadata.detect_display_name(
        files,
        repo_name
    )


    model.author = model_id.split("/")[0]

    model.model_key = model_id.lower().strip()

    model.architecture = classify_architecture(base_model)
    if model.architecture == "Other":
        model.architecture = classify_architecture(text)

    model.model_type = classify_model_type(text)

    repository_classification = classify_repository(raw)
    if repository_classification:
        classified_type = str(repository_classification.get("display_type") or "").strip()
        if classified_type and classified_type != "Other":
            model.model_type = classified_type
        if repository_classification.get("container") == "collection":
            if repository_classification.get("collection_shape") == "training_series":
                family_name = str(repository_classification.get("single_family_name") or "").strip()
                model.display_name = humanize_collection_family_name(family_name) or model.display_name
            else:
                model.display_name = synthesize_collection_title(
                    model.author,
                    model.architecture,
                    repository_classification.get("primary_artifact_type"),
                    repo_name,
                )

    model.source = raw.get(
        "source",
        ""
    )


    model.model_key = raw.get(
        "model_key",
        model_id.lower().strip()
    )


    model.url = raw.get(
        "url",
        ""
    )

    model.tags = tags

    model.files = files

    model.base_model = base_model

    model.description = metadata.extract_description(details)

    model.display_tags = metadata.extract_display_tags(
        details,
        files
    )
    if repository_classification:
        primary_artifact = str(repository_classification.get("primary_artifact_type") or "").strip()
        if repository_classification.get("container") == "collection":
            label = f"{primary_artifact} Collection" if primary_artifact else "Collection"
            model.display_tags = [label] + [tag for tag in model.display_tags if str(tag).casefold() != label.casefold()]
            model.display_tags = model.display_tags[:5]

    model.license = metadata.extract_license(details)

    model.pipeline = metadata.extract_pipeline(details)

    model.parameters = metadata.extract_parameters(details)

    model.quantization = metadata.extract_quantization(details)

    model.format = metadata.extract_format(files)

    model.parent_model = metadata.extract_parent_model(details)

    model.image = raw.get("image", "")

    model.preview_count = raw.get(
        "preview_count",
        0
    )

    model.has_media = raw.get(
        "has_media",
        False
    )

    model.has_video = raw.get(
        "has_video",
        False
    )

    model.media = raw.get(
        "media",
        []
    )


    # Use first image from media as card thumbnail
    # if no explicit image exists

    if not model.image and model.media:

        for item in model.media:

            if item.get("type") == "image":

                model.image = item.get(
                    "url",
                    ""
                )

                break


    debug_print(
        "PROCESSOR MEDIA:",
        model.name,
        len(model.media),
        model.media[:1]
    )

    debug_print(
        "MODEL IMAGE:",
        model.name,
        model.image
    )

    model.sha = raw.get(
        "sha",
        ""
    )

    model.created = raw.get(
        "created",
        details.get("createdAt", "")
    )

    model.updated = raw.get(
        "updated",
        details.get("lastModified", "")
    )

    model.downloads = details.get(
        "downloads",
        0
    )

    model.likes = details.get(
        "likes",
        0
    )

    model.gated = raw.get(
        "gated",
        False
    )

    model.card_data = raw.get(
        "card_data",
        {}
    )
    if not isinstance(model.card_data, dict):
        model.card_data = {}
    if repository_classification:
        model.card_data = dict(model.card_data)
        model.card_data["repository_classification"] = repository_classification

    model.sensitive = raw.get(
        "sensitive",
        False
    )

    if not model.display_name:
        print(
            "WARNING: EMPTY DISPLAY NAME",
            model_id
        )

    if model.display_name.lower() in [
        "model",
        "checkpoint",
        "adapter model",
    ]:
        debug_print(
            "GENERIC NAME:",
            model_id,
            "=>",
            model.display_name
        )

    return model