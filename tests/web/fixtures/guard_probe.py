"""Real kernel capability probes; unsupported controls are errors, never skips.

These small probes characterize the Docker platform before Task 3 implementation.
They do not certify the future worker, lifetime supervisor or complete sandbox.
Only synthetic temporary data is used. Every irreversible restriction runs in
its own process. Run this file inside the pinned local Linux container.
"""
import ctypes
import errno
import json
import os
from pathlib import Path
import platform
import signal
import socket
import subprocess
import sys
import tempfile


DENIED_SYSCALLS = (
    'socket', 'socketpair', 'connect', 'bind', 'listen', 'accept', 'accept4',
    'sendto', 'sendmsg', 'recvfrom', 'recvmsg', 'io_uring_setup',
    'io_uring_register', 'io_uring_enter', 'ptrace', 'bpf',
    'process_vm_readv', 'process_vm_writev', 'pidfd_getfd', 'setns', 'unshare',
)


def libc():
    value = ctypes.CDLL(None, use_errno=True)
    value.syscall.restype = ctypes.c_long
    return value


def no_new_privileges(c):
    if c.prctl(38, 1, 0, 0, 0):
        raise OSError(ctypes.get_errno(), 'no_new_privs_unsupported')


def parent_death_probe():
    c = libc()
    parent = os.getppid()
    no_new_privileges(c)
    if c.prctl(1, signal.SIGKILL, 0, 0, 0):
        raise OSError(ctypes.get_errno(), 'pdeathsig_unsupported')
    actual = ctypes.c_int()
    if c.prctl(2, ctypes.byref(actual), 0, 0, 0):
        raise OSError(ctypes.get_errno(), 'pdeathsig_query_failed')
    if actual.value != signal.SIGKILL or os.getppid() != parent:
        raise RuntimeError('parent_identity_changed')
    return {'no_new_privs': True, 'pdeathsig': actual.value}


def seccomp_probe():
    c = libc()
    no_new_privileges(c)
    library = ctypes.CDLL('libseccomp.so.2', use_errno=True)
    library.seccomp_init.argtypes = [ctypes.c_uint32]
    library.seccomp_init.restype = ctypes.c_void_p
    library.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    library.seccomp_syscall_resolve_name.restype = ctypes.c_int
    library.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    library.seccomp_load.argtypes = [ctypes.c_void_p]
    library.seccomp_release.argtypes = [ctypes.c_void_p]
    context = library.seccomp_init(0x7fff0000)  # SCMP_ACT_ALLOW
    if not context:
        raise RuntimeError('seccomp_init_failed')
    try:
        for name in DENIED_SYSCALLS:
            number = library.seccomp_syscall_resolve_name(name.encode('ascii'))
            if number < 0:
                raise RuntimeError('seccomp_required_syscall_unknown')
            result = library.seccomp_rule_add(context, 0x00050000 | errno.EACCES, number, 0)
            if result:
                raise OSError(-result, 'seccomp_rule_failed')
        result = library.seccomp_load(context)
        if result:
            raise OSError(-result, 'seccomp_load_unsupported')
    finally:
        library.seccomp_release(context)
    denied = []
    for label, address in (('socket', ('127.0.0.1', 9)),
                           ('own_sql', ('127.0.0.1', 5432)),
                           ('own_s3', ('127.0.0.1', 9000)),
                           ('metadata', ('169.254.169.254', 80))):
        try:
            with socket.socket() as connection:
                connection.connect(address)
        except PermissionError as error:
            if error.errno != errno.EACCES:
                raise RuntimeError('unexpected_socket_denial') from None
            denied.append(label)
        else:
            raise RuntimeError('socket_was_allowed')
    return {'installed': True, 'socket_denials': denied}


class Ruleset(ctypes.Structure):
    _fields_ = [('handled_access_fs', ctypes.c_uint64)]


class PathRule(ctypes.Structure):
    _layout_ = 'ms'  # Explicit packed 12-byte kernel ABI, no implicit ctypes layout.
    _pack_ = 1
    _fields_ = [('allowed_access', ctypes.c_uint64), ('parent_fd', ctypes.c_int)]


