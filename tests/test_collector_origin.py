"""Every module sends the builder's Hugging Face token to one trusted origin."""

import os
import re
import time

import pytest
from given_token import GIVEN_COLLECTOR_TOKEN, given_token_variables, set_variables

from microcosm_emitter.core.auth import CollectorSession
from microcosm_emitter.core.collector_origin import (
    configured_collector_origin,
    resolve_collector_origin,
)
from microcosm_emitter.core.constants import (
    CLOUD_RUN_COLLECTOR_URL,
    COLLECTOR_SETTING_TYPE_ERROR,
    COLLECTOR_URL_HTTPS_ERROR,
    COLLECTOR_URL_ORIGIN_ERROR,
    CONFLICTING_COLLECTOR_ORIGIN_ERROR,
    PRODUCTION_COLLECTOR_URL,
    TRUSTED_COLLECTOR_ORIGINS,
    UNTRUSTED_COLLECTOR_ORIGIN_ERROR,
)
from microcosm_emitter.host.contracts import ModuleContext
from microcosm_emitter.huggingface.service import module as huggingface_module
from microcosm_emitter.huggingface.service.module import HuggingFaceModule
from microcosm_emitter.orrery.service.module import OrreryModule
from microcosm_emitter.telemetry.client import TelemetryRun
from microcosm_emitter.telemetry.service.module import TelemetryModule

DEVELOPMENT_COLLECTOR_URL = "http://127.0.0.1:1"


def telemetry_module(tmp_path, context: ModuleContext, **settings) -> TelemetryModule:
    run = TelemetryRun("origin-run", "UK", "test", producer_id="origin-producer")
    return TelemetryModule(
        {
            "registration": run.as_registration(),
            "spool_path": str(tmp_path / "events.sqlite3"),
            **settings,
        },
        context,
    )


def orrery_module(tmp_path, context: ModuleContext, **settings) -> OrreryModule:
    return OrreryModule(
        {"spool_path": str(tmp_path / "graphs.sqlite3"), **settings}, context
    )


def submitting_huggingface_module(
    tmp_path, context: ModuleContext, **settings
) -> HuggingFaceModule:
    """Build the Hugging Face module with collector submissions switched on.

    It reaches neither the Hub nor a collector: these tests only start it.
    """
    return HuggingFaceModule(
        {
            "handover_database_path": str(tmp_path / "events.handovers.sqlite3"),
            "collector_submissions": True,
            **settings,
        },
        context,
        api_factory=lambda: None,
        collector_credential=lambda: None,
        background_workers=False,
    )


@pytest.fixture
def submission_session_origins(monkeypatch) -> list[str]:
    """Record the origins of the Hugging Face module's `artifacts:submit` sessions."""
    origins: list[str] = []

    class RecordingCollectorSession(CollectorSession):
        def __init__(self, origin: str, **options) -> None:
            origins.append(origin)
            super().__init__(origin, **options)

    monkeypatch.setattr(
        huggingface_module, "CollectorSession", RecordingCollectorSession
    )
    return origins


def test_every_trusted_origin_is_a_bare_https_origin() -> None:
    # A module configuration can select any trusted origin, the Cloud Run
    # address included, with or without a trailing slash.
    for trusted_origin in TRUSTED_COLLECTOR_ORIGINS:
        for value in (trusted_origin, trusted_origin + "/"):
            assert resolve_collector_origin(collector_origin=value) == trusted_origin
            assert configured_collector_origin({"collector_origin": value}) == (
                trusted_origin
            )


def test_configured_origin_outside_the_trusted_set_is_refused() -> None:
    for untrusted_origin in (
        "https://untrusted.example",
        "https://collector.example",
        PRODUCTION_COLLECTOR_URL.replace("https://", "https://evil."),
    ):
        with pytest.raises(ValueError, match=UNTRUSTED_COLLECTOR_ORIGIN_ERROR):
            resolve_collector_origin(collector_origin=untrusted_origin)


