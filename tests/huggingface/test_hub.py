"""The emitter's Hugging Face repository calls, ported from Microcosm's storage."""

import pytest
from fake_hugging_face import (
    EntryNotFoundError,
    FakeHubHTTPError,
    FakeHuggingFaceApi,
    RevisionNotFoundError,
)

from microcosm_emitter.huggingface.service.hub import (
    DatasetRepository,
    add_operation,
    hugging_face_api,
    is_absent,
    is_parent_conflict,
)

REPOSITORY = "policyengine/fake-staging"


@pytest.fixture
def hub():
    api = FakeHuggingFaceApi()
    api.create_repository(REPOSITORY, {"README.md": b"fixture"})
    return api


def test_commit_writes_every_operation_in_one_commit_and_returns_its_revision(
    hub, tmp_path
):
    local = tmp_path / "progress.json"
    local.write_bytes(b'{"stage":"created"}')
    repository = DatasetRepository(REPOSITORY, hub)
    revision = repository.commit(
        [
            add_operation("runs/run-1/progress.json", local),
            add_operation("runs/run-1/events.ndjson", b"{}\n"),
        ],
        message="Stage run files",
    )
    (commit,) = hub.commits(REPOSITORY)
    assert commit.oid == revision == repository.head_revision()
    assert commit.added == {
        "runs/run-1/progress.json": b'{"stage":"created"}',
        "runs/run-1/events.ndjson": b"{}\n",
    }
    assert commit.message == "Stage run files"


def test_commit_on_a_stale_parent_is_a_conflict(hub):
    repository = DatasetRepository(REPOSITORY, hub)
    stale = repository.head_revision()
    hub.write_directly(REPOSITORY, {"other.json": b"{}"})
    with pytest.raises(FakeHubHTTPError) as conflict:
        repository.commit(
            [add_operation("a.json", b"{}")], message="m", parent_commit=stale
        )
    assert is_parent_conflict(conflict.value)
    assert not is_parent_conflict(FakeHubHTTPError(500))
    assert not is_parent_conflict(ValueError("other"))


def test_downloads_read_exact_bytes_at_a_revision(hub):
    repository = DatasetRepository(REPOSITORY, hub)
    first = repository.head_revision()
    hub.write_directly(REPOSITORY, {"README.md": b"changed"})
    assert repository.download("README.md") == b"changed"
    assert repository.download("README.md", revision=first) == b"fixture"
    assert repository.file_exists("README.md")
    assert not repository.file_exists("missing.json")
    assert repository.list_files(revision=first) == ["README.md"]
    with pytest.raises(EntryNotFoundError) as missing:
        repository.download("missing.json")
    assert is_absent(missing.value)
    # A missing revision is not a missing file: the cycle is retried.
    assert not is_absent(RevisionNotFoundError())
    assert not is_absent(FakeHubHTTPError(503))


def real_http_error(error_class, status_code):
    """Build a huggingface_hub HTTP error with a response; nothing is sent."""
    import httpx

    request = httpx.Request("GET", "https://hub.invalid/runs.json")
    return error_class(
        "synthetic", response=httpx.Response(status_code, request=request)
    )


def test_only_the_hub_answering_404_for_the_file_counts_as_absent():
    """Only the Hub answering 404 for the file counts as the file being absent.

    ``LocalEntryNotFoundError`` subclasses ``EntryNotFoundError`` and
    ``FileNotFoundError``, but huggingface_hub raises it when the Hub did not
    answer: a connection error, a timeout or a 429 or 5xx response for a file
    that is not in the local cache.
    """
    from huggingface_hub import errors

    assert is_absent(real_http_error(errors.RemoteEntryNotFoundError, 404))
    assert not is_absent(errors.LocalEntryNotFoundError("connection error"))
    assert not is_absent(real_http_error(errors.RevisionNotFoundError, 404))
    assert not is_absent(real_http_error(errors.RepositoryNotFoundError, 404))
    assert not is_absent(real_http_error(errors.HfHubHTTPError, 503))
    assert not is_absent(real_http_error(errors.HfHubHTTPError, 429))
    assert not is_absent(FileNotFoundError("local cache file vanished"))


def test_last_commit_names_the_commit_that_last_changed_a_path(hub):
    repository = DatasetRepository(REPOSITORY, hub)
    changed = hub.write_directly(REPOSITORY, {"a.json": b"1"})
    hub.write_directly(REPOSITORY, {"b.json": b"2"})
    assert repository.last_commit("a.json") == changed
    assert repository.last_commit("never.json") is None


@pytest.mark.parametrize(
    ("access_token", "can_write"),
    [
        ({"role": "read"}, False),
        ({"role": "write"}, True),
        ({"role": "fineGrained", "fineGrained": {"global": ["repo.write"]}}, True),
        (
            {
                "role": "fineGrained",
                "fineGrained": {
                    "scoped": [
                        {
                            "entity": {"name": REPOSITORY, "type": "dataset"},
                            "permissions": ["repo.write"],
                        }
                    ]
                },
            },
            True,
        ),
        (
            {
                "role": "fineGrained",
                "fineGrained": {
                    "scoped": [
                        {
                            "entity": {"name": "policyengine", "type": "org"},
                            "permissions": ["repo.write"],
                        }
                    ]
                },
            },
            True,
        ),
        (
            {
                "role": "fineGrained",
                "fineGrained": {
                    "scoped": [
                        {
                            "entity": {"name": "someone-else", "type": "org"},
                            "permissions": ["repo.write"],
                        }
                    ]
                },
            },
            False,
        ),
        ({"role": "something-new"}, None),
    ],
    ids=[
        "read",
        "write",
        "fine-grained-global",
        "fine-grained-repository",
        "fine-grained-owner",
        "fine-grained-elsewhere",
        "unknown-role",
    ],
)
def test_write_scope_is_read_from_the_hub_description_of_the_token(
    access_token, can_write
):
    api = FakeHuggingFaceApi(whoami={"auth": {"accessToken": access_token}})
    repository = DatasetRepository(REPOSITORY, api)
    assert repository.credential_can_write() is can_write
    assert repository.credential_role() == access_token["role"]


def test_write_scope_is_unknown_when_the_hub_does_not_describe_the_token():
    api = FakeHuggingFaceApi(whoami={"name": "fixture"})
    assert DatasetRepository(REPOSITORY, api).credential_can_write() is None


@pytest.mark.parametrize(
    ("variables", "saved_login", "expected_token"),
    [
        (
            {"HF_TOKEN": "hf_fake_first", "HUGGINGFACE_TOKEN": "hf_fake_second"},
            "hf_fake_saved",
            "hf_fake_first",
        ),
        ({"HUGGINGFACE_TOKEN": "hf_fake_second"}, "hf_fake_saved", "hf_fake_second"),
        ({}, "hf_fake_saved", "hf_fake_saved"),
        ({}, None, None),
    ],
    ids=["hf-token", "huggingface-token", "saved-login", "none"],
)
def test_uploads_use_the_build_credential_found_in_the_core_auth_order(
    monkeypatch, variables, saved_login, expected_token
):
    import os

    for name, value in variables.items():
        monkeypatch.setenv(name, value)
    if saved_login is not None:
        token_path = os.environ["HF_TOKEN_PATH"]
        os.makedirs(os.path.dirname(token_path), exist_ok=True)
        with open(token_path, "w") as token_file:
            token_file.write(saved_login)
    api = hugging_face_api()
    if expected_token is None:
        assert api is None
    else:
        assert api.token == expected_token
