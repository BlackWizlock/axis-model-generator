"""Read elevations from an ASCII DXF site plan and keep where each one came from.

Three kinds of evidence are told apart and never mixed silently:
geometry that carries its own Z, flat polylines with an elevation, and flat
numeric labels tied to exactly one survey point. A drawing without any of them
yields mode 'none'. Units are never guessed: without a declared unit the caller
must state one. Coordinates are not recentred. No normative readiness is implied.
"""

from dataclasses import dataclass
import math
import re

from .limits import Limits, ReadError

# $INSUNITS values with an unambiguous length; 0 means the drawing declares none.
UNIT_METERS = {1: 0.0254, 2: 0.3048, 4: 0.001, 5: 0.01, 6: 1.0, 14: 0.1}
CODEPAGES = {'ANSI_1251': 'cp1251', 'ANSI_1252': 'cp1252', 'ANSI_1250': 'cp1250', 'UTF-8': 'utf-8'}
# Entities placed in their own object coordinate system; only the world Z axis is read.
PLANAR = {'LWPOLYLINE', 'TEXT', 'MTEXT', 'INSERT', 'POLYLINE', 'ATTRIB'}
NUMBER = re.compile(r'[+-]?\d{1,5}(?:[.,]\d{1,4})?')
# A label belongs to a survey point no farther than this many text heights.
LABEL_REACH = 3.0


@dataclass(frozen=True)
class HeightSample:
    x: float
    y: float
    z: float
    source: str
    entity: str
    handle: str
    layer: str


@dataclass(frozen=True)
class HeightLine:
    points: tuple
    closed: bool
    source: str
    entity: str
    handle: str
    layer: str


@dataclass(frozen=True)
class HeightSurvey:
    mode: str
    unit_meters: float
    samples: tuple
    lines: tuple
    counts: dict
    z_range: tuple


def _reject(message, rule='dxf.unsupported'):
    raise ReadError(rule, message)


def _pairs(data, limits):
    if len(data) > limits.dxf_bytes:
        _reject('DXF size budget exceeded', 'dxf.budget')
    if data.startswith(b'AutoCAD Binary DXF'):
        _reject('Binary DXF is unsupported')
    lines = data.split(b'\n')
    if lines and lines[-1].strip() == b'':
        lines.pop()
    if len(lines) % 2:
        _reject('Group code without a value', 'dxf.integrity')
    if len(lines) // 2 > limits.dxf_pairs:
        _reject('DXF group budget exceeded', 'dxf.budget')
    pairs = []
    for index in range(0, len(lines), 2):
        try:
            code = int(lines[index])
        except ValueError:
            _reject('Malformed group code', 'dxf.integrity')
        # 999 is a comment; converters put their name there.
        if code != 999:
            pairs.append((code, lines[index + 1].strip()))
    return pairs


def _sections(pairs):
    """Section name to its group pairs; every section must be closed and unique."""
    sections, index = {}, 0
    while index < len(pairs):
        code, value = pairs[index]
        if (code, value) == (0, b'EOF'):
            if index != len(pairs) - 1:
                _reject('Data after end of file', 'dxf.integrity')
            return sections
        if (code, value) != (0, b'SECTION') or index + 1 >= len(pairs) or pairs[index + 1][0] != 2:
            _reject('Malformed section', 'dxf.integrity')
        name = pairs[index + 1][1].decode('ascii', 'replace')
        end = next((i for i in range(index + 2, len(pairs)) if pairs[i] == (0, b'ENDSEC')), None)
        if end is None or name in sections:
            _reject('Unclosed or repeated section', 'dxf.integrity')
        sections[name] = pairs[index + 2:end]
        index = end + 1
    _reject('Missing end of file', 'dxf.integrity')


def _header(pairs):
    variables, name = {}, None
    for code, value in pairs:
        if code == 9:
            name = value.decode('ascii', 'replace')
            # Converters repeat unrelated variables; the ones read here must be unique.
            if name in variables and name in ('$ACADVER', '$DWGCODEPAGE', '$INSUNITS'):
                _reject('Repeated header variable', 'dxf.integrity')
            variables[name] = []
        elif name is not None:
            variables[name].append((code, value))
    return variables


def _records(pairs):
    """Split an entity stream into (type, groups) records."""
    records = []
    for code, value in pairs:
        if code == 0:
            records.append((value.decode('ascii', 'replace'), []))
        elif not records:
            _reject('Group outside an entity', 'dxf.integrity')
        else:
            records[-1][1].append((code, value))
    return records


def _number(value):
    try:
        number = float(value)
    except ValueError:
        _reject('Malformed number', 'dxf.integrity')
    if not math.isfinite(number):
        _reject('Non-finite number', 'dxf.integrity')
    return number


