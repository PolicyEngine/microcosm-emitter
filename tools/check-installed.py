"""Verify clean, non-editable installs away from the source checkout."""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(*arguments, **kwargs):
    subprocess.run(arguments, check=True, **kwargs)


def main():
    with tempfile.TemporaryDirectory(prefix="pe-wheel-") as directory:
        workspace = Path(directory)
        interpreter = workspace / "venv/bin/python"
        run("uv", "venv", "--python", sys.executable, str(workspace / "venv"))
        base = list((ROOT / "dist").glob("microcosm_provider_client-*.whl"))
        telemetry = list((ROOT / "dist").glob("microcosm_provider_telemetry-*.whl"))
        core = list((ROOT / "dist").glob("microcosm_provider_core-*.whl"))
        orrery = list((ROOT / "dist").glob("microcosm_provider_orrery-*.whl"))
        assert len(base) == len(telemetry) == len(core) == len(orrery) == 1, (
            "Expected exactly one release of each distribution"
        )
        run("uv", "pip", "install", "--python", str(interpreter), str(base[0]))
        run(
            str(interpreter),
            "-I",
            str(ROOT / "tools/installed-smoke.py"),
            "base",
            cwd=workspace,
        )
        run(
            "uv",
            "pip",
            "install",
            "--python",
            str(interpreter),
            str(core[0]),
            str(telemetry[0]),
            str(orrery[0]),
        )
        environment = {
            key: value
            for key, value in os.environ.items()
            if key
            not in {
                "HF_TOKEN",
                "HUGGINGFACE_TOKEN",
                "HUGGING_FACE_HUB_TOKEN",
                "PYTHONPATH",
            }
        }
        environment.update(
            HF_HOME=str(workspace / "hf"), HF_TOKEN_PATH=str(workspace / "hf/token")
        )
        run(
            str(interpreter),
            "-I",
            str(ROOT / "tools/installed-smoke.py"),
            "telemetry",
            cwd=workspace,
            env=environment,
        )
        run(
            str(interpreter),
            "-I",
            str(ROOT / "tools/installed-smoke.py"),
            "combined",
            cwd=workspace,
            env=environment,
        )


if __name__ == "__main__":
    main()
