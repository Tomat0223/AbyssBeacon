import re
import json


def _flatten_sensitive_text(value):

    if value is None:
        return ""

    if isinstance(value, dict):
        return " ".join(
            _flatten_sensitive_text(v)
            for v in value.values()
        )

    if isinstance(value, (list, tuple, set)):
        return " ".join(
            _flatten_sensitive_text(v)
            for v in value
        )

    return str(value)


def detect_sensitive(*values):
    """
    Normalize source metadata into ModelRadar's mature-content flag.

    Prefer explicit source labels/tags, then use boundary-aware terms so
    innocent substrings such as "cocktail" do not trigger the filter.
    """

    text = " ".join(
        _flatten_sensitive_text(value)
        for value in values
    ).lower()

    # Common source-provided labels and unambiguous phrases.
    phrases = [
        "not-for-all-audiences",
        "not for all audiences",
        "adult content",
        "adult-content",
        "sexually explicit",
        "explicit sexual",
    ]

    if any(phrase in text for phrase in phrases):
        return True

    # Boundary-aware vocabulary catches model names/tags without matching
    # ordinary words that merely contain one of these strings.
    terms = [
        "nsfw",
        "porn",
        "pornography",
        "xxx",
        "hentai",
        "erotic",
        "erotica",
        "nude",
        "nudity",
        "naked",
        "sexual",
        "sex",
        "cumshot",
        "cumshots",
        "blowjob",
        "fellatio",
        "genital",
        "genitals",
        "penis",
        "vagina",
        "vulva",
        "boobs",
        "tits",
        "pussy",
        "cock",
        "dick",
    ]

    return any(
        re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text)
        for term in terms
    )


def extract_format(files):

    filenames = []

    for file in files:

        if isinstance(file, dict):

            filename = file.get(
                "path",
                file.get("name", "")
            )

        else:

            filename = file

        if filename:

            filenames.append(
                str(filename)
            )


    text = " ".join(
        filenames
    ).lower()


    if ".safetensors" in text:
        return "safetensors"

    if ".gguf" in text:
        return "gguf"

    if ".ckpt" in text:
        return "ckpt"

    if ".pt" in text:
        return "pt"

    return ""


def extract_files(details):

    siblings = details.get(
        "siblings",
        []
    )

    files = []

    for file in siblings:

        filename = file.get(
            "rfilename",
            ""
        )

        if filename:
            files.append(filename)

    return files


def extract_description(details):
    """Return readable model-card prose while preserving useful paragraph structure."""
    import re

    def clean_description(value):
        if not isinstance(value, str) or not value.strip():
            return ""
        text = value.replace("\r\n", "\n").replace("\r", "\n")
        text = re.sub(r"```.*?```", "\n", text, flags=re.S)
        text = re.sub(r"!\[[^]]*\]\([^)]*\)", "\n", text)
        text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
        text = re.sub(r"</(?:p|div|li|h[1-6])>", "\n", text, flags=re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\[([^]]+)\]\([^)]*\)", r"\1", text)
        text = re.sub(r"^\s*#{1,6}\s*", "", text, flags=re.M)
        text = re.sub(r"^[ \t]*[-*+]\s+", "• ", text, flags=re.M)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r" *\n *", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip(" -*_`#\n\t")

        blocks=[]
        for part in re.split(r"\n\s*\n", text):
            part=part.strip()
            if not part:
                continue
            # Preserve lists and intentional line breaks; unwrap ordinary hard-wrapped prose.
            if "\n• " in part or part.startswith("• "):
                cleaned=part
            else:
                cleaned=re.sub(r"\n+", " ", part)
            cleaned=re.sub(r" {2,}", " ", cleaned).strip()
            if cleaned:
                blocks.append(cleaned)
        text="\n\n".join(blocks)

        # Some APIs return one enormous flattened paragraph. Add conservative breaks at
        # sentence boundaries only when it materially improves a wall of text.
        if "\n\n" not in text and len(text) > 700:
            sentences=re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", text)
            if len(sentences) >= 5:
                groups=[]
                for i in range(0, len(sentences), 3):
                    groups.append(" ".join(sentences[i:i+3]))
                text="\n\n".join(groups)
        return text[:5000].strip()

    card_data = details.get("cardData", {})
    if isinstance(card_data, dict):
        description = clean_description(card_data.get("description", ""))
        if description:
            return description

    for key in ("description", "Description", "ModelDescription", "model_description", "README", "Readme"):
        description = clean_description(details.get(key, ""))
        if description:
            return description

    return clean_description(details.get("readme", ""))

