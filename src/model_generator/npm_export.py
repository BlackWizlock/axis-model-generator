"""Assemble the placed building and its terrain into one low-polygon FBX.

Geometry is reduced within a stated deviation and written relative to a local
origin, because single precision readers lose centimetres at survey distances.
The origin is part of the report: local coordinates plus the origin give the
survey coordinates back. Materials keep their names, colours, triangle
assignments and embedded pictures; texture coordinates stay with the corners
of the triangles through the reduction.
"""

from array import array
from dataclasses import dataclass, replace
import math

from .fbx_write import PNG, ExportMaterial, ExportMesh, ExportTexture, write_fbx
from .simplify import simplify

TERRAIN = 'Terrain'
# Embedded pictures of one output file, all together.
PICTURE_NODES = 2048  # room kept for the nodes and connections of one use of a picture


@dataclass(frozen=True)
class NpmExport:
    data: bytes
    report: dict
    meshes: tuple = ()  # the written meshes, local to the origin of the report


def _name(node, fallback):
    text = node.props[1].split('\x00')[0] if len(node.props) > 1 and isinstance(node.props[1], str) else ''
    return ''.join(c if ord(c) >= 32 else '_' for c in text).strip() or fallback


def _text(node, name):
    child = node.child(name) if node is not None else None
    return child.props[0] if child is not None and len(child.props) == 1 and isinstance(child.props[0], str) else ''


def _untransformed(texture):
    """The picture lies on the texture coordinates as they are: no shift, turn or scale."""
    container = texture.child('Properties70')
    stated = {p.props[0]: p.props[4:] for p in (container.children if container else ())
              if p.name == 'P' and p.props and isinstance(p.props[0], str)}
    for name, rest in (('ModelUVTranslation', 0.0), ('ModelUVScaling', 1.0)):
        child = texture.child(name)
        if child is not None:
            stated[name] = child.props
    plain = {'Translation': 0.0, 'Rotation': 0.0, 'Scaling': 1.0, 'ModelUVTranslation': 0.0, 'ModelUVScaling': 1.0}
    return all(all(type(v) in (int, float) and v == rest for v in stated[name])
               for name, rest in plain.items() if name in stated)


def _pictures(scene, by_id):
    """Material id to its pictures in connection order: (channel, file name, PNG bytes).

    Pictures that are not an embedded PNG laid plainly on the texture
    coordinates are counted and left out.
    """
    connections = next(n for n in scene.source_nodes if n.name == 'Connections')
    links = [n.props for n in connections.children if n.name == 'C']
    kind = lambda object_id: by_id[object_id].name if type(object_id) is int and object_id in by_id else None
    media = {}
    for link in links:
        if len(link) == 3 and link[0] == 'OO' and kind(link[1]) == 'Video' and kind(link[2]) == 'Texture':
            media.setdefault(link[2], by_id[link[1]])
    # One texture and one material may meet in any number of connections:
    # each is looked at once, whatever the number.
    pictures, channels, plain, left_out = {}, {}, {}, 0
    for link in links:
        if not (len(link) == 4 and link[0] == 'OP' and kind(link[1]) == 'Texture' and kind(link[2]) == 'Material'):
            continue
        texture, video, channel = by_id[link[1]], media.get(link[1]), link[3]
        content = video.child('Content') if video is not None else None
        data = content.props[0] if content is not None and len(content.props) == 1 else None
        taken, known = pictures.setdefault(link[2], []), channels.setdefault(link[2], set())
        if (not isinstance(channel, str) or not channel.isascii() or not channel.replace('_', '').isalnum()
                or channel in known or not isinstance(data, bytes) or not data.startswith(PNG)):
            left_out += 1
            continue
        if link[1] not in plain:
            plain[link[1]] = _untransformed(texture)
        if not plain[link[1]]:
            left_out += 1
            continue
        known.add(channel)
        stated = _text(video, 'RelativeFilename') or _text(video, 'Filename') or _text(texture, 'FileName')
        name = ''.join(c if ord(c) >= 32 else '_' for c in stated.replace('\\', '/').rsplit('/', 1)[-1]).strip()
        taken.append((channel, name.rsplit('.', 1)[0] or 'texture', data))
    return pictures, left_out


