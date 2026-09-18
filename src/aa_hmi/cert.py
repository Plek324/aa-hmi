"""cert.py -- self-signed "fake phone" cert/key generation for the TLS
video session.

Direct port of aa_pi2display's aa_session.py ensure_cert() (added there
earlier this project), relocated here now that aa-hmi owns this TLS
communication itself rather than delegating it to that sibling script.
The display does no real certificate validation (see
docs/video-protocol-notes.md), so any self-signed cert works.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from .errors import AaHmiError
from .log import log

CERT_SUBJECT = "/C=US/ST=California/L=Mountain View/O=CarService/OU=01"


class CertGenerationError(AaHmiError):
    """openssl is missing, or failed to generate the cert/key pair."""


def ensure_cert(cert_file: Path, key_file: Path, subject: str = CERT_SUBJECT) -> None:
    """Make sure a self-signed cert/key pair exists at cert_file/key_file,
    generating one with openssl if either is missing. No-op if both
    already exist -- never regenerates a working pair unasked."""
    cert_file, key_file = Path(cert_file), Path(key_file)
    if cert_file.exists() and key_file.exists():
        return
    if shutil.which("openssl") is None:
        raise CertGenerationError(
            f"cert/key not found ({cert_file}, {key_file}) and openssl is not installed to "
            "generate them -- install openssl (e.g. `sudo apt install openssl`) or generate "
            "the pair yourself, see README.md's \"Regenerating the cert\" section."
        )
    cert_file.parent.mkdir(parents=True, exist_ok=True)
    key_file.parent.mkdir(parents=True, exist_ok=True)
    log(f"no cert/key at {cert_file} / {key_file} -- generating a self-signed pair with openssl")
    cmd = [
        "openssl", "req", "-x509", "-newkey", "rsa:2048",
        "-keyout", str(key_file), "-out", str(cert_file),
        "-days", "3650", "-nodes", "-subj", subject,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise CertGenerationError(f"openssl failed to generate the cert/key pair (exit {result.returncode}):\n"
                                   f"{result.stderr.strip()}")
    log(f"generated {cert_file} and {key_file}")


def default_cert_dir() -> Path:
    """Mirrors cache.default_cache_path()'s XDG-first resolution."""
    from .cache import _real_user_home  # local import: avoid a module-load-order dependency
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else _real_user_home() / ".config"
    return base / "aa-hmi" / "certs"
