"""TLS to Vertep (CORE) from the LAB.

``LAB_RESOURCE_CA_FILE`` is either an operator's CA or — after «Підключити лабораторію»
(#491) — CORE's own self-signed web certificate, pinned by fingerprint when the LAB
connected. Such a certificate names CORE's domain (``vertep.local``), not the address
the LAB uses. For exactly that pinned leaf the host-name check stays on, but against the
name the pinned certificate itself carries: the peer must present that certificate
(only it is trusted) and it must name what the pin names. A real CA keeps the ordinary
check against the address used.
"""

from __future__ import annotations

import os
import ssl


def _pinned_name(path: str) -> str | None:
    """DNS name of a single self-signed non-CA certificate in ``path``; otherwise None."""
    from cryptography import x509
    try:
        certificates = x509.load_pem_x509_certificates(open(path, "rb").read())
    except (OSError, ValueError):
        return None
    if len(certificates) != 1:
        return None
    certificate = certificates[0]
    try:
        if certificate.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:
            return None
    except x509.ExtensionNotFound:
        pass
    if certificate.issuer != certificate.subject:
        return None
    try:
        names = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return None
    dns = names.get_values_for_type(x509.DNSName)
    return dns[0] if dns else None


class _PinnedNameContext(ssl.SSLContext):
    """Checks the peer against the pinned certificate's own name, whatever address was used."""

    pinned_name = ""

    def wrap_socket(self, sock, server_side=False, do_handshake_on_connect=True,
                    suppress_ragged_eofs=True, server_hostname=None, session=None):
        return super().wrap_socket(sock, server_side, do_handshake_on_connect, suppress_ragged_eofs,
                                   self.pinned_name, session)

    def wrap_bio(self, incoming, outgoing, server_side=False, server_hostname=None, session=None):
        return super().wrap_bio(incoming, outgoing, server_side, self.pinned_name, session)


def resource_context() -> ssl.SSLContext:
    """Verification context for calls to Vertep; never switches verification off."""
    ca_file = os.getenv("LAB_RESOURCE_CA_FILE", "").strip()
    name = _pinned_name(ca_file) if ca_file else None
    if not name:
        return ssl.create_default_context(cafile=ca_file or None)
    context = _PinnedNameContext(ssl.PROTOCOL_TLS_CLIENT)  # CERT_REQUIRED + check_hostname
    context.verify_flags = ssl.create_default_context().verify_flags
    context.load_verify_locations(cafile=ca_file)
    context.pinned_name = name
    return context