def _material(node, object_id):
    color = None
    container = node.child('Properties70')
    for prop in container.children if container else ():
        if prop.name == 'P' and prop.props[:1] == ['DiffuseColor']:
            values = prop.props[4:]
            if len(values) == 3 and all(type(v) in (int, float) and 0 <= v <= 1 for v in values):
                color = tuple(float(v) for v in values)
    return ExportMaterial(_name(node, f'Material {object_id}'), color)


class _Assembly:
    """Meshes of one output file: unique names, shared materials, one local origin."""

    def __init__(self, origin, taken=()):
        self.origin, self.meshes, self.entries = origin, [], []
        self._names, self._material_names, self._materials = set(taken), set(), {}
        self._files, self._file_names, self.textures_left_out = {}, set(), 0
        self._uses, self._colour, self._counts = {}, set(), {}

    def texture(self, channel, stem, data):
        """The picture under a file name of this output."""
        if data not in self._files:
            self._files[data] = self.unique(stem, self._file_names) + '.png'
        self._uses[data] = self._uses.get(data, 0) + 1
        if channel == 'DiffuseColor':
            self._colour.add(data)
        return ExportTexture(channel, self._files[data], data)

    def _keeping(self, kept):
        """The meshes with only the pictures of `kept` on their materials."""
        trimmed = {}
        def material(source):
            if id(source) not in trimmed:
                trimmed[id(source)] = replace(source, textures=tuple(t for t in source.textures if t.content in kept))
            return trimmed[id(source)]
        return [replace(mesh, materials=tuple(material(m) for m in mesh.materials)) for mesh in self.meshes]

    def _write(self, max_bytes):
        """The file and the pictures it carries.

        Every picture is written when the file stays within `max_bytes`.
        Otherwise the pictures share the room the geometry leaves: the colour
        pictures first, the smaller before the larger.
        """
        kept = set(self._files)
        if max_bytes is None or not kept:
            return write_fbx(self.meshes), kept
        if sum(len(picture) for picture in kept) < max_bytes:
            data = write_fbx(self.meshes)
            if len(data) <= max_bytes:
                return data, kept
        bare = write_fbx(self._keeping(()))
        room, kept = max_bytes - len(bare), set()
        for picture in sorted(self._files, key=lambda item: (item not in self._colour, len(item), self._files[item])):
            cost = len(picture) + PICTURE_NODES * self._uses[picture]
            if cost <= room:
                kept.add(picture)
                room -= cost
        data = write_fbx(self._keeping(kept)) if kept else bare
        return (data, kept) if len(data) <= max_bytes else (bare, set())

    def local(self, positions):
        return array('d', (c - self.origin[i % 3] for i, c in enumerate(positions)))

    def unique(self, name, taken):
        # The count goes on from the last number given to this name.
        key = (id(taken), name)
        candidate, number = name, self._counts.get(key, 1)
        while candidate in taken:
            number += 1
            candidate = f'{name} {number}'
        self._counts[key] = number
        taken.add(candidate)
        return candidate

    def add_scene(self, scene, meshes, kind, max_error, expired=None, unwrapping=True):
        """Reduce the meshes of one source scene; names and materials come from the scene.

        Without `unwrapping` the texture coordinates and the pictures stay
        behind and every picture is counted as left out.
        """
        objects = next(n for n in scene.source_nodes if n.name == 'Objects')
        by_id = {n.props[0]: n for n in objects.children}
        pictures, left_out = _pictures(scene, by_id)
        if not unwrapping:
            left_out += len({picture[2] for found in pictures.values() for picture in found})
            pictures = {}
        self.textures_left_out += left_out
        for mesh in meshes:
            faces = len(mesh.triangles) // 3
            slots = mesh.material_indices
            if len(slots) == 1:
                slots = array('i', slots * faces)
            # Source triangles without a stated slot stay without a material.
            assigned = len(slots) == faces and bool(mesh.material_ids)
            reduced = simplify(self.local(mesh.positions), mesh.triangles, slots if assigned else (),
                               max_error=max_error, expired=expired,
                               uvs=mesh.uvs if unwrapping and len(mesh.uvs) == faces * 6 else ())
            materials = []
            for object_id in mesh.material_ids if assigned else ():
                key = (id(scene), object_id)
                if key not in self._materials:
                    material = _material(by_id[object_id], object_id)
                    textures = (self.texture(*picture) for picture in pictures.get(object_id, ()))
                    self._materials[key] = ExportMaterial(self.unique(material.name, self._material_names),
                                                          material.color, tuple(textures))
                materials.append(self._materials[key])
            name = self.unique(_name(by_id[mesh.model_id], f'Mesh {mesh.model_id}'), self._names)
            self.meshes.append(ExportMesh(name, reduced.positions, reduced.triangles, tuple(materials),
                                          reduced.material_indices, reduced.uvs))
            self.entries.append({'name': name, 'kind': kind, 'materials': [m.name for m in materials],
                                 'materials_assigned': assigned, 'texture_coordinates': bool(reduced.uvs),
                                 **reduced.report})

    def finish(self, max_bytes=None, **facts):
        data, kept = self._write(max_bytes)
        if len(kept) < len(self._files):
            self.meshes = self._keeping(kept)
        before = sum(e['input_triangles'] for e in self.entries)
        after = sum(e['output_triangles'] for e in self.entries)
        report = {'format': 'FBX 7400 binary', 'unit': 'metre', 'up_axis': 'Z', 'origin': self.origin,
                  'objects': self.entries, 'input_triangles': before, 'output_triangles': after,
                  'triangle_share': after / before,
                  'textures': {'written': len(kept), 'bytes': sum(len(picture) for picture in kept),
                               'left_out': self.textures_left_out + len(self._files) - len(kept)}, **facts}
        return NpmExport(data, report, tuple(self.meshes))


