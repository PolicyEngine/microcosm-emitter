"""The optional build description a bundle carries for the collector."""

import hashlib
from pathlib import Path

import pytest

from microcosm_emitter_interface.bundles import BundleFile, StageDatasetBundle
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
    "changes",
    [
        {"country_code": "uk"},
        {"country_code": "GBR"},
        {"build_id": "../attempt"},
        {"licensed_data": "true"},
        {"licensed_data": 1},
        {"release": ""},
    ],
)
def test_provenance_refuses_values_the_collector_would_refuse(
    changes: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
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
    with pytest.raises(ValueError):
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
