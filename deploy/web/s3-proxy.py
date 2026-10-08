"""Bounded HTTPS CONNECT to the sole S3 endpoint, with pinned public DNS IP."""
import ipaddress
import select
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
ALLOW='storage.yandexcloud.net:443'
slots=threading.BoundedSemaphore(16)
class Proxy(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def do_CONNECT(self):
        if self.path!=ALLOW or not slots.acquire(blocking=False):
            self.send_error(403);return
        upstream=None
        try:
            addresses=socket.getaddrinfo('storage.yandexcloud.net',443,type=socket.SOCK_STREAM)
            if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
                self.send_error(403);return
            family,kind,proto,_,address=addresses[0]
            upstream=socket.socket(family,kind,proto);upstream.settimeout(3);upstream.connect(address)
            self.send_response(200,'Connection established');self.end_headers()
            self.connection.setblocking(False);upstream.setblocking(False)
            deadline=time.monotonic()+600
            while time.monotonic()<deadline:
                readable,_,_=select.select([self.connection,upstream],[],[],3)
                if not readable: continue
                for source in readable:
                    target=upstream if source is self.connection else self.connection
                    data=source.recv(65536)
                    if not data:return
                    target.settimeout(3);target.sendall(data);target.setblocking(False)
        except (OSError,ValueError): pass
        finally:
            if upstream:upstream.close()
            self.close_connection=True;slots.release()
    def reject(self):self.send_error(403)
    do_GET=do_PUT=do_POST=do_DELETE=do_HEAD=reject
class Server(ThreadingHTTPServer):
    daemon_threads=True
    def verify_request(self,request,address):
        request.settimeout(3);return True
if __name__=='__main__': Server(('0.0.0.0',8080),Proxy).serve_forever()
