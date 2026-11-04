"""Write a message to one of the process's standard streams, exactly as print does.

The emitter's modules write their warnings, and ``microcosm-emitter-publish-graph``
its receipt, with ``write_line``, so each place names the stream it writes to
and none needs a ``print`` that Ruff's T201 would report as left from debugging.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import TextIO


def write_line(text: str, stream: TextIO | None, *, flush: bool = False) -> None:
    """Write ``text`` and a newline to ``stream``, as ``print(text, file=stream)`` does.

    It makes the calls ``print`` makes: one ``write`` of ``text``, one of the
    newline, then ``flush`` when ``flush`` is true, so the bytes, the encoding
    and any error the stream raises are the same. As with ``print``, a
    ``stream`` of ``None`` means ``sys.stdout``, and nothing is written when
    that is ``None`` too: each is ``None`` in a process started without it.
    """
    if stream is None:
        stream = sys.stdout
        if stream is None:
            return
    stream.write(text)
    stream.write("\n")
    if flush:
        stream.flush()
