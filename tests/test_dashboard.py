from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from symphony_dbcli.config import PolicyConfig, ProfileConfig, default_config
from symphony_dbcli.dashboard import DashboardRuntime, DashboardState, render_index
from symphony_dbcli.store import Store


def test_dashboard_uses_static_css(tmp_path: Path) -> None:
    store = Store(tmp_path / "symphony.db")
    store.init()

    html = render_index(store)

    assert '<link rel="stylesheet" href="/static/dashboard.css"' in html
    assert "<style>" not in html
    assert "Recent Attempts" in html
    assert "Dry Run" in html
    assert "On" in html


def test_dashboard_shows_live_mode_when_dry_run_is_disabled(tmp_path: Path) -> None:
    store = Store(tmp_path / "symphony.db")
    store.init()
    config = replace(
        default_config(),
        profile=ProfileConfig(active="prod"),
        policy=PolicyConfig(dry_run=False),
    )

    html = render_index(store, DashboardRuntime.from_config(config))

    assert "Dry Run" in html
    assert "Off" in html
    assert "prod profile" in html


def test_dashboard_state_updates_runtime_config() -> None:
    state = DashboardState(default_config())
    live_config = replace(default_config(), policy=PolicyConfig(dry_run=False))

    state.update_config(live_config)

    assert state.runtime().dry_run is False
