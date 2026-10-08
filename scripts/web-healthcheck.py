"""Read-only bounded JSON health probe; preserve the public Host policy."""
import argparse
import json
import os
import signal
import urllib.request
from urllib.parse import urlsplit

def check(url,host):
    request=urllib.request.Request(url,headers={'Host':host})
    with urllib.request.urlopen(request,timeout=3) as response:
        if response.status!=200 or 'application/json' not in response.headers.get('Content-Type',''):
            raise ValueError('Health response unavailable')
        data=response.read(4097)
        if len(data)>4096 or json.loads(data)!={'status':'ok'}: raise ValueError('Health response invalid')

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--url',required=True);parser.add_argument('--host')
    args=parser.parse_args()
    try:
        host=args.host or urlsplit(os.environ['MG_PUBLIC_ORIGIN']).netloc
        if not host or any(c in host for c in '\r\n'): raise ValueError
        def deadline(signum,frame):raise TimeoutError('Health deadline exceeded')
        signal.signal(signal.SIGALRM,deadline)
        signal.setitimer(signal.ITIMER_REAL,3)
        try:check(args.url,host)
        finally:signal.setitimer(signal.ITIMER_REAL,0)
    except Exception:
        raise SystemExit('Health gate failed.') from None
if __name__=='__main__': main()
