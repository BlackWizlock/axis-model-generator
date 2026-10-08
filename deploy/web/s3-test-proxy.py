"""Ephemeral test-only exact-target HTTP transport. No CONNECT or Internet."""
import http.client
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.parse import urlsplit
class Proxy(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    def log_message(self,*args): pass
    def do_CONNECT(self): self.send_error(403)
    def forward(self):
        target=urlsplit(self.path)
        if target.scheme!='http' or target.hostname!='minio' or target.port!=9000 or target.username or target.password:
            self.send_error(403); return
        if self.headers.get('Transfer-Encoding') or not self.headers.get('Content-Length','0').isdigit():
            self.send_error(400); return
        con=http.client.HTTPConnection('minio',9000,timeout=5)
        try:
            con.putrequest(self.command,target.path+('?' +target.query if target.query else ''),skip_host=True,skip_accept_encoding=True)
            for name,value in self.headers.items():
                if name.lower() not in {'proxy-connection','connection','expect'}: con.putheader(name,value)
            con.endheaders()
            remaining=int(self.headers.get('Content-Length','0'))
            while remaining:
                chunk=self.rfile.read(min(65536,remaining))
                if not chunk: raise OSError
                con.send(chunk); remaining-=len(chunk)
            response=con.getresponse(); self.send_response(response.status)
            for name,value in response.getheaders():
                if name.lower() not in {'connection','transfer-encoding'}: self.send_header(name,value)
            self.send_header('Connection','close'); self.end_headers()
            while chunk:=response.read(65536): self.wfile.write(chunk)
        except (OSError,http.client.HTTPException):
            self.close_connection=True
        finally: con.close(); self.close_connection=True
    do_GET=do_HEAD=do_PUT=do_POST=do_DELETE=forward
ThreadingHTTPServer(('0.0.0.0',8080),Proxy).serve_forever()
