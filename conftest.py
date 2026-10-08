"""Tests cannot consume ambient credentials or send traffic to the collector."""

from urllib.parse import urlsplit

import pytest


@pytest.fixture(autouse=True)
def isolate_credentials_and_network(tmp_path, monkeypatch):
    for name in ("HF_TOKEN", "HUGGINGFACE_TOKEN", "HUGGING_FACE_HUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    hf_home = tmp_path / "hf"
    monkeypatch.setenv("HF_HOME", str(hf_home))
    monkeypatch.setenv("HF_TOKEN_PATH", str(hf_home / "token"))
    from huggingface_hub import constants

    monkeypatch.setattr(constants, "HF_TOKEN_PATH", str(hf_home / "token"))
    from microcosm_provider_core import auth
    from microcosm_provider_telemetry.service import collector

    real_post = collector._http_post

    def loopback_post(url, *args, **kwargs):
        if urlsplit(url).hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise AssertionError("Tests must not contact a hosted collector")
        return real_post(url, *args, **kwargs)

    monkeypatch.setattr(collector, "_http_post", loopback_post)
    monkeypatch.setattr(auth, "_http_post", loopback_post)
