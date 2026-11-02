"""Request graph retries from the local service without registering a telemetry run."""

import argparse
import json
from pathlib import Path

from microcosm_emitter.orrery.client import GraphPublicationClient


def main(argv: list[str] | None = None) -> int:
    """Request a retry of one graph publication and print its receipt as JSON.

    With ``--directory``, the preserved graph's ``orrery.upload.json``
    inventory is enqueued first; a publication ID in it that differs from
    ``--publication-id`` is an argument error, and a ``failed`` enqueue receipt
    ends the command. The retry waits up to 30 seconds for a new attempt. The
    exit code is 0 when the last receipt is ``published`` and 1 otherwise.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spool", type=Path)
    parser.add_argument("--publication-id", required=True)
    parser.add_argument(
        "--directory",
        type=Path,
        help="Preserved graph directory for re-enqueueing on another machine.",
    )
    parser.add_argument("--origin", help="Graph publication origin.")
    args = parser.parse_args(argv)
    client = GraphPublicationClient.start(
        spool_path=args.spool, publication_origin=args.origin
    )
    try:
        if args.directory is not None:
            inventory = json.loads((args.directory / "orrery.upload.json").read_text())
            if inventory["publication_id"] != args.publication_id:
                parser.error("The preserved inventory has a different publication ID.")
            receipt = client.enqueue(args.directory, inventory)
            if receipt["status"] == "failed":
                print(json.dumps(receipt, sort_keys=True))
                return 1
            receipt = client.retry(args.publication_id, wait_seconds=30)
        else:
            receipt = client.retry(args.publication_id, wait_seconds=30)
        print(json.dumps(receipt, sort_keys=True))
        return 0 if receipt["status"] == "published" else 1
    finally:
        client.close()
