"""One reduced FBX built from the FBX files of an accepted input."""

import json
from pathlib import PurePosixPath
import time

from .archive import read_archive
from .fbx_binary import parse_fbx
from .fbx_scene import read_scene
from .limits import Limits
from .npm_export import export_scenes
from .simplify import OverBudget

MODEL_MAX_BYTES = 16 * 1024**2
MAX_ERROR = 0.02
# Processor seconds for one input, all its files together: well inside the time
# the checking process is given.
MODEL_CPU_SECONDS = 30.0
# The facts travel inside the report of the checks and must not crowd its
# findings out: a bounded list of meshes, and no list at all if names are long.
FACT_OBJECTS = 200
FACT_BYTES = 64 * 1024


def _facts(report):
    objects = report['objects']
    facts = {'status': 'built', 'reason': None, 'max_error': MAX_ERROR, **report,
             'objects': objects[:FACT_OBJECTS], 'objects_total': len(objects)}
    if len(json.dumps(facts, ensure_ascii=False).encode('utf-8')) > FACT_BYTES:
        facts['objects'] = []
    return facts


def build_reduced_model(data, kind, limits=None, max_bytes=MODEL_MAX_BYTES, cpu_seconds=MODEL_CPU_SECONDS):
    """Return (FBX bytes or None, facts) for a `zip-fbx` or `fbx` input.

    The source files are taken as already sharing coordinates: nothing is
    moved apart from the common shift to the local origin stated in the facts.
    An input the scene reader cannot read gives no file, never a guess.
    A reduction that outlasts `cpu_seconds` gives no file either: the report
    of the checks must not be lost to it.
    """
    limits = limits or Limits()
    started = time.process_time()
    expired = lambda: time.process_time() - started > cpu_seconds
    try:
        if kind == 'fbx':
            sources = [('model', data)]
        else:
            sources = [('ground' if PurePosixPath(entry.name).stem.lower().endswith('_ground') else 'building', entry.data)
                       for entry in read_archive(data, limits).entries if entry.name.lower().endswith('.fbx')]
        if not sources:
            return None, {'status': 'not_built', 'reason': 'model_unsupported'}
        scenes = [(role, read_scene(parse_fbx(source, limits), limits)) for role, source in sources]
        if expired():
            raise OverBudget
        result = export_scenes(scenes, max_error=MAX_ERROR, expired=expired)
    except (ValueError, ArithmeticError, RecursionError):
        return None, {'status': 'not_built', 'reason': 'model_unsupported'}
    except (MemoryError, OverBudget):
        return None, {'status': 'not_built', 'reason': 'model_budget'}
    if not 1 <= len(result.data) <= max_bytes:
        return None, {'status': 'not_built', 'reason': 'model_budget'}
    return result.data, _facts(result.report)
