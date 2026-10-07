"""Stand-ins for the media services tests run against: S3 and a ClamAV daemon.

* S3: moto's server mode, a real HTTP endpoint that serves presigned URLs. It does
  not check signatures, sizes, or types on upload; RustFS does (a PUT whose size or
  type differs from the signed one is refused), so those bindings are not proven
  here. The worker re-checks size and type itself either way.
* clamd: a small TCP server that speaks the INSTREAM protocol and flags the EICAR
  test string, exactly as ClamAV does; any other bytes are clean.
"""

from __future__ import annotations

import socketserver
import struct
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import boto3
from moto.server import ThreadedMotoServer

BUCKET = "beluno-media-test"
ACCESS_KEY_ID = "testing"
SECRET_ACCESS_KEY = "testing-secret"  # moto accepts any credentials
REGION = "us-east-1"
# The standard antivirus test file: harmless, and every scanner reports it.
EICAR = rb"X5O!P%@AP[4\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"


@dataclass(frozen=True)
class MediaServices:
    storage_url: str
    clamd_host: str
    clamd_port: int


class _ClamdHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        command = self._read_until(b"\0")
        if command != b"zINSTREAM\0":
            self.request.sendall(b"UNKNOWN COMMAND\0")
            return
        received = bytearray()
        while True:
            size = struct.unpack(">I", self._read_exactly(4))[0]
            if size == 0:
                break
            received += self._read_exactly(size)
        found = EICAR in received
        reply = b"stream: Eicar-Test-Signature FOUND\0" if found else b"stream: OK\0"
        self.request.sendall(reply)

    def _read_exactly(self, size: int) -> bytes:
        data = bytearray()
        while len(data) < size:
            chunk = self.request.recv(size - len(data))
            if not chunk:
                raise ConnectionError("client closed the stream")
            data += chunk
        return bytes(data)

    def _read_until(self, end: bytes) -> bytes:
        data = bytearray()
        while not data.endswith(end):
            chunk = self.request.recv(1)
            if not chunk:
                break
            data += chunk
        return bytes(data)


class _ClamdServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


@contextmanager
def media_services() -> Iterator[MediaServices]:
    s3 = ThreadedMotoServer(ip_address="127.0.0.1", port=0, verbose=False)
    s3.start()
    host, port = s3.get_host_and_port()
    url = f"http://{host}:{port}"
    boto3.client(
        "s3",
        endpoint_url=url,
        region_name=REGION,
        aws_access_key_id=ACCESS_KEY_ID,
        aws_secret_access_key=SECRET_ACCESS_KEY,
    ).create_bucket(Bucket=BUCKET)
    clamd = _ClamdServer(("127.0.0.1", 0), _ClamdHandler)
    thread = threading.Thread(target=clamd.serve_forever, daemon=True)
    thread.start()
    try:
        yield MediaServices(url, "127.0.0.1", clamd.server_address[1])
    finally:
        clamd.shutdown()
        clamd.server_close()
        s3.stop()