def test_configured_origin_must_be_an_origin_even_when_its_host_is_trusted() -> None:
    for value in (
        f"{CLOUD_RUN_COLLECTOR_URL}/v1",
        f"{CLOUD_RUN_COLLECTOR_URL}?next=1",
        CLOUD_RUN_COLLECTOR_URL.replace("https://", "https://user@"),
    ):
        with pytest.raises(ValueError, match=COLLECTOR_URL_ORIGIN_ERROR):
            resolve_collector_origin(collector_origin=value)
    with pytest.raises(ValueError, match=COLLECTOR_URL_HTTPS_ERROR):
        resolve_collector_origin(
            collector_origin=CLOUD_RUN_COLLECTOR_URL.replace("https://", "http://")
        )


def test_development_url_and_configured_origin_cannot_both_be_set() -> None:
    with pytest.raises(ValueError, match=CONFLICTING_COLLECTOR_ORIGIN_ERROR):
        resolve_collector_origin(
            development_collector_url=DEVELOPMENT_COLLECTOR_URL,
            collector_origin=PRODUCTION_COLLECTOR_URL,
        )
    with pytest.raises(ValueError, match=CONFLICTING_COLLECTOR_ORIGIN_ERROR):
        configured_collector_origin(
            {
                "development_collector_url": DEVELOPMENT_COLLECTOR_URL,
                "collector_origin": PRODUCTION_COLLECTOR_URL,
            }
        )


def test_empty_module_settings_count_as_unset() -> None:
    assert (
        configured_collector_origin(
            {"development_collector_url": None, "collector_origin": ""}
        )
        == PRODUCTION_COLLECTOR_URL
    )


@pytest.mark.parametrize(
    ("settings", "expected_origin"),
    [
        ({"development_collector_url": ""}, PRODUCTION_COLLECTOR_URL),
        (
            {
                "development_collector_url": "",
                "collector_origin": CLOUD_RUN_COLLECTOR_URL,
            },
            CLOUD_RUN_COLLECTOR_URL,
        ),
    ],
    ids=["default origin", "cloud run address"],
)
def test_modules_treat_an_empty_development_url_as_unset(
    tmp_path, settings, expected_origin
) -> None:
    context = ModuleContext(os.getpid())
    telemetry = telemetry_module(tmp_path, context, **settings)
    graph = orrery_module(tmp_path, context, **settings)
    telemetry.initialize()
    try:
        graph.initialize()
        try:
            assert context.services["collector_session"].origin == expected_origin
            assert telemetry.delivery.collector_url == expected_origin
        finally:
            graph.close(0)
    finally:
        telemetry.close(0)


def test_modules_refuse_an_untrusted_configured_origin(tmp_path) -> None:
    untrusted_origin = "https://untrusted.example"
    with pytest.raises(ValueError, match=UNTRUSTED_COLLECTOR_ORIGIN_ERROR):
        telemetry_module(
            tmp_path,
            ModuleContext(os.getpid()),
            collector_origin=untrusted_origin,
        ).initialize()
    with pytest.raises(ValueError, match=UNTRUSTED_COLLECTOR_ORIGIN_ERROR):
        orrery_module(
            tmp_path,
            ModuleContext(os.getpid()),
            collector_origin=untrusted_origin,
        ).initialize()


def test_every_module_without_a_setting_uses_the_production_collector(
    tmp_path, submission_session_origins
) -> None:
    context = ModuleContext(os.getpid())
    telemetry = telemetry_module(tmp_path, context)
    graph = orrery_module(tmp_path, context)
    hugging_face = submitting_huggingface_module(tmp_path, context)
    telemetry.initialize()
    try:
        graph.initialize()
        try:
            hugging_face.initialize()
            try:
                shared_origin = context.services["collector_session"].origin
                assert shared_origin == PRODUCTION_COLLECTOR_URL
                assert telemetry.delivery.collector_url == shared_origin
                assert hugging_face.collector_delivery is not None
                assert hugging_face.collector_delivery.client.origin == shared_origin
                assert submission_session_origins == [shared_origin]
            finally:
                hugging_face.close(0)
        finally:
            graph.close(0)
    finally:
        telemetry.close(0)


