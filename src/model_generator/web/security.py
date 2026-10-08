"""Receive limits, exact origin/host and safe ASGI responses."""
import asyncio
import ipaddress
import json
import logging
import math
import secrets
import time
from urllib.parse import urlsplit
from starlette.requests import Request, ClientDisconnect
from starlette.responses import JSONResponse
from http.cookies import SimpleCookie

logger=logging.getLogger('model_generator.web')
SECURITY_HEADERS={
    'Cache-Control':'no-store', 'X-Content-Type-Options':'nosniff',
    'Referrer-Policy':'no-referrer', 'X-Frame-Options':'DENY',
    'Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; worker-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
    'Permissions-Policy':'camera=(), microphone=(), geolocation=()',
}

PUBLIC_HTML_CSP=SECURITY_HEADERS['Content-Security-Policy'].replace("script-src 'self'","script-src 'self' https://mc.yandex.ru https://mc.yandex.com https://yastatic.net").replace("connect-src 'self'","connect-src 'self' https://mc.yandex.ru https://mc.yandex.com").replace("img-src 'self' data:","img-src 'self' data: https://mc.yandex.ru https://mc.yandex.com")


class ApiError(Exception):
    def __init__(self,code: str,message: str,status: int):
        super().__init__(code)
        self.code,self.message,self.status=code,message,status
        self.retry_after: int | None=None


def error_response(error: ApiError, request_id: str):
    from .diagnostic_catalog import error_help
    headers={'Retry-After':str(error.retry_after or 1)} if error.status in(429,503) else {}
    return JSONResponse({'error':{'code':error.code,'message':error.message,'request_id':request_id,**error_help(error.code,error.status)}},status_code=error.status,headers=headers)


def exact_origin(request: Request) -> None:
    values=request.headers.getlist('origin')
    if values != [request.app.state.settings.public_origin]:
        raise ApiError('origin_forbidden','Request origin is not allowed.',403)


def client_ip(request: Request,settings) -> str:
    peer=request.client.host if request.client else 'unknown'
    if peer in settings.trusted_proxy_ips:
        values=request.headers.getlist('x-forwarded-for')
        # Only the single address inserted by our configured proxy is accepted.
        if len(values)==1 and ',' not in values[0]:
            try: return str(ipaddress.ip_address(values[0].strip()))
            except ValueError: pass
    return peer


def _unique_pairs(pairs):
    result={}
    for key,value in pairs:
        if key in result: raise ValueError('duplicate key')
        result[key]=value
    return result


def _reject_constant(value):
    raise ValueError('non-finite value')


def _check_depth(value,depth=0):
    if isinstance(value,(dict,list)):
        if depth>=16: raise ValueError('depth exceeded')
        children=value.values() if isinstance(value,dict) else value
        for child in children: _check_depth(child,depth+1)
    elif isinstance(value,float) and not math.isfinite(value):
        raise ValueError('non-finite value')


async def bounded_json(request: Request,max_bytes: int=16384,idle_seconds: float=5,wall_seconds: float=10) -> dict:
    content_types=request.headers.getlist('content-type')
    if len(content_types)!=1 or content_types[0].split(';',1)[0].strip().lower()!='application/json':
        raise ApiError('unsupported_media_type','Expected application/json.',415)
    start=time.monotonic(); buffer=bytearray()
    try:
        while True:
            remaining=wall_seconds-(time.monotonic()-start)
            if remaining<=0: raise TimeoutError
            message=await asyncio.wait_for(request.receive(),timeout=min(idle_seconds,remaining))
            if message['type']=='http.disconnect': raise ClientDisconnect
            if message['type']!='http.request': raise ValueError('invalid receive')
            chunk=message.get('body',b'')
            if len(buffer)+len(chunk)>max_bytes:
                raise ApiError('body_too_large','JSON request is too large.',413)
            buffer.extend(chunk)
            if not message.get('more_body',False): break
        document=json.loads(buffer.decode('utf-8'),object_pairs_hook=_unique_pairs,parse_constant=_reject_constant)
        _check_depth(document)
        if not isinstance(document,dict): raise ValueError('expected object')
        return document
    except TimeoutError:
        raise ApiError('request_timeout','Request timed out.',408) from None
    except ClientDisconnect:
        raise ApiError('request_disconnected','Request disconnected.',400) from None
    except (ValueError,RecursionError,UnicodeError):
        raise ApiError('invalid_json','JSON request is invalid.',400) from None


