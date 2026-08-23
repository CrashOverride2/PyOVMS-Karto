"""
Static guards for whole classes of defect, not individual bugs.

Ported from the same module in the OVMS main server repository, where it exists because
of a real incident: three modules called `security_manager.record_failure(...)` without
importing the name. Every one of those call sites is a brute-force counter, so six
security-relevant paths raised NameError and returned HTTP 500 instead of recording a
failure — silently, because they only run when someone is already doing something wrong.

Karto has no such history, which is exactly why the guard is worth having here before it
does. A missing import inside a rarely-taken branch is invisible until that branch runs,
and Karto's error paths (render failures, MQTT reconnects, reaper sweeps) are all of that
shape. A single `ruff check --select F821` catches the class, which is why ruff is in
requirements-dev.txt too — this test makes it run without anyone remembering to.

Unlike the main server's copy, this one also covers the top-level modules. Karto's
rendering pipeline lives in map_worker.py rather than under app/, and it is the part
most made of exception handlers.
"""

import ast
import builtins
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
APP_DIR = REPO_ROOT / "app"

# Module-level dunders that are always present at runtime but are not builtins.
_MODULE_GLOBALS = {"__file__", "__name__", "__doc__", "__package__", "__spec__", "__loader__"}


def _bound_names(tree: ast.AST) -> set[str]:
    """Names bound anywhere in the module (deliberately flow-insensitive)."""
    bound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                bound.add(alias.asname or alias.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, ast.Global):
            bound.update(node.names)
        elif isinstance(node, ast.alias):
            bound.add((node.asname or node.name).split(".")[0])
    return bound


def _undefined_names(path: pathlib.Path) -> list[str]:
    tree = ast.parse(path.read_text(), str(path))
    bound = _bound_names(tree) | set(dir(builtins)) | _MODULE_GLOBALS
    return sorted({
        f"{path.relative_to(REPO_ROOT)}:{node.lineno} {node.id}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id not in bound
    })


# app/ recursively, plus the top-level modules. Deliberately not REPO_ROOT.rglob("*.py"):
# that walks .venv and would test several thousand third-party files.
ALL_MODULES = sorted(APP_DIR.rglob("*.py")) + sorted(REPO_ROOT.glob("*.py"))


@pytest.mark.parametrize("path", ALL_MODULES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_no_undefined_names(path):
    """Catches the missing-import class of bug that killed six rate-limit call sites."""
    assert _undefined_names(path) == []


def test_the_scan_actually_covers_something():
    """
    Guards the collection above. If APP_DIR is renamed or the glob stops matching, every
    parametrised case vanishes and the suite still reports green — a zero-case
    parametrize is not a failure. The counts are lower bounds, not exact, so ordinary
    additions and deletions do not touch this test.
    """
    assert len(ALL_MODULES) >= 20, f"only {len(ALL_MODULES)} modules collected; the glob is wrong"

    covered = {p.name for p in ALL_MODULES}
    for expected in ("api.py", "crud.py", "mqtt_subscriber.py", "map_worker.py"):
        assert expected in covered, f"{expected} is no longer being scanned"
