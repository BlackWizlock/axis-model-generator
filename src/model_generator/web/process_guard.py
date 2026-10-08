"""Fail-closed Linux launcher for fixed trusted validation commands."""
import argparse
import ctypes
import errno
import os
from pathlib import Path
import resource
import signal
import sys
import hashlib
import stat

BLENDER_SHA256 = '050c02562f81fe80ba616a80198fa02d381e60f8b61b8d39add881f4bca0d7d8'


def trusted_file(path, *, executable=False):
    """Read a fixed installation file, never a symlink or worker-writable source."""
    if not path.is_absolute() or '..' in path.parts or path.resolve() != path:
        raise RuntimeError('unsafe_runtime')
    for directory in path.parents:
        info=directory.stat()
        if info.st_uid!=0 or info.st_mode & 0o022:
            raise RuntimeError('unsafe_runtime')
    descriptor=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
    try:
        before=os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink!=1 or before.st_uid!=0
                or before.st_mode & 0o022 or (executable and not before.st_mode & 0o111)):
            raise RuntimeError('unsafe_runtime')
        # A root worker cannot establish installation immutability on writable FS.
        if os.geteuid()==0 and not os.fstatvfs(descriptor).f_flag & os.ST_RDONLY:
            raise RuntimeError('unsafe_runtime')
        digest=hashlib.sha256()
        while chunk:=os.read(descriptor,65536): digest.update(chunk)
        after=os.fstat(descriptor)
        if (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns):
            raise RuntimeError('unsafe_runtime')
        actual=path.stat(follow_symlinks=False)
        if (actual.st_dev,actual.st_ino,actual.st_mtime_ns,actual.st_ctime_ns)!=(after.st_dev,after.st_ino,after.st_mtime_ns,after.st_ctime_ns):
            raise RuntimeError('unsafe_runtime')
        return digest.hexdigest()
    finally: os.close(descriptor)


DENIED = ('socket', 'socketpair', 'connect', 'bind', 'listen', 'accept', 'accept4',
          'sendto', 'sendmsg', 'recvfrom', 'recvmsg', 'io_uring_setup', 'io_uring_register',
          'io_uring_enter', 'ptrace', 'bpf', 'process_vm_readv', 'process_vm_writev',
          'pidfd_getfd', 'setns', 'unshare')
BLENDER_DENIED=('setsid','setpgid','mount','umount2','pivot_root','chroot',
                'open_by_handle_at','name_to_handle_at','execveat')


def identity(pid):
    text = Path(f'/proc/{pid}/stat').read_text()
    return int(text[text.rfind(')') + 2:].split()[19]), Path('/proc/sys/kernel/random/boot_id').read_text().strip()


def parent_guard(pid, ticks, boot):
    c = ctypes.CDLL(None, use_errno=True)
    if os.getppid() != pid or identity(pid) != (ticks, boot):
        raise RuntimeError('parent_identity_mismatch')
    if c.prctl(1, signal.SIGKILL, 0, 0, 0):
        raise RuntimeError('parent_guard_unsupported')
    if os.getppid() != pid or identity(pid) != (ticks, boot):
        raise RuntimeError('parent_identity_mismatch')
    if c.prctl(38, 1, 0, 0, 0):
        raise RuntimeError('privilege_guard_unsupported')


def seccomp(*,blender=False):
    library = ctypes.CDLL('libseccomp.so.2', use_errno=True)
    library.seccomp_init.argtypes = [ctypes.c_uint32]
    library.seccomp_init.restype = ctypes.c_void_p
    library.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    library.seccomp_syscall_resolve_name.restype = ctypes.c_int
    library.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    library.seccomp_load.argtypes = [ctypes.c_void_p]
    library.seccomp_release.argtypes = [ctypes.c_void_p]
    context = library.seccomp_init(0x7fff0000)
    if not context:
        raise RuntimeError('network_guard_unsupported')
    try:
        for name in (*DENIED,*(BLENDER_DENIED if blender else ())):
            number = library.seccomp_syscall_resolve_name(name.encode())
            if number < 0 or library.seccomp_rule_add(context, 0x00050000 | errno.EACCES, number, 0):
                raise RuntimeError('network_guard_unsupported')
        if library.seccomp_load(context):
            raise RuntimeError('network_guard_unsupported')
    finally:
        library.seccomp_release(context)


