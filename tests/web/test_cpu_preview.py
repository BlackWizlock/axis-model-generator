"""Actual offline CPU mesh pixels and native guarded renderer, never a mock render."""
from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from PIL import Image
from model_generator.web.config import Settings
from model_generator.web.preview import (PreviewLimits,PreviewError,build_synthetic_demo,write_preview,
                                         build_preview)
from model_generator.package_reader import read_package_bytes
from model_generator.package_scene import inspect_package_scene
from fixtures.package_builders import make_package,make_scene


class CpuPixelsTests(unittest.TestCase):
    def render(self,document):
        from model_generator.web.preview_child import raster_mesh
        return raster_mesh(document)

    def test_actual_triangle_pixels_and_measured_arrays(self):
        document=build_synthetic_demo()
        png,measurements=self.render(document)
        image=Image.open(io.BytesIO(png)); image.load()
        self.assertEqual(image.size,(512,512))
        pixels=list(image.get_flattened_data())
        self.assertGreater(sum(pixel!=(30,34,39) for pixel in pixels),10000)
        self.assertLess(sum(pixel!=(30,34,39) for pixel in pixels),100000)
        self.assertEqual((measurements['engine'],measurements['version']),('python-cpu','1'))
        self.assertEqual((measurements['vertexCount'],measurements['triangleCount']),(3,1))
        self.assertEqual(measurements['bounds'],document['bounds'])
        self.assertLessEqual(measurements['float32MaxErrorMetres'],1e-5)

    def test_mirror_two_instances_and_9511m_source_hash_rebase(self):
        scene=make_scene(); original=scene['instances'][0]
        original['transform'][0]=-1; original['transform'][3]=9511
        second=json.loads(json.dumps(original)); second['instance_id']='second'; second['element_key']['unique_id']='second'; second['transform'][3]=9513
        scene['instances'].append(second)
        wire=make_package(scene_updates=scene); sha=hashlib.sha256(wire).hexdigest()
        package=read_package_bytes(wire)
        document=build_preview(package,inspect_package_scene(package),PreviewLimits())
        png,result=self.render(document)
        self.assertEqual(hashlib.sha256(wire).hexdigest(),sha)
        self.assertGreater(document['origin'][0],9510)
        self.assertEqual((result['vertexCount'],result['triangleCount']),(6,2))
        self.assertEqual(result['bounds'],document['bounds'])
        self.assertNotEqual(png,self.render(build_synthetic_demo())[0])

    def test_depth_buffer_occludes_far_triangle_independent_of_face_order(self):
        # Equal projected footprints separated along the fixed camera direction.
        from model_generator.web.preview_child import raster_arrays
        import numpy as np
        view=np.array((1.5,-2,1.7)); view=view/np.sqrt(np.sum(view*view))
        base=np.array(((-.6,-.6,0),(.6,-.6,0),(0,.6,0)))
        positions=np.concatenate((base-view*.25,base+view*.25))
        a=raster_arrays(positions,np.array(((0,1,2),(3,4,5))),face_colors=((255,0,0),(0,255,0)))
        b=raster_arrays(positions,np.array(((3,4,5),(0,1,2))),face_colors=((0,255,0),(255,0,0)))
        self.assertEqual(a.tobytes(),b.tobytes())
        # The fixed centre ray hits both faces; only the nearer green face wins.
        self.assertTupleEqual(tuple(a[255,255]),(0,255,0))
        self.assertTupleEqual(tuple(a[0,0]),(30,34,39))
        self.assertEqual(int(np.sum(np.all(a==(255,0,0),axis=2))),0)

    def test_raster_work_budget_rejects_before_pixel_grid_allocation(self):
        from model_generator.web.preview_child import raster_arrays
        import numpy as np
        positions=np.array(((-1,-1,0),(1,-1,0),(0,1,0)))
        triangles=np.tile(np.array(((0,1,2),)),(1000,1))
        with self.assertRaises(PreviewError) as raised:
            raster_arrays(positions,triangles)
        self.assertEqual(raised.exception.code,'preview_budget')


