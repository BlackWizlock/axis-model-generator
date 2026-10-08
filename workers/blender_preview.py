# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Axis Consult contributors.
# This original trusted bpy script is separately licensed under GPL-3.0-or-later.
# It is not the Blender application and does not change Blender's own license.
"""Trusted checked-JSON mesh bridge. Execute only inside the guarded Blender child.

No arbitrary file import operators, user materials, textures, URLs or source code.
The caller supplies only its controlled preview and worker-owned output directory.
Process isolation and independent output acceptance belong to the separate runner.
"""
import hashlib
import json
import math
from pathlib import Path
import sys

# A trusted installation path derived only from this script, never user input.
_SOURCE = Path(__file__).resolve().parents[1] / 'src'
if str(_SOURCE) not in sys.path:
    sys.path.insert(0, str(_SOURCE))

from model_generator.web.preview import (PreviewError, PreviewLimits,
                                         decode_preview_json, preview_measurements)


EXPECTED_VERSION = '4.5.14'


def parse_arguments(arguments):
    if (len(arguments) != 4 or arguments[0] != '--input' or
            arguments[2] != '--output-dir' or not arguments[1] or not arguments[3]):
        raise ValueError('Expected exactly --input PREVIEW --output-dir STAGING')
    return Path(arguments[1]), Path(arguments[3])


def read_checked_preview(path):
    limits = PreviewLimits()
    with path.open('rb') as stream:
        wire = stream.read(limits.wire_bytes + 1)
    return decode_preview_json(wire, limits), hashlib.sha256(wire).hexdigest()


def render_checked_preview(document, fingerprint, output_dir):
    # Geometry is already checked before this module or any bpy allocations.
    measurements = preview_measurements(document)
    import bpy
    from mathutils import Vector

    version = bpy.app.version_string
    if version != EXPECTED_VERSION:
        raise PreviewError('preview_unsupported', 'Unexpected Blender runtime version')
    if not output_dir.is_dir() or any(output_dir.iterdir()):
        raise ValueError('Expected an existing empty worker-owned staging directory')
    positions, indices = document['positions'], document['indices']
    vertices = [tuple(positions[n:n + 3]) for n in range(0, len(positions), 3)]
    triangles = [tuple(indices[n:n + 3]) for n in range(0, len(indices), 3)]
    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete(use_global=False)
    mesh = bpy.data.meshes.new('checked-preview')
    mesh.from_pydata(vertices, [], triangles)
    mesh.update()
    obj = bpy.data.objects.new('checked-preview', mesh)
    bpy.context.scene.collection.objects.link(obj)
    material = bpy.data.materials.new('neutral-preview')
    material.use_nodes = True
    shader = material.node_tree.nodes.get('Principled BSDF')
    shader.inputs['Base Color'].default_value = (.55, .6, .65, 1)
    shader.inputs['Roughness'].default_value = .7
    obj.data.materials.append(material)

    bounds = document['bounds']
    centre = Vector(tuple(low / 2 + high / 2 for low, high in zip(bounds['min'], bounds['max'])))
    extent = max(maximum - minimum for minimum, maximum in zip(bounds['min'], bounds['max']))
    radius = max(extent, .01)
    camera_data = bpy.data.cameras.new('preview-camera')
    camera = bpy.data.objects.new('preview-camera', camera_data)
    bpy.context.scene.collection.objects.link(camera)
    camera.location = centre + Vector((1.5, -2, 1.7)) * radius
    camera.rotation_euler = (centre - camera.location).to_track_quat('-Z', 'Y').to_euler()
    camera_data.type = 'ORTHO'
    camera_data.ortho_scale = radius * 2.5
    camera_data.clip_start = max(radius * .001, .00001)
    camera_data.clip_end = max(radius * 100, 100)
    bpy.context.scene.camera = camera
    light_data = bpy.data.lights.new('preview-light', 'AREA')
    light = bpy.data.objects.new('preview-light', light_data)
    bpy.context.scene.collection.objects.link(light)
    light.location = centre + Vector((1, -1, 3)) * radius
    light.rotation_euler = (centre - light.location).to_track_quat('-Z', 'Y').to_euler()
    light_data.energy = 500
    light_data.size = radius * 2
    scene = bpy.context.scene
    scene.world.color = (.12, .12, .12)
    scene.render.engine = 'CYCLES'
    scene.cycles.device = 'CPU'
    scene.cycles.samples = 8
    scene.cycles.use_denoising = False
    scene.render.resolution_x = scene.render.resolution_y = 512
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = 'PNG'
    scene.render.image_settings.color_mode = 'RGBA'
    scene.render.filepath = str(output_dir / 'thumbnail.png')
    scene.render.use_file_extension = True
    scene.render.threads_mode = 'FIXED'
    scene.render.threads = 1

    # Measure actual Blender mesh coordinates, never echo the declared bounds.
    actual_minimum = [math.inf] * 3
    actual_maximum = [-math.inf] * 3
    maximum_error = 0.0
    for n, vertex in enumerate(mesh.vertices):
        for axis in range(3):
            value = float(vertex.co[axis])
            if not math.isfinite(value):
                raise PreviewError('preview_roundtrip_error', 'Nonfinite Blender mesh coordinate')
            maximum_error = max(maximum_error, abs(value - positions[n * 3 + axis]))
            actual_minimum[axis] = min(actual_minimum[axis], value)
            actual_maximum[axis] = max(actual_maximum[axis], value)
    if maximum_error > measurements['float32_tolerance_metres']:
        raise PreviewError('preview_roundtrip_error', 'Blender bridge exceeds coordinate tolerance')
    mesh.calc_loop_triangles()
    result = {'schemaVersion': 1, 'fingerprint': fingerprint, 'version': version,
              'vertexCount': len(mesh.vertices), 'triangleCount': len(mesh.loop_triangles),
              'bounds': {'min': actual_minimum, 'max': actual_maximum},
              'float32MaxErrorMetres': maximum_error}
    if (result['vertexCount'] != document['vertexCount'] or
            result['triangleCount'] != document['triangleCount']):
        raise PreviewError('preview_roundtrip_error', 'Blender mesh counts disagree')
    bpy.ops.render.render(write_still=True)
    data = json.dumps(result, allow_nan=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    if len(data) > 65536:
        raise ValueError('Measurements exceed 64 KiB')
    with (output_dir / 'measurements.json').open('xb') as stream:
        stream.write(data)


def main():
    if '--' not in sys.argv:
        raise ValueError('Missing Blender script argument separator')
    preview_path, output_dir = parse_arguments(sys.argv[sys.argv.index('--') + 1:])
    document, fingerprint = read_checked_preview(preview_path)
    render_checked_preview(document, fingerprint, output_dir)


if __name__ == '__main__':
    main()
