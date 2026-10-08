"""Validated private runtime configuration and fixed service budgets."""
from dataclasses import dataclass, field, replace
from pathlib import Path
import ipaddress
import os
from urllib.parse import urlsplit, parse_qsl, unquote


@dataclass(frozen=True)
class StorageSettings:
    endpoint: str
    region: str
    bucket: str
    proxy_url: str
    access_key_file: Path = field(repr=False)
    secret_key_file: Path = field(repr=False)
    profile: str = "production"

    def validate(self):
        import re
        endpoint=urlsplit(self.endpoint); proxy=urlsplit(self.proxy_url)
        if self.profile not in {'production','ephemeral-emulator'}: raise ValueError('Invalid S3 profile')
        emulator=self.profile=='ephemeral-emulator'
        if emulator and os.environ.get('MG_TEST_MODE')!='1': raise ValueError('Emulator requires explicit test runtime')
        if (endpoint.scheme!=('http' if emulator else 'https') or not endpoint.hostname
                or endpoint.username or endpoint.password or endpoint.path or endpoint.query or endpoint.fragment):
            raise ValueError('Invalid exact S3 endpoint')
        if emulator and self.endpoint!='http://minio:9000': raise ValueError('Unexpected ephemeral S3 endpoint')
        if not emulator and self.endpoint!='https://storage.yandexcloud.net': raise ValueError('Unexpected production S3 endpoint')
        if (proxy.scheme!='http' or not proxy.hostname or proxy.username or proxy.password
                or proxy.path or proxy.query or proxy.fragment): raise ValueError('Explicit S3 proxy required')
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]{2,62}',self.bucket) or not self.bucket.startswith('model-generator-'):
            raise ValueError('Own S3 bucket required')
        if not re.fullmatch(r'[a-z0-9-]{1,32}',self.region): raise ValueError('Explicit S3 region required')
        for path in (self.access_key_file,self.secret_key_file):
            if not isinstance(path,Path) or not path.is_absolute() or path.is_symlink() or not path.is_file():
                raise ValueError('Explicit S3 secret file required')

    @classmethod
    def from_env(cls):
        value=cls(os.environ['MG_S3_ENDPOINT'],os.environ['MG_S3_REGION'],os.environ['MG_S3_BUCKET'],
                  os.environ['MG_S3_PROXY_URL'],Path(os.environ['MG_S3_ACCESS_KEY_FILE']),Path(os.environ['MG_S3_SECRET_KEY_FILE']),
                  os.environ.get('MG_S3_PROFILE','production'))
        value.validate(); return value