def test_every_module_sends_to_the_same_configured_origin(
    tmp_path, submission_session_origins
) -> None:
    context = ModuleContext(os.getpid())
    telemetry = telemetry_module(
        tmp_path, context, collector_origin=CLOUD_RUN_COLLECTOR_URL
    )
    graph = orrery_module(tmp_path, context, collector_origin=CLOUD_RUN_COLLECTOR_URL)
    hugging_face = submitting_huggingface_module(
        tmp_path, context, collector_origin=CLOUD_RUN_COLLECTOR_URL
    )
    telemetry.initialize()
    try:
        graph.initialize()
        try:
            hugging_face.initialize()
            try:
                shared_origin = context.services["collector_session"].origin
                assert shared_origin == CLOUD_RUN_COLLECTOR_URL
                assert telemetry.delivery.collector_url == shared_origin
                assert hugging_face.collector_delivery is not None
                assert hugging_face.collector_delivery.client.origin == shared_origin
                assert submission_session_origins == [shared_origin]
            finally:
                hugging_face.close(0)
        finally:
            graph.close(0)
    finally:
        telemetry.close(0)


@pytest.mark.parametrize(
    ("settings", "error"),
    [
        (
            {"collector_origin": "https://untrusted.example"},
            UNTRUSTED_COLLECTOR_ORIGIN_ERROR,
        ),
        (
            {
                "collector_origin": CLOUD_RUN_COLLECTOR_URL,
                "development_collector_url": DEVELOPMENT_COLLECTOR_URL,
            },
            CONFLICTING_COLLECTOR_ORIGIN_ERROR,
        ),
        ({"collector_origin": True}, COLLECTOR_SETTING_TYPE_ERROR),
        ({"collector_origin": [CLOUD_RUN_COLLECTOR_URL]}, COLLECTOR_SETTING_TYPE_ERROR),
        ({"development_collector_url": 1}, COLLECTOR_SETTING_TYPE_ERROR),
    ],
    ids=[
        "untrusted origin",
        "origin and development URL",
        "origin as a boolean",
        "origin as a list",
        "development URL as a number",
    ],
)
def test_collector_submissions_refuse_an_unusable_origin_at_start(
    tmp_path, capsys, submission_session_origins, settings, error
) -> None:
    module = submitting_huggingface_module(
        tmp_path, ModuleContext(os.getpid()), **settings
    )
    module.initialize()
    try:
        # A collector setting never stops the Hugging Face module: it turns
        # collector submissions off and keeps the Hugging Face delivery.
        assert module.collector_delivery is None
        assert module.delivery is not None
        assert submission_session_origins == []
    finally:
        module.close(0)
    assert "collector submissions are off" in capsys.readouterr().err
    with pytest.raises(ValueError, match=re.escape(error)):
        configured_collector_origin(settings)


