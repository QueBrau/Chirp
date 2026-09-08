"""Opt-in actual Redis TLS/AUTH compatibility. Only ephemeral loopback Redis is used.

Run explicitly with CHIRP_TEST_REDIS_BINARY pointing to an operator-provided,
TLS-enabled redis-server. Missing prerequisites fail; this is not a fake green or
a production connection tool. Certificates, passwords and data are synthetic.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import logging
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import time
from types import SimpleNamespace
from urllib.parse import quote, urlencode

import pytest
from redis.asyncio import Redis
from redis.exceptions import AuthenticationError, ConnectionError, RedisError

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
from app.ws import pubsub


def command(argv):
    result = subprocess.run(argv, capture_output=True, timeout=10)
    if result.returncode:
        pytest.fail("local certificate prerequisite failed; output suppressed", pytrace=False)


@pytest.fixture(scope="module")
def certificates(tmp_path_factory):
    directory = tmp_path_factory.mktemp("c372-synthetic-tls")
    openssl = os.environ.get("CHIRP_TEST_OPENSSL", shutil.which("openssl"))
    if not openssl:
        pytest.fail("CHIRP_TEST_OPENSSL or openssl required", pytrace=False)
    for ca in ("ca", "other-ca"):
        command([openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=c372-synthetic-" + ca,
                 "-keyout", str(directory / (ca + ".key")), "-out", str(directory / (ca + ".pem"))])
    for name, san, ca in (("server", "IP:127.0.0.1,DNS:localhost", "ca"), ("wrong-host", "DNS:unrelated.invalid", "ca"), ("rotated", "IP:127.0.0.1,DNS:localhost", "other-ca")):
        command([openssl, "req", "-new", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=c372-synthetic-server", "-keyout", str(directory / (name + ".key")), "-out", str(directory / (name + ".csr"))])
        extensions = directory / (name + ".ext")
        extensions.write_text("subjectAltName=" + san + "\nbasicConstraints=CA:FALSE\nkeyUsage=digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n")
        command([openssl, "x509", "-req", "-in", str(directory / (name + ".csr")), "-CA", str(directory / (ca + ".pem")), "-CAkey", str(directory / (ca + ".key")), "-CAcreateserial", "-days", "1", "-extfile", str(extensions), "-out", str(directory / (name + ".pem"))])
    (directory / "both-ca.pem").write_bytes((directory / "ca.pem").read_bytes() + (directory / "other-ca.pem").read_bytes())
    yield directory
    # pytest's temp retention must not retain even synthetic private keys/passwords.
    shutil.rmtree(directory)


class LocalRedis:
    def __init__(self, directory, certificates, certificate="server"):
        self.binary = os.environ.get("CHIRP_TEST_REDIS_BINARY")
        if not self.binary or not Path(self.binary).is_file():
            pytest.fail("explicit TLS-enabled CHIRP_TEST_REDIS_BINARY required", pytrace=False)
        self.directory, self.certificates, self.certificate = directory, certificates, certificate
        self.password = "c372:" + secrets.token_urlsafe(24) + "/@?#%"
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self.process = None
        self.config = directory / "redis-synthetic.conf"

    def start(self):
        cert = self.certificates
        self.config.write_text("\n".join([
            "bind 127.0.0.1", "protected-mode yes", "port 0", f"tls-port {self.port}",
            f'tls-cert-file "{cert / (self.certificate + ".pem")}"',
            f'tls-key-file "{cert / (self.certificate + ".key")}"',
            f'tls-ca-cert-file "{cert / "both-ca.pem"}"', "tls-auth-clients no",
            "tls-protocols \"TLSv1.2 TLSv1.3\"", "save \"\"", "appendonly no", "daemonize no",
            f'dir "{self.directory}"', f'requirepass "{self.password}"', "loglevel warning", ""]))
        self.config.chmod(0o600)
        self.process = subprocess.Popen([self.binary, str(self.config)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                        env={**os.environ, "LC_ALL": "C", "LANG": "C"})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                pytest.fail("owned synthetic TLS Redis failed to start; output suppressed", pytrace=False)
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=.1):
                    return
            except OSError:
                time.sleep(.02)
        self.stop()
        pytest.fail("owned synthetic TLS Redis startup deadline", pytrace=False)

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        self.config.unlink(missing_ok=True)

    def url(self, *, password="correct", ca="ca.pem", scheme="rediss", verify="required"):
        credential = self.password if password == "correct" else password
        credentials = ":" + quote(credential, safe="") + "@" if credential is not None else ""
        # Keep the installed client's default protocol, as the application does.
        query = {"socket_connect_timeout": "0.4", "socket_timeout": "0.4"}
        if scheme == "rediss":
            query.update(ssl_cert_reqs=verify, ssl_check_hostname="true")
            if ca is not None:
                query["ssl_ca_certs"] = str(self.certificates / ca)
        return f"{scheme}://{credentials}127.0.0.1:{self.port}/0?" + urlencode(query)


@pytest.fixture
def server(tmp_path, certificates):
    instance = LocalRedis(tmp_path, certificates)
    try:
        instance.start()
        yield instance
    finally:
        instance.stop()


@asynccontextmanager
async def client(url):
    instance = Redis.from_url(url, decode_responses=True)
    try:
        yield instance
    finally:
        await asyncio.wait_for(instance.aclose(), 2)


async def ping(instance):
    return await asyncio.wait_for(instance.ping(), 3)


@pytest.mark.asyncio
async def test_actual_factory_auth_pubsub_and_rate_keys(server, monkeypatch):
    previous = pubsub._client
    pubsub._client = None
    monkeypatch.setattr(pubsub, "get_settings", lambda: SimpleNamespace(redis_url=server.url()))
    connection = pubsub.get_redis()
    subscriber = connection.pubsub()
    try:
        assert pubsub.get_redis() is connection
        assert await ping(connection)
        assert await connection.incr("c372:synthetic:rate") == 1
        assert await connection.expire("c372:synthetic:rate", 5)
        await subscriber.subscribe("user:c372-synthetic")
        await subscriber.get_message(timeout=1)
        await pubsub.publish_to_user("c372-synthetic", {"type": "c372_synthetic_probe"})
        message = await asyncio.wait_for(subscriber.get_message(ignore_subscribe_messages=True, timeout=1), 2)
        assert message["data"] == '{"type": "c372_synthetic_probe"}'
    finally:
        await asyncio.wait_for(subscriber.aclose(), 2)
        await asyncio.wait_for(connection.aclose(), 2)
        pubsub._client = previous


@pytest.mark.asyncio
@pytest.mark.parametrize("password", [None, "incorrect-synthetic-password"])
async def test_missing_or_wrong_auth_fails(server, password):
    async with client(server.url(password=password)) as connection:
        with pytest.raises(AuthenticationError):
            await ping(connection)


@pytest.mark.asyncio
@pytest.mark.parametrize("ca", [None, "other-ca.pem", "missing-file.pem"])
async def test_untrusted_missing_or_wrong_ca_fails(server, ca):
    async with client(server.url(ca=ca)) as connection:
        with pytest.raises((ConnectionError, FileNotFoundError)):
            await ping(connection)


@pytest.mark.asyncio
async def test_wrong_hostname_rejected_with_correct_ca(server):
    server.stop()
    server.certificate = "wrong-host"
    server.start()
    async with client(server.url()) as connection:
        with pytest.raises(ConnectionError):
            await ping(connection)


@pytest.mark.asyncio
async def test_plaintext_client_cannot_use_tls_listener(server):
    async with client(server.url(scheme="redis")) as connection:
        with pytest.raises((RedisError, TimeoutError)):
            await ping(connection)


@pytest.mark.asyncio
async def test_current_startup_probe_accepts_tls_and_logs_no_failed_credential(server, caplog):
    from app.main import _probe_redis
    good = SimpleNamespace(env="staging", redis_url=server.url())
    with caplog.at_level(logging.INFO):
        await asyncio.wait_for(_probe_redis(good), 4)
    assert "redis reachable" in caplog.text and "redis unreachable" not in caplog.text
    bad = SimpleNamespace(env="staging", redis_url=server.url(password="failed-credential-canary"))
    await asyncio.wait_for(_probe_redis(bad), 4)
    assert "AuthenticationError" in caplog.text
    assert "failed-credential-canary" not in caplog.text and "rediss://" not in caplog.text


@pytest.mark.asyncio
async def test_same_client_reconnects_after_broker_restart(server):
    async with client(server.url()) as connection:
        assert await ping(connection)
        server.stop()
        with pytest.raises((RedisError, TimeoutError)):
            await ping(connection)
        server.start()
        assert await ping(connection)


@pytest.mark.asyncio
async def test_combined_ca_bundle_survives_new_ca_old_only_fails(server):
    async with client(server.url(ca="both-ca.pem")) as connection:
        assert await ping(connection)
        server.stop()
        server.certificate = "rotated"
        server.start()
        # The first operation can discover a stale socket and fail. An explicit
        # retry of this idempotent PING establishes the replacement connection;
        # this is not a promise that redis-py transparently replays every command.
        try:
            assert await ping(connection)
        except ConnectionError:
            assert await ping(connection)
    async with client(server.url(ca="ca.pem")) as old:
        with pytest.raises(ConnectionError):
            await ping(old)


@pytest.mark.asyncio
async def test_auth_rotation_old_url_fails_new_client_recovers(server):
    old_url = server.url()
    server.stop()
    server.password = secrets.token_urlsafe(32)
    server.start()
    async with client(old_url) as old:
        with pytest.raises(AuthenticationError):
            await ping(old)
    async with client(server.url()) as new:
        assert await ping(new)
