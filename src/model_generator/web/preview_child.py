"""Trusted native CPU rasterizer. Only bounded checked JSON, no external imports."""
import argparse
import hashlib
import io
import json
import math
import os
from pathlib import Path
import sys
import resource
import time
from .preview import (PreviewError,PreviewLimits,decode_preview_json,validate_preview,
                      ROUNDTRIP_TOLERANCE_METRES)

ENGINE='python-cpu'
VERSION='1'
RASTER_WORK_PIXELS=16_000_000
BACKGROUND=(30,34,39)


def raster_arrays(positions,triangles,*,face_colors=None):
    """Orthographic Z-up camera, triangle interiors and order-independent z buffer."""
    import numpy as np
    view=np.array((1.5,-2.0,1.7),dtype=np.float64)
    view/=math.sqrt(float(np.sum(view*view)))
    right=np.cross(np.array((0.0,0.0,1.0)),view)
    right/=math.sqrt(float(np.sum(right*right)))
    up=np.cross(view,right)
    low=np.min(positions,axis=0); high=np.max(positions,axis=0)
    centre=low/2+high/2
    extent=max(float(np.max(high-low)),.01)
    relative=positions-centre
    screen=np.column_stack((relative@right,relative@up))*(512/(extent*2.5))
    screen[:,0]+=255.5; screen[:,1]=255.5-screen[:,1]
    depth=relative@view
    projected=screen[triangles]
    minimum=np.maximum(np.ceil(np.min(projected,axis=1)).astype(np.int32),0)
    maximum=np.minimum(np.floor(np.max(projected,axis=1)).astype(np.int32),511)
    dimensions=np.maximum(maximum.astype(np.int64)-minimum+1,0)
    work=int(np.sum(dimensions[:,0]*dimensions[:,1],dtype=np.int64))
    if work>RASTER_WORK_PIXELS:
        raise PreviewError('preview_budget','CPU raster workload exceeds its fixed pixel budget')
    # The complete workload cap precedes any per-face grid or framebuffer work.
    image=np.empty((512,512,3),dtype=np.uint8); image[:]=BACKGROUND
    zbuffer=np.full((512,512),-np.inf,dtype=np.float64)
    light=np.array((1.,-1.,3.)); light/=math.sqrt(float(np.sum(light*light)))
    for n,indices in enumerate(triangles):
        x0,y0=minimum[n]; x1,y1=maximum[n]
        if x1<x0 or y1<y0: continue
        a,b,c=projected[n]
        denominator=(b[1]-c[1])*(a[0]-c[0])+(c[0]-b[0])*(a[1]-c[1])
        if abs(float(denominator))<1e-12: continue
        x,y=np.meshgrid(np.arange(x0,x1+1,dtype=np.float64),np.arange(y0,y1+1,dtype=np.float64))
        wa=((b[1]-c[1])*(x-c[0])+(c[0]-b[0])*(y-c[1]))/denominator
        wb=((c[1]-a[1])*(x-c[0])+(a[0]-c[0])*(y-c[1]))/denominator
        wc=1-wa-wb
        z=wa*depth[indices[0]]+wb*depth[indices[1]]+wc*depth[indices[2]]
        tile=zbuffer[y0:y1+1,x0:x1+1]
        visible=(wa>=-1e-12)&(wb>=-1e-12)&(wc>=-1e-12)&(z>tile)
        if face_colors is None:
            normal=np.cross(positions[indices[1]]-positions[indices[0]],positions[indices[2]]-positions[indices[0]])
            magnitude=math.sqrt(float(np.sum(normal*normal)))
            shade=.2+.8*abs(float(np.sum(normal*light)))/magnitude if magnitude else .2
            color=tuple(int(channel*shade) for channel in (140,153,166))
        else: color=face_colors[n]  # Internal pixel/occlusion tests, never a wire option.
        image[y0:y1+1,x0:x1+1][visible]=color
        tile[visible]=z[visible]
    return image


def raster_mesh(document):
    """Measure the actual Float32 mesh arrays, rasterize their faces and encode PNG."""
    validate_preview(document,PreviewLimits())
    import numpy as np
    from PIL import Image
    source=np.array(document['positions'],dtype=np.float64).reshape((-1,3))
    mesh=source.astype(np.float32).astype(np.float64)
    triangles=np.array(document['indices'],dtype=np.uint32).reshape((-1,3))
    error=float(np.max(np.abs(mesh-source)))
    if not np.all(np.isfinite(mesh)) or error>ROUNDTRIP_TOLERANCE_METRES:
        raise PreviewError('preview_roundtrip_error','CPU mesh exceeds fixed Float32 tolerance')
    result={'schemaVersion':1,'engine':ENGINE,'version':VERSION,'vertexCount':int(mesh.shape[0]),
            'triangleCount':int(triangles.shape[0]),'bounds':{'min':np.min(mesh,axis=0).tolist(),'max':np.max(mesh,axis=0).tolist()},
            'float32MaxErrorMetres':error}
    pixels=raster_arrays(mesh,triangles)
    stream=io.BytesIO(); Image.fromarray(pixels).save(stream,format='PNG',compress_level=6)
    image=stream.getvalue()
    if len(image)>4*1024**2: raise PreviewError('preview_budget','CPU PNG exceeds byte budget')
    return image,result


