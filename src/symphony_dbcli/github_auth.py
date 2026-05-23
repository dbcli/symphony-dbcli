from __future__ import annotations

import os
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

import jwt

from .config import GitHubConfig


class GitHubAuthError(RuntimeError):
    """Raised when GitHub authentication cannot be configured."""


@dataclass(frozen=True)
class InstallationToken:
    token: str
    expires_at_epoch: int

    def is_fresh(self, now: int) -> bool:
        return self.expires_at_epoch - now > 300


@dataclass(frozen=True)
class AppCredentials:
    app_id: str
    installation_id: str
    private_key: str

    @staticmethod
    def from_env(config: GitHubConfig) -> AppCredentials | None:
        app_id = os.environ.get(config.app_id_env)
        installation_id = os.environ.get(config.installation_id_env)
        private_key = _private_key_from_env(config)
        if app_id and installation_id and private_key:
            return AppCredentials(app_id=app_id, installation_id=installation_id, private_key=private_key)
        return None


class GitHubAuthenticator:
    def __init__(self, config: GitHubConfig):
        self.config = config
        self._installation_token: InstallationToken | None = None

    @property
    def uses_github_app(self) -> bool:
        if self.config.auth_strategy == "github_app":
            return True
        if self.config.auth_strategy == "token":
            return False
        return AppCredentials.from_env(self.config) is not None

    def api_token(self) -> str | None:
        if self.uses_github_app:
            return self.installation_token()
        return os.environ.get(self.config.token_env) or os.environ.get(self.config.fallback_token_env)

    def require_api_token(self) -> str:
        token = self.api_token()
        if not token:
            raise GitHubAuthError(
                "GitHub write access requires GitHub App env vars or "
                f"${self.config.token_env}/${self.config.fallback_token_env}."
            )
        return token

    def installation_token(self) -> str:
        credentials = AppCredentials.from_env(self.config)
        if credentials is None:
            raise GitHubAuthError(
                "GitHub App auth requires "
                f"${self.config.app_id_env}, ${self.config.installation_id_env}, and "
                f"${self.config.private_key_env} or ${self.config.private_key_path_env}."
            )
        now = int(time.time())
        if self._installation_token and self._installation_token.is_fresh(now):
            return self._installation_token.token
        jwt_token = create_app_jwt(credentials.app_id, credentials.private_key, now=now)
        self._installation_token = self.create_installation_token(credentials.installation_id, jwt_token)
        return self._installation_token.token

    def create_installation_token(self, installation_id: str, app_jwt: str) -> InstallationToken:
        from .github import request_json

        data = request_json(
            self.config.api_base_url,
            "POST",
            f"/app/installations/{installation_id}/access_tokens",
            token=app_jwt,
        )
        expires_at = str(data["expires_at"])
        return InstallationToken(
            token=str(data["token"]),
            expires_at_epoch=_parse_github_time_epoch(expires_at),
        )

    def app_jwt(self) -> str:
        credentials = AppCredentials.from_env(self.config)
        if credentials is None:
            raise GitHubAuthError("GitHub App credentials are not configured.")
        return create_app_jwt(credentials.app_id, credentials.private_key)

    def authenticated_git_url(self, repo: str) -> str:
        quoted = urllib.parse.quote(repo, safe="/")
        return f"https://github.com/{quoted}.git"


def create_app_jwt(app_id: str, private_key: str, *, now: int | None = None) -> str:
    issued_at = int(time.time()) if now is None else now
    payload = {
        "iat": issued_at - 60,
        "exp": issued_at + 540,
        "iss": app_id,
    }
    encoded = jwt.encode(payload, private_key, algorithm="RS256")
    return str(encoded)


def _parse_github_time_epoch(value: str) -> int:
    from datetime import datetime

    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())


def _private_key_from_env(config: GitHubConfig) -> str | None:
    private_key = os.environ.get(config.private_key_env)
    if private_key:
        return private_key.replace("\\n", "\n")
    path = os.environ.get(config.private_key_path_env)
    if not path:
        return None
    return Path(path).read_text(encoding="utf-8")
