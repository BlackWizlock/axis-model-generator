"""Convert DWG bytes to ASCII DXF with an external converter process.

The converter is a separate program (GNU LibreDWG dwg2dxf). It is never linked
into this package; the caller decides where it is installed and how the process
is confined. Nothing here interprets the drawing.
"""

from pathlib import Path
import re
import subprocess
import tempfile

from .limits import Limits, ReadError

# DWG files start with their format version, for example AC1032 for R2018.
SIGNATURE = re.compile(rb'AC10(12|14|15|18|21|24|27|32)')
# The converter writes stray bytes into XRECORD values of the OBJECTS section on
# some runs, and the file is then not a DXF. Measured on a real site plan: one
# run in three, every difference between runs inside that section. Geometry
# ends before it, so the result is cut there.
OBJECTS = re.compile(rb'(?:^|\n)[ \t]*0\r?\n[ \t]*SECTION\r?\n[ \t]*2\r?\n[ \t]*OBJECTS[ \t]*\r?\n')


def _geometry(dxf):
    found = OBJECTS.search(dxf)
    return dxf if found is None else dxf[:found.start()] + b'\n  0\nEOF\n'


def dwg_to_dxf(data, limits=None, program='dwg2dxf', timeout=120, workdir=None):
    """Return DXF bytes for DWG bytes, or refuse with a ReadError.

    `program` is the converter path or its leading arguments as a sequence.
    """
    limits = limits or Limits()
    command = [program] if isinstance(program, str) else list(program)
    if len(data) > limits.dxf_bytes:
        raise ReadError('dwg.budget', 'DWG size budget exceeded')
    if not SIGNATURE.match(data[:6]):
        raise ReadError('dwg.format', 'Not a supported DWG version')
    with tempfile.TemporaryDirectory(dir=workdir) as folder:
        source, target = Path(folder) / 'input.dwg', Path(folder) / 'output.dxf'
        source.write_bytes(data)
        try:
            # A confined caller may not open /dev/null: the converter talks to a file beside its input.
            with open(Path(folder) / 'converter.log', 'w+b') as log:
                result = subprocess.run([*command, '-y', '-o', str(target), str(source)], cwd=folder,
                                        stdin=log, stdout=log, stderr=log, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise ReadError('dwg.budget', 'DWG conversion time budget exceeded') from None
        except OSError:
            raise ReadError('dwg.converter', 'DWG converter is not available') from None
        written = target.is_file() and not target.is_symlink()
        # A converter stopped by the caller's file size limit leaves a file cut at that limit.
        if written and target.stat().st_size >= limits.dxf_bytes + (not result.returncode):
            raise ReadError('dwg.budget', 'Converted DXF size budget exceeded')
        if result.returncode or not written:
            raise ReadError('dwg.convert', 'DWG conversion failed')
        return _geometry(target.read_bytes())
