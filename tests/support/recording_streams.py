"""Text streams that record each call made on them, for tests of written output.

A test puts one in place of ``sys.stdout`` or ``sys.stderr`` and compares the
calls with those ``print`` makes: one ``write`` of the text, one of the
newline, then ``flush`` when asked. The same calls give the same bytes, the
same encoding and the same errors from the real stream.
"""

from __future__ import annotations


class RecordingStream:
    """Record ``write`` and ``flush`` calls; ``fail_on`` names one that raises."""

    def __init__(
        self, *, fail_on: str | None = None, error: Exception | None = None
    ) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.fail_on = fail_on
        self.error = error

    def write(self, text: str) -> int:
        self.calls.append(("write", text))
        if self.fail_on == "write" and self.error is not None:
            raise self.error
        return len(text)

    def flush(self) -> None:
        self.calls.append(("flush",))
        if self.fail_on == "flush" and self.error is not None:
            raise self.error


def printed_calls(text: str, *, flush: bool) -> list[tuple[str, ...]]:
    """Return the calls ``print(text, file=stream, flush=flush)`` makes on stream."""
    calls: list[tuple[str, ...]] = [("write", text), ("write", "\n")]
    if flush:
        calls.append(("flush",))
    return calls
