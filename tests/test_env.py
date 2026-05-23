from __future__ import annotations

import os
from pathlib import Path

import pytest

from symphony_dbcli.env import load_local_env, parse_env_file


def test_parse_env_file_ignores_comments_and_empty_values(tmp_path: Path) -> None:
    env_file = tmp_path / "github-app.env"
    env_file.write_text(
        "\n".join(
            [
                "# comment",
                "SYMPHONY_GITHUB_APP_ID=123",
                "SYMPHONY_GITHUB_INSTALLATION_ID=",
                "SYMPHONY_GITHUB_PRIVATE_KEY_PATH='/tmp/key.pem'",
                "",
            ]
        ),
        encoding="utf-8",
    )

    assert parse_env_file(env_file) == {
        "SYMPHONY_GITHUB_APP_ID": "123",
        "SYMPHONY_GITHUB_PRIVATE_KEY_PATH": "/tmp/key.pem",
    }


def test_load_local_env_does_not_override_existing_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / "github-app.env"
    env_file.write_text("A=from-file\nB=loaded\n", encoding="utf-8")
    monkeypatch.setenv("A", "already-set")
    monkeypatch.delenv("B", raising=False)

    result = load_local_env(env_file)

    assert result.loaded == ("B",)
    assert result.skipped == ("A",)
    assert result.path == env_file
    assert os.environ["A"] == "already-set"
    assert os.environ["B"] == "loaded"
