"""Bounded persistent generation fence for an exclusively owned API lock."""
import os
import re
import stat
from uuid import uuid4


def generation(descriptor):
    info=os.fstat(descriptor)
    value=os.pread(descriptor,33,0)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_size!=32 or not re.fullmatch(b'[0-9a-f]{32}',value):
        raise RuntimeError('API lock identity changed')
    return value.decode('ascii')


def initialize(descriptor):
    # Caller holds exclusive flock. Partial or malformed existing content is
    # evidence of uncertainty and must never be overwritten.
    info=os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:
        raise RuntimeError('API lock identity changed')
    if info.st_size==0:
        value=uuid4().hex.encode('ascii')
        if os.pwrite(descriptor,value,0)!=32:
            raise RuntimeError('API lock identity changed')
        os.fsync(descriptor)
    return generation(descriptor)


def valid(descriptor,path,expected):
    try:
        retained=os.fstat(descriptor)
        current=path.stat(follow_symlinks=False)
        return (stat.S_ISREG(current.st_mode) and current.st_nlink==1
                and (retained.st_dev,retained.st_ino)==(current.st_dev,current.st_ino)==expected[:2]
                and generation(descriptor)==expected[2])
    except (OSError,RuntimeError):
        return False
