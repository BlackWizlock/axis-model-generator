"""Adversarial protocol fixture only. This is neither Blender nor a guard probe."""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import zlib


def png(width=512, height=512):
    def chunk(kind, payload):
        return (struct.pack('>I', len(payload)) + kind + payload +
                struct.pack('>I', zlib.crc32(kind + payload)))
    # Actual synthetic RGB scanlines; no claims about rendered geometry.
    pixels = (b'\0' + b'\x80\x90\xa0' * width) * height
    return (b'\x89PNG\r\n\x1a\n' +
            chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)) +
            chunk(b'IDAT', zlib.compress(pixels)) + chunk(b'IEND', b''))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--case', required=True)
    args = parser.parse_args()
    wire = args.input.read_bytes()
    document = json.loads(wire)
    measurements = {'schemaVersion': 1, 'fingerprint': hashlib.sha256(wire).hexdigest(),
                    'version': '4.5.14', 'vertexCount': document['vertexCount'],
                    'triangleCount': document['triangleCount'],
                    'bounds': document['bounds'], 'float32MaxErrorMetres': 0.0}
    image = png()
    case = args.case
    if case == 'version': measurements['version'] = '4.5.13'
    if case == 'vertices': measurements['vertexCount'] += 1
    if case == 'triangles': measurements['triangleCount'] += 1
    if case == 'bool_count': measurements['triangleCount'] = True
    if case == 'float_count': measurements['vertexCount'] = 3.0
    if case == 'schema': measurements['schemaVersion'] = 2
    if case == 'bool_schema': measurements['schemaVersion'] = True
    if case == 'fingerprint': measurements['fingerprint'] = '0' * 64
    if case == 'bounds': measurements['bounds']['min'][0] -= .01
    if case == 'reversed_bounds': measurements['bounds']['min'][0] = 1
    if case == 'nan_bounds': measurements['bounds']['min'][0] = float('nan')
    if case == 'string_bounds': measurements['bounds']['min'][0] = '-0.5'
    if case == 'bool_bounds': measurements['bounds']['min'][0] = True
    if case == 'bounds_fields': measurements['bounds']['file'] = '../../external.json'
    if case == 'float32_error': measurements['float32MaxErrorMetres'] = 1.00001e-5
    if case == 'negative_error': measurements['float32MaxErrorMetres'] = -.1
    if case == 'nan_error': measurements['float32MaxErrorMetres'] = float('nan')
    if case == 'bool_error': measurements['float32MaxErrorMetres'] = False
    if case == 'descriptor': measurements['thumbnail_path'] = '../../external.png'
    if case == 'missing_field': del measurements['float32MaxErrorMetres']
    if case == 'tolerance_boundary':
        measurements['bounds']['min'][2] = -1e-5
        measurements['float32MaxErrorMetres'] = 1e-5
    if case == 'png_dimensions': image = png(256, 512)
    if case == 'png_signature': image = b'not a PNG'
    if case == 'png_crc': image = image[:-1] + bytes([image[-1] ^ 1])
    if case == 'png_truncated': image = image[:-8]
    if case == 'png_oversize': image += b'\0' * (4 * 1024**2 + 1)
    if case == 'png_exact_limit':
        payload = b'a' * (4 * 1024**2 - len(image) - 12)
        kind = b'tEXt'
        padding = (struct.pack('>I', len(payload)) + kind + payload +
                   struct.pack('>I', zlib.crc32(kind + payload)))
        image = image[:-12] + padding + image[-12:]
    encoded = json.dumps(measurements, separators=(',', ':')).encode()
    if case == 'duplicate': encoded = encoded[:-1] + b',"version":"4.5.14"}'
    if case == 'json_oversize': encoded += b' ' * (65537 - len(encoded))
    if case == 'json_exact_limit': encoded += b' ' * (65536 - len(encoded))
    if case == 'invalid_utf8': encoded = b'\xff'
    if case == 'json_array': encoded = b'[]'
    if case == 'json_deep': encoded = b'[' * 2000 + b'0' + b']' * 2000
    output = args.output_dir
    (output / 'measurements.json').write_bytes(encoded)
    (output / 'thumbnail.png').write_bytes(image)
    if case == 'extra_output': (output / 'descriptor.json').write_text('{}')
    if case == 'missing_png': (output / 'thumbnail.png').unlink()
    if case == 'missing_measurements': (output / 'measurements.json').unlink()
    if case in {'symlink_measurements', 'symlink_png', 'hardlink_measurements'}:
        name = 'thumbnail.png' if case == 'symlink_png' else 'measurements.json'
        target = output.parent / ('external-' + name)
        (output / name).rename(target)
        if case == 'hardlink_measurements': (output / name).hardlink_to(target)
        else: (output / name).symlink_to(target)
    if case == 'fifo':
        import os
        (output / 'measurements.json').unlink()
        os.mkfifo(output / 'measurements.json')


if __name__ == '__main__':
    main()
