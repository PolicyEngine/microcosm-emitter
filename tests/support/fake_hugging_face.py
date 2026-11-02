"""An in-memory Hugging Face Hub for tests: no network, no real repositories.

It implements the ``HfApi`` methods the emitter calls, with the Hub's
observable behaviour where the emitter depends on it: ``create_commit`` with a
``parent_commit`` that is no longer the head fails with HTTP 412, missing files
and revisions raise errors named like ``huggingface_hub``'s, and every commit
records its exact operations.
"""

from __future__ import annotations

import hashlib
import tempfile
import threading
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

STAGING_REPOSITORY = "policyengine/fixture-staging"
PRIVATE_REPOSITORY = "policyengine/fixture-private"


class FakeHubHTTPError(OSError):
    """Carries ``response.status_code`` like ``huggingface_hub.HfHubHTTPError``."""

    def __init__(self, status_code: int, message: str = "fake Hub error") -> None:
        super().__init__(message)
        self.response = SimpleNamespace(status_code=status_code)


class RevisionNotFoundError(FakeHubHTTPError):
    def __init__(self, message: str = "revision not found") -> None:
        super().__init__(404, message)


class EntryNotFoundError(FakeHubHTTPError):
    def __init__(self, message: str = "entry not found") -> None:
        super().__init__(404, message)


class RepositoryNotFoundError(FakeHubHTTPError):
    def __init__(self, message: str = "repository not found") -> None:
        super().__init__(404, message)


@dataclass
class FakeCommit:
    oid: str
    parent: str | None
    message: str
    files: dict[str, bytes]
    added: dict[str, bytes]
    #: The Hub's date for the commit, in seconds; ``None`` when undated.
    date: float | None = None


@dataclass
class FakeRepository:
    commits: list[FakeCommit] = field(default_factory=list)
    tags: dict[str, str] = field(default_factory=dict)
    branches: dict[str, str] = field(default_factory=dict)

    @property
    def head(self) -> FakeCommit:
        return self.commits[-1]


def _content(path_or_fileobj: Any) -> bytes:
    if isinstance(path_or_fileobj, bytes):
        return path_or_fileobj
    if isinstance(path_or_fileobj, (str, Path)):
        return Path(path_or_fileobj).read_bytes()
    return path_or_fileobj.read()


