"""Conservative, offline whole-card identity. Names and support files are not proof."""
import json
import re
from pathlib import PurePosixPath

MERGE_SOURCES = frozenset({'civitai', 'civitaired', 'seaart', 'tensorhub'})
WEIGHT_EXTENSIONS = frozenset({'.safetensors', '.ckpt', '.pt', '.pth', '.bin', '.gguf', '.onnx'})
SUPPORT_PATH = re.compile(
    r'(^|/)(vae|vae_approx|text_encoders?|tokenizers?|clip(?:_l|_g)?|'
    r't5(?:xxl|xl)?|umt5|byt5|siglip|sigclip|vision_encoders?|audio_encoders?|vocoder)'
    r'(/|[_.-]|$)|(^|/)(ae|taesd)\.(safetensors|pt|bin)$', re.I)
SUPPORT_ROLES = ('text encoder', 'tokenizer', 'vision encoder', 'audio encoder', 'vae', 'vocoder')


def decoded(value, fallback):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return fallback
    return value if isinstance(value, type(fallback)) else fallback


def sha256(file):
    candidates = [file.get(k) for k in ('sha256', 'sha', 'hash')]
    for key in ('hashes', 'hashs'):
        hashes = file.get(key)
        if isinstance(hashes, dict):
            candidates.extend(v for k, v in hashes.items() if str(k).replace('-', '').replace('_', '').lower() == 'sha256')
        elif isinstance(hashes, list):
            candidates.extend(h.get('hash') or h.get('value') or h.get('digest') for h in hashes
                              if isinstance(h, dict) and str(h.get('type') or h.get('name') or h.get('algorithm') or '').replace('-', '').lower() == 'sha256')
    for value in candidates:
        value = str(value or '').strip().lower()
        if re.fullmatch('[0-9a-f]{64}', value):
            return value
    return ''


def evidence(model):
    """Require every advertised version and model weight; never trust a card-level SHA."""
    files = decoded(model.get('files'), [])
    card = decoded(model.get('card_data'), {})
    provider = str(model.get('source') or '').lower()
    data = card.get(provider) if isinstance(card.get(provider), dict) else card
    versions = data.get('versions') or []
    if not isinstance(versions, list):
        return None
    declared = set()
    for version in versions:
        version_id = str((version.get('id') or version.get('version_id') or '') if isinstance(version, dict) else version).strip()
        if not version_id:
            return None
        declared.add(version_id)
    count = data.get('version_count')
    try:
        if count is not None and int(count) > len(declared):
            return None
    except (TypeError, ValueError):
        return None
    groups = {}
    for file in files:
        if not isinstance(file, dict):
            return None
        path = str(file.get('path') or file.get('name') or file.get('filename') or '').replace('\\', '/')
        role = str(file.get('role') or file.get('file_type') or '').lower().replace('_', ' ').replace('-', ' ')
        if SUPPORT_PATH.search(path) or any(word in role for word in SUPPORT_ROLES):
            continue
        if PurePosixPath(path).suffix.lower() not in WEIGHT_EXTENSIONS and file.get('primary') is not True:
            continue
        digest = sha256(file)
        if not digest:
            return None
        version_id = str(file.get('version_id') or file.get('model_version_id') or file.get('model_ver_no') or '').strip()
        if not version_id and len(declared) == 1:
            version_id = next(iter(declared))
        if declared and version_id not in declared:
            return None
        groups.setdefault(version_id, set()).add(digest)
    if not groups or (declared and set(groups) != declared):
        return None
    # Repeated identical weights in distinct versions remain distinct groups.
    return tuple(sorted(tuple(sorted(hashes)) for hashes in groups.values()))


def identity(model):
    source = str(model.get('source') or '').lower()
    kind = str(model.get('model_type') or '').strip().casefold()
    architecture = str(model.get('architecture') or '').strip().casefold()
    if source not in MERGE_SOURCES or kind not in {'checkpoint', 'lora'} or architecture in {'', 'other', 'unknown'}:
        return None
    weights = evidence(model)
    return (kind, architecture, weights) if weights else None
