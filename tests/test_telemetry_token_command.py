"""`microcosm-emitter telemetry-token`: a collector token for a build elsewhere.

The operator's workstation runs it when it launches a build on Modal, which
has no Hugging Face credential. Only the token, its expiry and the collector
origin reach standard output; nothing about the token reaches standard error.
"""

import json
from datetime import datetime, timedelta

import httpx
import pytest
from fake_collector import FakeCollector
from huggingface_hub.errors import HfHubHTTPError

from microcosm_emitter import cli
from microcosm_emitter.core import auth as auth_module
from microcosm_emitter.core.auth import CollectorSession

SYNTHETIC_HUGGING_FACE_TOKEN = "hf_synthetic_operator_token"
STAGE_LIFETIME_SECONDS = 18 * 3600
UNREACHABLE_COLLECTOR_URL = "http://127.0.0.1:1"


@pytest.fixture
def collector():
    fake = FakeCollector().start()
    try:
        yield fake
    finally:
        fake.stop()


@pytest.fixture
def operator_login(monkeypatch):
    """The operator's Hugging Face login, and nothing ambient."""
    monkeypatch.delenv("HUGGINGFACE_TOKEN", raising=False)
    monkeypatch.setenv("HF_TOKEN", SYNTHETIC_HUGGING_FACE_TOKEN)
    monkeypatch.setattr(auth_module, "get_token", lambda: None)


def unreadable_saved_login() -> str:
    error_message = "the saved token file is not readable"
    raise PermissionError(error_message)


def failed_oidc_exchange() -> str:
    """get_token() with HF_OIDC_RESOURCE set, when the Hub answers 503."""
    request = httpx.Request("POST", "https://huggingface.co/oauth/token")
    error_message = "503 Server Error: Service Unavailable"
    raise HfHubHTTPError(
        error_message,
        response=httpx.Response(503, request=request),
    )


def unexpected_error(*arguments, **options):
    error_message = f"an error quoting {SYNTHETIC_HUGGING_FACE_TOKEN}"
    raise RuntimeError(error_message)


def telemetry_token(collector_url: str, lifetime_seconds: int) -> int:
    try:
        return cli.main(
            [
                "telemetry-token",
                "--lifetime-seconds",
                str(lifetime_seconds),
                "--development-collector-url",
                collector_url,
            ]
        )
    except SystemExit as usage_error:
        return usage_error.code


def test_prints_a_telemetry_token_for_the_requested_lifetime(
    collector, operator_login, capsys
) -> None:
    requested_at = datetime.now().astimezone()

    exit_code = telemetry_token(collector.origin, STAGE_LIFETIME_SECONDS)

    output = capsys.readouterr()
    assert exit_code == 0
    assert output.err == ""
    printed = json.loads(output.out)
    assert sorted(printed) == ["access_token", "collector_origin", "expires_at"]
    assert printed["collector_origin"] == collector.origin
    assert printed["access_token"].startswith("session-token-")
    # The expiry counts from before the request, so it never overstates it.
    expires_at = datetime.fromisoformat(printed["expires_at"])
    assert expires_at.utcoffset() == timedelta(0)
    lifetime = expires_at - requested_at
    assert timedelta(seconds=STAGE_LIFETIME_SECONDS - 2) <= lifetime
    assert lifetime <= timedelta(seconds=STAGE_LIFETIME_SECONDS)
    assert collector.exchange_requests == [
        {"scope": "telemetry:write", "lifetime_seconds": STAGE_LIFETIME_SECONDS}
    ]
    assert collector.exchange_credentials == [SYNTHETIC_HUGGING_FACE_TOKEN]