@dataclass(frozen=True)
class Settings:
    data_root: Path
    public_origin: str
    auth_key: bytes = field(repr=False)
    database_url: str = field(repr=False)
    storage: StorageSettings | None = None
    db_role: str = "mg_api"
    http_inflight: int = 64
    scrypt_active: int = 2
    scrypt_queued: int = 2
    scrypt_acquire_seconds: float = 0.2
    json_idle_seconds: float = 5
    json_wall_seconds: float = 10
    source_url: str = "https://github.com/BlackWizlock/axis-model-generator"
    blender_path: Path | None = None
    trusted_proxy_ips: tuple[str, ...] = ()
    json_max_bytes: int = 16384
    upload_max_bytes: int = 256 * 1024**2
    upload_chunk_bytes: int = 64 * 1024
    upload_part_wall_seconds: int = 120
    upload_finalize_seconds: int = 600
    upload_wall_seconds: int = 600
    upload_idle_seconds: int = 30
    uploads_per_user: int = 1
    uploads_global: int = 2
    storage_per_user_bytes: int = 512 * 1024**2
    storage_global_bytes: int = 10 * 1024**3
    min_free_disk_bytes: int = 2 * 1024**3
    output_reserve_bytes: int = 64 * 1024**2
    jobs_per_user: int = 2
    jobs_global: int = 20
    accepted_per_user_day: int = 10
    accepted_global_day: int = 100
    retention_seconds: int = 86400
    unused_upload_seconds: int = 3600
    orphan_seconds: int = 900
    sweep_seconds: int = 60
    validation_wall_seconds: int = 120
    validation_cpu_seconds: int = 90
    validation_memory_bytes: int = 1536 * 1024**2
    blender_wall_seconds: int = 90
    blender_cpu_seconds: int = 60
    blender_memory_bytes: int = 1024**3
    preview_backend: str = 'python-cpu'
    preview_wall_seconds: int = 90
    preview_cpu_seconds: int = 60
    preview_memory_bytes: int = 1024**3
    job_wall_seconds: int = 240
    preview_max_instances: int = 1000
    preview_max_vertices: int = 300000
    preview_max_triangles: int = 200000
    preview_max_bytes: int = 16 * 1024**2
    report_max_bytes: int = 4 * 1024**2
    report_max_findings: int = 10000

    def validate(self) -> None:
        validate_database_url(self.database_url, self.db_role)
        if self.storage is not None: self.storage.validate()
        origin = urlsplit(self.public_origin)
        if (origin.scheme != 'https' or not origin.hostname or origin.username or origin.password
                or origin.path or origin.query or origin.fragment or origin.netloc != origin.netloc.lower()):
            raise ValueError('MG_PUBLIC_ORIGIN must be one HTTPS origin')
        try:
            origin.port
        except ValueError:
            raise ValueError('Invalid origin port') from None
        if not isinstance(self.auth_key, bytes) or len(self.auth_key) != 32:
            raise ValueError('MG_AUTH_KEY must contain 32 random bytes')
        if not isinstance(self.data_root, Path) or not self.data_root.is_absolute():
            raise ValueError('MG_DATA_ROOT must be absolute and private')
        root = self.data_root.resolve()
        if self.data_root.is_symlink() or self.data_root == Path('/') or any(
                part.lower() in {'static', 'public', 'www', 'html', 'dist', 'htdocs', 'node_modules'}
                for part in root.parts):
            raise ValueError('MG_DATA_ROOT must not be public storage')
        if self.source_url!='https://github.com/BlackWizlock/axis-model-generator':
            raise ValueError('Source URL must be the actual public repository')
        for ip in self.trusted_proxy_ips:
            ipaddress.ip_address(ip)
        if self.blender_path is not None and not self.blender_path.is_absolute():
            raise ValueError('Blender executable must be absolute')
        if self.preview_backend!='python-cpu': raise ValueError('Unsupported explicit preview backend')
        for name, upper in (('http_inflight',64),('scrypt_active',2),('scrypt_queued',2),
                            ('scrypt_acquire_seconds',0.2),('json_idle_seconds',5),('json_wall_seconds',10),
                            ('json_max_bytes',16384),('upload_max_bytes',268435456),('upload_chunk_bytes',65536),
                            ('upload_part_wall_seconds',120),('upload_finalize_seconds',600),('upload_wall_seconds',600),('upload_idle_seconds',30),('uploads_per_user',1),
                            ('uploads_global',2),('storage_per_user_bytes',512*1024**2),
                            ('storage_global_bytes',10*1024**3),('accepted_per_user_day',10),('accepted_global_day',100),
                            ('output_reserve_bytes',64*1024**2),('jobs_per_user',2),('jobs_global',20),
                            ('validation_wall_seconds',120),('validation_cpu_seconds',90),('validation_memory_bytes',1536*1024**2),
                            ('blender_wall_seconds',90),('blender_cpu_seconds',60),('blender_memory_bytes',1024**3),
                            ('preview_wall_seconds',90),('preview_cpu_seconds',60),('preview_memory_bytes',1024**3),
                            ('preview_max_instances',1000),('preview_max_vertices',300000),('preview_max_triangles',200000),('preview_max_bytes',16*1024**2),
                            ('job_wall_seconds',240),('report_max_bytes',4*1024**2),('report_max_findings',10000)):
            value=getattr(self,name)
            if isinstance(value,bool) or not isinstance(value,(int,float)) or not 0 < value <= upper:
                raise ValueError('Invalid service budget: '+name)
            if name.startswith(('blender_','preview_')) and type(value) is not int:
                raise ValueError('Invalid integer preview budget: '+name)

    @classmethod
    def from_env(cls) -> 'Settings':
        try:
            role=os.environ.get('MG_DATABASE_ROLE','mg_api')
            key=b'\x00'*32 if role=='mg_worker' else bytes.fromhex(read_secret_env("MG_AUTH_KEY"))
            settings=cls(Path(os.environ['MG_DATA_ROOT']),os.environ['MG_PUBLIC_ORIGIN'],key,read_secret_env("MG_DATABASE_URL"),
                         storage=StorageSettings.from_env() if any(k.startswith('MG_S3_') for k in os.environ) else None,
                         source_url=os.environ.get('MG_SOURCE_URL', "https://github.com/BlackWizlock/axis-model-generator"),
                         blender_path=Path(os.environ['MG_BLENDER_PATH']) if os.environ.get('MG_BLENDER_PATH') else None,
                         trusted_proxy_ips=tuple(filter(None,os.environ.get('MG_TRUSTED_PROXY_IPS','').split(','))))
            settings=replace(settings,db_role=role)
            settings.validate()
            return settings
        except (KeyError,ValueError,OSError):
            raise ValueError('Missing or invalid MG_* runtime configuration') from None


def read_secret_env(name: str) -> str:
    """A named MG variable or its explicit secret-file mount, never generic fallback."""
    direct, filename = os.environ.get(name), os.environ.get(name + '_FILE')
    if bool(direct) == bool(filename):
        raise ValueError('Missing or ambiguous runtime secret')
    value = direct if direct else Path(filename).read_text(encoding='utf-8').strip()
    if not value: raise ValueError('Missing runtime secret')
    return value


def validate_database_url(value: str, role: str, *, migration: bool = False) -> None:
    try:
        allowed = {'mg_migrator'} if migration else {'mg_api','mg_worker'}
        if role not in allowed or not isinstance(value,str): raise ValueError
        url = urlsplit(value)
        if (url.scheme not in {'postgresql','postgres'} or not url.hostname or url.username != role
                or not url.password or url.path != '/model_generator' or url.fragment
                or ',' in url.netloc or any(char in unquote(url.hostname) for char in '/, \t\n')):
            raise ValueError
        if url.port is not None and not 1 <= url.port <= 65535: raise ValueError
        parameters = parse_qsl(url.query,keep_blank_values=True,strict_parsing=True)
        if len({key for key,_ in parameters}) != len(parameters): raise ValueError
        for key, option in parameters:
            if key == 'options' and option == '-csearch_path=mg,pg_catalog': continue
            if key == 'sslmode' and option in {'verify-full','verify-ca','require','disable'}: continue
            raise ValueError
    except (ValueError,TypeError,AttributeError):
        raise ValueError('Invalid own PostgreSQL configuration') from None
