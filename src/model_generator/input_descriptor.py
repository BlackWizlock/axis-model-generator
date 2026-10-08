"""Versioned immutable upload metadata; filenames never depend on user paths."""
import re
import unicodedata
from .input_formats import require_diagnostics


def input_filename(version: int) -> str:
    if type(version) is not int or version not in (0, 1):
        raise ValueError('descriptor_version_unsupported')
    return ('input.zip', 'input.bin')[version]


def make_descriptor(kind: str, name: str, size: int, sha256: str) -> dict:
    require_diagnostics(kind)
    if type(size) is not int or not 1 <= size <= 256 * 1024**2:
        raise ValueError('invalid_upload')
    if not isinstance(name, str) or not isinstance(sha256, str):
        raise ValueError('invalid_upload')
    name = unicodedata.normalize('NFC', name)
    if not 1 <= len(name.encode('utf-8')) <= 160 or any(
            unicodedata.category(c).startswith('C') for c in name):
        raise ValueError('invalid_upload')
    if not re.fullmatch('[a-f0-9]{64}', sha256):
        raise ValueError('invalid_upload')
    return dict(version=1, kind=kind, displayName=name, bytes=size, sha256=sha256)
