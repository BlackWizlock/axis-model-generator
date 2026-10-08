"""Fixed guarded Blender process and independent acceptance after physical reap."""
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import selectors
import subprocess
import sys
import time
import shutil
from uuid import uuid4
from typing import Callable

from ..limits import ReadError
from ..png_inspection import inspect_png
from .config import Settings
from .preview import (PreviewError, PreviewLimits, ROUNDTRIP_TOLERANCE_METRES,
                      decode_preview_json)
from .process_guard import BLENDER_SHA256,identity,trusted_file


EXPECTED_VERSION = '4.5.14'
MEASUREMENTS_MAX_BYTES = 65536
THUMBNAIL_MAX_BYTES = 4 * 1024**2
_MEASUREMENT_FIELDS = frozenset({'schemaVersion', 'fingerprint', 'version',
                                'vertexCount', 'triangleCount', 'bounds',
                                'float32MaxErrorMetres'})


@dataclass(frozen=True)
class BlenderResult:
    version: str
    vertex_count: int
    triangle_count: int
    bounds: dict
    thumbnail_path: Path
    measurements_path: Path


@dataclass(frozen=True)
class _BlenderLaunch:
    argv: tuple[str, ...]
    environment: tuple[tuple[str, str], ...]
    cpu_seconds: int
    wall_seconds: int
    memory_bytes: int
    log_max_bytes: int = 65536


def _fail(code='preview_unsupported'):
    messages = {
        'preview_unsupported': 'Preview input, output or runtime contract is invalid',
        'preview_budget': 'Preview file exceeds its fixed byte budget',
        'preview_roundtrip_error': 'Preview measurements exceed the fixed geometry tolerance',
        'preview_cancelled': 'Preview was cancelled',
        'preview_runtime_unavailable': 'Verified guarded preview runtime is unavailable',
        'preview_resource': 'Preview exceeded its fixed process budget',
    }
    raise PreviewError(code, messages[code])


def _limits(settings):
    if not isinstance(settings, Settings):
        _fail()
    try:
        limits = PreviewLimits(settings.preview_max_instances, settings.preview_max_vertices,
                               settings.preview_max_triangles, settings.preview_max_bytes)
    except (ValueError, TypeError):
        _fail()
    for name, minimum, maximum in (
            ('blender_cpu_seconds', 1, 60), ('blender_wall_seconds', 1, 90),
            ('blender_memory_bytes', 32 * 1024**2, 1024**3)):
        value = getattr(settings, name)
        if type(value) is not int or not minimum <= value <= maximum:
            _fail()
    return limits


def _absolute(path):
    if not isinstance(path, Path) or not path.is_absolute() or '..' in path.parts:
        _fail()
    return path


def _relative(root, path):
    _absolute(path)
    try:
        relative = path.relative_to(root)
    except ValueError:
        _fail()
    if not relative.parts:
        _fail()
    return relative


def _directory_flags():
    # O_NOFOLLOW is mandatory. Unsupported hosts refuse instead of weakening it.
    try:
        return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    except AttributeError:
        _fail('preview_runtime_unavailable')


def _open_absolute_directory(path):
    _absolute(path)
    descriptor = os.open(path.anchor, _directory_flags())
    try:
        for name in path.parts[1:]:
            child = os.open(name, _directory_flags(), dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _private_directory(descriptor):
    metadata = os.fstat(descriptor)
    if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid() or
            stat.S_IMODE(metadata.st_mode) & 0o077):
        _fail()


@contextmanager
def _root(settings):
    root = _absolute(settings.data_root)
    if root == Path(root.anchor):
        _fail()
    descriptor = _open_absolute_directory(root)
    try:
        _private_directory(descriptor)
        yield root, descriptor
        # Refuse path replacement, including any ancestor symlink or rename.
        actual = _open_absolute_directory(root)
        try:
            before, after = os.fstat(descriptor), os.fstat(actual)
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                _fail()
        finally:
            os.close(actual)
    finally:
        os.close(descriptor)


@contextmanager
def _directory(root_descriptor, parts):
    descriptor = os.dup(root_descriptor)
    bindings = []
    try:
        for name in parts:
            child = os.open(name, _directory_flags(), dir_fd=descriptor)
            bindings.append((descriptor, name, child))
            descriptor = child
            _private_directory(child)
        yield descriptor
        for parent, name, child in bindings:
            _bound(parent, name, os.fstat(child))
    finally:
        os.close(descriptor)
        for parent, _, _ in bindings:
            os.close(parent)