class GuardedCpuTests(unittest.TestCase):
    def setUp(self):
        from model_generator.web.preview_runner import run_preview
        self.run=run_preview
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.settings=Settings(self.root,'https://example.invalid',b'0'*32,'postgresql://mg_worker:fake@db/model_generator',db_role='mg_worker')
        self.input=self.root/'preview-input.json'
        write_preview(build_synthetic_demo(),self.input,PreviewLimits())
        self.output=self.root/'render'; self.output.mkdir(mode=0o700)

    def test_real_native_guarded_triangle_and_preflight(self):
        from model_generator.web.preview_runner import preflight_preview,installed_preview_fingerprint
        result=self.run(self.input,self.output,self.settings,lambda:False)
        self.assertEqual((result.version,result.vertex_count,result.triangle_count),('1',3,1))
        self.assertGreater(result.thumbnail_path.stat().st_size,1000)
        self.assertGreater(result.cpu_seconds,0)
        self.assertLess(result.max_rss_kib,1024**2)
        runtime=preflight_preview(self.settings)
        self.assertEqual(runtime['identity'],installed_preview_fingerprint())
        self.assertTrue(runtime['verified'])
        self.assertFalse(list((self.root/'preflight').iterdir()))

    def test_actual_guarded_mirror_instances_and_9511m(self):
        scene=make_scene(); scene['instances'][0]['transform'][0]=-1; scene['instances'][0]['transform'][3]=9511
        second=json.loads(json.dumps(scene['instances'][0])); second['instance_id']='second'; second['element_key']['unique_id']='second'; second['transform'][3]=9513
        scene['instances'].append(second)
        wire=make_package(scene_updates=scene); sha=hashlib.sha256(wire).hexdigest(); package=read_package_bytes(wire)
        document=build_preview(package,inspect_package_scene(package),PreviewLimits())
        write_preview(document,self.input,PreviewLimits())
        result=self.run(self.input,self.output,self.settings,lambda:False)
        self.assertEqual((result.vertex_count,result.triangle_count),(6,2))
        self.assertEqual(result.bounds,document['bounds'])
        self.assertEqual(hashlib.sha256(wire).hexdigest(),sha)
        measured=json.loads(result.measurements_path.read_bytes())
        self.assertEqual(measured['fingerprint'],hashlib.sha256(self.input.read_bytes()).hexdigest())
        self.assertLessEqual(measured['float32MaxErrorMetres'],1e-5)
        image=Image.open(result.thumbnail_path); image.load()
        self.assertGreater(sum(pixel!=(30,34,39) for pixel in image.get_flattened_data()),1000)

    def test_actual_network_credentials_proc_and_escape_denied(self):
        sentinel=self.root.parent/'other-job-secret'; sentinel.write_bytes(b'synthetic sentinel')
        self.addCleanup(sentinel.unlink,missing_ok=True)
        result=self.run(self.input,self.output,self.settings,lambda:False,probe='guard')
        self.assertTrue(result.evidence['ownInput'])
        self.assertTrue(result.evidence['environmentClean'])
        self.assertEqual(set(result.evidence['pathsDenied']),{'secrets','parent','parentfd','otherjob','proc'})
        self.assertIn('socket',result.evidence['syscallsDenied'])
        self.assertIn('setsid',result.evidence['syscallsDenied'])

    def test_live_process_input_rewrite_cannot_redefine_expected_fingerprint(self):
        calls=0; original=self.input.read_bytes()
        def cancelled():
            nonlocal calls
            calls+=1
            if calls==2: self.input.write_bytes(original+b' ')
            return False
        with self.assertRaises(PreviewError) as raised:
            self.run(self.input,self.output,self.settings,cancelled)
        self.assertEqual(raised.exception.code,'preview_unsupported')
        # Actual renderer measured the new valid wire; parent kept the old binding.
        measured=json.loads((self.output/'measurements.json').read_bytes())
        self.assertEqual(measured['fingerprint'],hashlib.sha256(self.input.read_bytes()).hexdigest())
        self.assertNotEqual(measured['fingerprint'],hashlib.sha256(original).hexdigest())

    def test_wrong_engine_version_and_malformed_measurements_are_rejected(self):
        from model_generator.web.blender_runner import check_preview_outputs
        self.run(self.input,self.output,self.settings,lambda:False)
        path=self.output/'measurements.json'; original=path.read_bytes()
        for field,value in [('engine','blender'),('version','4.5.14'),('version',1),('vertexCount',False),('fingerprint','0'*64),('float32MaxErrorMetres',1.1e-5)]:
            with self.subTest(field=field,value=value):
                measured=json.loads(original); measured[field]=value; path.write_text(json.dumps(measured))
                with self.assertRaises(PreviewError): check_preview_outputs(self.input,self.output,self.settings,engine='python-cpu',version='1')
        path.write_bytes(original)

    def test_cpu_wall_memory_and_log_caps_are_actual_process_failures(self):
        for probe in ('cpu','wall','memory','oversized'):
            with self.subTest(probe=probe):
                settings=replace(self.settings,preview_cpu_seconds=1,preview_wall_seconds=3,preview_memory_bytes=128*1024**2)
                with self.assertRaises(PreviewError) as raised:
                    self.run(self.input,self.output,settings,lambda:False,probe=probe)
                self.assertEqual(raised.exception.code,'preview_resource')

    def test_cancel_reaps_stubborn_descendant_and_lease_error_reaps_too(self):
        import ctypes,time
        self.assertEqual(ctypes.CDLL(None).prctl(36,1,0,0,0),0)
        for lease_error in (False,True):
            with self.subTest(lease_error=lease_error):
                start=time.monotonic()
                def cancelled():
                    if time.monotonic()-start<.5: return False
                    marker=self.output/'descendant.pid'
                    if not marker.exists() or not marker.read_text().strip().isdigit():
                        # Leave margin for the runner's .25s polling interval.
                        if time.monotonic()-start>=4.5:
                            self.fail('Native descendant PID was not published within 5s')
                        return False
                    if lease_error: raise RuntimeError('worker_lease_lost')
                    return True
                with self.assertRaises(RuntimeError if lease_error else PreviewError):
                    self.run(self.input,self.output,self.settings,cancelled,probe='descendant')
                self.assertLess(time.monotonic()-start,20)
                pid=int((self.output/'descendant.pid').read_text())
                with self.assertRaises(ProcessLookupError): os.kill(pid,0)
                for path in self.output.iterdir(): path.unlink()


if __name__=='__main__': unittest.main()
