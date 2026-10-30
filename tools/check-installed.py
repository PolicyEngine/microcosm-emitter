"""Verify clean, non-editable installs away from the source checkout.

Two environments mirror production: the emitter's own, as
`uv tool install microcosm-emitter` creates it, and a build's, which holds only
the interface package and starts the emitter's command as its child. Neither
source archive may hold the repository's rules tool, its registry or their
documents.
"""

import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EMITTER_PREFIX = "microcosm_emitter-"
INTERFACE_PREFIX = "microcosm_emitter_interface-"
# Paths, relative to an archive's top directory, that no source archive may
# hold; one ending in / is a directory. pyproject.toml excludes them.
UNPUBLISHED_PATHS = (
    "scripts/rules/",
    "scripts/run_quality_guards.py",
    "rules/",
    "docs/generated/rules.md",
    "docs/engineering/skills/rules-registry.md",
)


def run(*arguments, **kwargs):
    """Run ``arguments`` as a command, raising ``CalledProcessError`` if it fails."""
    subprocess.run(arguments, check=True, **kwargs)


def only(paths: list[Path], prefix: str) -> Path:
    """Return the one path whose name starts with ``prefix``; assert there is one."""
    matches = [path for path in paths if path.name.startswith(prefix)]
    assert len(matches) == 1, f"Expected one {prefix}* artifact, found {matches}"
    return matches[0]


def unpublished_members(source: Path) -> list[str]:
    """Return the files of a source archive that UNPUBLISHED_PATHS names."""
    with tarfile.open(source) as archive:
        names = [member.name.split("/", 1)[-1] for member in archive.getmembers()]
    return sorted(
        name
        for name in names
        if any(
            name.startswith(path) if path.endswith("/") else name == path
            for path in UNPUBLISHED_PATHS
        )
    )


def create_environment(directory: Path, *wheels: Path) -> Path:
    """Create a virtual environment in ``directory`` and install ``wheels`` in it.

    Returns the environment's Python interpreter.
    """
    run("uv", "venv", "--python", sys.executable, str(directory))
    interpreter = directory / "bin/python"
    run("uv", "pip", "install", "--python", str(interpreter), *map(str, wheels))
    return interpreter


def main():
    """Check the built archives and run the installed checks in clean environments.

    ``dist`` must hold one wheel and one source archive for each distribution, and
    no source archive may hold ``UNPUBLISHED_PATHS``. The emitter and interface
    wheels are installed in one environment and only the interface wheel in
    another; installed-smoke.py and installed-interface-smoke.py then run in them
    without Hugging Face tokens, ``PYTHONPATH`` or ``MICROCOSM_EMITTER_COMMAND``.
    """
    with tempfile.TemporaryDirectory(prefix="pe-wheel-") as directory:
        workspace = Path(directory)
        wheels = list((ROOT / "dist").glob("*.whl"))
        sources = list((ROOT / "dist").glob("*.tar.gz"))
        assert len(wheels) == len(sources) == 2, (
            "Expected an emitter and an interface wheel and source archive"
        )
        for prefix in (EMITTER_PREFIX, INTERFACE_PREFIX):
            source = only(sources, prefix)
            unpublished = unpublished_members(source)
            assert not unpublished, f"{source.name} holds {unpublished}"
        emitter_wheel = only(wheels, EMITTER_PREFIX)
        interface_wheel = only(wheels, INTERFACE_PREFIX)
        emitter_interpreter = create_environment(
            workspace / "emitter-tool", emitter_wheel, interface_wheel
        )
        build_interpreter = create_environment(workspace / "build", interface_wheel)
        environment = {
            key: value
            for key, value in os.environ.items()
            if key
            not in {
                "HF_TOKEN",
                "HUGGINGFACE_TOKEN",
                "HUGGING_FACE_HUB_TOKEN",
                "PYTHONPATH",
                "MICROCOSM_EMITTER_COMMAND",
            }
        }
        environment.update(
            HF_HOME=str(workspace / "hf"), HF_TOKEN_PATH=str(workspace / "hf/token")
        )
        run(
            str(emitter_interpreter),
            "-I",
            str(ROOT / "tools/installed-smoke.py"),
            cwd=workspace,
            env=environment,
        )
        run(
            str(build_interpreter),
            "-I",
            str(ROOT / "tools/installed-interface-smoke.py"),
            str(workspace / "emitter-tool/bin/microcosm-emitter"),
            cwd=workspace,
            env=environment,
        )


if __name__ == "__main__":
    main()
