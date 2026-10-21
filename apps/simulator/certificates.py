"""Explicit loopback development identity provisioning, never implicit server startup."""

import ipaddress
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

SERVER_URI = "urn:hmi-mes:simulator:server"
GATEWAY_URI = "urn:hmi-mes:gateway:client"


def provision_development_certificates(directory: Path) -> None:
    """Create a new directory exclusively; refuse overwrites and partial reuse."""
    directory.mkdir(mode=0o700, parents=False, exist_ok=False)
    for name, uri, server in [("server", SERVER_URI, True), ("gateway", GATEWAY_URI, False)]:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"HMI-MES dev {name}")])
        now = datetime.now(UTC)
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(days=30))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(
                x509.SubjectAlternativeName(
                    [
                        x509.UniformResourceIdentifier(uri),
                        x509.DNSName("localhost"),
                        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                        x509.IPAddress(ipaddress.ip_address("::1")),
                    ]
                ),
                critical=False,
            )
            .add_extension(
                x509.KeyUsage(True, True, True, True, False, False, False, None, None),
                critical=True,
            )
            .add_extension(
                x509.ExtendedKeyUsage(
                    [ExtendedKeyUsageOID.SERVER_AUTH if server else ExtendedKeyUsageOID.CLIENT_AUTH]
                ),
                critical=False,
            )
            .sign(key, hashes.SHA256())
        )
        for suffix, payload in [
            ("der", cert.public_bytes(serialization.Encoding.DER)),
            (
                "pem",
                key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                ),
            ),
        ]:
            fd = os.open(
                directory / f"{name}.{suffix}", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