def landlock_probe():
    c = libc()
    no_new_privileges(c)
    abi = c.syscall(444, ctypes.c_void_p(0), ctypes.c_size_t(0), ctypes.c_uint(1))
    if abi < 1:
        raise OSError(ctypes.get_errno(), 'landlock_abi_unsupported')
    # No real credential mount is read. The denied file has synthetic bytes.
    with tempfile.TemporaryDirectory(prefix='mg-guard-capability-') as folder:
        root = Path(folder)
        scratch = root / 'own-scratch'
        scratch.mkdir()
        own_input = scratch / 'input.zip'
        own_input.write_bytes(b'synthetic-own-input')
        forbidden = root / 'parent-secret'
        forbidden.write_bytes(b'synthetic-not-a-credential')
        # Prepare cleanup FDs before restriction; cleanup is done by outer process.
        settings = Ruleset((1 << 13) - 1)
        ruleset = c.syscall(444, ctypes.byref(settings), ctypes.c_size_t(ctypes.sizeof(settings)), ctypes.c_uint(0))
        if ruleset < 0:
            raise OSError(ctypes.get_errno(), 'landlock_create_failed')
        directory = os.open(scratch, os.O_PATH | os.O_CLOEXEC)
        try:
            rule = PathRule((1 << 2) | (1 << 3), directory)  # READ_FILE and READ_DIR
            if c.syscall(445, ruleset, 1, ctypes.byref(rule), 0):
                raise OSError(ctypes.get_errno(), 'landlock_rule_failed')
            if c.syscall(446, ruleset, 0):
                raise OSError(ctypes.get_errno(), 'landlock_restrict_unsupported')
        finally:
            os.close(directory)
            os.close(ruleset)
        if own_input.read_bytes() != b'synthetic-own-input':
            raise RuntimeError('own_input_unreadable')
        denied = []
        for label, path in (('synthetic_parent_secret', forbidden),
                             ('parent_environ', Path(f'/proc/{os.getppid()}/environ')),
                             ('parent_mem', Path(f'/proc/{os.getppid()}/mem')),
                             ('parent_root', Path(f'/proc/{os.getppid()}/root/etc/passwd'))):
            try:
                with path.open('rb') as stream:
                    stream.read(1)
            except PermissionError:
                denied.append(label)
            else:
                raise RuntimeError('parent_source_was_allowed')
        # Restriction forbids deleting the temporary parent. Exit without asking
        # tempfile to clean up; container is ephemeral and outer process owns TTL.
        result = {'abi': abi, 'installed': True, 'own_input_read': True, 'read_denials': denied}
        print(json.dumps({'control': 'landlock', 'ok': True, 'details': result}), flush=True)
        os._exit(0)


def one(control):
    try:
        if platform.system() != 'Linux':
            raise RuntimeError('linux_required')
        result = {'pdeathsig': parent_death_probe, 'seccomp': seccomp_probe,
                  'landlock': landlock_probe}[control]()
        print(json.dumps({'control': control, 'ok': True, 'details': result}), flush=True)
        return 0
    except (OSError, RuntimeError) as error:
        print(json.dumps({'control': control, 'ok': False,
                          'code': str(error.args[-1]), 'errno': getattr(error, 'errno', None)}), flush=True)
        return 1


def capabilities():
    results = []
    for control in ('pdeathsig', 'seccomp', 'landlock'):
        try:
            process = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--control', control],
                                     stdin=subprocess.DEVNULL, capture_output=True,
                                     text=True, timeout=10, close_fds=True)
            result = json.loads(process.stdout)
            if process.returncode or not result.get('ok'):
                result['ok'] = False
        except (OSError, ValueError, subprocess.TimeoutExpired):
            result = {'control': control, 'ok': False, 'code': 'probe_failed'}
        results.append(result)
    output = {'schema': 1, 'platform': platform.system(), 'machine': platform.machine(),
              'controls': results, 'ok': all(item['ok'] for item in results)}
    print(json.dumps(output, sort_keys=True), flush=True)
    return 0 if output['ok'] else 1


if __name__ == '__main__':
    if sys.argv[1:] == ['--capabilities']:
        raise SystemExit(capabilities())
    if len(sys.argv) == 3 and sys.argv[1] == '--control' and sys.argv[2] in {'pdeathsig', 'seccomp', 'landlock'}:
        raise SystemExit(one(sys.argv[2]))
    raise SystemExit('Use --capabilities or --control pdeathsig|seccomp|landlock')
