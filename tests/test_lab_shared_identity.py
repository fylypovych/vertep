"""#491 L3 / #493 AC6: the LAB accepts a CORE-signed administrator assertion."""

import pytest


def test_core_assertion_is_verified_by_the_lab(tmp_path, monkeypatch):
    from core import lab_identity
    from lab import identity
    monkeypatch.setenv("LAB_IDENTITY_KEY_FILE", str(tmp_path / "lab-identity.key"))
    monkeypatch.setenv("LAB_VERTEP_IDENTITY_KEY", lab_identity.public_key_pem())
    identity.reset_cache()
    token = lab_identity.issue("root", "admin", now=1000)
    assert identity.verify(token, now=1100) == "root"
    with pytest.raises(identity.IdentityError):
        identity.verify(token, now=1000 + lab_identity.LIFETIME_SECONDS)  # expired
    body, signature = token.split(".")
    with pytest.raises(identity.IdentityError):
        identity.verify(body + "x." + signature, now=1100)  # tampered
    with pytest.raises(PermissionError):
        lab_identity.issue("mod", "moderator")
    monkeypatch.setenv("LAB_PANEL_URL", "https://lab.example:8443")
    assert lab_identity.allowed_return("https://lab.example:8443/")
    assert not lab_identity.allowed_return("https://evil.example/")
    identity.reset_cache()
