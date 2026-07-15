"""Access-token mode: GmailClient builds a bare bearer credential; the on-demand
smoke entrypoint wires straight to run_live_smoke. No network, no real OAuth.
"""

from pathlib import Path

import gmail_bot.gmail as gmail_mod
import gmail_bot.smoke as smoke_mod
from gmail_bot.config import Config
from gmail_bot.live_smoke import LiveSmokeResult

RTOK = "rtok"  # fake refresh-token stub for tests (not a real credential)


def _config(*, access_token=None, refresh_token=RTOK) -> Config:
    return Config(
        telegram_bot_token="tok",
        telegram_chat_id=123456789,
        gmail_client_id="cid",
        gmail_client_secret="csecret",
        gmail_refresh_token=refresh_token,
        gmail_token_uri="https://oauth2.googleapis.com/token",
        gmail_scopes=["https://www.googleapis.com/auth/gmail.modify"],
        anthropic_api_key="test-key",
        gmail_access_token=access_token,
    )


def test_access_token_mode_builds_bare_bearer_credentials(monkeypatch):
    captured = {}

    def fake_credentials(token=None, **kwargs):
        captured["token"] = token
        captured["kwargs"] = kwargs
        return object()

    def fake_build(service, version, *, credentials, cache_discovery):
        captured["built"] = (service, version)
        return object()

    monkeypatch.setattr(gmail_mod, "Credentials", fake_credentials)
    monkeypatch.setattr(gmail_mod, "build", fake_build)

    gmail_mod.GmailClient(_config(access_token="test-access-token", refresh_token=""))

    # Built from the bare access token only — no refresh_token / client_id leaked in.
    assert captured["token"] == "test-access-token"
    assert captured["kwargs"] == {}
    assert captured["built"] == ("gmail", "v1")


def test_refresh_token_mode_unchanged(monkeypatch):
    captured = {}

    def fake_credentials(token=None, **kwargs):
        captured["token"] = token
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(gmail_mod, "Credentials", fake_credentials)
    monkeypatch.setattr(gmail_mod, "build", lambda *a, **k: object())

    gmail_mod.GmailClient(_config(access_token=None, refresh_token=RTOK))

    # Falls back to the full refresh-token credential shape.
    assert captured["token"] is None
    assert captured["kwargs"]["refresh_token"] == "rtok"
    assert captured["kwargs"]["client_id"] == "cid"


def test_smoke_run_wires_to_run_live_smoke(monkeypatch, tmp_path):
    calls = {}

    def fake_load_config(env_path):
        calls["env_path"] = env_path
        return _config(access_token="test-access-token", refresh_token="")

    def fake_run_live_smoke(config, *, report_path):
        calls["config"] = config
        calls["report_path"] = report_path
        green = LiveSmokeResult()
        green.record(1, "config --check", True, "ok")
        return green

    monkeypatch.setattr(smoke_mod, "load_config", fake_load_config)
    monkeypatch.setattr(smoke_mod, "run_live_smoke", fake_run_live_smoke)

    report = tmp_path / "REPORT.html"
    rc = smoke_mod.run(env_path=Path("/fake/.env"), report_path=report)

    assert rc == 0
    assert calls["env_path"] == Path("/fake/.env")
    assert calls["config"].gmail_access_token == "test-access-token"
    assert calls["report_path"] == report


def test_smoke_run_returns_nonzero_on_failures(monkeypatch):
    def fake_run_live_smoke(config, *, report_path):
        red = LiveSmokeResult()
        red.record(2, "Gmail token refresh", False, "boom")
        return red

    monkeypatch.setattr(smoke_mod, "load_config", lambda env_path: _config())
    monkeypatch.setattr(smoke_mod, "run_live_smoke", fake_run_live_smoke)

    assert smoke_mod.run(report_path=None) == 1
