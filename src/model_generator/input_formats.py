"""Public capabilities for inputs with accepted server diagnostics adapters."""

_EXTENSIONS = (
    ('zip-fbx', ('.zip',)), ('portable-package', ('.zip',)),
    ('fbx', ('.fbx',)), ('ifc', ('.ifc',)), ('glb', ('.glb',)),
    ('gltf', ('.gltf',)), ('obj', ('.obj',)), ('rvt', ('.rvt',)),
    ('dwg', ('.dwg',)), ('skp', ('.skp',)), ('3dm', ('.3dm',)),
)


def public_formats() -> list[dict]:
    """Return fresh rows, without granting capabilities to planned adapters."""
    return [dict(id=kind, extensions=list(extensions),
        upload=kind in {'zip-fbx', 'portable-package'},
        diagnostics=kind in {'zip-fbx', 'portable-package'},
        preview=kind == 'portable-package', generation=False,
        reason=None if kind in {'zip-fbx', 'portable-package'}
                    else 'engine_unavailable') for kind, extensions in _EXTENSIONS]


def require_diagnostics(kind: str) -> None:
    """Reject unknown formats and adapters without accepted diagnostics."""
    rows = {row['id']: row for row in public_formats()}
    if kind not in rows:
        raise ValueError('unsupported_format')
    if not rows[kind]['diagnostics']:
        raise ValueError('engine_unavailable')
