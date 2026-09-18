import shutil

import pytest

from aa_hmi import cert
from aa_hmi.errors import AaHmiError

HAS_OPENSSL = shutil.which("openssl") is not None


def test_ensure_cert_is_a_noop_when_both_files_exist(tmp_path, monkeypatch):
    cert_file = tmp_path / "c.pem"
    key_file = tmp_path / "k.pem"
    cert_file.write_text("existing cert")
    key_file.write_text("existing key")

    def boom(*a, **kw):
        raise AssertionError("subprocess.run should not be called when both files already exist")

    monkeypatch.setattr(cert.subprocess, "run", boom)
    cert.ensure_cert(cert_file, key_file)  # must not raise
    assert cert_file.read_text() == "existing cert"
    assert key_file.read_text() == "existing key"


def test_ensure_cert_raises_when_openssl_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(cert.shutil, "which", lambda name: None)
    with pytest.raises(AaHmiError, match="openssl"):
        cert.ensure_cert(tmp_path / "c.pem", tmp_path / "k.pem")


@pytest.mark.skipif(not HAS_OPENSSL, reason="openssl not installed")
def test_ensure_cert_generates_a_real_valid_pair(tmp_path):
    cert_file = tmp_path / "nested" / "c.pem"
    key_file = tmp_path / "nested" / "k.pem"
    cert.ensure_cert(cert_file, key_file)
    assert cert_file.exists()
    assert key_file.exists()
    assert "BEGIN CERTIFICATE" in cert_file.read_text()
    assert "PRIVATE KEY" in key_file.read_text()


@pytest.mark.skipif(not HAS_OPENSSL, reason="openssl not installed")
def test_ensure_cert_second_call_does_not_regenerate(tmp_path):
    cert_file = tmp_path / "c.pem"
    key_file = tmp_path / "k.pem"
    cert.ensure_cert(cert_file, key_file)
    first_cert_bytes = cert_file.read_bytes()
    cert.ensure_cert(cert_file, key_file)
    assert cert_file.read_bytes() == first_cert_bytes