class Ruleset(ctypes.Structure):
    _fields_ = [('handled_access_fs', ctypes.c_uint64)]


class PathRule(ctypes.Structure):
    _layout_ = 'ms'
    _pack_ = 1
    _fields_ = [('allowed_access', ctypes.c_uint64), ('parent_fd', ctypes.c_int)]


def landlock(scratch: Path, *, blender=None, preview_input=None):
    c = ctypes.CDLL(None, use_errno=True)
    c.syscall.restype = ctypes.c_long
    abi = c.syscall(444, ctypes.c_void_p(0), ctypes.c_size_t(0), ctypes.c_uint(1))
    if abi < 3:
        raise RuntimeError('filesystem_guard_unsupported')
    handled = (1 << 15) - 1  # Include REFER and TRUNCATE, not newer unknown rights.
    settings = Ruleset(handled)
    ruleset = c.syscall(444, ctypes.byref(settings), ctypes.c_size_t(ctypes.sizeof(settings)), ctypes.c_uint(0))
    if ruleset < 0:
        raise RuntimeError('filesystem_guard_unsupported')
    try:
        # Allow runtime and application source reads, never /proc or /run/secrets.
        reads = [Path(sys.executable).resolve(), Path('/usr/local/lib'), Path('/usr/lib'),
                 Path(__file__).resolve().parents[2]]
        if preview_input is not None:
            reads += [preview_input,Path('/etc/ld.so.cache')]
        if blender is not None:
            # Official runtime resources and only installed trusted script/code.
            reads += [blender.parent, Path(__file__).resolve().parents[3]/'workers'/'blender_preview.py',
                      preview_input,Path('/etc/ld.so.cache'),Path('/etc/fonts'),Path('/usr/share/fonts')]
        grants=[(path,1|4|(8 if path.is_dir() else 0)) for path in reads if path.exists()]
        if preview_input is not None:
            grants += [(Path('/dev/null'),2|4),(Path('/dev/urandom'),4),(Path('/dev/random'),4)]
        for path, access in [*grants,(scratch,handled & ~1)]:
            descriptor = os.open(path, os.O_PATH | os.O_CLOEXEC | os.O_NOFOLLOW)
            try:
                rule = PathRule(access, descriptor)
                if c.syscall(445, ruleset, 1, ctypes.byref(rule), 0):
                    raise RuntimeError('filesystem_guard_unsupported')
            finally:
                os.close(descriptor)
        if c.syscall(446, ruleset, 0):
            raise RuntimeError('filesystem_guard_unsupported')
    finally:
        os.close(ruleset)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--parent-pid', type=int, required=True)
    parser.add_argument('--parent-start-ticks', type=int, required=True)
    parser.add_argument('--parent-boot-id', required=True)
    parser.add_argument('--scratch', type=Path, required=True)
    parser.add_argument('--cpu-seconds', type=int, default=90)
    parser.add_argument('--memory-bytes', type=int, default=1536 * 1024**2)
    parser.add_argument('--mode',choices=('validation','blender','preview'),default='validation')
    parser.add_argument('--binary',type=Path)
    parser.add_argument('--script-sha256')
    parser.add_argument('--preview-input',type=Path)
    parser.add_argument('--runtime-sha256')
    parser.add_argument('--probe',choices=('guard','cpu','wall','memory','descendant','oversized'))
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        command = args.command[1:] if args.command[:1] == ['--'] else args.command
        if sys.platform != 'linux':
            raise RuntimeError('untrusted_command')
        if args.mode=='validation':
            if not command or command[:3]!=[sys.executable,'-m','model_generator.web.validation_child'] or any((args.binary,args.script_sha256,args.preview_input,args.runtime_sha256,args.probe)):
                raise RuntimeError('untrusted_command')
            cpu_cap,memory_cap,file_cap=90,1536*1024**2,16*1024**2
            executable=Path(sys.executable)
            environment=dict(os.environ)
        elif args.mode=='blender':
            from .blender_runner import _prepare_launch
            from .config import Settings
            script=Path(__file__).resolve().parents[3]/'workers'/'blender_preview.py'
            if command or args.runtime_sha256 or args.probe or args.binary is None or args.preview_input is None or trusted_file(args.binary,executable=True)!=BLENDER_SHA256 or trusted_file(script)!=args.script_sha256:
                raise RuntimeError('untrusted_command')
            # Input is a single fixed private file beside owned staging, no raw ZIP.
            if (args.preview_input.name!='preview-input.json' or args.preview_input.parent!=args.scratch.parent or args.preview_input.is_symlink()
                    or not args.preview_input.is_file() or args.preview_input.stat().st_nlink!=1):
                raise RuntimeError('unsafe_preview')
            launch=_prepare_launch(args.preview_input,args.scratch,Settings(args.scratch.parent,'https://invalid.example',b'0'*32,'',blender_path=args.binary,
                                                                          blender_cpu_seconds=args.cpu_seconds,blender_memory_bytes=args.memory_bytes))
            command=list(launch.argv); environment=dict(launch.environment); executable=args.binary
            cpu_cap,memory_cap,file_cap=60,1024**3,4*1024**2
        else:
            from .preview_runner import installed_preview_fingerprint,_environment
            if (command or args.binary or args.script_sha256 or args.preview_input is None or not args.runtime_sha256
                    or installed_preview_fingerprint(strict=True)['sourceRuntimeSha256']!=args.runtime_sha256
                    or (args.probe is not None and os.environ.get('MG_TEST_MODE')!='1')):
                raise RuntimeError('untrusted_command')
            if (args.preview_input.name!='preview-input.json' or args.preview_input.parent!=args.scratch.parent
                    or args.preview_input.is_symlink() or not args.preview_input.is_file() or args.preview_input.stat().st_nlink!=1):
                raise RuntimeError('unsafe_preview')
            command=[sys.executable,'-m','model_generator.web.preview_child','--input',str(args.preview_input),'--output-dir',str(args.scratch)]
            if args.probe is not None: command+=['--probe',args.probe]
            environment=_environment(args.scratch,args.probe); executable=Path(sys.executable)
            cpu_cap,memory_cap,file_cap=60,1024**3,4*1024**2
        if not 1 <= args.cpu_seconds <= cpu_cap or not 32 * 1024**2 <= args.memory_bytes <= memory_cap:
            raise RuntimeError('invalid_child_budget')
        if not args.scratch.is_absolute() or args.scratch.is_symlink() or not args.scratch.is_dir():
            raise RuntimeError('unsafe_scratch')
        metadata=args.scratch.stat()
        if metadata.st_uid!=os.geteuid() or stat.S_IMODE(metadata.st_mode)&0o077:
            raise RuntimeError('unsafe_scratch')
        parent_guard(args.parent_pid, args.parent_start_ticks, args.parent_boot_id)
        resource.setrlimit(resource.RLIMIT_AS, (args.memory_bytes, args.memory_bytes))
        resource.setrlimit(resource.RLIMIT_CPU, (args.cpu_seconds, args.cpu_seconds + 1))
        resource.setrlimit(resource.RLIMIT_FSIZE,(file_cap,file_cap))
        resource.setrlimit(resource.RLIMIT_NOFILE,(64 if args.mode in {'blender','preview'} else 32,)*2)
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        seccomp(blender=args.mode in {'blender','preview'})
        landlock(args.scratch.resolve(),blender=args.binary if args.mode=='blender' else None,preview_input=args.preview_input)
        os.chdir(args.scratch)
        os.execve(executable,command,environment)
    except (OSError, RuntimeError, ValueError):
        raise SystemExit(78) from None


if __name__ == '__main__':
    main()