def _origin(meshes):
    low = [min(min(m.positions[axis::3]) for m in meshes) for axis in range(2)]
    return (float(math.floor(low[0])), float(math.floor(low[1])), 0.0)


def export_npm(scene, placed, terrain, max_error=0.02, terrain_error=None):
    """Return the FBX bytes and a report for a placed building on its terrain.

    `scene` is the source FbxScene of `placed`; it supplies names and materials.
    """
    assembly = _Assembly(_origin(placed.meshes), (TERRAIN,))
    reduced = simplify(assembly.local(terrain.positions), terrain.triangles,
                       max_error=max_error if terrain_error is None else terrain_error)
    assembly.meshes.append(ExportMesh(TERRAIN, reduced.positions, reduced.triangles, (ExportMaterial(TERRAIN),)))
    assembly.entries.append({'name': TERRAIN, 'kind': 'terrain', **reduced.report})
    assembly.add_scene(scene, placed.meshes, 'building', max_error)
    return assembly.finish(building_bounds=placed.bounds, control_residuals=placed.residuals,
                           ground_range=placed.ground_range)


def export_scenes(scenes, max_error=0.02, expired=None, max_bytes=None, unwrapping=True):
    """Return one reduced FBX for source scenes that already share coordinates.

    `scenes` is a sequence of (kind, FbxScene), for example a building and its
    ground exported from one model. Nothing is moved apart from the common
    shift to the local origin stated in the report. `expired` stops the
    reduction with OverBudget once the caller's time is spent. `max_bytes` is
    the size the file must keep to: pictures that do not fit are left out
    and counted, the geometry is never cut to make room. Without `unwrapping`
    the file carries neither texture coordinates nor pictures.
    """
    scenes = tuple(scenes)
    assembly = _Assembly(_origin([mesh for _, scene in scenes for mesh in scene.meshes]))
    for kind, scene in scenes:
        assembly.add_scene(scene, scene.meshes, kind, max_error, expired, unwrapping)
    return assembly.finish(max_bytes)
