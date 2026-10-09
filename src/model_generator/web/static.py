"""Explicit public build allowlist, unrelated to private scratch and object storage."""
from pathlib import Path
from fastapi import Request
from starlette.responses import FileResponse
from .security import ApiError

ROOT = Path(__file__).resolve().parents[3] / 'web' / 'dist'
PAGES = {'': 'index.html', 'privacy': 'privacy.html', 'support': 'support.html', 'analytics-consent': 'analytics-consent.html'}
PUBLIC = frozenset({
    'styles.css', 'THIRD-PARTY-NOTICES.md', 'robots.txt', 'sitemap.xml', 'favicon.ico',
    *('src/' + name + '.js' for name in ('api','auth','input-formats','upload','sha256','hash-worker','jobs','preview','app','checks','shell','analytics')),
    *('vendor/' + name for name in ('three.module.js','three.core.js','OrbitControls.js','THREE-LICENSE.txt')),
    *('assets/' + name for name in ('axis-sign.png','max-icon.png','inventory.json','revit-model.svg','dwg-plan.svg','npm-result.svg')),
    *('assets/fonts/' + name for name in ('manrope-latin-wght-normal.woff2','manrope-cyrillic-wght-normal.woff2',
      'geologica-latin-700-normal.woff2','geologica-cyrillic-700-normal.woff2','geologica-latin-800-normal.woff2',
      'geologica-cyrillic-800-normal.woff2','MANROPE-OFL.txt','GEOLOGICA-OFL.txt')),
})


def install_static(app):
    @app.get('/{public_path:path}', include_in_schema=False)
    async def public_file(request: Request, public_path: str):
        name = PAGES.get(public_path, public_path if public_path in PUBLIC else None)
        if name is None: raise ApiError('not_found', 'Resource is unavailable.', 404)
        path = ROOT / name
        if not path.is_file() or path.is_symlink() or path.resolve().parent not in {ROOT.resolve(), (ROOT/'src').resolve(), (ROOT/'vendor').resolve(), (ROOT/'assets').resolve(), (ROOT/'assets/fonts').resolve()}:
            raise ApiError('not_found', 'Resource is unavailable.', 404)
        media = {'.js': 'text/javascript', '.txt': 'text/plain', '.xml': 'application/xml', '.ico': 'image/vnd.microsoft.icon'}.get(path.suffix)
        return FileResponse(path,media_type=media,headers={'Content-Disposition':'inline'})
