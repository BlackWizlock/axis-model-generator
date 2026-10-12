"""Existing bounded validators in a credential-free guarded parsing process."""
import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import sys
from model_generator import __version__
from model_generator.input_descriptor import input_filename
from model_generator.validator import read_fbx_path, validate_path
from model_generator.limits import Limits, ReadError
from model_generator.diagnostics import Report, Finding
from model_generator.package_validator import validate_package_path
from model_generator.package_manifest import PackageLimits
from .reporting import sanitize_report
from model_generator.package_reader import read_package_bytes
from model_generator.package_scene import inspect_package_scene
from model_generator.package_manifest import PackageError
from .preview import PreviewError,PreviewLimits,build_mesh_preview,build_preview,write_preview
from model_generator.reduced_model import REPORT_CPU_MARGIN, cpu_left, reduce_input, stopped_after
from model_generator.simplify import OverBudget
from model_generator.dwg_convert import dwg_to_dxf
from model_generator.site_plan import PLAN_NAME, inspect_plan
from .process_guard import VALIDATION_FILE_BYTES

# The converter is a separate installed program, never linked into this package.
DWG_CONVERTER = '/usr/local/lib/libredwg/dwg2dxf'
DWG_SECONDS = 60
# The converter writes stray bytes into text fields on some runs and the result is
# then not a DXF. Measured on a real site plan: one run in three. Geometry of the
# runs that read is identical, so a refused result is converted again.
DWG_ATTEMPTS = 6
PLAN_COVERAGE = {'technical': 'partial', 'profile': 'research', 'procedure': 'unknown', 'external': 'not_checked'}

# The view of a reduced model is attempted only while the process has this
# many seconds of its own processor limit left: the report must not be lost
# to it. A process without a limit has nothing to protect.
VIEW_CPU_RESERVE = 40.0


def _view_time_left():
    import resource, time
    limit = resource.getrlimit(resource.RLIMIT_CPU)[0]
    return limit == resource.RLIM_INFINITY or limit - time.process_time() >= VIEW_CPU_RESERVE


@dataclass(frozen=True)
class ChildSettings:
    scratch: Path
    input_bytes: int = 256 * 1024**2
    expanded_bytes: int = 128 * 1024**2
    member_bytes: int = 64 * 1024**2
    report_bytes: int = 4 * 1024**2
    report_findings: int = 10000
    preview_limits: PreviewLimits = PreviewLimits()
    dwg_converter: object = DWG_CONVERTER


@dataclass(frozen=True)
class ValidationResult:
    report: dict
    preview_input_path: Path | None
    technical_failure: bool
    failure_code: str | None
    model_path: Path | None = None


