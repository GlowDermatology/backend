"""The shape of the ``accounts.services`` package: a facade that only re-exports, one home per name, sibling
imports that never go through the facade, and one logger name for every submodule (log lines are matched on
it). The layering of the submodules themselves is an import-linter contract (``.importlinter``)."""

from __future__ import annotations

import ast
import importlib
import logging
import pathlib
from types import ModuleType

import pytest

from glowderma.accounts import services

PACKAGE = pathlib.Path(services.__file__).parent
FACADE = "glowderma.accounts.services"


def submodules() -> list[ModuleType]:
    names = []
    for path in sorted(PACKAGE.rglob("*.py")):
        if path.name == "__init__.py":
            continue
        names.append(FACADE + "." + ".".join(path.relative_to(PACKAGE).with_suffix("").parts))
    return [importlib.import_module(name) for name in names]


def test_the_facade_only_re_exports() -> None:
    tree = ast.parse((PACKAGE / "__init__.py").read_text(encoding="utf-8"))
    for stmt in tree.body:
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            continue  # the module docstring
        if isinstance(stmt, ast.ImportFrom):
            assert (stmt.module or "").startswith(FACADE + "."), ast.unparse(stmt)
            continue
        assert isinstance(stmt, ast.Assign) and ast.unparse(stmt.targets[0]) == "__all__", ast.unparse(stmt)


def test_every_exported_name_has_one_home_and_is_the_same_object() -> None:
    homes: dict[str, list[ModuleType]] = {}
    for module in submodules():
        for name in getattr(module, "__all__", ()):
            homes.setdefault(name, []).append(module)
    assert len(services.__all__) == len(set(services.__all__))
    for name in services.__all__:
        assert len(homes.get(name, [])) == 1, f"{name}: {homes.get(name)}"
        assert getattr(services, name) is getattr(homes[name][0], name)


@pytest.mark.parametrize("module", submodules(), ids=lambda module: module.__name__)
def test_submodules_import_siblings_never_the_facade(module: ModuleType) -> None:
    tree = ast.parse(pathlib.Path(str(module.__file__)).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.level == 0, "absolute imports only"
            imported = {f"{node.module}.{alias.name}" for alias in node.names}
            assert FACADE not in imported, "a submodule must not import the facade"
            assert node.module != FACADE, "a submodule must not import names from the facade"


@pytest.mark.parametrize("module", submodules(), ids=lambda module: module.__name__)
def test_submodules_log_under_the_package_name(module: ModuleType) -> None:
    logger = getattr(module, "logger", None)
    if logger is not None:
        assert isinstance(logger, logging.Logger)
        assert logger.name == FACADE
