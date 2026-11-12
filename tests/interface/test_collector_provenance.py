"""The optional build description a bundle or publication carries for the collector.

Also the Hugging Face setting that switches collector submissions on.
"""

import hashlib
import re
from pathlib import Path

import pytest

from microcosm_emitter_interface.bundles import BundleFile, StageDatasetBundle
from microcosm_emitter_interface.identifiers import SAFE_IDENTIFIER
from microcosm_emitter_interface.publication import PublishRelease, ReleaseFile
from microcosm_emitter_interface.staging import HuggingFaceSettings
from microcosm_emitter_interface.submissions import (
    EXECUTION_OVERLAY_ROLE,
    ORRERY_EVIDENCE_ROLE,
    ORRERY_SCHEMA_ROLE,
    ORRERY_UPSTREAM_ROLE,
    CollectorProvenance,
)


def bundle_files() -> tuple[BundleFile, ...]:
    def file(name: str, role: str) -> BundleFile:
        return BundleFile(
            name=name,
            role=role,
            local_path=Path("/builds/uk") / name,
            sha256=hashlib.sha256(name.encode()).hexdigest(),
            bytes=10,
        )

    return (
        file("microcosm_uk_2024_25.h5", "dataset"),
        file("calibration_diagnostics.json", "calibration_diagnostics"),
        file("staged_manifest.json", "staged_manifest"),
    )


RELEASE = "microcosm-uk-2024-25-national"
BUILD = "uk-frs-calibration-attempt-20261019T083323Z-6cba5d71"


def release_files() -> tuple[ReleaseFile, ...]:
    def file(name: str, role: str) -> ReleaseFile:
        return ReleaseFile(
            name=name,
            role=role,
            local_path=Path("/builds/uk") / name,
            sha256=hashlib.sha256(name.encode()).hexdigest(),
            bytes=10,
        )

    return (
        file("release_manifest.json", "release_manifest"),
        file("microcosm_uk_2024_25.h5", "dataset"),
        file("calibration_diagnostics.json", "calibration_diagnostics"),
    )


def provenance(**changes: object) -> CollectorProvenance:
    values: dict[str, object] = {
        "country_code": "UK",
        "build_id": "uk-frs-calibration-attempt-20261019T083323Z-6cba5d71",
        "licensed_data": True,
    }
    values.update(changes)
    return CollectorProvenance(**values)


def test_provenance_describes_the_build_for_the_collector() -> None:
    described = provenance(release="microcosm-uk-2024-25-national")

    assert described.message() == {
        "country_code": "UK",
        "build_id": "uk-frs-calibration-attempt-20261019T083323Z-6cba5d71",
        "licensed_data": True,
        "release": "microcosm-uk-2024-25-national",
    }


def test_a_staged_build_has_no_release() -> None:
    assert provenance().message()["release"] is None


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"country_code": "uk"}, "country_code must be two upper-case letters."),
        ({"country_code": "GBR"}, "country_code must be two upper-case letters."),
        ({"build_id": "../attempt"}, f"build_id must match {SAFE_IDENTIFIER.pattern}."),
        ({"licensed_data": "true"}, "licensed_data must be True or False."),
        ({"licensed_data": 1}, "licensed_data must be True or False."),
        ({"release": ""}, f"release must match {SAFE_IDENTIFIER.pattern}."),
    ],
)
def test_provenance_refuses_values_the_collector_would_refuse(
    changes: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=re.escape(message)):
        provenance(**changes)


def test_a_bundle_without_provenance_keeps_its_message_and_identifier() -> None:
    bundle = StageDatasetBundle(
        repository="policyengine/populace-uk-staging",
        run_id="uk-frs-calibration-attempt-1",
        files=bundle_files(),
    )

    assert "collector" not in bundle.message()


def test_a_bundle_with_provenance_names_it_in_its_message() -> None:
    plain = StageDatasetBundle(
        repository="policyengine/populace-uk-staging",
        run_id="uk-frs-calibration-attempt-1",
        files=bundle_files(),
    )
    described = StageDatasetBundle(
        repository="policyengine/populace-uk-staging",
        run_id="uk-frs-calibration-attempt-1",
        files=bundle_files(),
        collector=provenance(),
    )

    assert described.message()["collector"] == provenance().message()
    assert described.handover_id() != plain.handover_id()


