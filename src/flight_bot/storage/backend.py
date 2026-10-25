"""物件儲存：正式環境用 R2，本機開發用資料夾，兩者路徑結構相同。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol


class Backend(Protocol):
    name: str

    def get(self, key: str) -> bytes | None: ...
    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None: ...
    def copy(self, src: str, dst: str) -> bool: ...
    def list(self, prefix: str) -> list[str]: ...


class LocalBackend:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.name = f"local:{self.root}"

    def _path(self, key: str) -> Path:
        return self.root / key

    def get(self, key):
        p = self._path(key)
        return p.read_bytes() if p.exists() else None

    def put(self, key, data, content_type="application/octet-stream"):
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def copy(self, src, dst):
        data = self.get(src)
        if data is None:
            return False
        self.put(dst, data)
        return True

    def list(self, prefix):
        base = self._path(prefix)
        folder = base if base.is_dir() else base.parent
        if not folder.exists():
            return []
        keys = [str(p.relative_to(self.root)) for p in folder.rglob("*") if p.is_file()]
        return sorted(k for k in keys if k.startswith(prefix))


class R2Backend:
    def __init__(self, account_id: str, access_key: str, secret_key: str, bucket: str):
        import boto3
        from botocore.config import Config as BotoConfig

        self.bucket = bucket
        self.name = f"r2:{bucket}"
        self._s3 = boto3.client(
            "s3",
            endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name="auto",
            config=BotoConfig(retries={"max_attempts": 5, "mode": "standard"}),
        )

    def get(self, key):
        try:
            res = self._s3.get_object(Bucket=self.bucket, Key=key)
        except self._s3.exceptions.NoSuchKey:
            return None
        return res["Body"].read()

    def put(self, key, data, content_type="application/octet-stream"):
        self._s3.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)

    def copy(self, src, dst):
        try:
            self._s3.copy_object(Bucket=self.bucket, Key=dst, CopySource={"Bucket": self.bucket, "Key": src})
        except self._s3.exceptions.ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                return False
            raise
        return True

    def list(self, prefix):
        pages = self._s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix)
        return sorted(o["Key"] for page in pages for o in page.get("Contents", []))


def from_env() -> Backend:
    """有 STORAGE_DIR 就用本機資料夾，否則用 R2（缺少設定時報錯）。"""
    local = os.environ.get("STORAGE_DIR")
    if local:
        return LocalBackend(local)
    keys = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")
    missing = [k for k in keys if not os.environ.get(k)]
    if missing:
        raise RuntimeError(f"缺少環境變數：{', '.join(missing)}（本機開發可改設 STORAGE_DIR）")
    return R2Backend(
        os.environ["R2_ACCOUNT_ID"],
        os.environ["R2_ACCESS_KEY_ID"],
        os.environ["R2_SECRET_ACCESS_KEY"],
        os.environ.get("R2_BUCKET", "flight-bot"),
    )
