"""Read-only HTTPS release checks, with normal certificate verification."""
import argparse
import json
import socket
import urllib.request
from urllib.parse import urlsplit

def verify(origin):
    parsed=urlsplit(origin)
    if parsed.scheme!='https' or not parsed.hostname or parsed.path or parsed.query or parsed.fragment or parsed.username:
        raise ValueError('One HTTPS origin required')
    socket.getaddrinfo(parsed.hostname,parsed.port or 443)
    for path in ('/health/live','/health/ready','/','/api/config'):
        with urllib.request.urlopen(origin+path,timeout=3) as response:
            if response.status!=200 or urlsplit(response.url).netloc!=parsed.netloc: raise ValueError('Unexpected HTTP response')
            data=response.read(1024*1024+1)
            if len(data)>1024*1024: raise ValueError('Response budget exceeded')
            if path.startswith('/health') and json.loads(data)!={'status':'ok'}: raise ValueError('Health unavailable')
            if path=='/' and (not response.headers.get('Content-Security-Policy') or 'Аксис Модель'.encode() not in data): raise ValueError('Static/CSP unavailable')
    return {'status':'ok','checks':['DNS','verified HTTPS','live','ready','static','CSP','config']}
if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--origin',required=True);args=parser.parse_args()
    try: print(json.dumps(verify(args.origin)))
    except Exception: raise SystemExit('Read-only deployment gate failed.') from None
