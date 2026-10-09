"""Original schematic assets are served explicitly; private SVGs remain denied."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse
from model_generator.web.static import install_static
from model_generator.web.security import ApiError

ROOT = Path(__file__).resolve().parents[2]

class StaticIllustrationsTests(unittest.TestCase):
    def test_public_illustrations_have_svg_media_type_and_private_files_are_denied(self):
        app = FastAPI()
        @app.exception_handler(ApiError)
        async def unavailable(request, error):
            return JSONResponse({'error': 'not_found'}, status_code=404)
        install_static(app)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'assets').mkdir()
            for name in ('revit-model.svg', 'dwg-plan.svg', 'npm-result.svg'):
                (root / 'assets' / name).write_bytes((ROOT / 'web/assets' / name).read_bytes())
            (root / 'assets/private.svg').write_text('<svg>private</svg>')
            with patch('model_generator.web.static.ROOT', root), TestClient(app) as client:
                for name in ('revit-model.svg', 'dwg-plan.svg', 'npm-result.svg'):
                    response = client.get('/assets/' + name)
                    self.assertEqual(response.status_code, 200, name)
                    self.assertEqual(response.headers['content-type'], 'image/svg+xml')
                    self.assertEqual(response.content, (ROOT / 'web/assets' / name).read_bytes())
                for url in ('/assets/private.svg', '/assets/missing.svg', '/assets/../private.svg'):
                    self.assertEqual(client.get(url).status_code, 404, url)
