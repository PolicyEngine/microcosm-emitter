"""Tests cannot consume ambient credentials or send traffic to the collector."""

from urllib.parse import urlsplit

import pytest


@pytest.fixture(autouse=True)
def isolate_credentials_and_network(tmp_path, monkeypatch):
    """Remove ambient credentials and refuse HTTP posts to non-loopback hosts.

    Deletes the Hugging Face token variables and the collector token variables in
    ``GIVEN_TOKEN_VARIABLES``, points ``HF_HOME`` and the Hugging Face token file
    into ``tmp_path``, and replaces the collector's and the auth module's HTTP post
    with one that raises ``AssertionError`` for any host other than a loopback
    address.
    """
    from given_token import GIVEN_TOKEN_VARIABLES

    for name in (
        "HF_TOKEN",
        "HUGGINGFACE_TOKEN",
        "HUGGING_FACE_HUB_TOKEN",
        *GIVEN_TOKEN_VARIABLES,
    ):
        monkeypatch.delenv(name, raising=False)
    hf_home = tmp_path / "hf"
    monkeypatch.setenv("HF_HOME", str(hf_home))
    monkeypatch.setenv("HF_TOKEN_PATH", str(hf_home / "token"))
    from huggingface_hub import constants

    monkeypatch.setattr(constants, "HF_TOKEN_PATH", str(hf_home / "token"))
    from microcosm_emitter.core import auth
    from microcosm_emitter.telemetry.service import collector

    real_post = collector._http_post

    def loopback_post(url, *args, **kwargs):
        if urlsplit(url).hostname not in {"localhost", "127.0.0.1", "::1"}:
            error_message = "Tests must not contact a hosted collector"
            raise AssertionError(error_message)
        return real_post(url, *args, **kwargs)

    monkeypatch.setattr(collector, "_http_post", loopback_post)
    monkeypatch.setattr(auth, "_http_post", loopback_post)
