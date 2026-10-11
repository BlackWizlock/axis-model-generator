"""Assemble the placed building and its terrain into one low-polygon FBX.

Geometry is reduced within a stated deviation and written relative to a local
origin, because single precision readers lose centimetres at survey distances.
The origin is part of the report: local coordinates plus the origin give the
survey coordinates back. Materials keep their names, colours and triangle
assignments; textures and UVs of the source are not carried over.
"""

from array import array
from dataclasses import dataclass
import math

from .fbx_write import ExportMaterial, ExportMesh, write_fbx
from .simplify import simplify

TERRAIN = 'Terrain'


@dataclass(frozen=True)
class NpmExport:
    data: bytes
    report: dict


def _name(node, fallback):
    text = node.props[1].split('\x00')[0] if len(node.props) > 1 and isinstance(node.props[1], str) else ''
    return ''.join(c if ord(c) >= 32 else '_' for c in text).strip() or fallback


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

    def local(self, positions):
        return array('d', (c - self.origin[i % 3] for i, c in enumerate(positions)))

    def unique(self, name, taken):
        candidate, number = name, 1
        while candidate in taken:
            number += 1
            candidate = f'{name} {number}'
        taken.add(candidate)
        return candidate

    def add_scene(self, scene, meshes, kind, max_error, expired=None):
        """Reduce the meshes of one source scene; names and materials come from the scene."""
        objects = next(n for n in scene.source_nodes if n.name == 'Objects')
        by_id = {n.props[0]: n for n in objects.children}
        for mesh in meshes:
            faces = len(mesh.triangles) // 3
            slots = mesh.material_indices
            if len(slots) == 1:
                slots = array('i', slots * faces)
            # Source triangles without a stated slot stay without a material.
            assigned = len(slots) == faces and bool(mesh.material_ids)
            reduced = simplify(self.local(mesh.positions), mesh.triangles, slots if assigned else (),
                               max_error=max_error, expired=expired)
            materials = []
            for object_id in mesh.material_ids if assigned else ():
                key = (id(scene), object_id)
                if key not in self._materials:
                    material = _material(by_id[object_id], object_id)
                    self._materials[key] = ExportMaterial(self.unique(material.name, self._material_names),
                                                          material.color)
                materials.append(self._materials[key])
            name = self.unique(_name(by_id[mesh.model_id], f'Mesh {mesh.model_id}'), self._names)
            self.meshes.append(ExportMesh(name, reduced.positions, reduced.triangles, tuple(materials),
                                          reduced.material_indices))
            self.entries.append({'name': name, 'kind': kind, 'materials': [m.name for m in materials],
                                 'materials_assigned': assigned, **reduced.report})

    def finish(self, **facts):
        before = sum(e['input_triangles'] for e in self.entries)
        after = sum(e['output_triangles'] for e in self.entries)
        report = {'format': 'FBX 7400 binary', 'unit': 'metre', 'up_axis': 'Z', 'origin': self.origin,
                  'objects': self.entries, 'input_triangles': before, 'output_triangles': after,
                  'triangle_share': after / before, 'textures': 'not exported', **facts}
        return NpmExport(write_fbx(self.meshes), report)


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


def export_scenes(scenes, max_error=0.02, expired=None):
    """Return one reduced FBX for source scenes that already share coordinates.

    `scenes` is a sequence of (kind, FbxScene), for example a building and its
    ground exported from one model. Nothing is moved apart from the common
    shift to the local origin stated in the report. `expired` stops the
    reduction with OverBudget once the caller's time is spent.
    """
    scenes = tuple(scenes)
    assembly = _Assembly(_origin([mesh for _, scene in scenes for mesh in scene.meshes]))
    for kind, scene in scenes:
        assembly.add_scene(scene, scene.meshes, kind, max_error, expired)
    return assembly.finish()
