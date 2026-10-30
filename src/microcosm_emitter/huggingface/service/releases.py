"""The release writer: the only code that changes a repository's ``releases.json``.

The repository is a Hugging Face dataset repository. Each change (publish a
build, make a build current, withdraw a build) is one commit:

1. read ``releases.json`` and the file list at ``main``'s head;
2. apply the change to the record;
3. run the shared checks in ``microcosm_emitter_interface.releases``;
4. commit the new build folder (for a publication) and ``releases.json``
   with ``parent_commit`` set to that head.

If another commit landed first, the Hub refuses the stale parent; the writer
then reads the new head and applies the change again, so concurrent
publications never lose each other's changes. The writer creates no Hugging
Face tags or branches, no ``current/`` folder and no ``latest*.json`` file,
and never touches existing tags or files outside the new build folder.

Who may make a change is not settled; ``ReleaseChangePolicy`` is where that
decision plugs in. The module applies it when a change is handed over, with
the caller's identity, and the default allows every change.

**Order.** A queued change can wait (an outage, a missing credential, backoff)
while newer changes land, from this machine or another one. Order is kept
per release: changes to different releases of one repository cannot undo
each other. So:

- A change still pending ``RELEASE_CHANGE_EXPIRY_SECONDS`` after it was handed
  over is final: ``refused`` with ``REQUEST_EXPIRED``. It never lands days
  later, for example after ``hf auth login``.
- A change that sets ``current`` (a promotion, or a publication that makes
  its build current; see ``sets_current``) is overtaken when such a change
  to the same release, requested after it, has already landed. An overtaken
  promotion is refused with ``NEWER_CHANGE_LANDED``. An overtaken
  publication still adds its build but leaves ``current`` as the newer
  change set it, so two publications that land at once both keep their
  builds and the newer request's build is current (plan item 5).
- The writer knows which changes landed from two places: this machine's
  queue (its own changes that set ``current`` and landed, including ones
  that left the record unchanged), and the title of the commit that last
  changed ``releases.json``, which ends
  ``[newest requests: <release>=<time>; ...]``: for each release, the newest
  request time of the changes applied to it that set ``current``. Every
  writer commit carries those times forward, dropping a release's time once
  it is ``ORDER_WINDOW_SECONDS`` older than the Hub's date for the last
  commit to ``releases.json``, by which point every change requested before
  it has expired.
- A withdrawal, and a publication that only adds a build, never change
  ``current``: they are never overtaken, never count as newer, and add no
  time. A withdrawal cannot conflict with a change to ``current`` either:
  ``withdraw_build`` refuses the current build and ``make_current`` refuses
  a withdrawn one, whichever lands first.

What this cannot see: a newer change on another machine that left
``releases.json`` unchanged (promoting the build that is already current)
makes no commit, so it leaves no time in a commit title; only the expiry
bounds how late an older change can still land after it.

Request times come from each machine's clock, so two machines whose clocks
differ by some amount can still land changes in the wrong order when they
were requested less than that amount apart. Every change is requested before
its commit, so a time in a commit title more than ``CLOCK_TOLERANCE_SECONDS``
after the Hub's date for that commit comes from a clock that runs fast, and
is ignored: such a machine's change is then ordered only by its own queue
and the expiry, but it can neither block later changes to its release on
other machines until real time catches up, nor push other releases' times
out of the titles.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from microcosm_emitter.huggingface.service.hub import (
    MAX_PARENT_CONFLICT_ATTEMPTS,
    PRESERVED_BYTES_CHANGED,
    PRESERVED_FILES_MISSING,
    REPOSITORY_UNAVAILABLE,
    UPLOAD_FAILED,
    DatasetRepository,
    add_operation,
    is_parent_conflict,
)
from microcosm_emitter.huggingface.service.queue import sha256_file
from microcosm_emitter_interface.publication import (
    PromoteBuild,
    PublishRelease,
    ReleaseChange,
)
from microcosm_emitter_interface.releases import (
    RELEASE_MANIFEST_NAME,
    RELEASES_RECORD_PATH,
    ReleaseRecordError,
    ReleasesRecord,
    Violation,
    add_build,
    build_folder,
    check_change,
    check_commit,
    check_repository,
    make_current,
    withdraw_build,
)

PARENT_CONFLICT = "PARENT_CONFLICT"
#: A change to the same release's ``current``, requested after this one, has
#: already landed.
NEWER_CHANGE_LANDED = "NEWER_CHANGE_LANDED"
#: The change was still pending ``RELEASE_CHANGE_EXPIRY_SECONDS`` after it
#: was handed over.
REQUEST_EXPIRED = "REQUEST_EXPIRED"
#: How long a release change may stay pending: long enough for a Hub outage
#: or a few backoffs (at most five minutes apart), short enough that an old
#: request never lands silently after the operator has moved on.
RELEASE_CHANGE_EXPIRY_SECONDS = 3600.0
#: How long a release's time stays in commit titles, counted back from the
#: Hub's date for the last commit to ``releases.json`` (from the newest time
#: listed when the Hub reports no date). A change requested before a dropped
#: time was requested more than this long before that commit, so it has
#: expired, unless its machine's clock is more than the window minus the
#: expiry slow.
ORDER_WINDOW_SECONDS = 2 * RELEASE_CHANGE_EXPIRY_SECONDS
#: How far after the Hub's date for a writer commit a time in its title may
#: be. Only a clock that runs fast records a later one; it is ignored.
CLOCK_TOLERANCE_SECONDS = 300.0
#: Ends the title of a writer commit once any change that sets ``current``
#: has been applied.
_NEWEST_REQUESTS = re.compile(r" \[newest requests: (?P<entries>[^\]]+)\]$")
type ReleaseStatus = Literal["pending", "published", "refused"]


@dataclass(frozen=True)
class ReleaseCaller:
    """Who handed a release change over, as far as the emitter can tell.

    ``user`` is the account the emitter runs as, which is the build's;
    ``parent_pid`` is the build process that started this emitter host. Who
    may promote or withdraw is not settled, so this may gain fields once it
    is.
    """

    user: str
    parent_pid: int


#: Returns ``None`` to allow a change, or a reason to refuse it
#: (``NOT_AUTHORIZED``). It runs when the change is handed over, so a refused
#: change is never queued.
type ReleaseChangePolicy = Callable[[ReleaseChange, ReleaseCaller], str | None]


def allow_every_change(change: ReleaseChange, caller: ReleaseCaller) -> str | None:
    """Allow every change by returning ``None``: the default ``ReleaseChangePolicy``.

    It stays the default until who may change releases is decided.
    """
    return None


@dataclass(frozen=True)
class ReleaseOutcome:
    """One attempt's result: final unless ``retry``.

    ``current`` is set only when ``published``: the release's current build
    in ``releases.json`` once the change was applied (or found applied), or
    ``None`` when the release has no current build.
    """

    status: ReleaseStatus
    error_code: str | None = None
    message: str | None = None
    commit: str | None = None
    retry: bool = False
    committed: bool = False
    current: str | None = None


def _refused(code: str, message: str) -> ReleaseOutcome:
    return ReleaseOutcome("refused", code, message)


def _pending(code: str) -> ReleaseOutcome:
    return ReleaseOutcome("pending", code, retry=True)


def _preserved_files(change: PublishRelease) -> ReleaseOutcome | None:
    for item in change.files:
        try:
            if not item.local_path.is_file():
                return _pending(PRESERVED_FILES_MISSING)
            if (
                item.local_path.stat().st_size != item.bytes
                or sha256_file(item.local_path) != item.sha256
            ):
                return _refused(
                    PRESERVED_BYTES_CHANGED,
                    f"{item.name} changed after it was handed over.",
                )
        except OSError:
            return _pending(PRESERVED_FILES_MISSING)
    return None


def expired_change(requested_at: float, now: float) -> ReleaseOutcome | None:
    """``REQUEST_EXPIRED`` once a change has waited too long; else ``None``."""
    if now - requested_at <= RELEASE_CHANGE_EXPIRY_SECONDS:
        return None
    return _refused(
        REQUEST_EXPIRED,
        "The change did not land within an hour of its request; request it "
        "again if it is still wanted.",
    )


def _request_time_text(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).isoformat(timespec="microseconds")


def _request_time(text: str) -> float | None:
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.timestamp() if parsed.tzinfo is not None else None


def _as_written(seconds: float) -> float:
    """``seconds`` as a commit title records it (to the microsecond)."""
    written = _request_time(_request_time_text(seconds))
    return seconds if written is None else written


def _newest_requests_text(newest_requests: Mapping[str, float]) -> str:
    return "; ".join(
        f"{release}={_request_time_text(seconds)}"
        for release, seconds in sorted(newest_requests.items())
    )


def _parsed_newest_requests(title: str | None) -> dict[str, float]:
    """Each release's newest request time from a writer commit's title."""
    match = _NEWEST_REQUESTS.search(title or "")
    if match is None:
        return {}
    newest_requests: dict[str, float] = {}
    for entry in match["entries"].split("; "):
        release, separator, time_text = entry.partition("=")
        seconds = _request_time(time_text) if separator else None
        if release and seconds is not None:
            newest_requests[release] = seconds
    return newest_requests


def _newest_requests_on_record(
    repository: DatasetRepository, head: str, files: list[str]
) -> tuple[dict[str, float], float | None]:
    """Read the newest request times and the date of the last ``releases.json`` commit.

    The times, one per release, come from the title of the commit that last
    changed ``releases.json``, and the date is the Hub's for that commit. The
    times are empty without a record, or when that commit was not the
    writer's (P1's one-time migration, for example). A time more than
    ``CLOCK_TOLERANCE_SECONDS`` after the Hub's date is left out.
    """
    if RELEASES_RECORD_PATH not in files:
        return {}, None
    title, committed_at = repository.last_commit_title_and_date(
        RELEASES_RECORD_PATH, revision=head
    )
    newest_requests = _parsed_newest_requests(title)
    if committed_at is None:
        return newest_requests, None
    latest_believable = committed_at + CLOCK_TOLERANCE_SECONDS
    return {
        release: seconds
        for release, seconds in newest_requests.items()
        if seconds <= latest_believable
    }, committed_at


def _newest_per_release(*sources: Mapping[str, float]) -> dict[str, float]:
    newest_requests: dict[str, float] = {}
    for source in sources:
        for release, seconds in source.items():
            written = _as_written(seconds)
            newest_requests[release] = max(
                newest_requests.get(release, written), written
            )
    return newest_requests


def _within_order_window(
    newest_requests: Mapping[str, float], record_committed_at: float | None
) -> dict[str, float]:
    """Return the times a commit title still needs (see ``ORDER_WINDOW_SECONDS``).

    The window ends at the Hub's date for the last commit to
    ``releases.json``, so no machine's clock decides which times are dropped;
    without that date, at the newest time listed.
    """
    if not newest_requests:
        return {}
    window_end = (
        max(newest_requests.values())
        if record_committed_at is None
        else record_committed_at
    )
    oldest_kept = window_end - ORDER_WINDOW_SECONDS
    return {
        release: seconds
        for release, seconds in newest_requests.items()
        if seconds >= oldest_kept
    }


def sets_current(change: ReleaseChange) -> bool:
    """Return whether the change sets the release's ``current``.

    That is a promotion, or a publication that makes its build current. Only
    such changes to one release can undo each other, so only they are
    overtaken by a newer one that landed first, and only they count as newer
    changes. A withdrawal never changes ``current``, and the record's rules
    refuse whichever of a withdrawal and a promotion of the same build lands
    second, so withdrawals are not ordered.
    """
    return isinstance(change, PromoteBuild) or (
        isinstance(change, PublishRelease) and change.make_current
    )


def _commit_message(
    change: ReleaseChange,
    newest_requests: Mapping[str, float],
    *,
    makes_current: bool,
) -> str:
    release, build = change.release, change.build
    if isinstance(change, PublishRelease):
        suffix = " and make it current" if makes_current else ""
        title = f"Publish build {build} of {release}{suffix}"
    elif isinstance(change, PromoteBuild):
        title = f"Make build {build} the current build of {release}"
    else:
        title = f"Withdraw build {build} of {release}"
    if not newest_requests:
        return title
    return f"{title} [newest requests: {_newest_requests_text(newest_requests)}]"


def _not_made_current(change: PublishRelease, record: ReleasesRecord) -> str:
    current = record.releases[change.release].current
    now_current = (
        f"the current build is {current}"
        if current is not None
        else "the release has no current build"
    )
    return (
        f"Build {change.build} of {change.release} was published but not made "
        f"current: a change to {change.release} requested after this one has "
        f"already landed, and {now_current}."
    )


def _refused_for(violations: Sequence[Violation]) -> ReleaseOutcome:
    """Refused with the first broken rule's code and every rule's message."""
    return _refused(violations[0].code, "; ".join(item.message for item in violations))


def apply_release_change(
    repository: DatasetRepository,
    change: ReleaseChange,
    *,
    published_at: str,
    requested_at: float | None = None,
    newest_local_requests: Mapping[str, float] | None = None,
) -> ReleaseOutcome:
    """Apply one validated release change, or report why it did not land.

    ``requested_at`` is when the change was handed over (now, by default).
    ``newest_local_requests`` gives, for each release, the newest request time
    of this machine's changes to it that set ``current`` (see
    ``sets_current``) and landed on the repository.
    """
    requested = _as_written(time.time() if requested_at is None else requested_at)
    if isinstance(change, PublishRelease):
        preserved = _preserved_files(change)
        if preserved is not None:
            return preserved
    release, build = change.release, change.build
    folder = build_folder(release, build)
    new_files = {
        f"{folder}/{item.name}": item.local_path
        for item in (change.files if isinstance(change, PublishRelease) else ())
    }
    for _ in range(MAX_PARENT_CONFLICT_ATTEMPTS):
        try:
            head = repository.head_revision()
            files = repository.list_files(revision=head)
            record_bytes = (
                repository.download(RELEASES_RECORD_PATH, revision=head)
                if RELEASES_RECORD_PATH in files
                else None
            )
            before = (
                None if record_bytes is None else ReleasesRecord.parse(record_bytes)
            )
        except ReleaseRecordError as error:
            return _refused_for(error.violations)
        # Any other failure reading the Hub leaves the change pending for a
        # retry.
        except Exception:  # noqa: BLE001
            return _pending(REPOSITORY_UNAVAILABLE)
        record = before or ReleasesRecord.empty()
        if isinstance(change, PublishRelease) and any(
            path.startswith(folder + "/") for path in files
        ):
            return _already_published(repository, change, record, head)
        try:
            on_record, record_committed_at = _newest_requests_on_record(
                repository, head, files
            )
        # Any failure reading the Hub leaves the change pending for a retry.
        except Exception:  # noqa: BLE001
            return _pending(REPOSITORY_UNAVAILABLE)
        newest_requests = _newest_per_release(on_record, newest_local_requests or {})
        newest_for_release = newest_requests.get(release)
        changes_current = sets_current(change)
        overtaken = (
            changes_current
            and newest_for_release is not None
            and newest_for_release > requested
        )
        if overtaken and isinstance(change, PromoteBuild):
            return _refused(
                NEWER_CHANGE_LANDED,
                f"A change to {release} requested after this one has already "
                "landed; request this change again, as a new request, if it is "
                "still wanted.",
            )
        makes_current = (
            isinstance(change, PublishRelease) and change.make_current and not overtaken
        )
        try:
            if isinstance(change, PublishRelease):
                after = add_build(
                    record,
                    release=release,
                    build=build,
                    release_role=change.release_role,
                    published_at=published_at,
                    make_current=makes_current,
                )
            elif isinstance(change, PromoteBuild):
                after = make_current(record, release=release, build=build)
            else:
                after = withdraw_build(record, release=release, build=build)
        except ReleaseRecordError as error:
            return _refused_for(error.violations)
        added_paths = [*new_files, RELEASES_RECORD_PATH]

        def read_file(path: str, revision: str = head) -> bytes:
            local = new_files.get(path)
            if local is not None:
                return local.read_bytes()
            return repository.download(path, revision=revision)

        try:
            violations = [
                *check_change(before, after),
                *check_commit(
                    before_files=files,
                    added_paths=added_paths,
                    deleted_paths=[],
                    before=before,
                    after=after,
                ),
                *check_repository(after, [*files, *added_paths], read_file),
            ]
        # Any failure while the checks read files leaves the change pending for
        # a retry.
        except Exception:  # noqa: BLE001
            return _pending(REPOSITORY_UNAVAILABLE)
        if violations:
            return _refused_for(violations)
        current = after.releases[release].current
        if not new_files and record_bytes == after.serialize():
            # Nothing to write (the build is already current, for example):
            # the Hub would skip an empty commit and answer with its head.
            return ReleaseOutcome("published", commit=head, current=current)
        operations = [add_operation(path, local) for path, local in new_files.items()]
        operations.append(add_operation(RELEASES_RECORD_PATH, after.serialize()))
        if changes_current and not overtaken:
            newest_requests[release] = requested
        message = _commit_message(
            change,
            _within_order_window(newest_requests, record_committed_at),
            makes_current=makes_current,
        )
        try:
            commit = repository.commit(operations, message=message, parent_commit=head)
        # A failed commit is tried again after a parent conflict, and otherwise
        # leaves the change pending for a retry.
        except Exception as error:  # noqa: BLE001
            if is_parent_conflict(error):
                continue
            return _pending(UPLOAD_FAILED)
        note = (
            _not_made_current(change, after)
            if overtaken and isinstance(change, PublishRelease)
            else None
        )
        return ReleaseOutcome(
            "published", message=note, commit=commit, committed=True, current=current
        )
    return _pending(PARENT_CONFLICT)


def _already_published(
    repository: DatasetRepository,
    change: PublishRelease,
    record: ReleasesRecord,
    head: str,
) -> ReleaseOutcome:
    """Refuse a publication whose build folder exists, unless this build landed.

    A retried publication whose earlier attempt landed (its commit raised, or
    the receipt was lost) finds its build listed with a byte-identical
    manifest: that is ``published``, not refused, even if another build has
    been made current since. Publishing never promotes an already published
    build; ``promote_build`` does that.
    """
    release, build = change.release, change.build
    entry = record.releases.get(release)
    manifest_path = f"{build_folder(release, build)}/{RELEASE_MANIFEST_NAME}"
    local_manifest = next(
        item.local_path for item in change.files if item.name == RELEASE_MANIFEST_NAME
    )
    exists_message = f"{build_folder(release, build)}/ already exists."
    if entry is None or build not in entry.builds:
        return _refused("BUILD_FOLDER_EXISTS", exists_message)
    try:
        same = repository.download(manifest_path, revision=head) == (
            local_manifest.read_bytes()
        )
        commit = repository.last_commit(manifest_path) if same else None
    # Any failure reading either manifest or the commit leaves the change
    # pending for a retry.
    except Exception:  # noqa: BLE001
        return _pending(REPOSITORY_UNAVAILABLE)
    if not same:
        return _refused("BUILD_FOLDER_EXISTS", exists_message)
    message = None
    if change.make_current and entry.current != build:
        message = (
            f"Build {build} of {release} was already published; the current "
            f"build is {entry.current}."
        )
    return ReleaseOutcome(
        "published", message=message, commit=commit or head, current=entry.current
    )
