"""Public capabilities for inputs with accepted server diagnostics adapters."""

_EXTENSIONS = (
    ('zip-fbx', ('.zip',)), ('portable-package', ('.zip',)),
    ('fbx', ('.fbx',)), ('ifc', ('.ifc',)), ('glb', ('.glb',)),
    ('gltf', ('.gltf',)), ('obj', ('.obj',)), ('rvt', ('.rvt',)),
    ('dwg', ('.dwg',)), ('skp', ('.skp',)), ('3dm', ('.3dm',)),
)
# Standalone FBX and DWG have no legacy transport name: they exist only as descriptor v1.
# A DWG is taken as the site plan: its lines with heights are read and shown.
_ACCEPTED = frozenset({'zip-fbx', 'portable-package', 'fbx', 'dwg'})
DESCRIPTOR_ONLY = frozenset({'fbx', 'dwg'})


def public_formats() -> list[dict]:
    """Return fresh rows, without granting capabilities to planned adapters."""
    return [dict(id=kind, extensions=list(extensions),
        upload=kind in _ACCEPTED, diagnostics=kind in _ACCEPTED,
        preview=kind in _ACCEPTED, generation=False,
        reason=None if kind in _ACCEPTED else 'engine_unavailable')
        for kind, extensions in _EXTENSIONS]


def require_diagnostics(kind: str) -> None:
    """Reject unknown formats and adapters without accepted diagnostics."""
    rows = {row['id']: row for row in public_formats()}
    if kind not in rows:
        raise ValueError('unsupported_format')
    if not rows[kind]['diagnostics']:
        raise ValueError('engine_unavailable')
