# Preview component licenses and source inventory

The selected Task 4 renderer is `python-cpu`, version `1`. It uses the original
Apache-2.0 sources `src/model_generator/web/preview_child.py`, `preview_runner.py`
and the existing checked preview bridge. Blender is an explicit future backend;
there is no automatic fallback to Blender and no successful Blender render claim.

| Component | Pin and source | License and distribution boundary |
|---|---|---|
| CPU renderer and original checked geometry | This Git repository, paths above | Apache-2.0, root LICENSE and NOTICE |
| NumPy | 2.5.3, [source release](https://github.com/numpy/numpy/tree/v2.5.3) | BSD-3-Clause for NumPy; the wheel retains its complete bundled dependency notices, including OpenBLAS/LAPACK and GCC runtime terms |
| Pillow | 12.3.0, [source release](https://github.com/python-pillow/Pillow/tree/12.3.0) | HPND for Pillow; retain its shipped native codec and bundled library notices |
| Optional original bpy script | `workers/blender_preview.py`, this Git repository | GPL-3.0-or-later; complete license in workers/COPYING. It is separately licensed from the Apache application |
| Optional Blender runtime | [official 4.5.14 Linux x64 archive](https://download.blender.org/release/Blender4.5/blender-4.5.14-linux-x64.tar.xz), [corresponding source](https://download.blender.org/source/blender-4.5.14.tar.xz) | Blender and its bundled components retain their upstream licenses. No binary is included in this repository |

`requirements-web.in` is the version source. `requirements-web.lock` includes
verified PyPI SHA256 hashes for CPython 3.14 manylinux ARM64 and x64 wheels;
installations use `pip --require-hashes`. Exact wheel metadata, including complete
licenses, remains installed in the immutable image. Server release inventory must
preserve those notices and satisfy any bundled source obligations for that image.
This component inventory does not itself certify a future production distribution.

The locked wheels use CPython ABI `cp314-cp314` and platform tags
`manylinux_2_27`/`manylinux_2_28`. Wheel SHA256 values obtained from the official
[NumPy PyPI metadata](https://pypi.org/pypi/numpy/2.5.3/json) and
[Pillow PyPI metadata](https://pypi.org/pypi/Pillow/12.3.0/json):

| Wheel | SHA256 |
|---|---|
| NumPy 2.5.3 aarch64 | `be5a8381859b6da607c84f4f7d6847725f1cf1853ef8a2c9e115b7d58bef47dc` |
| NumPy 2.5.3 x86_64 | `b0521d0f4aebb6e06189451025fa17a913287b13c03d5fe05c017333b654ea5b` |
| Pillow 12.3.0 aarch64 | `e9aeb04d6aef139de265b29683e119b638208f88cf73cdd1658aa07221165321` |
| Pillow 12.3.0 x86_64 | `251bf95b67017e27b13d82f5b326234ca62d70f9cf4c2b9032de2358a3b12c7b` |

The actual installed ARM64 NumPy wheel contains
`numpy-2.5.3.dist-info/licenses/LICENSE.txt` (SHA256
`4860083caa0de2ac3292ca98bd074bd8f45d8b32624e37b1e70a240bff61e488`),
including bundled library notices, plus notices for libdivide, pythoncapi-compat,
Highway, Dragon4, x86-simd-sort, SVML, pocketfft, lapack_lite and random sources.
The installed Pillow wheel contains `pillow-12.3.0.dist-info/licenses/LICENSE`
(SHA256 `dda12a98c1979cf3d94df1cff45d27a4cb3f04a60c76f76902ac54cac03ec0ce`).
Those complete upstream license texts remain in the immutable installed image;
their copyright and source terms are not replaced by the application license.

The actual ARM64 NumPy native payload includes
`numpy.libs/libscipy_openblas64_-f552eb69.so` and
`numpy.libs/libgfortran-e1b7dfc8-d8198c01.so.5.0.0`. Its main notice includes
OpenBLAS, LAPACK, GCC runtime library with runtime exception, and libquadmath
source/license terms. The Pillow native payload includes Xau, libavif, Brotli,
FreeType, HarfBuzz, libjpeg, LCMS2, liblzma, OpenJPEG, libpng, SharpYUV, libtiff,
WebP/demux/mux, XCB and Zstandard libraries. Its complete notice includes AOM,
Brotli, bzip2, dav1d, FreeType, HarfBuzz, LCMS2, libavif, libjpeg, liblzma,
libpng, libtiff, WebP, libyuv, OpenJPEG, Raqm, Tcl/Tk, Xau, XCB, XDMCP, zlib
and Zstandard terms. The wheel sources and the bundled projects' corresponding
source pointers in those notices remain the distribution authority.

`workers/COPYING` is the complete GNU GPL version 3 text from the pinned Debian
base's `/usr/share/common-licenses/GPL-3`, for the optional original bpy script.

Official Blender archive SHA256:
`9ba871ff2ecd36526b77432745980b7e6664ecd0c7ca11c48849073dcfe06da3`.
Its inspected executable SHA256:
`050c02562f81fe80ba616a80198fa02d381e60f8b61b8d39add881f4bca0d7d8`.
The optional launch mode pins that executable, but local emulation failed under
the mandatory guards and 1 GiB address-space cap. An archive/hash inventory is
not a runtime verification. The currently selected native Python engine avoids
that emulated path and never labels its results as Blender.