def validate_input(input_path: Path, kind: str, settings: ChildSettings, observer=None) -> ValidationResult:
    if (input_path.parent.resolve() != settings.scratch.resolve()
            or input_path.is_symlink()
            or input_path.name not in {'input.zip', 'input.bin'}):
        raise ValueError('Unsafe child input')
    preview_path=None
    preview_reason=None
    model_path=None
    model_facts=None
    if kind in {'zip-fbx', 'fbx'}:
        if kind == 'fbx':
            # Standalone FBX is never stored under the legacy ZIP transport name.
            if input_path.name != 'input.bin': raise ValueError('Unsafe child input')
        preview_reason = 'preview_unsupported'
        limits = Limits(input_bytes=settings.input_bytes, expanded_bytes=settings.expanded_bytes,
                        member_bytes=settings.member_bytes, fbx_array_bytes=192 * 1024**2)
        try:
            # A standalone FBX is read once: the scene of its report is the
            # scene the reduced file is built from.
            report,wire = read_fbx_path(input_path, limits, observer=observer) if kind == 'fbx' \
                else (validate_path(input_path, limits, observer=observer), None)
            if wire is None:
                # The reduced file is built from the same accepted bytes; a source
                # the scene reader cannot read gives no file and a stated reason.
                with input_path.open('rb') as stream: wire=stream.read(settings.input_bytes+1)
                if len(wire)>settings.input_bytes: raise ValueError('Input budget changed')
            model,model_facts,meshes=reduce_input(wire,kind,limits)
            del wire
            if model is not None:
                model_path=settings.scratch/'model.fbx'
                with model_path.open('xb') as stream:
                    stream.write(model); stream.flush()
                del model
                # The view shows the built file and nothing else; without the
                # file there is nothing to show.
                try:
                    if not _view_time_left():
                        raise PreviewError('preview_budget','No processor time left for the view')
                    candidate=settings.scratch/'preview-input.json'
                    # Short of the limit of the process the view is given up,
                    # the report and the file stay.
                    with stopped_after(cpu_left()-REPORT_CPU_MARGIN):
                        write_preview(build_mesh_preview(meshes,settings.preview_limits),candidate,settings.preview_limits)
                    preview_path=candidate
                    preview_reason=None
                except PreviewError as error:
                    preview_reason=error.code
                except OverBudget:
                    candidate.unlink(missing_ok=True)
                    preview_reason='preview_budget'
                except MemoryError:
                    preview_reason='preview_budget'
            elif model_facts.get('reason')=='model_budget':
                preview_reason='preview_budget'
            del meshes
        except ReadError as error:
            sha = hashlib.sha256()
            with input_path.open('rb') as stream:
                while chunk := stream.read(65536):
                    sha.update(chunk)
            report = Report(sha.hexdigest(), profile={'status': 'research', 'normative_readiness': False},
                            coverage={'technical': 'partial', 'profile': 'research', 'procedure': 'unknown', 'external': 'not_checked'},
                            findings=[Finding(error.rule, 'fail', '', None, None, str(error))])
    elif kind == 'dwg':
        if input_path.name != 'input.bin': raise ValueError('Unsafe child input')
        # The drawing and its DXF are files of this process: a larger one is a budget finding, not a failed write.
        limits = Limits(input_bytes=settings.input_bytes, dxf_bytes=VALIDATION_FILE_BYTES)
        with input_path.open('rb') as stream: wire=stream.read(settings.input_bytes+1)
        if len(wire)>settings.input_bytes: raise ValueError('Input budget changed')
        report = Report(hashlib.sha256(wire).hexdigest(), profile={'status': 'research', 'normative_readiness': False},
                        coverage=dict(PLAN_COVERAGE))
        preview_reason = 'preview_unsupported'
        if observer: observer('input.dwg', 'checking')
        try:
            for attempt in range(DWG_ATTEMPTS):
                dxf = dwg_to_dxf(wire, limits, program=settings.dwg_converter, timeout=DWG_SECONDS, workdir=settings.scratch)
                try:
                    facts, findings, mesh = inspect_plan(dxf, limits)
                    break
                except ReadError as error:
                    if error.rule != 'dxf.integrity' or attempt == DWG_ATTEMPTS - 1: raise
            del wire, dxf
            report.files.append(facts)
            report.findings.extend(findings)
            try:
                candidate=settings.scratch/'preview-input.json'
                write_preview(build_mesh_preview([mesh],settings.preview_limits,'site_plan'),candidate,settings.preview_limits)
                preview_path=candidate
                preview_reason=None
            except PreviewError as error:
                preview_reason=error.code
        except ReadError as error:
            report.files.append({'file': PLAN_NAME, 'read_status': 'failed'})
            report.findings.append(Finding(error.rule, 'fail', PLAN_NAME, None, None, str(error)))
        if observer: observer('input.dwg', 'passed', report.findings)
    elif kind == 'portable-package':
        limits=PackageLimits(input_bytes=settings.input_bytes,expanded_bytes=settings.expanded_bytes,member_bytes=settings.member_bytes)
        report=validate_package_path(input_path,limits,observer)
        preview_reason='preview_unsupported'
        if report.coverage.get('package_status')=='passed':
            try:
                with input_path.open('rb') as stream: wire=stream.read(settings.input_bytes+1)
                if len(wire)>settings.input_bytes: raise ValueError('Input budget changed')
                package=read_package_bytes(wire,limits)
                inspection=inspect_package_scene(package,limits)
                preview=build_preview(package,inspection,settings.preview_limits)
                candidate=settings.scratch/'preview-input.json'
                write_preview(preview,candidate,settings.preview_limits)
                preview_path=candidate
                preview_reason=None
            except (PackageError,PreviewError) as error:
                preview_reason=error.code if isinstance(error,PreviewError) else 'preview_unsupported'
    else:
        raise ValueError('Unsupported input')
    raw = json.loads(report.to_json())
    if model_facts is not None: raw['reduced_model'] = model_facts
    if observer:
        observer.finish(report.findings,any(item.get('read_status') in {'failed','unsupported'} for item in report.files))
        raw['check_evidence'] = observer.snapshot()
    value = sanitize_report(raw, max_bytes=settings.report_bytes, max_findings=settings.report_findings)
    return ValidationResult(value,preview_path,False,preview_reason,model_path)