def test_a_bundle_refuses_provenance_of_another_type() -> None:
    with pytest.raises(
        ValueError, match=re.escape("collector must be a CollectorProvenance value.")
    ):
        StageDatasetBundle(
            repository="policyengine/populace-uk-staging",
            run_id="uk-frs-calibration-attempt-1",
            files=bundle_files(),
            collector={"country_code": "UK"},  # type: ignore[arg-type]
        )


def test_orrery_input_roles_are_valid_bundle_roles() -> None:
    roles = [
        ORRERY_SCHEMA_ROLE,
        EXECUTION_OVERLAY_ROLE,
        ORRERY_UPSTREAM_ROLE,
        ORRERY_EVIDENCE_ROLE,
    ]
    files = [
        BundleFile(
            name=f"file-{index}.json",
            role=role,
            local_path=Path("/builds/uk") / f"file-{index}.json",
            sha256="a" * 64,
            bytes=1,
        )
        for index, role in enumerate(roles)
    ]

    assert [file.role for file in files] == [
        "orrery_schema",
        "execution_overlay",
        "orrery_upstream",
        "orrery_evidence",
    ]


def publication(**changes: object) -> PublishRelease:
    values: dict[str, object] = {
        "repository": "policyengine/populace-uk-private",
        "release": RELEASE,
        "build": BUILD,
        "release_role": "national_default",
        "make_current": True,
        "files": release_files(),
        "request_id": "release-request-1",
    }
    values.update(changes)
    return PublishRelease(**values)


def test_a_publication_without_provenance_keeps_its_message() -> None:
    assert "collector" not in publication().message()


def test_a_publication_with_provenance_names_it_in_its_message() -> None:
    described = publication(collector=provenance(build_id=BUILD, release=RELEASE))

    assert described.message() == {
        **publication().message(),
        "collector": provenance(build_id=BUILD, release=RELEASE).message(),
    }
    assert described.handover_id() != publication().handover_id()


def test_a_publication_refuses_provenance_of_another_type() -> None:
    with pytest.raises(
        ValueError, match=re.escape("collector must be a CollectorProvenance value.")
    ):
        publication(collector={"country_code": "UK"})


@pytest.mark.parametrize(
    "other",
    [
        {"release": None},
        {"release": "microcosm-uk-2024-25-other"},
        {"build_id": "uk-frs-calibration-attempt-another"},
    ],
    ids=["no release", "another release", "another build"],
)
def test_a_publication_refuses_provenance_of_another_release_or_build(
    other: dict[str, object],
) -> None:
    """The collector refuses a release folder whose provenance names another one."""
    values: dict[str, object] = {"build_id": BUILD, "release": RELEASE, **other}
    with pytest.raises(
        ValueError,
        match=re.escape("collector must name the publication's own release and build."),
    ):
        publication(collector=provenance(**values))


def test_collector_submissions_are_off_unless_switched_on(tmp_path: Path) -> None:
    event_queue_path = tmp_path / "events.sqlite3"

    assert "collector_submissions" not in HuggingFaceSettings().configuration(
        event_queue_path
    )
    assert HuggingFaceSettings(collector_submissions=True).configuration(
        event_queue_path
    ) == {
        "handover_database_path": str(tmp_path / "events.handovers.sqlite3"),
        "collector_submissions": True,
    }


def test_a_development_collector_url_is_passed_only_when_set(tmp_path: Path) -> None:
    event_queue_path = tmp_path / "events.sqlite3"
    settings = HuggingFaceSettings(
        collector_submissions=True, development_collector_url="http://127.0.0.1:8080"
    )

    assert settings.configuration(event_queue_path)["development_collector_url"] == (
        "http://127.0.0.1:8080"
    )
    assert "development_collector_url" not in HuggingFaceSettings(
        collector_submissions=True
    ).configuration(event_queue_path)


@pytest.mark.parametrize("switch", ["true", 1, None])
def test_the_collector_switch_must_be_a_boolean(switch: object) -> None:
    with pytest.raises(
        ValueError, match=re.escape("collector_submissions must be True or False.")
    ):
        HuggingFaceSettings(collector_submissions=switch)  # type: ignore[arg-type]


@pytest.mark.parametrize("url", [1, b"http://127.0.0.1:8080", ["http://127.0.0.1"]])
def test_a_development_collector_url_must_be_a_string(url: object) -> None:
    with pytest.raises(
        ValueError, match=re.escape("development_collector_url must be a string.")
    ):
        HuggingFaceSettings(development_collector_url=url)  # type: ignore[arg-type]
