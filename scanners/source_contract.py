"""Common source-adapter contract for current and future ModelRadar scanners.

Scanners expose NAME, DISPLAY, ENABLED and scan(term, seen, settings), and scan()
returns model objects compatible with this normalized field set. This module is
intentionally documentation-first so adding a new provider does not require
copying assumptions out of an existing scanner.
"""
REQUIRED_MODEL_FIELDS = (
    "name", "display_name", "author", "source", "url", "model_key",
    "image", "description", "base_model", "architecture", "model_type",
    "created", "updated", "downloads", "likes", "license", "media",
)


def missing_model_fields(model):
    return [name for name in REQUIRED_MODEL_FIELDS if not hasattr(model, name)]
