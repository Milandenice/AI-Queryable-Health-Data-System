import json
import os
from typing import Iterator

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


def s3_client():
    return boto3.client(
        "s3",
        endpoint_url=os.environ.get("S3_ENDPOINT", "http://localhost:9000"),
        aws_access_key_id=os.environ.get("S3_ACCESS_KEY", ""),
        aws_secret_access_key=os.environ.get("S3_SECRET_KEY", ""),
        region_name="us-east-1",
        config=Config(
            s3={"addressing_style": "path"},
            retries={"max_attempts": 5, "mode": "standard"},
            connect_timeout=3,
            read_timeout=60,
        ),
    )


def object_keys(client, bucket: str, prefix: str = "") -> Iterator[str]:
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for item in page.get("Contents", []):
            yield item["Key"]


def read_json_object(client, bucket: str, key: str):
    try:
        response = client.get_object(Bucket=bucket, Key=key)
    except ClientError as error:
        code = error.response.get("Error", {}).get("Code")
        if code in {"404", "NoSuchKey", "NotFound"}:
            return None
        raise
    return json.loads(response["Body"].read())


def write_json_object(client, bucket: str, key: str, value) -> None:
    client.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(value, sort_keys=True).encode("utf-8"),
        ContentType="application/json",
    )


def download_curated(client, bucket: str, destination: str) -> int:
    from pathlib import Path

    root = Path(destination).resolve()
    root.mkdir(parents=True, exist_ok=True)
    count = 0
    for key in object_keys(client, bucket, "curated/"):
        relative = Path(key).relative_to("curated")
        target = (root / relative).resolve()
        if not target.is_relative_to(root):
            raise ValueError("Object key escapes the curated cache directory")
        target.parent.mkdir(parents=True, exist_ok=True)
        client.download_file(bucket, key, str(target))
        count += 1
    if count == 0:
        raise RuntimeError(f"No curated Parquet objects found in bucket {bucket}")
    return count


def download_computed(client, bucket: str, destination: str) -> int:
    from pathlib import Path

    root = Path(destination).resolve()
    root.mkdir(parents=True, exist_ok=True)
    count = 0
    for key in object_keys(client, bucket, "weekly_features/"):
        relative = Path(key)
        target = (root / relative).resolve()
        if not target.is_relative_to(root):
            raise ValueError("Object key escapes the computed cache directory")
        target.parent.mkdir(parents=True, exist_ok=True)
        client.download_file(bucket, key, str(target))
        count += 1
    if count == 0:
        raise RuntimeError(f"No computed feature objects found in bucket {bucket}")
    return count