class FakeHuggingFaceApi:
    """The subset of ``HfApi`` the emitter uses, backed by dictionaries."""

    def __init__(self, *, whoami: Mapping[str, Any] | None = None) -> None:
        self.repositories: dict[str, FakeRepository] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.whoami_response = whoami
        self._failures: dict[str, list[BaseException]] = {}
        self._before_commit: list[Callable[[], None]] = []
        self._after_commit_failures: list[BaseException] = []
        self._lock = threading.RLock()
        self._download_directory = Path(tempfile.mkdtemp(prefix="fake-hub-"))
        self._sequence = 0
        #: The Hub's own clock. When set, each commit is dated with it and
        #: ``get_paths_info`` reports that date; otherwise commits are undated.
        self.commit_clock: Callable[[], float] | None = None

    # Test controls -------------------------------------------------------

    def create_repository(
        self, repository: str, files: Mapping[str, bytes] | None = None
    ) -> str:
        with self._lock:
            self.repositories[repository] = FakeRepository()
            return self._append_commit(
                repository, dict(files or {}), "Initial commit", parent=None
            )

    def fail_next(self, method: str, error: BaseException, *, times: int = 1) -> None:
        """Raise ``error`` from the next ``times`` calls of ``method``."""
        self._failures.setdefault(method, []).extend([error] * times)

    def fail_after_next_commit(self, error: BaseException) -> None:
        """Let the next commit land, then raise ``error`` as if it had not."""
        self._after_commit_failures.append(error)

    def run_before_next_commit(self, action: Callable[[], None]) -> None:
        """Run ``action`` (for example another writer's commit) first."""
        self._before_commit.append(action)

    def files(self, repository: str, revision: str | None = None) -> dict[str, bytes]:
        return dict(self._commit_at(repository, revision).files)

    def commits(self, repository: str) -> list[FakeCommit]:
        return list(self.repositories[repository].commits[1:])

    def write_directly(
        self, repository: str, files: Mapping[str, bytes], message: str = "Direct"
    ) -> str:
        """Another writer's commit, outside the code under test."""
        with self._lock:
            head = self.repositories[repository].head
            return self._append_commit(
                repository, {**head.files, **files}, message, parent=head.oid
            )

    def delete_directly(
        self, repository: str, paths: Iterable[str], message: str = "Delete"
    ) -> str:
        """Another writer's commit that deletes ``paths``."""
        with self._lock:
            head = self.repositories[repository].head
            removed = set(paths)
            return self._append_commit(
                repository,
                {
                    path: content
                    for path, content in head.files.items()
                    if path not in removed
                },
                message,
                parent=head.oid,
            )

    # HfApi surface -------------------------------------------------------

    def _record(self, method: str, **arguments: Any) -> None:
        self.calls.append((method, arguments))
        pending = self._failures.get(method)
        if pending:
            raise pending.pop(0)

    def _repository(self, repo_id: str) -> FakeRepository:
        repository = self.repositories.get(repo_id)
        if repository is None:
            raise RepositoryNotFoundError(repo_id)
        return repository

    def _commit_at(self, repo_id: str, revision: str | None) -> FakeCommit:
        repository = self._repository(repo_id)
        if revision in (None, "main"):
            return repository.head
        if revision in repository.tags:
            revision = repository.tags[revision]
        if revision in repository.branches:
            revision = repository.branches[revision]
        for commit in repository.commits:
            if commit.oid == revision:
                return commit
        raise RevisionNotFoundError(str(revision))

    def _append_commit(
        self,
        repository: str,
        files: dict[str, bytes],
        message: str,
        *,
        parent: str | None,
        added: dict[str, bytes] | None = None,
    ) -> str:
        self._sequence += 1
        oid = hashlib.sha1(f"{repository}:{self._sequence}".encode()).hexdigest()
        date = None if self.commit_clock is None else self.commit_clock()
        self.repositories[repository].commits.append(
            FakeCommit(oid, parent, message, files, dict(added or {}), date)
        )
        return oid

    def repo_info(
        self, repo_id: str, *, repo_type: str | None = None, revision=None, **_: Any
    ):
        self._record("repo_info", repo_id=repo_id, revision=revision)
        return SimpleNamespace(sha=self._commit_at(repo_id, revision).oid)

    def create_commit(
        self,
        repo_id: str,
        operations: Iterable[Any],
        *,
        commit_message: str,
        repo_type: str | None = None,
        revision: str | None = None,
        parent_commit: str | None = None,
        **_: Any,
    ):
        operations = list(operations)
        self._record(
            "create_commit",
            repo_id=repo_id,
            operations=[operation.path_in_repo for operation in operations],
            commit_message=commit_message,
            revision=revision,
            parent_commit=parent_commit,
        )
        while self._before_commit:
            self._before_commit.pop(0)()
        with self._lock:
            repository = self._repository(repo_id)
            head = repository.head
            if parent_commit is not None and parent_commit != head.oid:
                raise FakeHubHTTPError(412, "A commit has happened since.")
            added = {
                operation.path_in_repo: _content(operation.path_or_fileobj)
                for operation in operations
            }
            oid = self._append_commit(
                repo_id,
                {**head.files, **added},
                commit_message,
                parent=head.oid,
                added=added,
            )
        if self._after_commit_failures:
            raise self._after_commit_failures.pop(0)
        return SimpleNamespace(oid=oid, commit_url=f"https://fake/{oid}")

    def hf_hub_download(
        self,
        repo_id: str,
        filename: str,
        *,
        repo_type: str | None = None,
        revision: str | None = None,
        **_: Any,
    ) -> str:
        self._record(
            "hf_hub_download", repo_id=repo_id, filename=filename, revision=revision
        )
        files = self._commit_at(repo_id, revision).files
        if filename not in files:
            raise EntryNotFoundError(filename)
        target = self._download_directory / uuid_name(filename)
        target.write_bytes(files[filename])
        return str(target)

    def file_exists(
        self,
        repo_id: str,
        filename: str,
        *,
        repo_type: str | None = None,
        revision: str | None = None,
        **_: Any,
    ) -> bool:
        self._record("file_exists", repo_id=repo_id, filename=filename)
        return filename in self._commit_at(repo_id, revision).files

    def list_repo_files(
        self, repo_id: str, *, revision: str | None = None, **_: Any
    ) -> list[str]:
        self._record("list_repo_files", repo_id=repo_id, revision=revision)
        return sorted(self._commit_at(repo_id, revision).files)

    def get_paths_info(
        self, repo_id: str, paths, *, expand: bool = False, revision=None, **_: Any
    ):
        self._record("get_paths_info", repo_id=repo_id, paths=list(paths))
        repository = self._repository(repo_id)
        entries = []
        for path in paths:
            last = None
            previous = None
            for commit in repository.commits:
                content = commit.files.get(path)
                if content is not None and content != previous:
                    last = commit
                previous = content
            if last is not None:
                entries.append(
                    SimpleNamespace(
                        path=path,
                        last_commit=SimpleNamespace(
                            oid=last.oid,
                            title=last.message.split("\n", 1)[0],
                            date=(
                                None
                                if last.date is None
                                else datetime.fromtimestamp(last.date, UTC)
                            ),
                        ),
                    )
                )
        return entries

    def whoami(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self._record("whoami")
        if self.whoami_response is None:
            raise FakeHubHTTPError(401, "no credential")
        return dict(self.whoami_response)

    def create_tag(self, repo_id: str, **arguments: Any) -> None:
        self._record("create_tag", repo_id=repo_id, **arguments)
        error_message = "The emitter must never create a Hugging Face tag."
        raise AssertionError(error_message)

    def create_branch(self, repo_id: str, **arguments: Any) -> None:
        self._record("create_branch", repo_id=repo_id, **arguments)
        error_message = "The emitter must never create a Hugging Face branch."
        raise AssertionError(error_message)


def uuid_name(filename: str) -> str:
    return f"{uuid.uuid4().hex}-{Path(filename).name}"