def extract_base_model(details):
    """Extract explicit base-model metadata before using loose repository tags."""
    if not isinstance(details, dict):
        return ""

    containers = [
        details,
        details.get("cardData") if isinstance(details.get("cardData"), dict) else {},
        details.get("card_data") if isinstance(details.get("card_data"), dict) else {},
        details.get("model") if isinstance(details.get("model"), dict) else {},
    ]
    keys = (
        "base_model", "baseModel", "BaseModel",
        "base_model_name", "baseModelName",
        "base_model_title", "baseModelTitle",
    )

    for container in containers:
        for key in keys:
            value = container.get(key)
            if isinstance(value, list):
                cleaned = [str(x).strip() for x in value if str(x).strip()]
                if cleaned:
                    return " ".join(cleaned)
            elif value is not None and str(value).strip():
                return str(value).strip()

    tags = details.get("tags", [])
    if isinstance(tags, dict):
        tags = list(tags.values())
    if not isinstance(tags, (list, tuple, set)):
        tags = [tags]

    explicit = []
    repository_like = []
    for raw_tag in tags:
        tag = str(raw_tag or "").strip()
        if not tag:
            continue
        lower = tag.casefold()

        if lower.startswith("base_model:"):
            value = tag[len("base_model:"):].strip()
            for qualifier in ("quantized:", "adapter:", "finetune:", "merge:"):
                if value.casefold().startswith(qualifier):
                    value = value[len(qualifier):].strip()
            if value:
                explicit.append(value)

        elif "/" in tag and not lower.startswith(
            ("license:", "region:", "language:", "pipeline:")
        ):
            repository_like.append(tag)

    if explicit:
        return max(explicit, key=len)
    if repository_like:
        return max(repository_like, key=len)

    return ""


def extract_license(details):

    card_data = details.get(
        "cardData",
        {}
    )

    if isinstance(card_data, dict):

        return card_data.get(
            "license",
            ""
        )


    return ""


def extract_pipeline(details):

    pipeline = details.get(
        "pipeline_tag",
        ""
    )

    return pipeline


def extract_parameters(details):

    return ""


def extract_parent_model(details):

    card_data = details.get(
        "cardData",
        {}
    )

    if isinstance(card_data, dict):

        return card_data.get(
            "parent_model",
            ""
        )


    return ""


def extract_quantization(details):

    tags = details.get(
        "tags",
        []
    )


    if isinstance(tags, list):

        text = " ".join(tags).lower()

    else:

        text = str(tags).lower()


    for q in [
        "gguf",
        "int8",
        "int4",
        "8bit",
        "4bit",
        "quantized",
        "nf4"
    ]:

        if q in text:
            return q


    return ""


def detect_display_name(files, fallback):

    ignored = {

        "model",
        "models",
        "model_file",
        "model_weights",
        "weights",
        "checkpoint",
        "checkpoints",
        "adapter_model",
        "pytorch_model",
        "diffusion_pytorch_model",
        "last",
        "latest",
        "text_encoder",
        "unet",
        "vae",
        "tokenizer"

    }


    candidates = []


    for filename in files:

        if isinstance(filename, dict):

            filename = filename.get(
                "path",
                filename.get("name", "")
            )

        name = filename.split("/")[-1]


        if not name.lower().endswith(
            (
                ".safetensors",
                ".ckpt",
                ".pt"
            )
        ):
            continue


        name = name.rsplit(
            ".",
            1
        )[0]


        lower_name = name.lower()


        # Ignore generic names
        if lower_name in ignored:
            continue


        # Ignore numbered shards
        if name.isdigit():
            continue


        # Ignore index files
        if "index" in lower_name:
            continue


        candidates.append(name)


    if candidates:

        name = max(
            candidates,
            key=len
        )


        name = re.sub(
            r"-?\d{5}-of-\d{5}$",
            "",
            name
        )


        return name.replace(
            "_",
            " "
        ).strip()


    return fallback.replace(
        "-",
        " "
    ).strip()


def extract_display_tags(
    details,
    files
):

    display_tags = []


    # pipeline
    pipeline = details.get(
        "pipeline",
        ""
    )

    if pipeline:
        display_tags.append(
            pipeline.replace("-", " ")
        )


    # format
    model_format = extract_format(files)

    if model_format:
        display_tags.append(
            model_format
        )


    # base model
    base = extract_base_model(details)

    if base:

        # keep only the useful final name
        base = base.split("/")[-1]

        display_tags.append(
            base
        )


    # important keywords
    text = (
        str(details.get("tags", ""))
        + " "
        + str(details.get("readme", ""))
    ).lower()


    keywords = [

        "krea",
        "flux",
        "sdxl",
        "stable diffusion",
        "image-to-image",
        "text-to-image",
        "lora",
        "workflow"

    ]


    for keyword in keywords:

        if keyword in text:

            pretty = keyword.replace(
                "-",
                " "
            )

            if pretty not in display_tags:

                display_tags.append(
                    pretty
                )


    return display_tags[:5]


def count_preview_images(media):

    if not media:
        return 0


    if isinstance(media, str):

        try:
            media = json.loads(media)

        except Exception:
            return 0


    return len(
        [
            item
            for item in media
            if item.get("type") == "image"
        ]
    )


def is_gated(card_data):

    if not card_data:
        return False


    # card_data is stored as JSON text in SQLite, and a few older rows can be
    # double-encoded. Decode strings defensively, but never assume the decoded
    # value is an object.
    for _ in range(2):

        if not isinstance(card_data, str):
            break

        try:
            card_data = json.loads(card_data)

        except Exception:
            return False


    if not isinstance(card_data, dict):
        return False


    return bool(
        card_data.get("gated", False)
    )
