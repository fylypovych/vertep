"""TLS to Vertep (CORE) from the LAB.

``LAB_RESOURCE_CA_FILE`` is either an operator's CA or — after «Підключити лабораторію»
(#491) — CORE's own self-signed web certificate, pinned by fingerprint when the LAB
connected. Such a certificate names CORE's domain (``vertep.local``), not the address
the LAB uses, so for exactly that pinned leaf the host name is not compared: the
certificate itself must still match (only it is trusted). A real CA keeps the full
host-name check.
"""

from __future__ import annotations

import os
import ssl


def _pinned_leaf(path: str) -> bool:
    from cryptography import x509
    try:
        certificates = x509.load_pem_x509_certificates(open(path, "rb").read())
    except (OSError, ValueError):
        return False
    if len(certificates) != 1:
        return False
    certificate = certificates[0]
    try:
        is_ca = certificate.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    except x509.ExtensionNotFound:
        is_ca = False
    return not is_ca and certificate.issuer == certificate.subject


def resource_context() -> ssl.SSLContext:
    """Verification context for calls to Vertep; never switches verification off."""
    ca_file = os.getenv("LAB_RESOURCE_CA_FILE", "").strip()
    context = ssl.create_default_context(cafile=ca_file or None)
    if ca_file and _pinned_leaf(ca_file):
        context.check_hostname = False  # verify_mode stays CERT_REQUIRED against that one certificate
    return context
