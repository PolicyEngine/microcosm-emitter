"""Which collector submissions a staged bundle produces, and their contents."""

import hashlib

from microcosm_emitter.submissions.planning import (
    planned_submissions,
    wants_collector_submission,
)

REVISION = "0123456789abcdef0123456789abcdef01234567"


def entry(name: str, role: str, content: bytes = b"bytes") -> dict:
    return {
        "name": name,
        "role": role,
        "local_path": f"/builds/uk/{name}",
        "sha256": hashlib.sha256(content).hexdigest(),
        "bytes": len(content),
    }


def bundle_request(files: list[dict], *, collector: dict | None = None) -> dict:
    request = {
        "protocol_version": 1,
        "command": "stage_dataset_bundle",
        "repository": "policyengine/populace-uk-staging",
        "run_id": "uk-frs-calibration-attempt-1",
        "prefix": "staged",
        "files": files,
        "commit_message": None,
    }
    if collector is not None:
        request["collector"] = collector
    return request


PROVENANCE = {
    "country_code": "UK",
    "build_id": "uk-frs-calibration-attempt-1",
    "licensed_data": True,
    "release": None,
}
SIDECARS = [
    entry("microcosm_uk_2024_25.h5", "dataset"),
    entry("staged_manifest.json", "staged_manifest"),
    entry("sha256sums.txt", "checksums"),
]


def test_a_bundle_naming_diagnostics_gives_a_calibration_submission() -> None:
    request = bundle_request(
        [
            *SIDECARS,
            entry("calibration_diagnostics.json", "calibration_diagnostics"),
            entry("rowwise_candidate_manifest.json", "build_manifest"),
        ],
        collector=PROVENANCE,
    )

    [planned] = planned_submissions(request, REVISION)

    assert planned.kind == "calibration-diagnostics"
    assert planned.body == {
        "contract_version": 1,
        "kind": "calibration-diagnostics",
        "licensed_data": True,
        "provenance": {
            "run_id": "uk-frs-calibration-attempt-1",
            "build_id": "uk-frs-calibration-attempt-1",
            "country_code": "UK",
            "release": None,
            "huggingface": {
                "repo": "policyengine/populace-uk-staging",
                "commit": REVISION,
                "folder": "staged/uk-frs-calibration-attempt-1",
            },
        },
        "files": [
            {
                "name": "calibration_diagnostics.json",
                "role": "calibration_diagnostics",
                "size": 5,
                "sha256": hashlib.sha256(b"bytes").hexdigest(),
            },
            {
                "name": "rowwise_candidate_manifest.json",
                "role": "build_manifest",
                "size": 5,
                "sha256": hashlib.sha256(b"bytes").hexdigest(),
            },
        ],
    }
    assert planned.local_paths == {
        "calibration_diagnostics.json": "/builds/uk/calibration_diagnostics.json",
        "rowwise_candidate_manifest.json": "/builds/uk/rowwise_candidate_manifest.json",
    }


def test_orrery_inputs_are_submitted_with_the_collectors_role_names() -> None:
    request = bundle_request(
        [
            *SIDECARS,
            entry("graph.schema.json", "orrery_schema"),
            entry("graph.execution.json", "execution_overlay"),
            entry("upstream-" + "a" * 64 + ".json", "orrery_upstream"),
            entry("evidence-preflight-graph.json", "orrery_evidence"),
        ],
        collector=PROVENANCE,
    )

    [planned] = planned_submissions(request, REVISION)

    assert planned.kind == "orrery-inputs"
    assert [(file["name"], file["role"]) for file in planned.body["files"]] == [
        ("evidence-preflight-graph.json", "evidence"),
        ("graph.execution.json", "execution_overlay"),
        ("graph.schema.json", "schema"),
        ("upstream-" + "a" * 64 + ".json", "upstream"),
    ]


def test_a_bundle_naming_both_gives_one_submission_of_each_kind() -> None:
    request = bundle_request(
        [
            *SIDECARS,
            entry("calibration_diagnostics.json", "calibration_diagnostics"),
            entry("graph.schema.json", "orrery_schema"),
            entry("graph.execution.json", "execution_overlay"),
        ],
        collector={**PROVENANCE, "release": "microcosm-uk-2024-25-national"},
    )

    planned = planned_submissions(request, REVISION)

    assert [submission.kind for submission in planned] == [
        "calibration-diagnostics",
        "orrery-inputs",
    ]
    assert {submission.body["provenance"]["release"] for submission in planned} == {
        "microcosm-uk-2024-25-national"
    }


def test_orrery_inputs_need_both_the_schema_and_the_execution_overlay() -> None:
    request = bundle_request(
        [*SIDECARS, entry("graph.schema.json", "orrery_schema")],
        collector=PROVENANCE,
    )

    assert planned_submissions(request, REVISION) == []
    assert wants_collector_submission(request) is False


def test_only_a_bundle_with_provenance_wants_a_collector_submission() -> None:
    files = [
        *SIDECARS,
        entry("calibration_diagnostics.json", "calibration_diagnostics"),
    ]

    assert wants_collector_submission(bundle_request(files)) is False
    assert wants_collector_submission(bundle_request(files, collector=PROVENANCE))


def test_a_bundle_with_only_its_dataset_wants_no_submission() -> None:
    assert (
        wants_collector_submission(bundle_request(SIDECARS, collector=PROVENANCE))
        is False
    )
