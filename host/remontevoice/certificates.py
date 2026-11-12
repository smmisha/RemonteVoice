"""Persistent self-signed TLS identity for pinned local WebSocket sessions."""

from __future__ import annotations

import hashlib
import ipaddress
import ssl
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


def load_or_create_identity(identity_dir: Path) -> tuple[ssl.SSLContext, str]:
    identity_dir.mkdir(parents=True, exist_ok=True)
    key_path = identity_dir / "host-key.pem"
    certificate_path = identity_dir / "host-certificate.pem"

    if not key_path.exists() or not certificate_path.exists():
        _create_identity(key_path, certificate_path)

    certificate_der = x509.load_pem_x509_certificate(
        certificate_path.read_bytes()
    ).public_bytes(serialization.Encoding.DER)
    fingerprint = hashlib.sha256(certificate_der).hexdigest().upper()

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certfile=certificate_path, keyfile=key_path)
    return context, fingerprint


def _create_identity(key_path: Path, certificate_path: Path) -> None:
    private_key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(timezone.utc)
    subject = issuer = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "RemonteVoice local host")]
    )
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before((now - timedelta(minutes=5)).replace(tzinfo=None))
        .not_valid_after((now + timedelta(days=3650)).replace(tzinfo=None))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("remontevoice.local")]),
            critical=False,
        )
        .sign(private_key, hashes.SHA256())
    )
    key_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))