@pytest.mark.parametrize(
    ("failure", "expected_exit_code"),
    [
        ("no Hugging Face login", 3),
        ("credential refused with 401", 4),
        ("not an organization member (403)", 4),
        # Not a bearer token the collector would accept, so it counts as refused.
        ("credential with a line break", 4),
        ("unreadable saved login", 3),
        # Not a saved login that cannot be read, so not exit 3.
        ("OIDC token exchange that fails", 7),
        ("collector unreachable", 5),
        ("collector answers 503", 5),
        ("collector answer that is not HTTP", 5),
        ("collector without lifetime_seconds (422)", 6),
        ("lifetime below 1", 2),
        ("development collector that is not loopback", 2),
        # Its text could quote the credential, so only its type is printed.
        ("unexpected error", 7),
    ],
)
def test_each_failure_has_its_own_exit_code_and_prints_nothing(
    collector, operator_login, monkeypatch, capsys, failure, expected_exit_code
) -> None:
    collector_url = collector.origin
    lifetime_seconds = STAGE_LIFETIME_SECONDS
    if failure == "no Hugging Face login":
        monkeypatch.delenv("HF_TOKEN")
    elif failure == "credential refused with 401":
        collector.exchange_status = 401
    elif failure == "not an organization member (403)":
        collector.exchange_status = 403
    elif failure == "collector unreachable":
        collector_url = UNREACHABLE_COLLECTOR_URL
    elif failure == "collector answers 503":
        collector.exchange_status = 503
    elif failure == "collector answer that is not HTTP":
        collector.malformed_exchange_answer = True
    elif failure == "lifetime below 1":
        lifetime_seconds = 0
    elif failure == "development collector that is not loopback":
        collector_url = "https://collector.example"
    elif failure == "credential with a line break":
        monkeypatch.setenv("HF_TOKEN", f"{SYNTHETIC_HUGGING_FACE_TOKEN}\nsecond-part")
    elif failure == "unreadable saved login":
        monkeypatch.delenv("HF_TOKEN")
        monkeypatch.setattr(auth_module, "get_token", unreadable_saved_login)
    elif failure == "OIDC token exchange that fails":
        monkeypatch.delenv("HF_TOKEN")
        monkeypatch.setenv("HF_OIDC_RESOURCE", "datasets/policyengine/synthetic")
        monkeypatch.setattr(auth_module, "get_token", failed_oidc_exchange)
    elif failure == "unexpected error":
        monkeypatch.setattr(
            auth_module, "exchange_huggingface_credential", unexpected_error
        )
    else:
        collector.accepts_lifetime_seconds = False

    exit_code = telemetry_token(collector_url, lifetime_seconds)

    output = capsys.readouterr()
    assert exit_code == expected_exit_code
    assert output.out == ""
    assert output.err.strip()
    assert SYNTHETIC_HUGGING_FACE_TOKEN not in output.err
    if failure == "credential with a line break":
        assert "cannot be sent as a bearer token" in output.err
    elif failure == "unreadable saved login":
        assert "could not be read (PermissionError)" in output.err
    elif failure == "OIDC token exchange that fails":
        assert "unexpected HfHubHTTPError" in output.err
    if expected_exit_code in {2, 3, 7} or failure == "credential with a line break":
        assert collector.exchange_requests == []


def test_an_exchanged_token_leaves_the_token_out_of_its_repr(collector) -> None:
    exchanged = auth_module.exchange_huggingface_credential(
        collector.origin, SYNTHETIC_HUGGING_FACE_TOKEN
    )

    assert isinstance(exchanged, auth_module.ExchangedToken)
    assert exchanged.access_token not in repr(exchanged)


@pytest.mark.parametrize(
    "answer",
    [
        {"access_token": "", "expires_in": 3600},
        {"access_token": "session-token"},
        {"access_token": "session-token", "expires_in": 0},
        {"access_token": "session-token", "expires_in": "3600"},
        # Not sendable as a bearer token, like an unsendable Hugging Face
        # credential: the emitter given it would refuse it at start.
        {"access_token": "session token", "expires_in": 3600},
        {"access_token": "session-token\nsecond-part", "expires_in": 3600},
        {"access_token": "session-t\u00f6ken", "expires_in": 3600},
    ],
    ids=[
        "empty token",
        "no lifetime",
        "zero lifetime",
        "lifetime as text",
        "token with a space",
        "token with a line break",
        "token with a non-ASCII character",
    ],
)
def test_a_token_answer_without_a_usable_token_and_a_lifetime_is_unusable(
    collector, operator_login, capsys, answer
) -> None:
    collector.exchange_answer = answer

    exit_code = telemetry_token(collector.origin, STAGE_LIFETIME_SECONDS)

    assert exit_code == 5
    assert capsys.readouterr().out == ""
    # The emitter's own sessions check an exchange answer the same way.
    assert CollectorSession(collector.origin).credential() == ("unavailable", None)
