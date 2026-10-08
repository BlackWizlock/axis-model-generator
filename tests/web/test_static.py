"""Public static build contract tested against real app middleware and private scratch."""
from html.parser import HTMLParser
from pathlib import Path
import tempfile
import unittest
from web.helpers import TestClient,make_test_app,settings_for,reset_database

ROOT=Path(__file__).resolve().parents[2]

class Html(HTMLParser):
    def __init__(self): super().__init__(); self.tags=[]; self.text=[]
    def handle_starttag(self,tag,attrs): self.tags.append((tag,dict(attrs)))
    def handle_data(self,data): self.text.append(data)

class StaticTests(unittest.TestCase):
    def test_same_shell_policy_and_support_real_links_and_no_inline_code(self):
        for name in ('index','privacy','support','analytics-consent'):
            with self.subTest(page=name):
                html=Html(); html.feed((ROOT/'web'/f'{name}.html').read_text())
                text=' '.join(html.text)
                self.assertIn('Разработано',text); self.assertIn('Axis Consult',text); self.assertIn('Axis Platform',text)
                self.assertTrue(any(tag=='main' for tag,_ in html.tags));self.assertTrue(any(tag=='footer' for tag,_ in html.tags))
                links=[attr for tag,attr in html.tags if tag=='a']
                for target in ('tel:+74951514135','mailto:info@axisconsult.ru','/privacy','/support','/#workspace','/#checks-section','/#preview-section'):
                    self.assertTrue(any(attr.get('href')==target for attr in links),target)
                for target in ('https://axisconsult.ru','https://axisplatform.ru','https://max.ru/id501208311297_bot'):
                    self.assertTrue(any(attr.get('href')==target and attr.get('target')=='_blank' and 'noopener' in attr.get('rel','') for attr in links))
                self.assertTrue(any(attr.get('id')=='menu-toggle' and attr.get('aria-expanded')=='false' and attr.get('aria-controls')=='site-menu' for _,attr in html.tags))
                self.assertFalse(any(key.startswith('on') for _,attr in html.tags for key in attr))
                self.assertFalse(any(tag=='style' or (tag=='script' and not attr.get('src','').startswith('/src/')) for tag,attr in html.tags))
                if name!='index': self.assertTrue(any(tag=='meta' and attr.get('name')=='robots' and attr.get('content')=='noindex, nofollow' for tag,attr in html.tags))
        css=(ROOT/'web/styles.css').read_text();self.assertIn(':focus-visible',css);self.assertIn('max-width:359px',css);self.assertIn('prefers-reduced-motion',css)

    def test_static_allowlist_csp_and_private_scratch_never_served(self):
        reset_database()
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'private.zip').write_text('private')
            app=make_test_app(settings_for(root))
            with TestClient(app,base_url='https://testserver') as client:
                for url in ('/','/privacy','/support','/analytics-consent','/styles.css','/src/app.js','/src/input-formats.js','/vendor/three.module.js','/assets/axis-sign.png'):
                    response=client.get(url);self.assertEqual(response.status_code,200,(url,response.text[:100]));self.assertIn("script-src 'self'",response.headers['content-security-policy']);self.assertEqual(response.headers['cache-control'],'no-store')
                    if url in ('/','/privacy','/support','/analytics-consent'):
                        for directive in ('script-src','connect-src','img-src'):
                            policy=next(part for part in response.headers['content-security-policy'].split(';') if part.strip().startswith(directive+' '))
                            self.assertIn('https://mc.yandex.ru',policy)
                            self.assertIn('https://mc.yandex.com',policy)
                        self.assertNotIn('unsafe-inline',response.headers['content-security-policy'])
                        self.assertNotIn('unsafe-eval',response.headers['content-security-policy'])
                    else:
                        self.assertNotIn('mc.yandex',response.headers['content-security-policy'])
                robots=client.get('/robots.txt');self.assertEqual(robots.status_code,200)
                self.assertTrue(robots.headers['content-type'].startswith('text/plain'))
                self.assertIn('Sitemap: https://model.axisconsult.ru/sitemap.xml',robots.text)
                self.assertIn('Disallow: /api/',robots.text)
                sitemap=client.get('/sitemap.xml');self.assertEqual(sitemap.status_code,200)
                from xml.etree import ElementTree
                locations=[node.text for node in ElementTree.fromstring(sitemap.text).iter('{http://www.sitemaps.org/schemas/sitemap/0.9}loc')]
                self.assertEqual(locations,['https://model.axisconsult.ru/'])
                self.assertEqual(client.get('/favicon.ico').status_code,200)
                self.assertEqual(client.get('/src/analytics.js').status_code,200)
                self.assertNotIn('mc.yandex',client.get('/health/live').headers['content-security-policy'])
                for url in ('/nonexistent','/private.zip','/data/private.zip','/src/config.py','/package-lock.json','/vendor/package.json','/%2e%2e/private.zip'):
                    self.assertEqual(client.get(url).status_code,404,url)
