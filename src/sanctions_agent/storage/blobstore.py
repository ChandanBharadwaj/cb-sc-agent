"""Content-addressed, write-once storage for raw downloads (BRD 9.4, NFR-05).

Files are gzip-compressed and addressed by the SHA-256 of the *original* bytes, so the hash in the
evidence tables always refers to exactly what the publisher served.

* ``fs``: ``<root>/ab/cd/<sha256>.gz``, created with O_EXCL and made read-only (0444)
* ``s3``: optional; supports S3 Object Lock (COMPLIANCE mode) for true WORM retention
"""

from __future__ import annotations

import gzip
import hashlib
import os
import shutil
import tempfile
from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO

from sanctions_agent.settings import get_settings


class BlobIntegrityError(Exception):
    pass


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


class BlobStore(ABC):
    @abstractmethod
    def put_file(self, path: Path, sha256: str) -> str:
        """Store ``path`` (uncompressed original) under ``sha256``; idempotent. Returns the blob URI."""

    @abstractmethod
    def exists(self, sha256: str) -> bool: ...

    @abstractmethod
    @contextmanager
    def open(self, uri: str) -> Iterator[IO[bytes]]:
        """Open a stored blob as a decompressed binary stream."""

    def materialize(self, uri: str, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with self.open(uri) as src, open(dest, "wb") as out:
            shutil.copyfileobj(src, out, 1024 * 1024)
        return dest

    def put_bytes(self, data: bytes) -> tuple[str, str]:
        sha = hashlib.sha256(data).hexdigest()
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(data)
            tmp_path = Path(tmp.name)
        try:
            return sha, self.put_file(tmp_path, sha)
        finally:
            tmp_path.unlink(missing_ok=True)


class FsBlobStore(BlobStore):
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, sha256: str) -> Path:
        return self.root / sha256[:2] / sha256[2:4] / f"{sha256}.gz"

    def exists(self, sha256: str) -> bool:
        return self._path(sha256).exists()

    def put_file(self, path: Path, sha256: str) -> str:
        actual = sha256_file(path)
        if actual != sha256:
            raise BlobIntegrityError(f"hash mismatch: expected {sha256}, file has {actual}")
        target = self._path(sha256)
        uri = f"fs://{sha256[:2]}/{sha256[2:4]}/{sha256}.gz"
        if target.exists():
            return uri
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=".tmp-")
        try:
            with (
                os.fdopen(fd, "wb") as raw,
                gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz,
                open(path, "rb") as src,
            ):
                shutil.copyfileobj(src, gz, 1024 * 1024)
            try:
                os.link(tmp_name, target)  # atomic create-if-absent (write-once)
            except FileExistsError:
                return uri
            os.chmod(target, 0o444)
        finally:
            Path(tmp_name).unlink(missing_ok=True)
        return uri

    def _resolve(self, uri: str) -> Path:
        if not uri.startswith("fs://"):
            raise ValueError(f"not an fs blob uri: {uri}")
        rel = uri[len("fs://") :]
        p = (self.root / rel).resolve()
        if self.root.resolve() not in p.parents:
            raise ValueError("blob uri escapes the blob root")
        return p

    @contextmanager
    def open(self, uri: str) -> Iterator[IO[bytes]]:
        with gzip.open(self._resolve(uri), "rb") as fh:
            yield fh  # type: ignore[misc]


class S3BlobStore(BlobStore):  # pragma: no cover - exercised only with real S3/MinIO
    def __init__(self, bucket: str, prefix: str, object_lock_days: int | None) -> None:
        import boto3  # optional dependency: pip install sanctions-agent[s3]

        self.s3 = boto3.client("s3")
        self.bucket = bucket
        self.prefix = prefix
        self.lock_days = object_lock_days

    def _key(self, sha256: str) -> str:
        return f"{self.prefix}{sha256[:2]}/{sha256}.gz"

    def exists(self, sha256: str) -> bool:
        try:
            self.s3.head_object(Bucket=self.bucket, Key=self._key(sha256))
            return True
        except Exception:
            return False

    def put_file(self, path: Path, sha256: str) -> str:
        if sha256_file(path) != sha256:
            raise BlobIntegrityError("hash mismatch")
        key = self._key(sha256)
        uri = f"s3://{self.bucket}/{key}"
        if self.exists(sha256):
            return uri
        with tempfile.NamedTemporaryFile(suffix=".gz") as tmp:
            with gzip.GzipFile(fileobj=tmp, mode="wb", mtime=0) as gz, open(path, "rb") as src:
                shutil.copyfileobj(src, gz, 1024 * 1024)
            tmp.flush()
            tmp.seek(0)
            extra: dict[str, object] = {"Metadata": {"sha256": sha256}}
            if self.lock_days:
                extra["ObjectLockMode"] = "COMPLIANCE"
                extra["ObjectLockRetainUntilDate"] = datetime.now(UTC) + timedelta(days=self.lock_days)
            self.s3.put_object(Bucket=self.bucket, Key=key, Body=tmp, **extra)
        return uri

    @contextmanager
    def open(self, uri: str) -> Iterator[IO[bytes]]:
        _, _, rest = uri.partition("s3://")
        bucket, _, key = rest.partition("/")
        obj = self.s3.get_object(Bucket=bucket, Key=key)
        with gzip.GzipFile(fileobj=obj["Body"]) as fh:
            yield fh  # type: ignore[misc]


_store: BlobStore | None = None


def get_blob_store() -> BlobStore:
    global _store
    s = get_settings()
    if _store is None or (isinstance(_store, FsBlobStore) and _store.root != s.blob_root):
        if s.blob_backend == "s3":
            if not s.s3_bucket:
                raise ValueError("SANCTIONS_S3_BUCKET is required for the s3 blob backend")
            _store = S3BlobStore(s.s3_bucket, s.s3_prefix, s.s3_object_lock_days)
        else:
            _store = FsBlobStore(s.blob_root)
    return _store
