from __future__ import annotations

import html
import json
import secrets
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class GitHubAppManifest:
    account: str
    owner_type: str
    name: str
    homepage_url: str
    redirect_url: str
    webhook_url: str
    state: str

    @property
    def action_url(self) -> str:
        state = urllib.parse.urlencode({"state": self.state})
        if self.owner_type == "org":
            return f"https://github.com/organizations/{self.account}/settings/apps/new?{state}"
        return f"https://github.com/settings/apps/new?{state}"

    @property
    def payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "url": self.homepage_url,
            "hook_attributes": {
                "url": self.webhook_url,
                "active": False,
            },
            "redirect_url": self.redirect_url,
            "callback_urls": [self.redirect_url],
            "description": "Symphony worker orchestration for DBCLI GitHub Issues.",
            "public": False,
            "default_permissions": {
                "contents": "write",
                "issues": "write",
                "metadata": "read",
                "pull_requests": "write",
            },
            "default_events": [
                "issues",
                "issue_comment",
                "pull_request",
                "push",
            ],
        }

    def render_form(self) -> str:
        manifest = html.escape(json.dumps(self.payload, separators=(",", ":")), quote=True)
        action = html.escape(self.action_url, quote=True)
        return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Create Symphony GitHub App</title>
</head>
<body>
  <main>
    <h1>Create Symphony GitHub App</h1>
    <p>This form posts a GitHub App manifest to GitHub. After creating the app,
    GitHub redirects to <code>{html.escape(self.redirect_url)}</code> with a temporary code.</p>
    <form action="{action}" method="post">
      <input type="hidden" name="manifest" value="{manifest}" />
      <button type="submit">Create GitHub App</button>
    </form>
  </main>
</body>
</html>
"""


def default_manifest(
    *,
    account: str,
    owner_type: str,
    name: str = "symphony-dbcli",
    homepage_url: str = "https://github.com/amjith/symphony-dbcli",
    redirect_url: str = "http://127.0.0.1:8765/github-app/callback",
    webhook_url: str = "https://github.com/amjith/symphony-dbcli",
) -> GitHubAppManifest:
    return GitHubAppManifest(
        account=account,
        owner_type=owner_type,
        name=name,
        homepage_url=homepage_url,
        redirect_url=redirect_url,
        webhook_url=webhook_url,
        state=secrets.token_urlsafe(24),
    )


def write_manifest_form(manifest: GitHubAppManifest, destination: str | Path) -> Path:
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(manifest.render_form(), encoding="utf-8")
    return path