@pytest.mark.parametrize(
    ("variables", "named_variable"),
    [
        (
            given_token_variables(PRODUCTION_COLLECTOR_URL),
            "MICROCOSM_COLLECTOR_ORIGIN",
        ),
        (
            {"MICROCOSM_COLLECTOR_TOKEN": GIVEN_COLLECTOR_TOKEN},
            "MICROCOSM_COLLECTOR_TOKEN_EXPIRES_AT",
        ),
        (
            {
                **given_token_variables(DEVELOPMENT_COLLECTOR_URL),
                "MICROCOSM_COLLECTOR_TOKEN_EXPIRES_AT": "tomorrow",
            },
            "MICROCOSM_COLLECTOR_TOKEN_EXPIRES_AT",
        ),
        (
            {
                **given_token_variables(DEVELOPMENT_COLLECTOR_URL),
                "MICROCOSM_COLLECTOR_TOKEN_EXPIRES_AT": "2026-10-29T06:00:00",
            },
            "MICROCOSM_COLLECTOR_TOKEN_EXPIRES_AT",
        ),
        (
            given_token_variables(
                DEVELOPMENT_COLLECTOR_URL, token=f"{GIVEN_COLLECTOR_TOKEN}\nsecond-part"
            ),
            "MICROCOSM_COLLECTOR_TOKEN",
        ),
    ],
    ids=[
        "token for another collector",
        "incomplete variables",
        "unreadable expiry",
        "expiry without a time zone",
        "token with a line break",
    ],
)
def test_an_unusable_given_token_stops_the_emitter_at_start(
    tmp_path, monkeypatch, capsys, variables, named_variable
) -> None:
    # The origin variable is only compared: each module still resolves its
    # own origin. The host stops at the first module whose start fails, so
    # whichever of these comes first says why, before opening its database.
    set_variables(monkeypatch, variables)
    for module, database in (
        (
            telemetry_module(
                tmp_path,
                ModuleContext(os.getpid()),
                development_collector_url=DEVELOPMENT_COLLECTOR_URL,
            ),
            "events.sqlite3",
        ),
        (
            orrery_module(
                tmp_path,
                ModuleContext(os.getpid()),
                development_collector_url=DEVELOPMENT_COLLECTOR_URL,
            ),
            "graphs.sqlite3",
        ),
    ):
        with pytest.raises(ValueError, match=named_variable) as refusal:
            module.initialize()

        error = capsys.readouterr().err
        assert "The Microcosm emitter cannot start" in error
        assert named_variable in error
        assert GIVEN_COLLECTOR_TOKEN not in error + str(refusal.value)
        assert not (tmp_path / database).exists()


def test_orrery_sends_a_given_token_only_to_the_collector_and_ends_it_alone(
    tmp_path, monkeypatch
) -> None:
    set_variables(monkeypatch, given_token_variables(DEVELOPMENT_COLLECTOR_URL))
    context = ModuleContext(os.getpid())
    telemetry = telemetry_module(
        tmp_path, context, development_collector_url=DEVELOPMENT_COLLECTOR_URL
    )
    # The Orrery module gets its credential from the collector session, and
    # sends it to its publication origin: by default another service.
    (tmp_path / "default").mkdir()
    (tmp_path / "collector").mkdir()
    publishing_elsewhere = orrery_module(
        tmp_path / "default",
        context,
        development_collector_url=DEVELOPMENT_COLLECTOR_URL,
    )
    publishing_to_the_collector = orrery_module(
        tmp_path / "collector",
        context,
        development_collector_url=DEVELOPMENT_COLLECTOR_URL,
        publication_origin=DEVELOPMENT_COLLECTOR_URL,
    )
    modules = (telemetry, publishing_elsewhere, publishing_to_the_collector)
    for module in modules:
        module.initialize()
    try:
        # Withheld on purpose, which Orrery's jobs record as
        # `credential_withheld_from_destination`, not as a missing login.
        assert publishing_elsewhere.delivery.credential() == (
            "withheld_from_destination",
            None,
        )
        credential = publishing_to_the_collector.delivery.credential()
        assert credential == ("ok", GIVEN_COLLECTOR_TOKEN)
        # Printed, the session's answer and its given token leave the token out.
        printed = repr(credential) + repr(telemetry.delivery.session.given_token)
        assert GIVEN_COLLECTOR_TOKEN not in printed

        # Orrery received a 401: the token ends for Orrery, not for telemetry.
        publishing_to_the_collector.delivery.invalidate_credential()

        assert publishing_to_the_collector.delivery.credential() == (
            "token_rejected",
            None,
        )
        assert telemetry.delivery.session.credential() == (
            "ok",
            GIVEN_COLLECTOR_TOKEN,
        )
    finally:
        for module in modules:
            module.close(time.monotonic())
