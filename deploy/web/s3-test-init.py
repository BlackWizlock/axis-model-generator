"""Own emulator only, temporary private model bucket, no production bootstrap."""
import os
from pathlib import Path
import boto3
from botocore.config import Config
assert os.environ.get('MG_TEST_MODE')=='1'
assert os.environ['MG_S3_PROFILE']=='ephemeral-emulator'
assert os.environ['MG_S3_ENDPOINT']=='http://minio:9000'
assert os.environ['MG_S3_BUCKET']=='model-generator-test'
s3=boto3.client('s3',endpoint_url=os.environ['MG_S3_ENDPOINT'],region_name=os.environ['MG_S3_REGION'],
    aws_access_key_id=Path(os.environ['MG_S3_ACCESS_KEY_FILE']).read_text().strip(),
    aws_secret_access_key=Path(os.environ['MG_S3_SECRET_KEY_FILE']).read_text().strip(),
    config=Config(request_checksum_calculation='when_required',response_checksum_validation='when_required',proxies={'http':os.environ['MG_S3_PROXY_URL']},s3={'addressing_style':'path'},retries={'total_max_attempts':1}))
try: s3.head_bucket(Bucket='model-generator-test')
except s3.exceptions.ClientError: s3.create_bucket(Bucket='model-generator-test')
# MinIO's emulator does not implement AWS PublicAccessBlock XML. A bucket without
# any policy or ACL grants is private; integration tests assert actual denial.
s3.delete_bucket_policy(Bucket='model-generator-test')
s3.put_bucket_versioning(Bucket='model-generator-test',VersioningConfiguration={'Status':'Suspended'})
print('Own private ephemeral S3 bucket verified.')
