"""Explicit python-cpu renderer, actual native guard and independent file checks."""
from dataclasses import dataclass
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import selectors
import shutil
import subprocess
import sys
import sysconfig
import time
from uuid import uuid4
from .blender_runner import (_root,_relative,_directory,_input,check_preview_outputs,
                             _fail)
from .config import Settings
from .preview import PreviewLimits,write_preview,build_synthetic_demo
from .process_guard import identity,trusted_file

ENGINE='python-cpu'
VERSION='1'
DEPENDENCIES={'numpy':'2.5.3','Pillow':'12.3.0'}
PROBES={'guard','cpu','wall','memory','descendant','oversized'}


@dataclass(frozen=True)
class PreviewResult:
    version: str
    vertex_count: int
    triangle_count: int
    bounds: dict
    thumbnail_path: Path
    measurements_path: Path
    evidence: dict | None = None
    wall_seconds: float = 0.0
    cpu_seconds: float = 0.0
    max_rss_kib: int = 0


def _limits(settings):
    if not isinstance(settings,Settings) or settings.preview_backend!=ENGINE: _fail('preview_runtime_unavailable')
    for name,minimum,maximum in [('preview_cpu_seconds',1,60),('preview_wall_seconds',1,90),
                                  ('preview_memory_bytes',32*1024**2,1024**3)]:
        value=getattr(settings,name)
        if type(value) is not int or not minimum<=value<=maximum: _fail('preview_unsupported')
    try: return PreviewLimits(settings.preview_max_instances,settings.preview_max_vertices,settings.preview_max_triangles,settings.preview_max_bytes)
    except (TypeError,ValueError): _fail('preview_unsupported')


def _files():
    root=Path(__file__).resolve().parents[3]
    files=[Path(sys.executable).resolve(),root/'requirements-web.lock',
           *(Path(__file__).with_name(name).resolve() for name in
             ('preview_child.py','preview_runner.py','process_guard.py','preview.py','blender_runner.py','config.py'))]
    library=Path(sysconfig.get_config_var('LIBDIR'))/sysconfig.get_config_var('LDLIBRARY')
    if library.exists(): files.append(library.resolve())
    for name,version in DEPENDENCIES.items():
        distribution=importlib.metadata.distribution(name)
        if distribution.version!=version: _fail('preview_runtime_unavailable')
        for entry in distribution.files or ():
            path=Path(distribution.locate_file(entry)).resolve()
            if path.suffix in {'.py','.so'} or '.so.' in path.name:
                files.append(path)
    return root,sorted(set(files),key=str)


def installed_preview_fingerprint(*,strict=False):
    """Actual interpreter/library/module/dependency bytes bind every checkpoint."""
    try:
        root,files=_files()
        digest=hashlib.sha256()
        binary=Path(sys.executable).resolve()
        for path in files:
            label=str(path.relative_to(root)) if path.is_relative_to(root) else str(path)
            value=trusted_file(path,executable=path==binary) if strict else hashlib.sha256(path.read_bytes()).hexdigest()
            digest.update(label.encode()); digest.update(bytes.fromhex(value))
        return {'engine':ENGINE,'version':VERSION,'pythonVersion':sys.version.split()[0],
                'pythonSha256':hashlib.sha256(binary.read_bytes()).hexdigest(),
                'dependencies':DEPENDENCIES,'sourceRuntimeSha256':digest.hexdigest()}
    except (OSError,ValueError,TypeError,RuntimeError,importlib.metadata.PackageNotFoundError):
        _fail('preview_runtime_unavailable')


def _environment(output_dir,probe):
    environment={'PATH':'/usr/local/bin:/usr/bin:/bin','HOME':'/nonexistent','TMPDIR':str(output_dir),
                 'LANG':'C.UTF-8','LC_ALL':'C.UTF-8','PYTHONNOUSERSITE':'1',
                 'PYTHONDONTWRITEBYTECODE':'1','PYTHONUNBUFFERED':'1','OMP_NUM_THREADS':'1',
                 'OPENBLAS_NUM_THREADS':'1','MKL_NUM_THREADS':'1','NUMEXPR_NUM_THREADS':'1',
                 'PYTHONPATH':str(Path(__file__).resolve().parents[2])}
    if probe is not None: environment['MG_TEST_MODE']='1'
    return environment