class _Entity:
    def __init__(self, kind, groups, encoding):
        self.kind, self.groups, self.encoding = kind, groups, encoding

    def all(self, code):
        return [value for key, value in self.groups if key == code]

    def number(self, code, default=None):
        values = self.all(code)
        if len(values) > 1:
            _reject('Repeated group in ' + self.kind, 'dxf.integrity')
        return _number(values[0]) if values else default

    def text(self, code, default=''):
        values = self.all(code)
        try:
            return values[0].decode(self.encoding) if values else default
        except UnicodeDecodeError:
            _reject('Text does not match the declared code page', 'dxf.integrity')

    def point(self, base):
        x, y = self.number(base), self.number(base + 10)
        if x is None or y is None:
            _reject('Incomplete point in ' + self.kind, 'dxf.integrity')
        return x, y, self.number(base + 20, 0.0)

    def world_plane(self):
        normal = (self.number(210, 0.0), self.number(220, 0.0), self.number(230, 1.0))
        return normal == (0.0, 0.0, 1.0)


def _label_value(text):
    """A label is a height only when the whole text is one plain number."""
    plain = re.sub(r'\\[A-Za-z][^;]*;|[{}]', '', text).strip()
    if not NUMBER.fullmatch(plain):
        return None
    return float(plain.replace(',', '.'))


