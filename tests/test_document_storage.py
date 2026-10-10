from __future__ import annotations

import asyncio
import hashlib
import io
import struct
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import boto3
import pytest
from botocore.response import StreamingBody
from botocore.stub import Stubber
from fastapi import HTTPException

from app.documents.storage import ClamAV, ObjectSpec, S3Storage, StorageSettings, validate_content

DATA = b"%PDF-1.4\nSynthetic contract-only content.\n%%EOF"
ARN = "arn:aws:kms:us-east-1:000000000000:key/00000000-0000-0000-0000-000000000000"


def spec(data=DATA, content_type="application/pdf"):
    return ObjectSpec("tenants/test/documents/test", "tenant", "document", content_type,
                      len(data), hashlib.sha256(data).hexdigest(),
                      datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=30))


def settings(**extra):
    return StorageSettings(storage_enabled=True, s3_bucket="private-documents-test",
        s3_region="us-east-1", kms_key_arn=ARN, clamav_socket="/tmp/unused-clamd", **extra)


def test_storage_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("DOCUMENT_STORAGE_ENABLED", raising=False)
    assert StorageSettings().storage_enabled is False


@pytest.mark.parametrize("field,value", [("s3_bucket", ""), ("s3_region", ""),
    ("kms_key_arn", "alias/key"), ("clamav_socket", "relative/socket")])
def test_enabled_configuration_rejects_incomplete_authority(field, value):
    values = settings().model_dump()
    values[field] = value
    with pytest.raises(ValueError):
        StorageSettings(**values)


@pytest.mark.parametrize("data,mime", [(DATA, "application/pdf"),
    (b"\x89PNG\r\n\x1a\ncontent\x00\x00\x00\x00IEND\xaeB`\x82", "image/png"),
    (b"\xff\xd8\xffimage\xff\xd9", "image/jpeg")])
def test_content_requires_exact_size_digest_and_allowed_signature(data, mime):
    validate_content(data, spec(data, mime))
    with pytest.raises(ValueError):
        validate_content(data + b"extra", spec(data, mime))
    with pytest.raises(ValueError):
        validate_content(b"?" + data[1:], spec(b"?" + data[1:], mime))


def test_s3_upload_signs_size_checksum_conditional_write_encryption_and_retention():
    client = boto3.client("s3", region_name="us-east-1", aws_access_key_id="testing",
                          aws_secret_access_key="testing", config=__import__("botocore.config", fromlist=["Config"]).Config(signature_version="s3v4"))
    store = S3Storage(settings(), client)
    with Stubber(client) as stub:
        bucket = {"Bucket": store.settings.s3_bucket}
        stub.add_response("get_bucket_versioning", {"Status": "Enabled"}, bucket)
        stub.add_response("get_public_access_block", {"PublicAccessBlockConfiguration": {
            "BlockPublicAcls": True, "IgnorePublicAcls": True,
            "BlockPublicPolicy": True, "RestrictPublicBuckets": True,
        }}, bucket)
        stub.add_response("get_object_lock_configuration", {"ObjectLockConfiguration": {"ObjectLockEnabled": "Enabled"}}, bucket)
        upload = store.upload(spec(), 60)
        query = parse_qs(urlsplit(upload["url"]).query)
        signed = query["X-Amz-SignedHeaders"][0].split(";")
        for header in upload["headers"]:
            assert header.lower() in signed
        assert query["X-Amz-Expires"] == ["60"]
        assert upload["headers"]["If-None-Match"] == "*"
        stub.assert_no_pending_responses()


@pytest.mark.parametrize("version", [None, "null"])
def test_inspection_rejects_unversioned_objects(version):
    store = S3Storage(settings(), SimpleNamespace(head_object=lambda **kw: {"VersionId": version}))
    with pytest.raises(ValueError, match="VERSION_REQUIRED"):
        store.inspect(spec())


def test_version_read_is_bounded_and_closes_the_body():
    calls = []
    raw = io.BytesIO(DATA)
    def get(**kw):
        calls.append(kw)
        return {"VersionId": "immutable-v1", "ContentLength": len(DATA), "Body": StreamingBody(raw, len(DATA))}
    store = S3Storage(settings(), SimpleNamespace(get_object=get))
    assert store.read(spec(), "immutable-v1") == DATA
    assert calls[0]["VersionId"] == "immutable-v1"
    assert raw.closed


@pytest.mark.parametrize("reply,expected", [
    (b"stream: OK\0", "CLEAN"), (b"stream: Test.Signature FOUND\0", "INFECTED"),
    (b"INSTREAM size limit exceeded. ERROR\0", None), (b"OK\0", None),
])
@pytest.mark.asyncio
async def test_clamav_real_socket_protocol_is_fail_closed(tmp_path, reply, expected):
    received = []
    async def handler(reader, writer):
        try:
            assert await reader.readuntil(b"\0") == b"zINSTREAM\0"
            data = bytearray()
            while size := struct.unpack("!I", await reader.readexactly(4))[0]:
                data.extend(await reader.readexactly(size))
            received.append(bytes(data))
            writer.write(reply)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
    path = str(tmp_path / "clamd.sock")
    server = await asyncio.start_unix_server(handler, path)
    cfg = settings().model_copy(update={"clamav_socket": path})
    async with server:
        if expected:
            assert await ClamAV(cfg).scan(DATA) == expected
        else:
            with pytest.raises(HTTPException) as denied:
                await ClamAV(cfg).scan(DATA)
            assert denied.value.status_code == 503
    assert received == [DATA]


@pytest.mark.asyncio
async def test_missing_scanner_never_marks_clean(tmp_path):
    cfg = settings().model_copy(update={"clamav_socket": str(tmp_path / "absent")})
    with pytest.raises(HTTPException) as denied:
        await ClamAV(cfg).scan(DATA)
    assert denied.value.status_code == 503
