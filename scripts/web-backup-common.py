"""Backup transport uses distinct file credentials and a distinct private bucket."""
import hashlib
import os
from pathlib import Path
from urllib.parse import urlsplit,unquote
import boto3
from botocore.config import Config

def pg_environment(*,restore=False):
    role='mg_migrator' if restore else 'mg_backup'
    filename=os.environ['MG_RESTORE_DATABASE_URL_FILE' if restore else 'MG_BACKUP_DATABASE_URL_FILE']
    path=Path(filename)
    if not path.is_absolute() or path.is_symlink():raise ValueError('Own secret file required')
    url=urlsplit(path.read_text().strip())
    if url.scheme!='postgresql' or url.path!='/model_generator' or url.username!=role or not url.password or not url.hostname:raise ValueError('Own backup role required')
    env=os.environ.copy()
    env.update(PGHOST=url.hostname,PGPORT=str(url.port or 5432),PGUSER=role,PGPASSWORD=unquote(url.password),PGDATABASE='model_generator')
    return env

def storage():
    bucket=os.environ['MG_BACKUP_BUCKET']
    if not bucket.startswith('model-generator-backup-') or bucket==os.environ.get('MG_S3_BUCKET'):raise ValueError('Distinct own backup bucket required')
    def secret(name):
        path=Path(os.environ[name+'_FILE'])
        if not path.is_absolute() or path.is_symlink():raise ValueError('Explicit backup file required')
        return path.read_text().strip()
    endpoint='https://storage.yandexcloud.net'
    if os.environ.get('MG_BACKUP_PROFILE')=='ephemeral-emulator':
        if os.environ.get('MG_TEST_MODE')!='1':raise ValueError('Explicit test mode required')
        endpoint='http://minio:9000'
    config=Config(connect_timeout=3,read_timeout=10,retries={'max_attempts':0},s3={'addressing_style':'path'},proxies={'https':os.environ['MG_BACKUP_PROXY_URL'],'http':os.environ['MG_BACKUP_PROXY_URL']})
    return boto3.client('s3',endpoint_url=endpoint,region_name=os.environ['MG_BACKUP_REGION'],aws_access_key_id=secret('MG_BACKUP_ACCESS_KEY'),aws_secret_access_key=secret('MG_BACKUP_SECRET_KEY'),config=config),bucket

def digest(path):
    value=hashlib.sha256()
    with path.open('rb') as stream:
        while chunk:=stream.read(65536):value.update(chunk)
    return value.hexdigest()