def run_preview(preview_path:Path,output_dir:Path,settings:Settings,cancel_check,*,probe=None)->PreviewResult:
    limits=_limits(settings)
    if probe is not None and (probe not in PROBES or os.environ.get('MG_TEST_MODE')!='1'):
        _fail('preview_unsupported')
    try:
        with _root(settings) as (root,descriptor):
            original_document,original_digest=_input(root,descriptor,preview_path,limits)
            relative=_relative(root,output_dir)
            with _directory(descriptor,relative.parts) as directory:
                if os.listdir(directory): _fail('preview_unsupported')
        if not callable(cancel_check): _fail('preview_unsupported')
        cancelled=cancel_check()
        if type(cancelled) is not bool: _fail('preview_unsupported')
        if cancelled: _fail('preview_cancelled')
        runtime=installed_preview_fingerprint(strict=True)
    except (OSError,TypeError,AttributeError): _fail('preview_unsupported')
    ticks,boot=identity(os.getpid())
    argv=[sys.executable,'-m','model_generator.web.process_guard','--mode','preview',
          '--parent-pid',str(os.getpid()),'--parent-start-ticks',str(ticks),'--parent-boot-id',boot,
          '--scratch',str(output_dir),'--preview-input',str(preview_path),
          '--runtime-sha256',runtime['sourceRuntimeSha256'],
          '--cpu-seconds',str(settings.preview_cpu_seconds),'--memory-bytes',str(settings.preview_memory_bytes)]
    if probe is not None: argv+=['--probe',probe]
    from .worker import terminate_group
    start=time.monotonic()
    try:
        process=subprocess.Popen(argv,env=_environment(output_dir,probe),stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE,stderr=subprocess.PIPE,close_fds=True,start_new_session=True)
    except OSError: _fail('preview_runtime_unavailable')
    buffers={process.stdout:bytearray(),process.stderr:bytearray()}
    reaped=False; failure=None
    try:
        with selectors.DefaultSelector() as poll:
            for pipe in buffers:
                os.set_blocking(pipe.fileno(),False); poll.register(pipe,selectors.EVENT_READ)
            last_check=0
            while poll.get_map() or process.poll() is None:
                now=time.monotonic()
                if now-start>=settings.preview_wall_seconds: failure='preview_resource'; break
                if now-last_check>=.25:
                    last_check=now; cancelled=cancel_check()
                    if type(cancelled) is not bool: _fail('preview_unsupported')
                    if cancelled: failure='preview_cancelled'; break
                for key,_ in poll.select(.02):
                    chunk=os.read(key.fd,65536)
                    if not chunk: poll.unregister(key.fileobj); continue
                    if len(buffers[key.fileobj])+len(chunk)>65536: failure='preview_resource'; break
                    buffers[key.fileobj].extend(chunk)
                if failure: break
        terminate_group(process); reaped=True
        if failure: _fail(failure)
        if process.returncode:
            if process.returncode==71:
                try: message=json.loads(buffers[process.stdout])
                except (ValueError,UnicodeError): _fail('preview_resource')
                if type(message) is dict and set(message)=={'failureCode'} and message['failureCode'] in {'preview_budget','preview_roundtrip_error','preview_unsupported'}: _fail(message['failureCode'])
            _fail('preview_runtime_unavailable' if process.returncode==78 else 'preview_resource')
        cancelled=cancel_check()
        if type(cancelled) is not bool: _fail('preview_unsupported')
        if cancelled: _fail('preview_cancelled')
        if installed_preview_fingerprint(strict=True)!=runtime: _fail('preview_runtime_unavailable')
        with _root(settings) as (root,descriptor):
            current_document,current_digest=_input(root,descriptor,preview_path,limits)
        if current_document!=original_document or current_digest!=original_digest:
            _fail('preview_unsupported')
        checked=check_preview_outputs(preview_path,output_dir,settings,engine=ENGINE,version=VERSION)
        with _root(settings) as (root,descriptor):
            current_document,current_digest=_input(root,descriptor,preview_path,limits)
        if current_document!=original_document or current_digest!=original_digest:
            _fail('preview_unsupported')
        try:
            signal=json.loads(buffers[process.stdout])
            if type(signal) is not dict or set(signal)!={'probe','telemetry'}: raise ValueError
            telemetry=signal['telemetry']
            if type(telemetry) is not dict or set(telemetry)!={'wallSeconds','cpuSeconds','maxRssKiB'}: raise ValueError
            for name in ('wallSeconds','cpuSeconds'):
                if type(telemetry[name]) not in {int,float} or not math.isfinite(telemetry[name]) or telemetry[name]<0: raise ValueError
            if type(telemetry['maxRssKiB']) is not int or not 0<telemetry['maxRssKiB']<=settings.preview_memory_bytes//1024: raise ValueError
            if (probe=='guard')!=(type(signal['probe']) is dict) or (probe!='guard' and signal['probe'] is not None): raise ValueError
        except (ValueError,TypeError,KeyError,UnicodeError): _fail('preview_unsupported')
        evidence=signal['probe']
        return PreviewResult(checked.version,checked.vertex_count,checked.triangle_count,checked.bounds,
                             checked.thumbnail_path,checked.measurements_path,evidence,time.monotonic()-start,
                             telemetry['cpuSeconds'],telemetry['maxRssKiB'])
    finally:
        if not reaped: terminate_group(process)
        for pipe in buffers: pipe.close()


def preflight_preview(settings:Settings,cancel_check=lambda:False)->dict:
    installed_preview_fingerprint(strict=True)
    scratch=settings.data_root/'preflight'/uuid4().hex
    scratch.parent.mkdir(mode=0o700,parents=True,exist_ok=True); scratch.mkdir(mode=0o700)
    try:
        input_path=scratch/'preview-input.json'; write_preview(build_synthetic_demo(),input_path,_limits(settings))
        output=scratch/'render'; output.mkdir(mode=0o700)
        result=run_preview(input_path,output,settings,cancel_check)
        if (result.vertex_count,result.triangle_count)!=(3,1): _fail('preview_unsupported')
        return {'identity':installed_preview_fingerprint(strict=True),'verified':True,'wallSeconds':result.wall_seconds}
    finally: shutil.rmtree(scratch,ignore_errors=True)