def _stamp(metadata):
    return (metadata.st_dev, metadata.st_ino, metadata.st_size,
            metadata.st_mtime_ns, metadata.st_ctime_ns, metadata.st_nlink,
            metadata.st_mode, metadata.st_uid)


def _bound(directory, name, metadata):
    actual = os.stat(name, dir_fd=directory, follow_symlinks=False)
    if _stamp(actual) != _stamp(metadata):
        _fail()


def _read(directory, name, cap):
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                         dir_fd=directory)
    try:
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or
                before.st_uid != os.geteuid() or stat.S_IMODE(before.st_mode) & 0o022):
            _fail()
        if before.st_size > cap:
            _fail('preview_budget')
        buffer = bytearray()
        while True:
            chunk = os.read(descriptor, min(65536, cap + 1 - len(buffer)))
            if not chunk:
                break
            buffer.extend(chunk)
            if len(buffer) > cap:
                _fail('preview_budget')
        after = os.fstat(descriptor)
        if _stamp(before) != _stamp(after) or len(buffer) != after.st_size:
            _fail()
        _bound(directory, name, after)
        return bytes(buffer), after
    finally:
        os.close(descriptor)


def _input(root, root_descriptor, path, limits):
    relative = _relative(root, path)
    with _directory(root_descriptor, relative.parts[:-1]) as parent:
        wire, metadata = _read(parent, relative.name, limits.wire_bytes)
        document = decode_preview_json(wire, limits)
        _bound(parent, relative.name, metadata)
    return document, hashlib.sha256(wire).hexdigest()


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail()
        result[key] = value
    return result


def _number(value):
    if type(value) not in {float, int}:
        _fail()
    try:
        number = float(value)
    except (ValueError, OverflowError):
        _fail()
    if not math.isfinite(number):
        _fail()
    return number


def _measurements(wire, document, fingerprint, *, engine=None, version=EXPECTED_VERSION):
    try:
        result = json.loads(wire.decode('utf-8'), object_pairs_hook=_unique,
                            parse_constant=lambda value: _fail())
    except (UnicodeError, ValueError, RecursionError):
        _fail()
    fields=_MEASUREMENT_FIELDS | ({'engine'} if engine is not None else set())
    if type(result) is not dict or set(result) != fields:
        _fail()
    if type(result['schemaVersion']) is not int or result['schemaVersion'] != 1:
        _fail()
    if engine is not None and (type(result['engine']) is not str or result['engine']!=engine): _fail()
    if (type(result['version']) is not str or result['version'] != version or
            type(result['fingerprint']) is not str or result['fingerprint'] != fingerprint):
        _fail()
    for name in ('vertexCount', 'triangleCount'):
        if type(result[name]) is not int or result[name] != document[name]:
            _fail()
    bounds = result['bounds']
    if type(bounds) is not dict or set(bounds) != {'min', 'max'}:
        _fail()
    checked = {}
    for name in ('min', 'max'):
        vector = bounds[name]
        if type(vector) is not list or len(vector) != 3:
            _fail()
        checked[name] = [_number(value) for value in vector]
        for actual, expected in zip(checked[name], document['bounds'][name]):
            if abs(actual - expected) > ROUNDTRIP_TOLERANCE_METRES:
                _fail('preview_roundtrip_error')
    if any(low > high for low, high in zip(checked['min'], checked['max'])):
        _fail()
    error = _number(result['float32MaxErrorMetres'])
    if error < 0:
        _fail()
    if error > ROUNDTRIP_TOLERANCE_METRES:
        _fail('preview_roundtrip_error')
    return result, checked


