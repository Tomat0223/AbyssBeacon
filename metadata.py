import re
import json

def extract_source_metadata(item):

    tags = item.get(
        "tags",
        []
    )

    if isinstance(tags, list):

        tags_text = " ".join(
            str(x)
            for x in tags
        )

    else:

        tags_text = str(tags)


    return {
        "tags": tags_text,

        "pipeline": item.get(
            "pipeline_tag",
            ""
        ),

        "sha": item.get(
            "sha",
            ""
        ),

        "updated": item.get(
            "lastModified",
            ""
        ),

        "downloads": item.get(
            "downloads",
            0
        ),

        "likes": item.get(
            "likes",
            0
        )
    }

def extract_description(details):

    card = details.get(
        "cardData",
        {}
    )


    if isinstance(card, dict):

        description = (
            card.get("description")
            or card.get("model_description")
            or ""
        )


        if description:

            if isinstance(description, list):

                return "\n".join(
                    str(x)
                    for x in description
                )

            return str(description)


    readme = details.get(
        "readme",
        ""
    )


    if readme:

        # remove markdown images
        readme = re.sub(
            r"!\[.*?\]\(.*?\)",
            "",
            readme
        )


        # remove excessive markdown headings
        readme = re.sub(
            r"#+ ",
            "",
            readme
        )


        return readme[:800].strip()


    return "No description available"


def extract_license(details):

    card = details.get(
        "cardData",
        {}
    )


    if isinstance(card, dict):

        license_name = card.get(
            "license",
            ""
        )

        if license_name:

            if isinstance(license_name, list):

                return ", ".join(
                    str(x)
                    for x in license_name
                )

            return str(license_name)


    tags = details.get(
        "tags",
        []
    )


    for tag in tags:

        if tag.startswith("license:"):

            return tag.replace(
                "license:",
                ""
            )


    return "Not specified"


def extract_base_model(details):

    card = details.get(
        "cardData",
        {}
    )


    if not isinstance(card, dict):
        return ""


    base = (
        card.get("base_model")
        or card.get("base_models")
        or card.get("finetuned_from")
        or ""
    )


    if isinstance(base, list):

        return ", ".join(
            str(x) for x in base
        )


    return str(base)


def extract_files(details):

    files = []


    for file in details.get("siblings", []):

        filename = file.get(
            "rfilename",
            ""
        )


        if filename:

            files.append(filename)


    return files



def extract_parameters(details):

    card = details.get(
        "cardData",
        {}
    )


    if not isinstance(card, dict):
        return ""


    params = (
        card.get("parameters")
        or card.get("params")
        or ""
    )


    if isinstance(params, list):

        return ", ".join(
            str(x) for x in params
        )


    return str(params)



def extract_parent_model(details):

    card = details.get(
        "cardData",
        {}
    )


    if not isinstance(card, dict):
        return ""


    parent = (
        card.get("base_model")
        or card.get("parent_model")
        or ""
    )


    if isinstance(parent, list):

        return ", ".join(
            str(x) for x in parent
        )


    return str(parent)


def extract_pipeline(details):

    pipeline = details.get(
        "pipeline_tag",
        ""
    )


    if pipeline:
        return pipeline


    card = details.get(
        "cardData",
        {}
    )


    if isinstance(card, dict):

        pipeline = (
            card.get("pipeline")
            or card.get("pipeline_tag")
            or ""
        )


        if pipeline:
            return pipeline


    tags = details.get(
        "tags",
        []
    )


    if isinstance(tags, list):

        for tag in tags:

            tag = str(tag).lower()


            if tag in [
                "text-to-image",
                "image-to-image",
                "text-generation",
                "image-classification",
                "diffusion"
            ]:

                return tag


    return ""


def is_gated(card_data):

    if not card_data:
        return False


    if isinstance(card_data, str):

        try:
            card_data = json.loads(card_data)

        except Exception:
            return False


    return bool(
        card_data.get("gated", False)
    )