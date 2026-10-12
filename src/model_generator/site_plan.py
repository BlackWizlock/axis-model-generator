"""The lines of a site plan with their heights, as a view of the drawing.

A drawing gives lines at stated levels. Each one is shown where it lies, as a
thin flat ribbon at its own heights. Nothing between the lines is filled in:
this is the drawing, not a surface, and it claims no relief.
"""

from array import array
from dataclasses import dataclass
import math

from .diagnostics import Finding
from .dxf_heights import read_heights
from .limits import Limits, ReadError

PLAN_NAME = 'input.dwg'
RIBBON_WIDTH = 0.12       # metres, across a line
PLAN_SEGMENTS = 45_000    # four vertices and two triangles each
FACT_LAYERS = 100


@dataclass(frozen=True)
class PlanMesh:
    positions: array
    triangles: array


def plan_mesh(survey, width=RIBBON_WIDTH, max_segments=PLAN_SEGMENTS):
    """One mesh of ribbons along the lines of `survey`, in its own coordinates."""
    positions, triangles, half, segments = array('d'), array('I'), width / 2, 0
    for line in survey.lines:
        points = list(line.points)
        if line.closed:
            points.append(points[0])
        for (ax, ay, az), (bx, by, bz) in zip(points, points[1:]):
            length = math.hypot(bx - ax, by - ay)
            if length < 1e-9:
                continue
            segments += 1
            if segments > max_segments:
                raise ReadError('plan.budget', 'Site plan line budget exceeded')
            nx, ny = (ay - by) / length * half, (bx - ax) / length * half
            base = len(positions) // 3
            positions.extend((ax + nx, ay + ny, az, ax - nx, ay - ny, az,
                              bx - nx, by - ny, bz, bx + nx, by + ny, bz))
            triangles.extend((base, base + 1, base + 2, base, base + 2, base + 3))
    if not segments:
        raise ReadError('plan.heights', 'No lines with heights in the drawing')
    return PlanMesh(positions, triangles)


def inspect_plan(dxf, limits=None):
    """Return (facts, findings, mesh) for the DXF bytes of a site plan.

    The facts name layers and state numbers only. A drawing without lines at
    stated heights is refused: there would be nothing to show.
    """
    limits = limits or Limits()
    survey = read_heights(dxf, limits)
    mesh = plan_mesh(survey)
    layers = {}
    for line in survey.lines:
        row = layers.setdefault(line.layer, {'lines': 0, 'low': math.inf, 'high': -math.inf})
        row['lines'] += 1
        row['low'] = min(row['low'], *(p[2] for p in line.points))
        row['high'] = max(row['high'], *(p[2] for p in line.points))
    xs, ys = [p[0] for l in survey.lines for p in l.points], [p[1] for l in survey.lines for p in l.points]
    facts = {'file': PLAN_NAME, 'read_status': 'read', 'unit_meters': survey.unit_meters, 'mode': survey.mode,
             'lines': len(survey.lines), 'points': len(survey.samples), 'counts': dict(survey.counts),
             'z_range': list(survey.z_range), 'bounds': [[min(xs), min(ys)], [max(xs), max(ys)]],
             'layers_total': len(layers),
             'layers': [{'name': name, **row} for name, row in sorted(layers.items())[:FACT_LAYERS]]}
    findings = [Finding('plan.heights', 'pass', PLAN_NAME, {'lines': len(survey.lines), 'z_range': list(survey.z_range)},
                        None, 'Lines with stated heights read in metres')]
    unused = survey.counts.get('labels_unanchored', 0)
    if unused:
        findings.append(Finding('plan.labels', 'warn', PLAN_NAME, unused, 0,
                                'Height labels without a single survey point are not used'))
    return facts, findings, mesh
