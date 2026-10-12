"""One reduced FBX built from the FBX files of an accepted input."""

from contextlib import contextmanager
import gc
import json
from pathlib import PurePosixPath
import math
import threading
import time

try:
    import signal
except ImportError:  # pragma: no cover
    signal = None

from .archive import read_archive
from .fbx_binary import parse_fbx
from .fbx_scene import FbxScene, read_scene, unwrap
from .limits import Limits
from .npm_export import export_outline, export_scenes
from .simplify import OverBudget

MODEL_MAX_BYTES = 16 * 1024**2
MAX_ERROR = 0.02
# Processor seconds for one build of an input, all its files together. A build
# with the unwrapping that runs out of time, of memory or of room is repeated
# once without it, so two builds at most stay inside the time the checking
# process is given. Reading the input has the same time before the builds.
MODEL_CPU_SECONDS = 30.0
# Processor time kept past the build without the unwrapping when the process
# has a limit: the build with the unwrapping never spends into it.
PLAIN_CPU_MARGIN = 5.0
# Processor time left to write the report when a build is stopped at the limit.
REPORT_CPU_MARGIN = 3.0
# A mesh above this is reduced without its unwrapping from the start: with it
# the reduction would not fit the memory of the checking process.
UNWRAPPING_MESH_TRIANGLES = 400_000
# A source above this many triangles is not reduced within the stated error:
# that would not fit the time of the checking process. It gives its outline,
# one coarse mesh small enough for the view.
OUTLINE_SOURCE_TRIANGLES = 1_500_000
OUTLINE_TRIANGLES = 180_000
# The facts travel inside the report of the checks and must not crowd its
# findings out: a bounded list of meshes, and no list at all if names are long.
FACT_OBJECTS = 200
FACT_BYTES = 64 * 1024


def _facts(report):
    objects = report['objects']
    # An outline keeps to its cell, not to the error of a reduction.
    facts = {'status': 'built', 'reason': None, 'max_error': None if 'outline' in report else MAX_ERROR, **report,
             'objects': objects[:FACT_OBJECTS], 'objects_total': len(objects)}
    if len(json.dumps(facts, ensure_ascii=False).encode('utf-8')) > FACT_BYTES:
        facts['objects'] = []
    return facts


def cpu_left():
    """Processor seconds the process may still spend, infinite without a limit."""
    try:
        import resource
    except ImportError:
        return math.inf
    limit = resource.getrlimit(resource.RLIMIT_CPU)[0]
    return math.inf if limit == resource.RLIM_INFINITY else limit - time.process_time()


@contextmanager
def stopped_after(seconds):
    """Raise OverBudget in the running code once `seconds` of processor time pass.

    The reduction looks at its time between its own steps only. This stops
    whatever else a build does, so no work on an input can outlast the time
    given to it. It fires once and only for the main thread of a process
    that has such a timer; elsewhere the build keeps its own checks.
    """
    if seconds == math.inf or signal is None or not hasattr(signal, 'setitimer') \
            or threading.current_thread() is not threading.main_thread():
        yield
        return
    if seconds <= 0:
        raise OverBudget
    state = {'armed': True}
    def stop(*_):
        if state['armed']:
            state['armed'] = False
            raise OverBudget
    previous = signal.signal(signal.SIGPROF, stop)
    signal.setitimer(signal.ITIMER_PROF, seconds)
    try:
        yield
    finally:
        state['armed'] = False
        signal.setitimer(signal.ITIMER_PROF, 0)
        signal.signal(signal.SIGPROF, previous)


def build_reduced_model(data, kind, limits=None, max_bytes=MODEL_MAX_BYTES, cpu_seconds=MODEL_CPU_SECONDS):
    """Return (FBX bytes or None, facts) for a `zip-fbx` or `fbx` input."""
    return reduce_input(data, kind, limits, max_bytes, cpu_seconds)[:2]