class SecurityMiddleware:
    def __init__(self,app,settings):
        self.app,self.settings=app,settings
        self.inflight=0

    async def __call__(self,scope,receive,send):
        if scope['type']!='http': return await self.app(scope,receive,send)
        request_id=secrets.token_hex(16); scope.setdefault('state',{})['request_id']=request_id
        start=time.monotonic(); started=False; status=500; admitted=False
        async def safe_send(message):
            nonlocal started,status
            if message['type']=='http.response.start':
                started=True; status=message['status']
                owned={name.lower().encode() for name in SECURITY_HEADERS}
                headers=[(key,value) for key,value in message.get('headers',[]) if key.lower() not in owned]
                response_headers=dict(SECURITY_HEADERS)
                if status==200 and scope.get('path') in ('/','/privacy','/support','/analytics-consent') and any(key.lower()==b'content-type' and value.split(b';',1)[0]==b'text/html' for key,value in headers):
                    response_headers['Content-Security-Policy']=PUBLIC_HTML_CSP
                headers.extend((key.lower().encode(),value.encode()) for key,value in response_headers.items())
                headers.append((b'x-request-id',request_id.encode()))
                renewal=scope['state'].get('guest_cookie')
                if renewal and status<400:
                    cookie=SimpleCookie(); cookie['__Host-mg_session']=renewal[0]
                    value=cookie['__Host-mg_session']; value['path']='/'; value['secure']=True; value['httponly']=True; value['samesite']='lax'; value['max-age']=str(renewal[1])
                    headers=[(key,val) for key,val in headers if key.lower()!=b'set-cookie']
                    headers.append((b'set-cookie',value.OutputString().encode('latin-1')))
                message={**message,'headers':headers}
            await send(message)
        received=0
        content_type=[value for key,value in scope.get('headers',[]) if key.lower()==b'content-type']
        is_json=len(content_type)==1 and content_type[0].split(b';',1)[0].strip().lower()==b'application/json'
        async def counted_receive():
            nonlocal received
            message=await receive()
            if is_json and message['type']=='http.request':
                received+=len(message.get('body',b''))
                if received>self.settings.json_max_bytes:
                    raise ApiError('body_too_large','JSON request is too large.',413)
            return message
        try:
            hosts=[value.decode('latin-1') for key,value in scope.get('headers',[]) if key.lower()==b'host']
            origin=urlsplit(self.settings.public_origin)
            allowed={origin.netloc}
            if origin.port in(None,443): allowed.add(origin.hostname)
            if len(hosts)!=1 or hosts[0] not in allowed:
                raise ApiError('host_forbidden','Request host is not allowed.',403)
            if self.inflight>=self.settings.http_inflight:
                raise ApiError('service_busy','Service is busy. Try again shortly.',503)
            self.inflight+=1; admitted=True
            await self.app(scope,counted_receive,safe_send)
        except ApiError as error:
            from .journal import emit
            journal=getattr(getattr(scope.get('app'),'state',None),'journal',None)
            emit(journal,'stream' if started else 'request',error.code,request_id,error)
            if not started: await error_response(error,request_id)(scope,receive,safe_send)
        except Exception as error:
            from .journal import emit
            journal=getattr(getattr(scope.get('app'),'state',None),'journal',None)
            emit(journal,'stream' if started else 'request','internal_error',request_id,error)
            if not started: await error_response(ApiError('internal_error','Request could not be completed.',500),request_id)(scope,receive,safe_send)
        finally:
            if admitted: self.inflight-=1
            route=scope.get('route'); template=getattr(route,'path','unmatched')
            logger.info('request_id=%s route=%s status=%d duration_ms=%d',request_id,template,status,int((time.monotonic()-start)*1000))
