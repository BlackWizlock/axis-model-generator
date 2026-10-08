"""Executed inside the actual API image by the host-owned deploy runner."""
import base64
import hashlib
import http.cookies
import json
import sys
import time
import urllib.error
import urllib.request

ORIGIN = 'https://testserver'
BASE = 'http://localhost:8000'

def request(path, *, method='GET', data=None, cookie=None, csrf=None, raw=False):
    headers = {'Host': 'testserver', 'Origin': ORIGIN}
    if cookie:
        headers['Cookie'] = cookie
    if csrf:
        headers['X-CSRF-Token'] = csrf
    if data is not None:
        if raw:
            headers['Content-Type'] = 'application/octet-stream'
        else:
            data = json.dumps(data).encode()
            headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            wire = response.read()
            return response.status, response.headers, wire
    except urllib.error.HTTPError as error:
        return error.code, error.headers, error.read()

def guest():
    status, headers, wire = request('/api/auth/guest', method='POST')
    assert status == 200, (status, wire)
    cookies = http.cookies.SimpleCookie(headers['Set-Cookie'])
    assert cookies and all(item['max-age'] == '172800' for item in cookies.values())
    return '; '.join(f'{key}={item.value}' for key, item in cookies.items()), json.loads(wire)

def verify(proof):
    cookie = proof['ownerCookie']
    path = '/api/jobs/' + proof['jobId']
    status, _, wire = request(path, cookie=cookie)
    assert status == 200 and json.loads(wire)['state'] == 'completed', (status, wire)
    assert request(path, cookie=proof['neighbourCookie'])[0] == 404
    assert request(path)[0] == 401
    for artifact in proof['artifacts']:
        url = path + '/artifacts/' + artifact['id']
        status, _, wire = request(url, cookie=cookie)
        assert status == 200 and hashlib.sha256(wire).hexdigest() == artifact['sha256']
        assert request(url, cookie=proof['neighbourCookie'])[0] == 404
        assert request(url)[0] == 401

payload = json.load(sys.stdin)
if payload['mode'] == 'create':
    cookie, owner = guest()
    neighbour, _ = guest()
    data = base64.b64decode(payload['fixture'])
    status, _, wire = request('/api/uploads', method='POST', cookie=cookie, csrf=owner['csrfToken'], data={
        'kind': 'portable-package', 'displayName': 'synthetic-production.zip', 'bytes': len(data),
        'sha256': hashlib.sha256(data).hexdigest()})
    assert status == 201, (status, wire)
    upload = json.loads(wire)['id']
    assert request('/api/uploads/' + upload + '/content', method='PUT', cookie=cookie,
                   csrf=owner['csrfToken'], data=data, raw=True)[0] == 200
    status, _, wire = request('/api/jobs', method='POST', cookie=cookie, csrf=owner['csrfToken'], data={
        'uploadId': upload, 'region': 'moscow', 'procedure': 'diagnostic', 'submissionDate': '2026-10-07'})
    assert status == 201, (status, wire)
    job = json.loads(wire)
    deadline = time.monotonic() + 90
    while job['state'] not in ('completed', 'failed', 'cancelled') and time.monotonic() < deadline:
        time.sleep(.5)
        status, _, wire = request('/api/jobs/' + job['id'], cookie=cookie)
        assert status == 200, (status, wire)
        job = json.loads(wire)
    assert job['state'] == 'completed', job
    assert {'report', 'preview', 'thumbnail'} <= {item['kind'] for item in job['artifacts']}, job
    proof = {'ownerCookie': cookie, 'neighbourCookie': neighbour,
             'jobId': job['id'], 'artifacts': job['artifacts']}
    verify(proof)
    # Captured privately by the runner; never print cookies in the acceptance log.
    print(json.dumps(proof))
else:
    verify(payload['proof'])
    print('Restored private HTTP and artifact SHA256 verification passed')
