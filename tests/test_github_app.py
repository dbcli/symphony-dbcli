from __future__ import annotations

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from symphony_dbcli.github_app import default_manifest
from symphony_dbcli.github_auth import create_app_jwt


def test_default_manifest_has_required_permissions() -> None:
    manifest = default_manifest(account="amjith", owner_type="user")

    assert manifest.action_url.startswith("https://github.com/settings/apps/new?state=")
    assert manifest.payload["default_permissions"] == {
        "contents": "write",
        "issues": "write",
        "metadata": "read",
        "pull_requests": "write",
    }
    assert "pull_request" in manifest.payload["default_events"]


def test_manifest_form_posts_json_payload() -> None:
    manifest = default_manifest(account="dbcli", owner_type="org")
    form = manifest.render_form()

    assert "https://github.com/organizations/dbcli/settings/apps/new" in form
    assert "name=&quot;symphony-dbcli&quot;" not in form
    assert "Create GitHub App" in form


def test_app_jwt_uses_rs256_and_app_issuer() -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    token = create_app_jwt("12345", private_pem, now=1_700_000_000)
    decoded = jwt.decode(
        token,
        public_pem,
        algorithms=["RS256"],
        issuer="12345",
        options={"verify_exp": False},
    )
    header = jwt.get_unverified_header(token)

    assert header["alg"] == "RS256"
    assert decoded["iss"] == "12345"
    assert decoded["iat"] == 1_699_999_940
    assert decoded["exp"] == 1_700_000_540