def check_preview_outputs(preview_path: Path, output_dir: Path, settings: Settings, *, engine=None, version=EXPECTED_VERSION) -> BlenderResult:
    """Check actual private files after child reap, without trusting descriptors.

    PNG acceptance is structural (existing CRC/chunk reader), not pixel decoding
    or evidence of rendering. The fixed rebased tolerance proves no geodetic or
    regulatory fidelity. Returned paths require immediate guarded publication.
    """
    limits = _limits(settings)
    try:
        with _root(settings) as (root, root_descriptor):
            document, fingerprint = _input(root, root_descriptor, preview_path, limits)
            relative = _relative(root, output_dir)
            with _directory(root_descriptor, relative.parts) as directory:
                if set(os.listdir(directory)) != {'measurements.json', 'thumbnail.png'}:
                    _fail()
                wire, measurements_stat = _read(directory, 'measurements.json', MEASUREMENTS_MAX_BYTES)
                result, bounds = _measurements(wire,document,fingerprint,engine=engine,version=version)
                image, thumbnail_stat = _read(directory, 'thumbnail.png', THUMBNAIL_MAX_BYTES)
                png = inspect_png(image)
                if png['width'] != 512 or png['height'] != 512:
                    _fail()
                _bound(directory, 'measurements.json', measurements_stat)
                _bound(directory, 'thumbnail.png', thumbnail_stat)
                if set(os.listdir(directory)) != {'measurements.json', 'thumbnail.png'}:
                    _fail()
            # Catch input changes while checking outputs as well as its own read.
            checked_document, checked_fingerprint = _input(root, root_descriptor, preview_path, limits)
            if checked_fingerprint != fingerprint or checked_document != document:
                _fail()
        return BlenderResult(result['version'], result['vertexCount'], result['triangleCount'],
                             bounds, output_dir / 'thumbnail.png', output_dir / 'measurements.json')
    except (OSError, ReadError, TypeError, AttributeError):
        _fail()


def check_blender_outputs(preview_path: Path, output_dir: Path, settings: Settings) -> BlenderResult:
    return check_preview_outputs(preview_path,output_dir,settings)


def _prepare_launch(preview_path, output_dir, settings):
    """Immutable fixed command and minimal environment used also by the guard."""
    _limits(settings)
    _absolute(preview_path)
    _absolute(output_dir)
    executable = _absolute(settings.blender_path)
    trusted_script = Path(__file__).resolve().parents[3] / 'workers' / 'blender_preview.py'
    argv = (str(executable), '--background', '--factory-startup', '--disable-autoexec',
            '--threads', '1', '--python', str(trusted_script), '--',
            '--input', str(preview_path), '--output-dir', str(output_dir))
    environment = (('PATH', '/usr/bin:/bin'), ('HOME', '/nonexistent'),
                   ('TMPDIR', str(output_dir)), ('LANG', 'C.UTF-8'), ('LC_ALL', 'C.UTF-8'),
                   ('PYTHONNOUSERSITE', '1'), ('PYTHONDONTWRITEBYTECODE', '1'),
                   ('OMP_NUM_THREADS', '1'), ('OPENBLAS_NUM_THREADS', '1'),
                   ('BLENDER_USER_CONFIG', '/nonexistent'), ('BLENDER_USER_SCRIPTS', '/nonexistent'),
                   ('BLENDER_USER_DATAFILES', '/nonexistent'))
    return _BlenderLaunch(argv, environment, settings.blender_cpu_seconds,
                          settings.blender_wall_seconds, settings.blender_memory_bytes)


def installed_runtime_fingerprint():
    """Pin the inspected official executable even in the API's binary-free image."""
    script=Path(__file__).resolve().parents[3]/'workers'/'blender_preview.py'
    return {'version':EXPECTED_VERSION,'binarySha256':BLENDER_SHA256,
            'scriptSha256':hashlib.sha256(script.read_bytes()).hexdigest()}


def runtime_identity(settings):
    try:
        if sys.platform!='linux' or settings.blender_path is None: _fail('preview_runtime_unavailable')
        expected=installed_runtime_fingerprint()
        script=Path(__file__).resolve().parents[3]/'workers'/'blender_preview.py'
        if trusted_file(settings.blender_path,executable=True)!=expected['binarySha256'] or trusted_file(script)!=expected['scriptSha256']:
            _fail('preview_runtime_unavailable')
        return expected
    except (OSError,RuntimeError,TypeError,AttributeError):
        _fail('preview_runtime_unavailable')


