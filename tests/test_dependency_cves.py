"""
Dependency supply-chain checks.

Counterpart to the same module in the OVMS main server repository. Two kinds of check,
deliberately not the same kind:

  * The lockfile checks are properties of two files in the repository — offline and
    deterministic, so they run in the ordinary suite.

  * The CVE scan needs the current advisory database, which means network. A test that
    silently turns green when it cannot reach PyPI would be worse than no test, so it is
    marked `cve` and deselected by default. Run it explicitly:

        pytest -m cve

    CI runs the same scan on a schedule, because a new advisory against an already
    pinned version arrives without anybody making a commit.

requirements.txt is generated — never edit it by hand:

    uv pip compile requirements.in --universal --python-version 3.12 -o requirements.txt
"""

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS_IN = REPO_ROOT / "requirements.in"
REQUIREMENTS_TXT = REPO_ROOT / "requirements.txt"
CI_WORKFLOW = REPO_ROOT / ".forgejo" / "workflows" / "security-audit.yml"

# The one definition of what "audit the dependencies" means. The CI workflow runs the
# same command — it cannot import this module, because it deliberately installs only
# pip-audit and not the application, so test_ci_workflow_uses_the_same_flags() compares
# the two instead of trusting them to stay in step.
#
# --no-deps and --disable-pip are both needed, and neither is enough alone:
#   --no-deps      audit exactly the versions written in the file instead of re-resolving
#                  the tree, which is what makes the answer independent of who ran it.
#   --disable-pip  don't shell out to pip at all. --no-deps skips *resolution* but
#                  pip-audit still builds a throwaway environment and installs into it —
#                  and this service's numpy pin has no wheel for Python 3.14, so the
#                  audit died trying to compile it rather than reporting anything.
#                  Together they make this a pure offline read of the pinned file.
_PIP_AUDIT_ARGS = ["--no-deps", "--disable-pip", "--strict", "--progress-spinner=off"]


def _normalise(name: str) -> str:
    """PEP 503 normalisation: PyJWT, pyjwt and py-jwt are one package."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _declared_packages() -> set[str]:
    """Top-level names from requirements.in, extras stripped."""
    names = set()
    for line in REQUIREMENTS_IN.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        match = re.match(r"^([A-Za-z0-9._-]+)(\[[^\]]*\])?$", line)
        if match:
            names.add(_normalise(match.group(1)))
    return names


def _pinned_packages() -> dict[str, str]:
    """name -> version for every `name==version` line in requirements.txt."""
    pins = {}
    for line in REQUIREMENTS_TXT.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        match = re.match(r"^([A-Za-z0-9._-]+)(\[[^\]]*\])?==([^\s;]+)", line)
        if match:
            pins[_normalise(match.group(1))] = match.group(3)
    return pins


# ---------------------------------------------------------------------------
# Lockfile integrity — offline, always runs
# ---------------------------------------------------------------------------

def test_every_declared_dependency_is_pinned():
    """
    Catches the failure this split invites: someone adds a package to requirements.in
    and forgets to re-run the compile. The service then works on their machine, where
    they installed it by hand, and is missing it everywhere else.
    """
    missing = sorted(_declared_packages() - set(_pinned_packages()))
    assert not missing, (
        f"Declared in requirements.in but not pinned in requirements.txt: {missing}. "
        f"Re-run: uv pip compile requirements.in --universal --python-version 3.12 -o requirements.txt"
    )


def test_lockfile_is_fully_pinned():
    """
    Every requirement must be an exact ==. A range in the lockfile means two
    deployments can install different code from the same commit, which also makes any
    CVE result below true only for whoever happened to run it.
    """
    unpinned = []
    for line in REQUIREMENTS_TXT.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        if not re.match(r"^[A-Za-z0-9._-]+(\[[^\]]*\])?==", line):
            unpinned.append(line)
    assert not unpinned, f"Requirements without an exact pin: {unpinned}"


def test_ci_workflow_uses_the_same_flags():
    """
    The scheduled CI audit and `pytest -m cve` must ask the same question.

    They cannot share code — the workflow installs pip-audit alone, without the
    application, precisely so the audit does not depend on the pinned set installing on
    the runner's Python. So the flags are compared instead.
    """
    if not CI_WORKFLOW.is_file():
        pytest.skip(f"no CI workflow at {CI_WORKFLOW}")

    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    audit_lines = [ln for ln in workflow.splitlines() if "pip-audit --requirement" in ln]
    assert audit_lines, f"{CI_WORKFLOW.name} no longer runs pip-audit against a requirements file"

    for line in audit_lines:
        missing = [flag for flag in _PIP_AUDIT_ARGS if flag not in line]
        assert not missing, (
            f"{CI_WORKFLOW.name} runs pip-audit without {missing}, but "
            f"tests/test_dependency_cves.py uses {_PIP_AUDIT_ARGS}. Keep them identical."
        )


def test_pycryptodome_stays_out():
    """
    Counterpart to test_python_jose_stays_out() in the main server. pycryptodome sat in
    requirements.in without a single `from Crypto` anywhere under app/ — a compiled C
    extension carried as build cost and CVE surface for code that never ran. Karto's only
    crypto is JWT verification via pyjwt[crypto]. requirements.in says so in a comment,
    and a comment does not survive a hurried merge.
    """
    assert "pycryptodome" not in _pinned_packages(), (
        "pycryptodome is back in requirements.txt. See the NOTE at the bottom of "
        "requirements.in for why it was removed."
    )


# ---------------------------------------------------------------------------
# CVE scan — needs the advisory database, so opt-in
# ---------------------------------------------------------------------------

@pytest.mark.cve
def test_no_known_vulnerabilities_in_pinned_dependencies():
    """
    Audit the pinned set against the PyPI advisory database.

    --no-deps is the point: requirements.txt is a complete `uv pip compile` output, so the
    set to audit is exactly what is written there. Without it pip-audit re-resolves the
    tree against the running interpreter, which makes the answer depend on who ran it.
    """
    if shutil.which("pip-audit") is None:
        pytest.skip("pip-audit is not installed (pip install -r requirements-dev.txt)")

    result = subprocess.run(
        ["pip-audit", "--requirement", str(REQUIREMENTS_TXT), *_PIP_AUDIT_ARGS],
        capture_output=True,
        text=True,
        timeout=300,
    )

    if result.returncode != 0 and "temporarily unable" in (result.stderr or "").lower():
        pytest.skip(f"advisory database unreachable: {result.stderr.strip()[:200]}")

    assert result.returncode == 0, (
        "pip-audit reported vulnerable dependencies.\n\n"
        f"{result.stdout}\n{result.stderr}\n"
        "Fix by bumping the affected package in requirements.in (or letting the "
        "transitive pin move) and re-running:\n"
        "    uv pip compile requirements.in --universal --python-version 3.12 -o requirements.txt"
    )


@pytest.mark.cve
def test_pip_audit_is_available_to_ci():
    """
    Guards the skip above. `pytest -m cve` passing because pip-audit was absent is the
    exact false green this file exists to avoid.
    """
    assert shutil.which("pip-audit") is not None, (
        "pip-audit not on PATH. Install the dev requirements:\n"
        f"    {sys.executable} -m pip install -r requirements-dev.txt"
    )
