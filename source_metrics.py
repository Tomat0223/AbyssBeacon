"""Source/version popularity without attributing listing totals to a version."""
import json


def mapping(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return {}
    return value if isinstance(value, dict) else {}


def count(value):
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        number = int(value)
        return number if number >= 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def version_statistics(version):
    stats = mapping(version.get("stats")) or mapping(version.get("rank"))
    def first(*keys):
        for key in keys:
            value = count(stats.get(key))
            if value is not None:
                return value
        return None
    return {
        "downloads": first("downloadCount", "downloadCountAllTime"),
        "likes": first("thumbsUpCount", "favoriteCount", "thumbsUpCountAllTime"),
        "created": version.get("createdAt") or version.get("publishedAt") or "",
        "updated": version.get("updatedAt") or version.get("publishedAt") or "",
    }


def versions(snapshot):
    card = mapping(snapshot.get("card_data"))
    values = card.get("versions") or snapshot.get("versions") or []
    return [v for v in values if isinstance(v, dict)] if isinstance(values, list) else []


def rank_value(snapshot, field, architectures, matches):
    """Sum exact matching version counts once; unknown is never a guessed zero.

    Mirrors are handled by the caller taking max across sources, not summing.
    A heterogeneous listing cannot donate its total to one selected architecture.
    """
    if snapshot.get("_mirrored_download_fallback"):
        return None
    records = versions(snapshot)
    if str(snapshot.get("source")) in {"civitai", "civitaired"} and not records:
        return None
    architecture_labels = {str(v.get("architecture") or v.get("base_model") or "").casefold() for v in records}
    if records and (str(snapshot.get("source")) in {"civitai", "civitaired"} or len(architecture_labels - {""}) > 1):
        selected = []
        seen = set()
        for version in records:
            identity = str(version.get("id") or version.get("name") or "")
            if not identity or identity in seen:
                continue
            seen.add(identity)
            architecture = version.get("architecture") or version.get("base_model") or ""
            if architectures and not matches(architecture, architectures):
                continue
            value = count(version.get(field))
            if value is None:
                value = version_statistics(version).get(field)
            selected.append(value)
        if not selected or any(value is None for value in selected):
            return None
        return sum(selected)
    # A provider repository/project count is usable only for its own known
    # architecture; it cannot stand in for an alternate architecture snapshot.
    architecture = snapshot.get("architecture") or snapshot.get("base_model") or ""
    if architectures and not matches(architecture, architectures):
        return None
    return count(snapshot.get(field))


def detail_version_fields(snapshot, version, format_date):
    source = str(snapshot.get("source") or "")
    specific = source in {"civitai", "civitaired"}
    stats = version_statistics(version)
    fields = {}
    for field in ("downloads", "likes"):
        value = count(version.get(field))
        if value is None:
            value = stats[field]
        if value is None and not specific:
            value = count(snapshot.get(field))
            scope = "Listing"
        else:
            scope = "Version"
        fields[field] = value
        fields[field + "_label"] = scope + " " + field
    for field in ("created", "updated"):
        value = version.get(field) or stats[field] or ""
        scope = "Version"
        if not value and not specific:
            value = snapshot.get(field) or ""
            scope = "Listing"
        fields[field] = format_date(value) if value else ""
        fields[field + "_label"] = scope + " " + field
    if snapshot.get("_mirrored_download_fallback"):
        return {"downloads": None, "likes": None, "created": "", "updated": "",
                "downloads_label": "Downloads", "likes_label": "Likes"}
    return fields
