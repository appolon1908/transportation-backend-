"""AWS S3 conditional upload and version-bound reads; ClamAV over a local socket."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import re
import struct
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from typing import Literal

from fastapi import HTTPException
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class StorageSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DOCUMENT_", extra="ignore")
    storage_enabled: bool = False
    s3_bucket: str = ""
    s3_region: str = ""
    kms_key_arn: str = ""
    clamav_socket: str = ""
    max_bytes: int = Field(default=10_485_760, ge=1, le=10_485_760)
    upload_ttl_seconds: int = Field(default=300, ge=30, le=900)
    retention_days: int = Field(default=30, ge=1, le=3650)
    scan_timeout_seconds: float = Field(default=30, gt=0, le=120)

    @model_validator(mode="after")
    def validate_enabled(self):
        if self.storage_enabled:
            if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", self.s3_bucket):
                raise ValueError("DOCUMENT_S3_BUCKET is required")
            if not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-\d", self.s3_region):
                raise ValueError("DOCUMENT_S3_REGION is required")
            if not re.fullmatch(r"arn:aws(?:-us-gov|-cn)?:kms:[a-z0-9-]+:\d{12}:key/[a-zA-Z0-9-]+", self.kms_key_arn):
                raise ValueError("DOCUMENT_KMS_KEY_ARN must be an explicit key ARN, not an alias")
            if not self.clamav_socket.startswith("/") or "\x00" in self.clamav_socket:
                raise ValueError("DOCUMENT_CLAMAV_SOCKET must be an absolute local socket path")
        return self


def unavailable(code="STORAGE_UNAVAILABLE"):
    return HTTPException(503, {"code": code, "message": "Document storage or scanning is unavailable."})


@dataclass(frozen=True)
class ObjectSpec:
    key: str
    tenant_id: str
    document_id: str
    content_type: str
    size: int
    checksum: str
    retain_until: datetime


def validate_content(data: bytes, spec: ObjectSpec) -> None:
    """Verify byte length, SHA-256 and allowed container signatures, not sanitization."""
    if len(data) != spec.size or hashlib.sha256(data).hexdigest() != spec.checksum:
        raise ValueError("CHECKSUM_MISMATCH")
    valid = {
        "application/pdf": data.startswith(b"%PDF-") and data.rstrip().endswith(b"%%EOF"),
        "image/png": data.startswith(b"\x89PNG\r\n\x1a\n") and data.endswith(b"\x00\x00\x00\x00IEND\xaeB`\x82"),
        "image/jpeg": data.startswith(b"\xff\xd8\xff") and data.endswith(b"\xff\xd9"),
    }
    if not valid.get(spec.content_type, False):
        raise ValueError("CONTENT_TYPE_MISMATCH")


class S3Storage:
    def __init__(self, settings: StorageSettings, client=None):
        self.settings = settings
        if client is None:
            import boto3
            from botocore.config import Config
            client = boto3.client("s3", region_name=settings.s3_region, config=Config(
                signature_version="s3v4", connect_timeout=5, read_timeout=10,
                retries={"max_attempts": 2, "mode": "standard"},
                ignore_configured_endpoint_urls=True,
            ))
        self.client = client

    def verify_bucket(self) -> None:
        bucket = {"Bucket": self.settings.s3_bucket}
        if self.client.get_bucket_versioning(**bucket).get("Status") != "Enabled":
            raise ValueError("versioning_required")
        blocks = self.client.get_public_access_block(**bucket)["PublicAccessBlockConfiguration"]
        if not all(blocks.get(k) is True for k in (
            "BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets",
        )):
            raise ValueError("private_bucket_required")
        if self.client.get_object_lock_configuration(**bucket)["ObjectLockConfiguration"].get("ObjectLockEnabled") != "Enabled":
            raise ValueError("retention_lock_required")

    def upload(self, spec: ObjectSpec, ttl: int) -> dict:
        self.verify_bucket()
        checksum = base64.b64encode(bytes.fromhex(spec.checksum)).decode()
        params = {
            "Bucket": self.settings.s3_bucket, "Key": spec.key,
            "ContentType": spec.content_type, "ContentLength": spec.size,
            "ChecksumSHA256": checksum, "IfNoneMatch": "*",
            "ServerSideEncryption": "aws:kms", "SSEKMSKeyId": self.settings.kms_key_arn,
            "ObjectLockMode": "COMPLIANCE", "ObjectLockRetainUntilDate": spec.retain_until,
            "Metadata": {"tenant-id": spec.tenant_id, "document-id": spec.document_id},
        }
        url = self.client.generate_presigned_url("put_object", Params=params, ExpiresIn=ttl, HttpMethod="PUT")
        return {"method": "PUT", "url": url, "headers": {
            "Content-Type": spec.content_type, "Content-Length": str(spec.size),
            "x-amz-checksum-sha256": checksum, "If-None-Match": "*",
            "x-amz-server-side-encryption": "aws:kms",
            "x-amz-server-side-encryption-aws-kms-key-id": self.settings.kms_key_arn,
            "x-amz-object-lock-mode": "COMPLIANCE",
            "x-amz-object-lock-retain-until-date": spec.retain_until.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "x-amz-meta-tenant-id": spec.tenant_id, "x-amz-meta-document-id": spec.document_id,
        }}

    def inspect(self, spec: ObjectSpec) -> str:
        head = self.client.head_object(Bucket=self.settings.s3_bucket, Key=spec.key)
        version = head.get("VersionId")
        if not version or version == "null":
            raise ValueError("VERSION_REQUIRED")
        if (head.get("ContentLength") != spec.size or head.get("ContentType") != spec.content_type
                or head.get("ServerSideEncryption") != "aws:kms"
                or head.get("SSEKMSKeyId") != self.settings.kms_key_arn
                or head.get("Metadata", {}).get("tenant-id") != spec.tenant_id
                or head.get("Metadata", {}).get("document-id") != spec.document_id
                or head.get("ObjectLockMode") != "COMPLIANCE"
                or head.get("ObjectLockRetainUntilDate", datetime.min.replace(tzinfo=timezone.utc)) < spec.retain_until):
            raise ValueError("OBJECT_CONTRACT_MISMATCH")
        return version

    def read(self, spec: ObjectSpec, version: str) -> bytes:
        result = self.client.get_object(Bucket=self.settings.s3_bucket, Key=spec.key, VersionId=version)
        body = result["Body"]
        try:
            if result.get("VersionId") != version or result.get("ContentLength") != spec.size:
                raise ValueError("OBJECT_VERSION_MISMATCH")
            data = body.read(spec.size + 1)
            if body.read(1):
                raise ValueError("OBJECT_TOO_LARGE")
            validate_content(data, spec)
            return data
        finally:
            body.close()


class ClamAV:
    def __init__(self, settings: StorageSettings):
        self.settings = settings

    async def scan(self, data: bytes) -> Literal["CLEAN", "INFECTED"]:
        if len(data) > self.settings.max_bytes:
            raise unavailable("SCAN_LIMIT_EXCEEDED")
        writer = None
        try:
            async with asyncio.timeout(self.settings.scan_timeout_seconds):
                reader, writer = await asyncio.open_unix_connection(self.settings.clamav_socket, limit=4096)
                writer.write(b"zINSTREAM\0")
                for start in range(0, len(data), 65536):
                    chunk = data[start:start + 65536]
                    writer.write(struct.pack("!I", len(chunk)) + chunk)
                    await writer.drain()
                writer.write(b"\0\0\0\0")
                await writer.drain()
                result = (await reader.readuntil(b"\0"))[:-1]
                if result == b"stream: OK":
                    return "CLEAN"
                if result.startswith(b"stream: ") and result.endswith(b" FOUND"):
                    return "INFECTED"
                raise unavailable("SCAN_FAILED")
        except (OSError, TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError) as exc:
            raise unavailable("SCAN_UNAVAILABLE") from exc
        finally:
            if writer:
                writer.close()
                try:
                    async with asyncio.timeout(1):
                        await writer.wait_closed()
                except (OSError, TimeoutError):
                    pass


@lru_cache
def storage_settings() -> StorageSettings:
    return StorageSettings()


def configured_settings() -> StorageSettings:
    try:
        settings = storage_settings()
    except ValueError as exc:
        raise unavailable("STORAGE_NOT_CONFIGURED") from exc
    if not settings.storage_enabled:
        raise unavailable("STORAGE_NOT_CONFIGURED")
    return settings


@lru_cache
def storage_adapter() -> S3Storage:
    return S3Storage(configured_settings())


def scanner_adapter() -> ClamAV:
    return ClamAV(configured_settings())