def reduce_input(data, kind, limits=None, max_bytes=MODEL_MAX_BYTES, cpu_seconds=MODEL_CPU_SECONDS):
    """Return (FBX bytes or None, facts, written meshes) for a `zip-fbx` or `fbx` input.

    The meshes are those of the file, for a view of exactly what was built.
    `data` is the bytes of the input or, for `fbx`, its scene already read
    with the same limits: the reading then costs this build nothing.

    The source files are taken as already sharing coordinates: nothing is
    moved apart from the common shift to the local origin stated in the facts.
    An input the scene reader cannot read gives no file, never a guess.
    A reduction that outlasts `cpu_seconds` gives no file either: the report
    of the checks must not be lost to it. The unwrapping and the pictures are
    kept when the file with them fits `max_bytes`, `cpu_seconds` and the memory
    of the process; otherwise the model is built again without them, with
    the same time of its own. A source above OUTLINE_SOURCE_TRIANGLES gives
    its outline instead, in `cpu_seconds` of its own after the reading.
    """
    limits = limits or Limits()
    started = time.process_time()
    expired = lambda: time.process_time() - started > cpu_seconds
    result = None
    try:
        if isinstance(data, FbxScene):
            scenes = [('model', data)]
        else:
            if kind == 'fbx':
                sources = [('model', data)]
            else:
                sources = [('ground' if PurePosixPath(entry.name).stem.lower().endswith('_ground') else 'building', entry.data)
                           for entry in read_archive(data, limits).entries if entry.name.lower().endswith('.fbx')]
            if not sources:
                return None, {'status': 'not_built', 'reason': 'model_unsupported'}, ()
            scenes = [(role, read_scene(parse_fbx(source, limits), limits)) for role, source in sources]
        outlined = sum(len(mesh.triangles) for _, scene in scenes for mesh in scene.meshes) // 3 > OUTLINE_SOURCE_TRIANGLES
        if outlined:
            # Reading such a source takes most of the time a build has; the
            # outline is quick and starts its own count.
            started = time.process_time()
            with stopped_after(cpu_left() - REPORT_CPU_MARGIN):
                result = export_outline(scenes, OUTLINE_TRIANGLES, expired=expired, max_bytes=max_bytes)
        elif expired():
            raise OverBudget
        # The build with the unwrapping stops while the process still has the
        # whole time of the build without it.
        spent = lambda: expired() or cpu_left() < cpu_seconds + PLAIN_CPU_MARGIN
        unwrapped = not outlined and not spent() and all(len(mesh.triangles) // 3 <= UNWRAPPING_MESH_TRIANGLES
                                        for _, scene in scenes for mesh in scene.meshes)
        if unwrapped:
            try:
                with stopped_after(min(cpu_seconds - (time.process_time() - started),
                                        cpu_left() - cpu_seconds - PLAIN_CPU_MARGIN)):
                    # The texture coordinates are read here and nowhere before:
                    # checking and reading an input cost what they cost without them.
                    result = export_scenes([(role, unwrap(scene, limits)) for role, scene in scenes],
                                           max_error=MAX_ERROR, expired=spent, max_bytes=max_bytes)
            except (MemoryError, OverBudget, ValueError, ArithmeticError, RecursionError):
                # Whatever stops the build with the unwrapping, the build
                # without it still has its turn.
                pass
        if not outlined and (result is None or len(result.data) > max_bytes):
            # The first build and its texture coordinates are let go before
            # the build without them takes its memory.
            result = None
            gc.collect()
            # After a build with the unwrapping the build without it has the
            # whole time of its own. Without one it shares its time with the
            # reading, as it always did.
            if unwrapped:
                started = time.process_time()
            # Short of the limit of the process it stops with the report kept.
            with stopped_after(cpu_left() - REPORT_CPU_MARGIN):
                result = export_scenes(scenes, max_error=MAX_ERROR, expired=expired, max_bytes=max_bytes, unwrapping=False)
    except (ValueError, ArithmeticError, RecursionError):
        return None, {'status': 'not_built', 'reason': 'model_unsupported'}, ()
    except (MemoryError, OverBudget):
        return None, {'status': 'not_built', 'reason': 'model_budget'}, ()
    if not 1 <= len(result.data) <= max_bytes:
        return None, {'status': 'not_built', 'reason': 'model_budget'}, ()
    return result.data, _facts(result.report), result.meshes
