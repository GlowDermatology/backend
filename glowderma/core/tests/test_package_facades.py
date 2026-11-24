"""Every package facade in the project stays a pure facade (AGENTS.md "Split by subject, behind a facade").

A facade ``__init__.py`` (one with an ``__all__``) holds a docstring, absolute imports from its own
submodules and the ``__all__`` list, nothing else: no logic, no re-export of another package's name, and no
name in ``__all__`` that it does not import.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

import glowderma

ROOT = Path(glowderma.__file__).resolve().parent
REPO = ROOT.parent


def _dotted(path: Path) -> str:
    return ".".join(path.parent.relative_to(REPO).parts)


def _facades() -> list[Path]:
    found = []
    for path in sorted(ROOT.rglob("__init__.py")):
        parts = path.relative_to(ROOT).parts
        if "tests" in parts or "migrations" in parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if any(
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets)
            for node in tree.body
        ):
            found.append(path)
    return found


# Modules that are packages for another reason and keep an ``__all__`` of their own code.
NOT_FACADES = frozenset({"glowderma.reporting.dashboards"})
FACADES = [path for path in _facades() if _dotted(path) not in NOT_FACADES]


def test_the_scan_finds_the_facades() -> None:
    dotted = {_dotted(path) for path in FACADES}
    assert {
        "glowderma.customers.services",
        "glowderma.professionals.services",
        "glowderma.notifications.services",
        "glowderma.payments.services",
        "glowderma.integrations.billz.client",
        "glowderma.commerce.api.customer.views",
    } <= dotted


@pytest.mark.parametrize("path", FACADES, ids=_dotted)
def test_a_facade_only_re_exports_its_own_submodules(path: Path) -> None:
    package = _dotted(path)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    body = tree.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]
    imported: set[str] = set()
    exported: list[str] = []
    for node in body:
        if isinstance(node, ast.ImportFrom):
            assert node.level == 0, f"{package}: relative import"
            submodules = node.module == package  # ``from <package> import a, b``: registers its submodules
            assert node.module is not None and (submodules or node.module.startswith(f"{package}.")), (
                f"{package}: re-exports {node.module}, not one of its submodules"
            )
            imported |= {alias.asname or alias.name for alias in node.names}
        elif isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            assert node.targets[0].id == "__all__", f"{package}: a facade assigns only __all__"
            exported = list(ast.literal_eval(node.value))
        else:
            pytest.fail(f"{package}: a facade holds imports and __all__ only, found {ast.dump(node)[:80]}")
    assert len(exported) == len(set(exported)), f"{package}: duplicate names in __all__"
    assert set(exported) == imported, f"{package}: __all__ differs from the imports"
    module = importlib.import_module(package)
    for name in exported:
        assert hasattr(module, name), f"{package}.{name}"
