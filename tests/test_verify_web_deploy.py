"""The read-only release gate accepts the published page and rejects a foreign or unprotected one."""
import importlib.util
import json
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('verify_web_deploy',ROOT/'scripts/verify-web-deploy.py')
gate=importlib.util.module_from_spec(spec);spec.loader.exec_module(gate)
ORIGIN='https://model.example'


class Response:
    def __init__(self,url,data,status=200,headers=None):
        self.url=url;self.data=data;self.status=status;self.headers=headers or {}
    def __enter__(self): return self
    def __exit__(self,*args): return False
    def read(self,limit): return self.data[:limit]


def site(index=None,headers=None,health=b'{"status":"ok"}',status=200,host=ORIGIN):
    page=(ROOT/'web/index.html').read_bytes() if index is None else index
    headers={'Content-Security-Policy':"default-src 'self'"} if headers is None else headers
    def opener(url,timeout):
        path=url[len(ORIGIN):]
        if path.startswith('/health'): return Response(host+path,health,status)
        if path=='/': return Response(host+path,page,status,headers)
        return Response(host+path,b'{}',status)
    return opener


class VerifyWebDeployTests(unittest.TestCase):
    def run_gate(self,opener,origin=ORIGIN):
        with patch.object(gate.socket,'getaddrinfo',return_value=[]),patch.object(gate.urllib.request,'urlopen',side_effect=opener):
            return gate.verify(origin)

    def test_actual_index_page_passes(self):
        result=self.run_gate(site())
        self.assertEqual(result['status'],'ok');self.assertIn('CSP',result['checks'])

    def test_foreign_page_and_missing_policy_are_rejected(self):
        for opener in (site(index=b'<html><title>Axis</title></html>'),site(headers={})):
            with self.assertRaises(ValueError): self.run_gate(opener)

    def test_health_status_redirect_and_budget_are_rejected(self):
        for opener in (site(health=json.dumps({'status':'down'}).encode()),site(status=503),site(host='https://other.example'),site(index=b'x'*(1024*1024+1))):
            with self.assertRaises(ValueError): self.run_gate(opener)

    def test_only_one_plain_https_origin_is_accepted(self):
        for origin in ('http://model.example','https://model.example/path','https://user'+'@'+'model.example','https://model.example?x=1'):
            with self.assertRaises(ValueError): self.run_gate(site(),origin)


if __name__=='__main__':
    unittest.main()
