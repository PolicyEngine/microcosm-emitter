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
        wheels = list((ROOT / "dist").glob("*.whl"))
        sources = list((ROOT / "dist").glob("*.tar.gz"))
        assert len(wheels) == len(sources) == 1, (
            "Expected exactly one emitter wheel and source archive"
        )
        assert wheels[0].name.startswith("microcosm_emitter-")
        assert sources[0].name.startswith("microcosm_emitter-")
        run("uv", "pip", "install", "--python", str(interpreter), str(wheels[0]))
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
            cwd=workspace,
            env=environment,
        )


if __name__ == "__main__":
    main()
