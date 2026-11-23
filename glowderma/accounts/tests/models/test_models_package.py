"""The shape of the ``accounts.models`` package: a facade that only re-exports, one home per model, and every
model of the app registered by Django from a submodule of the package. The layering of the submodules is an
import-linter contract (``.importlinter``)."""

from __future__ import annotations

import ast
import importlib
import pathlib
from types import ModuleType

from django.apps import apps

from glowderma.accounts import models

PACKAGE = pathlib.Path(models.__file__).parent
FACADE = "glowderma.accounts.models"


def submodules() -> list[ModuleType]:
    paths = sorted(path for path in PACKAGE.glob("*.py") if path.name != "__init__.py")
    return [importlib.import_module(f"{FACADE}.{path.stem}") for path in paths]


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
    assert len(models.__all__) == len(set(models.__all__))
    for name in models.__all__:
        assert len(homes.get(name, [])) == 1, f"{name}: {homes.get(name)}"
        assert getattr(models, name) is getattr(homes[name][0], name)


def test_every_registered_model_is_exported_from_its_own_submodule() -> None:
    registered = list(apps.get_app_config("accounts").get_models())
    assert {model.__name__ for model in registered} <= set(models.__all__)
    for model in registered:
        assert model.__module__.startswith(FACADE + "."), model


def test_migration_referenced_callables_stay_importable() -> None:
    # Migrations 0002/0003/0012 freeze this dotted path; it must keep resolving to the same function.
    from glowderma.accounts.models import defaults

    assert models.date_joined_default is defaults.date_joined_default


def test_submodules_use_absolute_imports_and_never_the_facade() -> None:
    for module in submodules():
        tree = ast.parse(pathlib.Path(str(module.__file__)).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert node.level == 0, f"{module.__name__}: absolute imports only"
                assert node.module != FACADE, f"{module.__name__} must not import from the facade"
