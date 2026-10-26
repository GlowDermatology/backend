"""``deploy/env.production.example`` lists every variable production reads (static analysis, no import).

The variables come from the settings modules (``config/settings/base.py``, ``production.py`` and
``config/gunicorn.conf.py``, parsed with ``ast``): the string arguments of the ``config.env`` readers, of
every local helper that forwards its parameter to one (found by a fix point), of ``os.environ`` /
``os.getenv`` reads and of production's validators; a name passed as a loop variable is resolved through its
``for`` (a literal tuple/list/dict, a module constant, ``{**A, **B}``, ``.items()``/``.keys()``). Plus
``os.environ`` reads in ``glowderma/`` outside tests and migrations, and the variables ``compose.prod.yaml``
interpolates or sets.

The example documents a name as an active ``NAME=value`` line, a commented ``#NAME=value`` line, or a
``# NAME — …`` line (set by compose, written by a script, or refused by production).
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from django.conf import settings

ROOT = Path(settings.BASE_DIR)
SETTINGS_FILES = ("config/settings/base.py", "config/settings/production.py", "config/gunicorn.conf.py")
BASE_READERS = frozenset(
    {"env_str", "env_int", "env_bool", "env_list", "env_json", "env_datetime", "env_networks"}
)
# production.py validators: their first argument names a variable they refuse when empty or a placeholder.
VALIDATORS = frozenset({"_refuse_placeholder", "_https_url", "_not_an_example"})
REQUIRED_HELPERS = frozenset({"_required"})


@dataclass
class Module:
    path: str
    tree: ast.Module
    constants: dict[str, ast.expr] = field(default_factory=dict)
    parents: dict[ast.AST, ast.AST] = field(default_factory=dict)


@dataclass
class Read:
    name: str
    where: str
    has_default: bool
    conditional: bool


def load(rel: str) -> Module:
    tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"), filename=rel)
    module = Module(rel, tree)
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            module.parents[child] = node
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
            module.constants[stmt.targets[0].id] = stmt.value
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.value is not None:
            module.constants[stmt.target.id] = stmt.value
    return module


def literal_strings(expr: ast.expr, modules: list[Module]) -> list[str] | None:
    """The strings a loop over ``expr`` yields first (tuple items, first items of pairs, dict keys)."""
    if (
        isinstance(expr, ast.Call)
        and isinstance(expr.func, ast.Attribute)
        and expr.func.attr in {"items", "keys"}
    ):
        return literal_strings(expr.func.value, modules)
    if (
        isinstance(expr, ast.Call)
        and isinstance(expr.func, ast.Name)
        and expr.func.id in {"frozenset", "set", "tuple"}
    ):
        return literal_strings(expr.args[0], modules) if expr.args else []
    if isinstance(expr, ast.Name):
        for module in modules:
            if expr.id in module.constants:
                return literal_strings(module.constants[expr.id], modules)
        return None
    out: list[str] = []
    if isinstance(expr, (ast.Tuple, ast.List, ast.Set)):
        for item in expr.elts:
            first = item.elts[0] if isinstance(item, (ast.Tuple, ast.List)) and item.elts else item
            if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
                return None
            out.append(first.value)
        return out
    if isinstance(expr, ast.Dict):
        for key, value in zip(expr.keys, expr.values, strict=True):
            if key is None:
                merged = literal_strings(value, modules)
                if merged is None:
                    return None
                out.extend(merged)
            elif isinstance(key, ast.Constant) and isinstance(key.value, str):
                out.append(key.value)
            else:
                return None
        return out
    return None


def enclosing(node: ast.AST, module: Module, kinds: tuple[type[ast.AST], ...]) -> ast.AST | None:
    cursor = module.parents.get(node)
    while cursor is not None and not isinstance(cursor, kinds):
        cursor = module.parents.get(cursor)
    return cursor


def resolve(arg: ast.expr, node: ast.AST, module: Module, modules: list[Module]) -> list[str] | None:
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return [arg.value]
    if not isinstance(arg, ast.Name):
        return None
    cursor = module.parents.get(node)
    while cursor is not None and not isinstance(cursor, ast.FunctionDef):
        if isinstance(cursor, ast.For):
            target = cursor.target
            first = target.elts[0] if isinstance(target, ast.Tuple) and target.elts else target
            if isinstance(first, ast.Name) and first.id == arg.id:
                return literal_strings(cursor.iter, modules)
        cursor = module.parents.get(cursor)
    return None


def is_os_environ(expr: ast.expr) -> bool:
    return (
        isinstance(expr, ast.Attribute)
        and expr.attr == "environ"
        and isinstance(expr.value, ast.Name)
        and expr.value.id == "os"
    )


def has_default(call: ast.Call, index: int) -> bool:
    return len(call.args) > index + 1 or any(kw.arg == "default" for kw in call.keywords)


def forwarding_helpers(modules: list[Module]) -> dict[str, tuple[int, ast.Call | None]]:
    """{reader: (index of the variable-name argument, the base reader call inside it)}."""
    readers: dict[str, tuple[int, ast.Call | None]] = dict.fromkeys(BASE_READERS, (0, None))
    changed = True
    while changed:
        changed = False
        for module in modules:
            for fn in ast.walk(module.tree):
                if not isinstance(fn, ast.FunctionDef) or fn.name in readers:
                    continue
                params = [a.arg for a in fn.args.args]
                for call in ast.walk(fn):
                    if (
                        isinstance(call, ast.Call)
                        and isinstance(call.func, ast.Name)
                        and call.func.id in readers
                    ):
                        index = readers[call.func.id][0]
                        arg = call.args[index] if len(call.args) > index else None
                        if isinstance(arg, ast.Name) and arg.id in params:
                            readers[fn.name] = (params.index(arg.id), call)
                            changed = True
                            break
    return readers


def settings_reads() -> tuple[list[Read], set[str]]:
    """Every read of the settings modules, and the names production refuses when set."""
    modules = [load(rel) for rel in SETTINGS_FILES]
    readers = forwarding_helpers(modules)
    reads: list[Read] = []
    for module in modules:
        for node in ast.walk(module.tree):
            if isinstance(node, ast.Call):
                func, index = node.func, 0
                reader: str | None = None
                if isinstance(func, ast.Name) and func.id in readers:
                    reader, index = func.id, readers[func.id][0]
                elif isinstance(func, ast.Attribute) and func.attr == "get" and is_os_environ(func.value):
                    reader = "os.environ.get"
                elif (
                    isinstance(func, ast.Attribute)
                    and func.attr == "getenv"
                    and isinstance(func.value, ast.Name)
                ):
                    reader = "os.getenv" if func.value.id == "os" else None
                elif (
                    isinstance(func, ast.Name)
                    and func.id in VALIDATORS
                    and module.path.endswith("production.py")
                ):
                    if node.args and enclosing(node, module, (ast.FunctionDef,)) is None:
                        for name in resolve(node.args[0], node, module, modules) or []:
                            reads.append(Read(name, module.path, False, True))
                    continue
                if reader is None or len(node.args) <= index:
                    continue
                function = enclosing(node, module, (ast.FunctionDef,))
                if isinstance(function, ast.FunctionDef) and function.name in readers:
                    continue  # a helper's own body: its parameter, counted at the call sites
                names = resolve(node.args[index], node, module, modules)
                assert names is not None, (
                    f"{module.path}:{node.lineno}: cannot resolve the name read by {reader}"
                )
                default = reader.startswith("os.") or (
                    reader not in REQUIRED_HELPERS and has_default(node, index)
                )
                conditional = (
                    enclosing(node, module, (ast.If, ast.IfExp, ast.BoolOp, ast.FunctionDef)) is not None
                )
                reads.extend(Read(name, module.path, default, conditional) for name in names)
            elif isinstance(node, ast.Subscript) and is_os_environ(node.value):
                for name in resolve(node.slice, node, module, modules) or []:
                    reads.append(Read(name, module.path, False, True))
            elif isinstance(node, ast.Compare) and any(is_os_environ(c) for c in node.comparators):
                for name in resolve(node.left, node, module, modules) or []:
                    reads.append(Read(name, module.path, True, True))
    production = modules[1]
    dev_only = production.constants.get("DEV_ONLY_VARIABLES", ast.Tuple(elts=[]))
    refused = set(literal_strings(dev_only, modules) or [])
    for node in ast.walk(production.tree):
        if isinstance(node, ast.If) and any(isinstance(stmt, ast.Raise) for stmt in node.body):
            for call in ast.walk(node.test):
                if (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and is_os_environ(call.func.value)
                ):
                    refused.update(resolve(call.args[0], call, production, modules) or [])
    return reads, refused


def code_environ_reads() -> set[str]:
    names: set[str] = set()
    for path in sorted((ROOT / "glowderma").rglob("*.py")):
        rel = str(path.relative_to(ROOT))
        if "/tests/" not in rel and "/migrations/" not in rel:
            text = path.read_text(encoding="utf-8")
            names.update(re.findall(r"os\.(?:environ\.get|getenv)\(\s*\"([A-Z][A-Z0-9_]*)\"", text))
    return names


def compose_names() -> tuple[set[str], set[str], set[str]]:
    """Interpolated names, names the backend environment map sets, and the ones it blanks ("")."""
    text = (ROOT / "compose.prod.yaml").read_text(encoding="utf-8")
    interpolated = set(re.findall(r"(?<!\$)\$\{([A-Z][A-Z0-9_]*)", text))
    environment: dict[str, object] = yaml.safe_load(text)["x-backend"]["environment"]
    blanked = {name for name, value in environment.items() if value == ""}
    return interpolated, set(environment) - blanked, blanked


def example_names() -> dict[str, str]:
    """{name: "active" | "commented" | "documented:<text>"} of deploy/env.production.example."""
    names: dict[str, str] = {}
    for line in (ROOT / "deploy" / "env.production.example").read_text(encoding="utf-8").splitlines():
        if match := re.match(r"^([A-Z][A-Z0-9_]*)=", line):
            names[match.group(1)] = "active"
        elif match := re.match(r"^#([A-Z][A-Z0-9_]*)=", line):
            names.setdefault(match.group(1), "commented")
        elif match := re.match(r"^# ([A-Z][A-Z0-9_]*) — (.*)$", line):
            names.setdefault(match.group(1), "documented:" + match.group(2))
    return names


READS, REFUSED = settings_reads()
SETTINGS_NAMES = {read.name for read in READS}
CODE_NAMES = code_environ_reads()
INTERPOLATED, SET_BY_COMPOSE, BLANKED = compose_names()
EXAMPLE = example_names()


def test_the_analysis_finds_the_known_reads() -> None:
    """Guards the parser itself: a refactor of the settings that hid the reads would pass vacuously."""
    assert len(SETTINGS_NAMES) > 150
    required = {read.name for read in READS if not read.has_default and not read.conditional}
    assert {
        "SECRET_KEY",
        "INTERNAL_API_SECRET",
        "DATABASE_URL",
        "SMS_CODE_HMAC_KEY",
        "SECRETBOX_KEY",
    } <= required
    assert {"BILLZ_SECRET_KEY", "FCM_CREDENTIALS_FILE", "ESKIZ_SENDER"} <= SETTINGS_NAMES
    assert {"FAKE_NOW", "POS_API_KEYS", "ESKIZ_FAKE_CODE"} <= REFUSED
    assert "SEED_ADMIN_PASSWORD" in CODE_NAMES
    assert {"POSTGRES_APP_PASSWORD", "REDIS_PASSWORD", "GIT_SHA"} <= INTERPOLATED


def test_every_variable_production_reads_is_in_the_example() -> None:
    missing = sorted((SETTINGS_NAMES | CODE_NAMES | INTERPOLATED) - set(EXAMPLE))
    assert not missing, f"add to deploy/env.production.example: {missing}"


def test_variables_compose_sets_are_documented_as_such_and_never_set() -> None:
    for name in sorted(SET_BY_COMPOSE):
        state = EXAMPLE.get(name, "missing")
        assert state.startswith("documented:") and "set by compose" in state, f"{name}: {state}"


def test_blanked_passwords_are_set_in_the_example() -> None:
    """Compose blanks them in the backend containers; the operator sets them in .env."""
    assert BLANKED == {"POSTGRES_PASSWORD", "POSTGRES_APP_PASSWORD", "REDIS_PASSWORD"}
    assert all(EXAMPLE.get(name) == "active" for name in BLANKED)


def test_refused_variables_are_never_active_lines() -> None:
    assert REFUSED, "the production refusals were not found"
    assert not [name for name in sorted(REFUSED) if EXAMPLE.get(name) in {"active", "commented"}]


def test_the_example_lists_nothing_stale() -> None:
    known = SETTINGS_NAMES | CODE_NAMES | INTERPOLATED | SET_BY_COMPOSE | BLANKED
    stale = sorted(set(EXAMPLE) - known)
    assert not stale, f"deploy/env.production.example lists names nothing reads: {stale}"
