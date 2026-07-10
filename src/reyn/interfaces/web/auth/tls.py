"""TLS material provisioning for the cross-machine (T3) transport tier.

A network-reachable bind must be encrypted. The secure-by-default posture is
**self-signed, trust-on-first-use (TOFU)**: when the operator supplies no
certificate, generate a fresh self-signed cert/key, print its SHA-256
fingerprint at startup, and let a first-connecting client pin it (the same
model an SSH host key or a Jupyter self-signed cert uses). An operator who has
a real certificate overrides both paths and this module just computes the
fingerprint to print.

The generated material is an **ephemeral runtime artifact**, not recovery-core
state — it is written under the ephemeral run dir and regenerated on the next
start if absent. ``cryptography`` is an optional dependency of the ``[web]``
extra; a missing install surfaces as :class:`TlsProvisioningError` so the CLI
can print an actionable message rather than crashing deep in the stack.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


class TlsProvisioningError(RuntimeError):
    """TLS material could not be provisioned (missing dep / unreadable cert)."""


@dataclass(frozen=True)
class TlsMaterial:
    """A resolved cert/key pair plus the SHA-256 fingerprint to advertise."""

    certfile: Path
    keyfile: Path
    fingerprint_sha256: str  # colon-separated uppercase hex (TOFU pin value)


def fingerprint_of_cert(certfile: Path) -> str:
    """Return the SHA-256 fingerprint of the DER form of *certfile* (PEM in)."""
    try:
        from cryptography import x509
    except ImportError as exc:  # pragma: no cover - dep-gated
        raise TlsProvisioningError(
            "TLS requires the 'cryptography' package (install the [web] extra)."
        ) from exc
    pem = certfile.read_bytes()
    cert = x509.load_pem_x509_certificate(pem)
    der = cert.public_bytes(_der_encoding())
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))


def provision_tls(
    run_dir: Path,
    *,
    certfile: str | None = None,
    keyfile: str | None = None,
    host: str = "localhost",
) -> TlsMaterial:
    """Resolve TLS material for a T3 bind.

    When *certfile* and *keyfile* are both given, use the operator's material
    (only computing the fingerprint). Otherwise generate a fresh self-signed
    cert/key under *run_dir* (TOFU). Raises :class:`TlsProvisioningError` when
    ``cryptography`` is unavailable or an operator-supplied file is unreadable.
    """
    if certfile and keyfile:
        cert_path = Path(certfile)
        key_path = Path(keyfile)
        if not cert_path.is_file() or not key_path.is_file():
            raise TlsProvisioningError(
                f"operator TLS cert/key not found: {cert_path} / {key_path}"
            )
        return TlsMaterial(cert_path, key_path, fingerprint_of_cert(cert_path))
    if certfile or keyfile:
        raise TlsProvisioningError(
            "TLS cert and key must be provided together (got only one)."
        )
    return _generate_self_signed(run_dir, host=host)


def _generate_self_signed(run_dir: Path, *, host: str) -> TlsMaterial:
    """Generate a self-signed cert/key under *run_dir* and return the material."""
    try:
        import datetime

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError as exc:  # pragma: no cover - dep-gated
        raise TlsProvisioningError(
            "TLS requires the 'cryptography' package (install the [web] extra)."
        ) from exc

    run_dir.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=825))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False)
        .sign(key, hashes.SHA256())
    )

    cert_path = run_dir / "web-tls-cert.pem"
    key_path = run_dir / "web-tls-key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    # 0600 — operator-owned key material.
    key_path.chmod(0o600)

    der = cert.public_bytes(_der_encoding())
    digest = hashlib.sha256(der).hexdigest().upper()
    fp = ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))
    return TlsMaterial(cert_path, key_path, fp)


def _der_encoding():
    from cryptography.hazmat.primitives import serialization

    return serialization.Encoding.DER


__all__ = ["TlsMaterial", "TlsProvisioningError", "provision_tls", "fingerprint_of_cert"]
