"""Test helper: a LAB API client signed in as a Vertep administrator (#491 L2/L3).

Every LAB API call except the probes needs an administrator, so read-only test
calls carry a real CORE-signed assertion. A call with explicit ``headers`` (even
``{}``) is sent as given, which keeps the 401 checks anonymous.
"""

import pytest
from fastapi.testclient import TestClient

ADMIN = "root"


def admin_assertion(user: str = ADMIN) -> str:
    from core import lab_identity
    return lab_identity.issue(user, "admin")


@pytest.fixture
def vertep_identity(tmp_path, monkeypatch):
    """CORE's LAB identity key in a temp dir; the LAB trusts its public half."""
    from core import lab_identity
    from lab import identity
    monkeypatch.setenv("LAB_IDENTITY_KEY_FILE", str(tmp_path / "lab-identity.key"))
    monkeypatch.setenv("LAB_VERTEP_IDENTITY_KEY", lab_identity.public_key_pem())
    identity.reset_cache()
    yield admin_assertion
    identity.reset_cache()


class AdminClient(TestClient):
    def request(self, method, url, *args, headers=None, **kwargs):
        if headers is None:
            headers = {"X-Vertep-Identity": admin_assertion()}
        return super().request(method, url, *args, headers=headers, **kwargs)
