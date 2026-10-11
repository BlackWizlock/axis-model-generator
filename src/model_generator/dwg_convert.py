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
            result = subprocess.run([*command, '-y', '-o', str(target), str(source)], cwd=folder,
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise ReadError('dwg.budget', 'DWG conversion time budget exceeded') from None
        except OSError:
            raise ReadError('dwg.converter', 'DWG converter is not available') from None
        if result.returncode or not target.is_file() or target.is_symlink():
            raise ReadError('dwg.convert', 'DWG conversion failed')
        if target.stat().st_size > limits.dxf_bytes:
            raise ReadError('dwg.budget', 'Converted DXF size budget exceeded')
        return target.read_bytes()