def main():
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument('--kind', choices=('zip-fbx', 'portable-package', 'fbx', 'dwg'), required=True)
    parser.add_argument('--scratch', type=Path, required=True)
    parser.add_argument('--descriptor-version', type=int, choices=(0, 1), default=0)
    parser.add_argument('--input-hash')
    parser.add_argument('--attempt')
    parser.add_argument('--preview-max-instances',type=int,default=1000)
    parser.add_argument('--preview-max-vertices',type=int,default=300000)
    parser.add_argument('--preview-max-triangles',type=int,default=200000)
    parser.add_argument('--preview-max-bytes',type=int,default=16777216)
    parser.add_argument('--probe', choices=('cpu','memory','wall','crash','oversized','oversized_file','descendant','guard'))
    args = parser.parse_args()
    try:
        preview_limits=PreviewLimits(args.preview_max_instances,args.preview_max_vertices,
                                     args.preview_max_triangles,args.preview_max_bytes)
        if args.probe:
            if os.environ.get('MG_TEST_MODE')!='1': raise ValueError('Test-only probe')
            if args.probe=='cpu':
                while True: pass
            if args.probe=='memory':
                data=bytearray(2*1024**3)
            if args.probe=='wall':
                __import__('time').sleep(300)
            if args.probe=='crash': os._exit(73)
            if args.probe=='oversized':
                sys.stdout.write('a'*(4*1024**2+1)); sys.stdout.flush()
                __import__('time').sleep(300)
            if args.probe=='oversized_file':
                import resource
                resource.setrlimit(resource.RLIMIT_FSIZE,(4*1024**2,)*2)
                with (args.scratch/'report.json').open('xb') as stream:
                    stream.write(b'a'*(5*1024**2)); stream.flush()
                raise ValueError('Oversized report unexpectedly written')
            if args.probe=='descendant':
                import signal,time
                child=os.fork()
                if child:
                    (args.scratch/'descendant.pid').write_text(str(child))
                else:
                    signal.signal(signal.SIGTERM,signal.SIG_IGN)
                    with (args.scratch/'descendant.writes').open('ab',buffering=0) as stream:
                        while True:
                            stream.write(b'x'); time.sleep(.05)
                signal.signal(signal.SIGTERM,signal.SIG_IGN)
                time.sleep(300)
            if args.probe=='guard':
                import ctypes,errno,socket
                library=ctypes.CDLL('libseccomp.so.2'); library.seccomp_syscall_resolve_name.argtypes=[ctypes.c_char_p]
                c=ctypes.CDLL(None,use_errno=True); c.syscall.restype=ctypes.c_long
                denied=[]
                from .process_guard import DENIED
                for name in DENIED:
                    number=library.seccomp_syscall_resolve_name(name.encode())
                    ctypes.set_errno(0)
                    if c.syscall(number,0,0,0,0,0,0)!=-1 or ctypes.get_errno()!=errno.EACCES:
                        raise ValueError('Denied syscall was available')
                    denied.append(name)
                for label,path in [('mount',Path('/run/secrets/worker_dsn')),('parent',Path(f'/proc/{os.getppid()}/environ')),('parentfd',Path(f'/proc/{os.getppid()}/fd/0')),('parentroot',Path(f'/proc/{os.getppid()}/root/etc/passwd')),('parentmem',Path(f'/proc/{os.getppid()}/mem'))]:
                    try: path.open('rb')
                    except PermissionError: denied.append(label)
                    else: raise ValueError('Forbidden source opened')
                (args.scratch/'guard.json').write_text(json.dumps({'denied':denied,'ownInput':(args.scratch/input_filename(args.descriptor_version)).read_bytes()==b'not-a-zip'}))
        from .progress import Observer
        observer = Observer(args.scratch, args.input_hash, args.attempt) if args.attempt else None
        result = validate_input(args.scratch / input_filename(args.descriptor_version), args.kind,
                                ChildSettings(args.scratch,preview_limits=preview_limits),observer)
        import resource
        # Preview stage uses 16 MiB; report stage is explicitly capped at 4 MiB.
        resource.setrlimit(resource.RLIMIT_FSIZE,(4*1024**2,)*2)
        output = json.dumps(result.report, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
        if len(output) > 4 * 1024**2:
            raise ValueError('Child output cap')
        # Complete/closed fixed scratch checkpoint precedes the bounded JSON signal.
        path = args.scratch / 'report.json'
        with path.open('xb') as stream:
            stream.write(output)
            stream.flush()
        preview=None
        if result.preview_input_path is not None:
            wire=result.preview_input_path.read_bytes()
            preview={'kind':'preview','bytes':len(wire),'sha256':hashlib.sha256(wire).hexdigest()}
        model=None
        if result.model_path is not None:
            wire=result.model_path.read_bytes()
            model={'kind':'model','bytes':len(wire),'sha256':hashlib.sha256(wire).hexdigest()}
        message = {'schema': 1, 'tool': __version__, 'inputHash': result.report['input_sha256'],
                   'report': {'kind': 'report', 'bytes': len(output), 'sha256': hashlib.sha256(output).hexdigest()},
                   'previewInput':preview,'model':model,'technicalFailure':False,'failureCode':result.failure_code}
        print(json.dumps(message, allow_nan=False, sort_keys=True), flush=True)
    except (OSError, ValueError, MemoryError):
        raise SystemExit(70) from None


if __name__ == '__main__':
    main()
