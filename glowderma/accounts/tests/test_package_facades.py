"""The shape of every accounts package that sits behind a facade (``authentication``, ``mfa``,
``second_factor``, ``selectors``; ``models`` and ``services`` have their own tests): the facade only
re-exports, every exported name has one home and is the same object there, and the submodules use absolute
imports and import their siblings as modules, never a name through the facade."""

from __future__ import annotations

import ast
import importlib
import pathlib
from types import ModuleType

import pytest

FACADES = [
    "glowderma.accounts.authentication",
    "glowderma.accounts.console.views",
    "glowderma.accounts.mfa",
    "glowderma.accounts.second_factor",
    "glowderma.accounts.selectors",
    "glowderma.accounts.tasks",
]


def submodules(facade: ModuleType) -> list[ModuleType]:
    package = pathlib.Path(str(facade.__file__)).parent
    paths = sorted(path for path in package.glob("*.py") if path.name != "__init__.py")
    return [importlib.import_module(f"{facade.__name__}.{path.stem}") for path in paths]


@pytest.fixture(params=FACADES)
def facade(request: pytest.FixtureRequest) -> ModuleType:
    return importlib.import_module(request.param)


def test_the_facade_only_re_exports(facade: ModuleType) -> None:
    tree = ast.parse(pathlib.Path(str(facade.__file__)).read_text(encoding="utf-8"))
    for stmt in tree.body:
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            continue  # the module docstring
        if isinstance(stmt, ast.ImportFrom):
            assert (stmt.module or "").startswith(facade.__name__ + "."), ast.unparse(stmt)
            continue
        assert isinstance(stmt, ast.Assign) and ast.unparse(stmt.targets[0]) == "__all__", ast.unparse(stmt)


def test_every_exported_name_has_one_home_and_is_the_same_object(facade: ModuleType) -> None:
    homes: dict[str, list[ModuleType]] = {}
    for module in submodules(facade):
        for name in getattr(module, "__all__", ()):
            homes.setdefault(name, []).append(module)
    exported = facade.__all__
    assert len(exported) == len(set(exported))
    for name in exported:
        assert len(homes.get(name, [])) == 1, f"{name}: {homes.get(name)}"
        assert getattr(facade, name) is getattr(homes[name][0], name)


def test_submodules_use_absolute_imports_and_never_the_facade(facade: ModuleType) -> None:
    siblings = {module.__name__.rsplit(".", 1)[1] for module in submodules(facade)}
    for module in submodules(facade):
        tree = ast.parse(pathlib.Path(str(module.__file__)).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert node.level == 0, f"{module.__name__}: absolute imports only"
                if node.module == facade.__name__:  # only sibling modules, never a re-exported name
                    names = {alias.name for alias in node.names}
                    assert names <= siblings, f"{module.__name__} imports {names - siblings} from its facade"
                imported = {f"{node.module}.{alias.name}" for alias in node.names}
                assert facade.__name__ not in imported, f"{module.__name__} must not import its facade"