def run_blender_preview(preview_path: Path, output_dir: Path, settings: Settings,
                        cancel_check: Callable[[], bool]) -> BlenderResult:
    """Launch only fixed checked geometry, with no credentials or inherited FDs."""
    limits = _limits(settings)
    try:
        with _root(settings) as (root, root_descriptor):
            original_document,original_digest=_input(root,root_descriptor,preview_path,limits)
            relative = _relative(root, output_dir)
            with _directory(root_descriptor, relative.parts) as directory:
                if os.listdir(directory):
                    _fail()
        if not callable(cancel_check):
            _fail()
        try:
            cancelled = cancel_check()
        except Exception:
            _fail('preview_cancelled')
        if type(cancelled) is not bool:
            _fail()
        if cancelled:
            _fail('preview_cancelled')
        runtime=runtime_identity(settings)
        launch=_prepare_launch(preview_path,output_dir,settings)
    except (OSError, TypeError, AttributeError):
        _fail()
    ticks,boot=identity(os.getpid())
    argv=[sys.executable,'-m','model_generator.web.process_guard','--mode','blender',
          '--parent-pid',str(os.getpid()),'--parent-start-ticks',str(ticks),'--parent-boot-id',boot,
          '--scratch',str(output_dir),'--binary',str(settings.blender_path),
          '--script-sha256',runtime['scriptSha256'],'--preview-input',str(preview_path),
          '--cpu-seconds',str(launch.cpu_seconds),'--memory-bytes',str(launch.memory_bytes)]
    environment=dict(launch.environment)
    # Needed only to import the fixed launcher; the guard removes it before exec.
    environment['PYTHONPATH']=str(Path(__file__).resolve().parents[2])
    from .worker import terminate_group
    process=subprocess.Popen(argv,env=environment,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE,close_fds=True,start_new_session=True)
    deadline=time.monotonic()+launch.wall_seconds
    buffers={process.stdout:bytearray(),process.stderr:bytearray()}
    failure=None
    reaped=False
    try:
        with selectors.DefaultSelector() as poll:
            for pipe in buffers:
                os.set_blocking(pipe.fileno(),False); poll.register(pipe,selectors.EVENT_READ)
            last_check=0
            while poll.get_map() or process.poll() is None:
                now=time.monotonic()
                if now>=deadline: failure='preview_resource'; break
                if now-last_check>=.25:
                    last_check=now
                    # Lease errors propagate after the finally block physically reaps.
                    cancelled=cancel_check()
                    if type(cancelled) is not bool: _fail()
                    if cancelled: failure='preview_cancelled'; break
                for key,_ in poll.select(.02):
                    chunk=os.read(key.fd,65536)
                    if not chunk: poll.unregister(key.fileobj); continue
                    if len(buffers[key.fileobj])+len(chunk)>launch.log_max_bytes:
                        failure='preview_resource'; break
                    buffers[key.fileobj].extend(chunk)
                if failure: break
        terminate_group(process)
        reaped=True
        if failure: _fail(failure)
        if process.returncode: _fail('preview_runtime_unavailable' if process.returncode==78 else 'preview_resource')
        if cancel_check(): _fail('preview_cancelled')
        if runtime_identity(settings)!=runtime: _fail('preview_runtime_unavailable')
        with _root(settings) as (root,descriptor):
            current_document,current_digest=_input(root,descriptor,preview_path,limits)
        if current_document!=original_document or current_digest!=original_digest: _fail()
        return check_blender_outputs(preview_path,output_dir,settings)
    finally:
        if not reaped: terminate_group(process)
        for pipe in buffers: pipe.close()


def preflight_blender(settings: Settings, cancel_check=lambda:False) -> dict:
    """Only an actual checked triangle render can verify the runtime heartbeat."""
    from .preview import build_synthetic_demo,write_preview
    runtime_identity(settings)
    scratch=settings.data_root/'preflight'/uuid4().hex
    scratch.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    scratch.mkdir(mode=0o700,parents=True)
    try:
        preview=scratch/'preview-input.json'
        write_preview(build_synthetic_demo(),preview,_limits(settings))
        staging=scratch/'blender'; staging.mkdir(mode=0o700)
        start=time.monotonic()
        result=run_blender_preview(preview,staging,settings,cancel_check)
        if (result.vertex_count,result.triangle_count)!=(3,1): _fail()
        return {**runtime_identity(settings),'verified':True,'wallSeconds':time.monotonic()-start}
    finally:
        shutil.rmtree(scratch,ignore_errors=True)