def read_heights(data, limits=None, unit_meters=None):
    """Return a HeightSurvey in metres from ASCII DXF bytes.

    unit_meters states the drawing unit when the file declares none; a stated
    unit that contradicts a declared one is refused rather than preferred.
    """
    limits = limits or Limits()
    sections = _sections(_pairs(data, limits))
    if 'ENTITIES' not in sections:
        _reject('Missing entities section', 'dxf.integrity')
    header = _header(sections.get('HEADER', []))
    codepage = b''.join(v for _, v in header.get('$DWGCODEPAGE', [])).decode('ascii', 'replace')
    version = b''.join(v for _, v in header.get('$ACADVER', [])).decode('ascii', 'replace')
    # AC1021 (2007) and later store text as UTF-8 regardless of the code page.
    encoding = 'utf-8' if version >= 'AC1021' else CODEPAGES.get(codepage.upper() or 'ANSI_1252')
    if encoding is None:
        _reject('Unsupported code page')
    declared = None
    units = header.get('$INSUNITS', [])
    if units:
        if len(units) != 1 or not units[0][1].lstrip(b'-').isdigit():
            _reject('Malformed drawing unit', 'dxf.integrity')
        code = int(units[0][1])
        if code and code not in UNIT_METERS:
            _reject('Unsupported drawing unit')
        declared = UNIT_METERS.get(code)
    if unit_meters is not None and (not math.isfinite(unit_meters) or unit_meters <= 0):
        _reject('Stated unit must be positive', 'dxf.integrity')
    if declared is not None and unit_meters is not None and declared != unit_meters:
        _reject('Stated unit contradicts the drawing unit', 'dxf.units')
    unit = declared if declared is not None else unit_meters
    if unit is None:
        _reject('Drawing unit is not declared and none was stated', 'dxf.units')

    records = _records(sections['ENTITIES'])
    if len(records) > limits.dxf_entities:
        _reject('DXF entity budget exceeded', 'dxf.budget')
    counts = {'entities': len(records), 'paper_space': 0, 'tilted': 0, 'flat': 0,
              'blocks': 0, 'unread': 0, 'labels': 0, 'labels_unanchored': 0}
    samples, lines, anchors, labels = [], [], [], []

    def origin(entity):
        return entity.kind, entity.text(5), entity.text(8)

    def sample(point, source, entity):
        samples.append(HeightSample(point[0] * unit, point[1] * unit, point[2] * unit, source, *origin(entity)))

    def line(points, closed, source, entity):
        lines.append(HeightLine(tuple((x * unit, y * unit, z * unit) for x, y, z in points),
                                closed, source, *origin(entity)))

    index = 0
    while index < len(records):
        entity = _Entity(*records[index], encoding)
        index += 1
        kind = entity.kind
        follow = []
        if kind in ('POLYLINE', 'INSERT') and entity.number(66, 0.0) == 1 or kind == 'POLYLINE':
            # Vertices and attributes follow their owner up to SEQEND.
            while index < len(records) and records[index][0] != 'SEQEND':
                follow.append(_Entity(*records[index], encoding))
                index += 1
            if index == len(records):
                _reject('Unterminated entity sequence', 'dxf.integrity')
            index += 1
        if entity.number(67, 0.0) == 1:
            counts['paper_space'] += 1
            continue
        if kind in PLANAR and not entity.world_plane():
            counts['tilted'] += 1
            continue
        if kind == 'POINT':
            point = entity.point(10)
            anchors.append((point, entity))
            if point[2] != 0:
                sample(point, 'geometry', entity)
            else:
                counts['flat'] += 1
        elif kind == 'LINE':
            points = [entity.point(10), entity.point(11)]
            if all(p[2] != 0 for p in points):
                line(points, False, 'geometry', entity)
            else:
                counts['flat'] += 1
        elif kind == '3DFACE':
            points = [entity.point(base) for base in (10, 11, 12, 13)]
            if points[3] == points[2]:
                points.pop()
            if all(p[2] != 0 for p in points):
                line(points, True, 'geometry', entity)
            else:
                counts['flat'] += 1
        elif kind == 'LWPOLYLINE':
            xs, ys = entity.all(10), entity.all(20)
            elevation = entity.number(38, 0.0)
            if len(xs) != len(ys) or len(xs) < 2:
                _reject('Malformed polyline vertices', 'dxf.integrity')
            if elevation != 0:
                line([(_number(x), _number(y), elevation) for x, y in zip(xs, ys)],
                     int(entity.number(70, 0.0)) & 1 == 1, 'elevation', entity)
            else:
                counts['flat'] += 1
        elif kind == 'POLYLINE':
            flags = int(entity.number(70, 0.0))
            if flags & (16 | 64):
                counts['unread'] += 1
                continue
            vertices = [v for v in follow if v.kind == 'VERTEX']
            if len(vertices) != len(follow) or len(vertices) < 2:
                _reject('Malformed polyline sequence', 'dxf.integrity')
            if flags & 8:
                points, source = [v.point(10) for v in vertices], 'geometry'
            else:
                elevation = entity.number(30, 0.0)
                points, source = [(*v.point(10)[:2], elevation) for v in vertices], 'elevation'
            if all(p[2] != 0 for p in points):
                line(points, flags & 1 == 1, source, entity)
            else:
                counts['flat'] += 1
        elif kind in ('TEXT', 'MTEXT'):
            text = entity.text(1) if kind == 'TEXT' else ''.join(
                value.decode(encoding, 'replace') for value in entity.all(3)) + entity.text(1)
            value = _label_value(text)
            if value is None:
                continue
            height = entity.number(40)
            if height is None or height <= 0:
                _reject('Label without a text height', 'dxf.integrity')
            aligned = kind == 'TEXT' and (entity.number(72, 0.0) or entity.number(73, 0.0))
            labels.append((entity.point(11 if aligned else 10), value, height, entity))
            counts['labels'] += 1
        elif kind == 'INSERT':
            counts['blocks'] += 1
            point = entity.point(10)
            anchors.append((point, entity))
            if point[2] != 0:
                sample(point, 'geometry', entity)
            for attribute in follow:
                value = _label_value(attribute.text(1)) if attribute.kind == 'ATTRIB' else None
                # One numeric attribute states the height of its own block; two are ambiguous.
                if value is not None:
                    labels.append((point, value, None, entity))
                    counts['labels'] += 1
        else:
            counts['unread'] += 1

    # A flat label is a height only for the single survey point within its reach,
    # and only when that point has no second label. Everything else stays unused.
    claimed = {}
    for position, value, height, entity in labels:
        if height is None:
            near = [i for i, (point, owner) in enumerate(anchors) if owner is entity]
        else:
            reach = height * LABEL_REACH
            near = [i for i, (point, _) in enumerate(anchors)
                    if math.hypot(point[0] - position[0], point[1] - position[1]) <= reach]
        if len(near) == 1:
            claimed.setdefault(near[0], []).append((value, entity))
        else:
            counts['labels_unanchored'] += 1
    for anchor, values in claimed.items():
        point, _ = anchors[anchor]
        if len(values) != 1 or point[2] != 0:
            counts['labels_unanchored'] += len(values)
            continue
        value, entity = values[0]
        # Label text states metres; it is not scaled by the drawing unit.
        samples.append(HeightSample(point[0] * unit, point[1] * unit, value, 'label', *origin(entity)))

    sources = {s.source for s in samples} | {l.source for l in lines}
    if 'geometry' in sources:
        mode = 'geometry'
    elif 'elevation' in sources:
        mode = 'elevation'
    elif 'label' in sources:
        mode = 'label'
    else:
        mode = 'none'
    heights = [s.z for s in samples] + [p[2] for l in lines for p in l.points]
    if any(not math.isfinite(v) for s in samples for v in (s.x, s.y, s.z)) or any(
            not math.isfinite(v) for l in lines for p in l.points for v in p):
        _reject('Coordinate overflow', 'dxf.integrity')
    return HeightSurvey(mode, unit, tuple(samples), tuple(lines), counts,
                        (min(heights), max(heights)) if heights else None)
