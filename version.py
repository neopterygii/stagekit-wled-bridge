"""Build identity for the running bridge.

The dashboard's version line answers one operational question: *is the
container in front of me the image I just pushed?* Only build-time metadata can
answer it. A constant in the source says what the source says — it cannot tell
you what the registry actually handed Docker, which is exactly the gap the
operator is trying to close when a deploy looks like it did not take.

So the identifying fields (commit, build time) are injected at image build time
by the Dockerfile's build args, which CI fills from the commit being built; see
`.github/workflows/docker.yml`. `__version__` below is only the human-readable
name for the release, and is the one field a stale source tree can lie about.

Running from a source checkout there are no build args, so `git describe` fills
in instead and the report is labelled `checkout` to say so. That path never runs
in the container: the image ships no `.git`, and the subprocess is attempted
exactly once, at import, so nothing on the render or snapshot path can block on
it.
"""

import os
import subprocess
from pathlib import Path

# Human-readable release name. Bump on release and tag the commit to match —
# CI's BUILD_VERSION overrides it when building from a `v*` tag, so a forgotten
# bump shows up as a mismatch here rather than silently shipping a wrong name.
__version__ = "1.1.0"

_GIT_DESCRIBE_TIMEOUT = 2.0


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def _git_describe() -> str:
    """`git describe` for a source checkout, or "" when it cannot be had.

    Every failure mode is the same answer — no git, no repo, no tags, git
    hanging on a lock — so they collapse into one empty return rather than
    being distinguished. The caller only needs to know whether it got an
    identity.
    """
    try:
        proc = subprocess.run(
            ["git", "describe", "--tags", "--always", "--dirty"],
            cwd=Path(__file__).resolve().parent,
            capture_output=True, text=True,
            timeout=_GIT_DESCRIBE_TIMEOUT, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def _build_info() -> dict:
    """Compose the identity block. Called once, at import."""
    sha = _env("BUILD_SHA")
    built = _env("BUILD_TIME")
    version = _env("BUILD_VERSION") or __version__

    if sha:
        # Built as an image: trust the injected metadata over anything local.
        commit = sha[:7]
        source = "image"
        label = f"v{version} · {commit}"
        if built:
            label += f" · built {built}"
    else:
        described = _git_describe()
        source = "checkout" if described else "unknown"
        commit = described
        label = f"{described} (checkout)" if described else f"v{version} (unknown build)"

    return {
        "version": version,
        "commit": commit,
        "built": built,
        "source": source,
        "label": label,
    }


BUILD_INFO = _build_info()

#: Single pre-composed string for the dashboard and the startup banner.
VERSION_LABEL = BUILD_INFO["label"]
