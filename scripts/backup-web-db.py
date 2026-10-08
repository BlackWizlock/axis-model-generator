"""Daily custom-format PostgreSQL dump; upload a hash manifest last."""
import importlib.util
import json
import subprocess
import tempfile
import time
from pathlib import Path
spec=importlib.util.spec_from_file_location('backup_common',Path(__file__).with_name('web-backup-common.py'))
common=importlib.util.module_from_spec(spec);spec.loader.exec_module(common)
def backup():
    client,bucket=common.storage()
    with tempfile.TemporaryDirectory(prefix='mg-backup-') as directory:
        dump=Path(directory)/'database.dump'
        subprocess.run(['pg_dump','--format=custom','--no-owner','--file',str(dump)],env=common.pg_environment(),check=True,timeout=600,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        now=int(time.time());key=f'database/{now}/database.dump'
        manifest={'schema':1,'createdAt':now,'expiresAt':now+7*86400,'sha256':common.digest(dump),'bytes':dump.stat().st_size,'key':key,'format':'postgres-custom','database':'model_generator'}
        client.upload_file(str(dump),bucket,key)
        client.put_object(Bucket=bucket,Key=key+'.json',Body=json.dumps(manifest).encode(),ContentType='application/json')
        return {'status':'uploaded','createdAt':now,'sha256':manifest['sha256']}
if __name__=='__main__':
    try: print(json.dumps(backup()))
    except Exception: raise SystemExit('Own database backup failed.') from None