def _probe(mode,scratch,input_path):
    """Fixed synthetic acceptance probes, disabled in every production invocation."""
    if os.environ.get('MG_TEST_MODE')!='1': raise ValueError('Test-only probe')
    if mode=='cpu':
        while True: pass
    if mode=='wall': __import__('time').sleep(300)
    if mode=='memory': data=bytearray(2*1024**3)
    if mode=='oversized':
        sys.stdout.write('x'*70000); sys.stdout.flush(); __import__('time').sleep(300)
    if mode=='descendant':
        import signal,time
        child=os.fork()
        if child: (scratch/'descendant.pid').write_text(str(child))
        else:
            signal.signal(signal.SIGTERM,signal.SIG_IGN)
            while True: time.sleep(.1)
        signal.signal(signal.SIGTERM,signal.SIG_IGN)
        while True: time.sleep(.1)
    if mode=='guard':
        import ctypes,errno
        from .process_guard import DENIED,BLENDER_DENIED
        library=ctypes.CDLL('libseccomp.so.2'); library.seccomp_syscall_resolve_name.restype=ctypes.c_int
        c=ctypes.CDLL(None,use_errno=True); c.syscall.restype=ctypes.c_long
        denied=[]
        for name in (*DENIED,*BLENDER_DENIED):
            number=library.seccomp_syscall_resolve_name(name.encode()); ctypes.set_errno(0)
            if c.syscall(number,0,0,0,0,0,0)!=-1 or ctypes.get_errno()!=errno.EACCES: raise ValueError('Denied syscall available')
            denied.append(name)
        paths=[]
        for label,path in [('secrets',Path('/run/secrets/worker_dsn')),('parent',Path(f'/proc/{os.getppid()}/environ')),('parentfd',Path(f'/proc/{os.getppid()}/fd/0')),('otherjob',input_path.parent.parent/'other-job-secret'),('proc',Path('/proc/self/status'))]:
            try: path.open('rb')
            except PermissionError: paths.append(label)
            else: raise ValueError('Forbidden source available')
        return {'syscallsDenied':denied,'pathsDenied':paths,'ownInput':input_path.is_file(),
                'environmentClean':not any(key.startswith(('MG_AUTH','MG_S3','MG_DATABASE','AWS_')) for key in os.environ)}
    return None


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--input',type=Path,required=True)
    parser.add_argument('--output-dir',type=Path,required=True)
    parser.add_argument('--probe',choices=('guard','cpu','wall','memory','descendant','oversized'))
    args=parser.parse_args()
    try:
        if not args.output_dir.is_dir() or any(args.output_dir.iterdir()): raise ValueError('Unsafe staging')
        with args.input.open('rb') as stream: wire=stream.read(PreviewLimits().wire_bytes+1)
        document=decode_preview_json(wire,PreviewLimits())
        evidence=_probe(args.probe,args.output_dir,args.input) if args.probe else None
        started=time.monotonic()
        png,result=raster_mesh(document)
        result['fingerprint']=hashlib.sha256(wire).hexdigest()
        measurements=json.dumps(result,allow_nan=False,sort_keys=True,separators=(',',':')).encode()
        if len(measurements)>65536: raise ValueError('Measurements budget')
        for name,data in [('thumbnail.png',png),('measurements.json',measurements)]:
            with (args.output_dir/name).open('xb') as stream:
                stream.write(data); stream.flush(); os.fsync(stream.fileno())
        usage=resource.getrusage(resource.RUSAGE_SELF)
        print(json.dumps({'probe':evidence,'telemetry':{'wallSeconds':time.monotonic()-started,
                         'cpuSeconds':usage.ru_utime+usage.ru_stime,'maxRssKiB':usage.ru_maxrss}},sort_keys=True),flush=True)
    except PreviewError as error:
        # Fixed protocol code only, never an exception body or input value.
        print(json.dumps({'failureCode':error.code}),flush=True)
        raise SystemExit(71) from None
    except (OSError,ValueError,MemoryError): raise SystemExit(70) from None


if __name__=='__main__': main()
