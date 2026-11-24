"""``core.audit_scopes``: the audit tier rule table, the viewer filters, masking, facets, and the scan that
keeps every audited action classified (admin contract §2.13, design "audit tiers")."""

from __future__ import annotations

import ast
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.db import connection

import glowderma
from glowderma.core import audit, audit_scopes, enums
from glowderma.core.models import AuditEntry
from glowderma.core.timeutils import TASHKENT

# --- the rule table --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "action,actor_type,entity_type,expected",
    [
        ("console.login_failed", "system", "staff", audit_scopes.SYSTEM),
        ("ops.outbox_requeued", "staff", "outbox", audit_scopes.SYSTEM),
        ("ops_access.granted", "staff", "staff", audit_scopes.SYSTEM),
        ("integrations.inbox_purged", "system", "webhook_inbox", audit_scopes.SYSTEM),
        ("outbox.dead", "system", "outbox", audit_scopes.SYSTEM),
        ("report.consistency", "system", "report", audit_scopes.SYSTEM),
        ("pos.qr_resolved", "integration", "pos_device", audit_scopes.SYSTEM),
        # first match wins: the hand-run fixes sit under business prefixes but are operator-only
        ("billz.sale_recorded_by_hand", "staff", "order", audit_scopes.SYSTEM),
        ("billz.payment_attempt_forgotten", "staff", "order", audit_scopes.SYSTEM),
        ("loyalty.reconciliation_completed", "system", "loyalty", audit_scopes.SYSTEM),
        ("loyalty.reconciliation_mismatch", "system", "loyalty", audit_scopes.SYSTEM),
        ("billz.import_started", "staff", "job", audit_scopes.BUSINESS),
        ("loyalty.balance_negative", "system", "customer", audit_scopes.BUSINESS),
        ("staff.created", "staff", "staff", audit_scopes.STAFF_ADMIN),
        ("staff.password_reset", "system", "staff", audit_scopes.STAFF_ADMIN),
        ("auth.login_succeeded", "staff", "staff", audit_scopes.STAFF_SECURITY),
        ("auth.login_failed", "system", "staff", audit_scopes.STAFF_SECURITY),
        ("auth.session_revoked", "system", "auth_session", audit_scopes.STAFF_SECURITY),
        ("auth.session_revoked", "customer", "auth_session", audit_scopes.STAFF_SECURITY),
        ("auth.mfa_failed", "system", "staff", audit_scopes.STAFF_SECURITY),
        ("export.downloaded", "staff", "audit-log", audit_scopes.STAFF_SECURITY),
        ("auth.device_mismatch", "customer", "customer", audit_scopes.BUSINESS),
        ("auth.something", "system", "customer", audit_scopes.SYSTEM),  # no rule names it: fail closed
        ("cashback.adjusted", "staff", "customer", audit_scopes.BUSINESS),
        ("service.deleted", "staff", "service", audit_scopes.BUSINESS),
        (
            "professional_service.updated",
            "staff",
            "professional_service",
            audit_scopes.SYSTEM,
        ),  # retired link
        ("customer.created_by_staff", "staff", "customer", audit_scopes.BUSINESS),
        ("working_hours_override.deleted", "staff", "working_hours_override", audit_scopes.BUSINESS),
        ("customer.sessions_revoked", "customer", "customer", audit_scopes.BUSINESS),
        ("brand_new.thing", "staff", "x", audit_scopes.SYSTEM),
        ("stafflike.created", "staff", "x", audit_scopes.SYSTEM),  # a prefix is a whole word
        ("nodot", "staff", "x", audit_scopes.SYSTEM),
    ],
)
def test_scope_of(action: str, actor_type: str, entity_type: str, expected: str) -> None:
    assert audit_scopes.scope_of(action, actor_type, entity_type) == expected


def test_rule_groups_are_disjoint() -> None:
    groups = [
        audit_scopes.SYSTEM_PREFIXES,
        audit_scopes.STAFF_ADMIN_PREFIXES,
        audit_scopes.SECURITY_PREFIXES,
        audit_scopes.BUSINESS_PREFIXES,
        frozenset({audit_scopes.AUTH_PREFIX}),
    ]
    assert sum(len(group) for group in groups) == len(audit_scopes.known_prefixes())
    assert {audit_scopes.prefix(action) for action in audit_scopes.SYSTEM_ACTIONS} <= (
        audit_scopes.BUSINESS_PREFIXES
    )


def test_mask_network_any_depth() -> None:
    document = {
        "ip": "203.0.113.7",
        "user_agent": "Mozilla",
        "nested": [{"last_ip": "198.51.100.1", "location": "Tashkent, UZ", "location_id": 3}],
        "amount": 2950,
    }
    assert audit_scopes.mask_network(document) == {
        "ip": None,
        "user_agent": None,
        "nested": [{"last_ip": None, "location": None, "location_id": 3}],
        "amount": 2950,
    }
    assert audit_scopes.mask_network(None) is None
    assert audit_scopes.mask_network("ip") == "ip"


@dataclass
class Viewer:
    pk: int
    is_manager: bool = False
    is_superuser: bool = False


def test_facets_per_tier() -> None:
    staff = audit_scopes.facets(Viewer(1))
    assert staff["tier"] == enums.AuditViewerTier.STAFF and staff["ip_visible"] is False
    assert "staff" not in staff["visible_prefixes"] and "ops" not in staff["visible_prefixes"]
    assert {"auth", "export", "appointment", "order"} <= set(staff["visible_prefixes"])
    assert staff["own_only_prefixes"] == ["auth", "export"]
    assert staff["hidden_actions"] == sorted(audit_scopes.SYSTEM_ACTIONS)
    manager = audit_scopes.facets(Viewer(2, is_manager=True))
    assert manager["tier"] == enums.AuditViewerTier.MANAGER and manager["ip_visible"] is True
    assert "staff" in manager["visible_prefixes"] and "console" not in manager["visible_prefixes"]
    assert manager["own_only_prefixes"] == []
    superuser = audit_scopes.facets(Viewer(3, is_superuser=True))
    assert superuser["tier"] == enums.AuditViewerTier.SUPERUSER
    assert superuser["visible_prefixes"] == sorted(audit_scopes.known_prefixes())
    assert superuser["hidden_actions"] == [] and superuser["own_only_prefixes"] == []
    # a superuser who is also a manager is a superuser; no flags at all is ordinary staff
    assert audit_scopes.viewer_tier(Viewer(4, is_manager=True, is_superuser=True)) == "superuser"
    assert audit_scopes.viewer_tier(object()) == "staff"


# --- the viewer filter in SQL agrees with the rule table -------------------------------------------------


def _row(action: str, actor_type: str, actor_id: int | None, entity_type: str, entity_id: str) -> AuditEntry:
    return AuditEntry.objects.create(
        action=action,
        actor_type=actor_type,
        actor_id=actor_id,
        entity_type=entity_type,
        entity_id=entity_id,
        created_at=datetime(2026, 11, 1, 9, tzinfo=TASHKENT),
    )


@pytest.mark.django_db
def test_visible_q_matches_scope_of_for_every_tier() -> None:
    me, other = 7, 8
    rows = [
        _row("cashback.adjusted", "staff", other, "customer", "5"),
        _row("billz.import_started", "staff", other, "job", "1"),
        _row("billz.sale_recorded_by_hand", "staff", me, "order", "1"),
        _row("loyalty.reconciliation_mismatch", "system", None, "loyalty", "x"),
        _row("staff.created", "staff", other, "staff", str(me)),
        _row("auth.login_succeeded", "staff", me, "staff", str(me)),
        _row("auth.login_succeeded", "staff", other, "staff", str(other)),
        _row("auth.login_failed", "system", None, "staff", str(me)),
        _row("auth.login_failed", "system", None, "staff", str(other)),
        _row("auth.session_revoked", "staff", me, "auth_session", "s1"),
        _row("auth.session_revoked", "staff", other, "auth_session", "s2"),
        _row("auth.device_mismatch", "customer", 3, "customer", "3"),
        _row("auth.unknown", "system", None, "customer", "3"),
        _row("export.downloaded", "staff", me, "orders", "orders"),
        _row("export.downloaded", "staff", other, "orders", "orders"),
        _row("console.login_failed", "system", None, "staff", str(me)),
        _row("outbox.dead", "system", None, "outbox", "1"),
        _row("unclassified.thing", "staff", me, "x", "1"),
        _row("customer.logout", "customer", 3, "customer", "3"),
    ]

    def visible(viewer: Viewer) -> set[int]:
        q = audit_scopes.visible_q(viewer)
        qs = AuditEntry.objects.all() if q is None else AuditEntry.objects.filter(q)
        return set(qs.values_list("pk", flat=True))

    def expected(viewer: Viewer) -> set[int]:
        tier = audit_scopes.viewer_tier(viewer)
        result = set()
        for row in rows:
            scope = audit_scopes.scope_of(row.action, row.actor_type, row.entity_type)
            own = (row.actor_type == "staff" and row.actor_id == viewer.pk) or (
                row.entity_type == "staff" and row.entity_id == str(viewer.pk)
            )
            if (
                tier == "superuser"
                or scope == audit_scopes.BUSINESS
                or (tier == "manager" and scope in (audit_scopes.STAFF_ADMIN, audit_scopes.STAFF_SECURITY))
                or (scope == audit_scopes.STAFF_SECURITY and own)
            ):
                result.add(row.pk)
        return result

    for viewer in (Viewer(me), Viewer(me, is_manager=True), Viewer(me, is_superuser=True)):
        assert visible(viewer) == expected(viewer), viewer
    ordinary = {AuditEntry.objects.get(pk=pk).action for pk in visible(Viewer(me))}
    assert ordinary == {
        "cashback.adjusted",
        "billz.import_started",
        "auth.login_succeeded",
        "auth.login_failed",
        "auth.session_revoked",
        "auth.device_mismatch",
        "export.downloaded",
        "customer.logout",
    }
    assert len(visible(Viewer(me))) == 8  # 4 business + own sign-in, own failed login, own revoke, own export
    assert len(visible(Viewer(me, is_manager=True))) == 13  # + staff.created and every security row
    assert len(visible(Viewer(me, is_superuser=True))) == len(rows)


@pytest.mark.django_db
def test_own_entries_use_the_viewers_real_id() -> None:
    staff = get_user_model().objects.create_user(email="own@x.uz", password="x" * 12)
    entry = audit.record(action="auth.logout", entity_type="staff", entity_id=str(staff.pk), actor=staff)
    q = audit_scopes.visible_q(staff)
    assert q is not None and AuditEntry.objects.filter(q, pk=entry.pk).exists()
    other = get_user_model().objects.create_user(email="other@x.uz", password="x" * 12)
    q_other = audit_scopes.visible_q(other)
    assert q_other is not None and not AuditEntry.objects.filter(q_other, pk=entry.pk).exists()


# --- the indexes of core.0014 ------------------------------------------------------------------------------


@pytest.mark.django_db
def test_audit_indexes_exist() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = %s", [AuditEntry._meta.db_table]
        )
        indexes = dict(cursor.fetchall())
    like = indexes["audit_request_id_like_idx"]
    assert "varchar_pattern_ops" in like and "WHERE" in like and "request_id" in like
    assert "(actor_type, actor_id, created_at)" in indexes["audit_actor_created_idx"]


@pytest.mark.django_db
def test_request_id_prefix_filter_can_use_the_partial_index() -> None:
    from glowderma.reporting import selectors  # the query the admin API runs

    # the filter alone: with the list's ORDER BY the planner may walk the ``created_at`` index of a nearly
    # empty test table and filter, whatever the statistics left by earlier tests say
    qs = selectors.audit_log({"request_id": "6f1c2a0e"}, Viewer(1, is_superuser=True)).order_by()
    with connection.cursor() as cursor:
        cursor.execute("SET LOCAL enable_seqscan = off")
        sql, params = qs.query.sql_with_params()
        cursor.execute(f"EXPLAIN {sql}", params)
        plan = "\n".join(row[0] for row in cursor.fetchall())
    assert "audit_request_id_like_idx" in plan


# --- the scan: every audited action is classified ----------------------------------------------------------

ROOT = Path(glowderma.__file__).resolve().parent
REPO = ROOT.parent


class Unresolved(Exception):
    pass


@dataclass
class Module:
    path: Path
    tree: ast.Module
    constants: dict[str, ast.expr] = field(default_factory=dict)
    functions: dict[str, ast.FunctionDef] = field(default_factory=dict)
    # local name → (dotted module, attribute or None for a module alias)
    imports: dict[str, tuple[str, str | None]] = field(default_factory=dict)

    @property
    def dotted(self) -> str:
        return ".".join(self.path.relative_to(REPO).with_suffix("").parts)

    @property
    def rel(self) -> str:
        return self.path.relative_to(REPO).as_posix()


def _production_files() -> Iterator[Path]:
    for path in sorted(ROOT.rglob("*.py")):
        parts = path.relative_to(ROOT).parts
        if "tests" in parts or "migrations" in parts:
            continue
        yield path


def _load() -> dict[str, Module]:
    modules: dict[str, Module] = {}
    for path in _production_files():
        module = Module(path, ast.parse(path.read_text(encoding="utf-8")))
        for node in module.tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        module.constants[target.id] = node.value
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value:
                module.constants[node.target.id] = node.value
            elif isinstance(node, ast.FunctionDef):
                module.functions[node.name] = node
        for any_node in ast.walk(module.tree):
            if (
                isinstance(any_node, ast.ImportFrom)
                and any_node.module
                and any_node.module.startswith("glowderma")
            ):
                for alias in any_node.names:
                    module.imports[alias.asname or alias.name] = (any_node.module, alias.name)
            elif isinstance(any_node, ast.Import):
                for alias in any_node.names:
                    if alias.name.startswith("glowderma"):
                        module.imports[alias.asname or alias.name] = (alias.name, None)
        modules[module.dotted] = module
    return modules


def _module_named(modules: dict[str, Module], dotted: str) -> Module | None:
    return modules.get(dotted) or modules.get(f"{dotted}.__init__")


def _target_module(modules: dict[str, Module], module: Module, alias: str) -> Module | None:
    """The module a local alias stands for (``from glowderma.x import y`` with ``y`` a module, or
    ``import glowderma.x.y as y``)."""
    if alias not in module.imports:
        return None
    source, name = module.imports[alias]
    if name is None:
        return _module_named(modules, source)
    return _module_named(modules, f"{source}.{name}")


def _enclosing_functions(tree: ast.Module) -> dict[int, ast.FunctionDef | ast.AsyncFunctionDef]:
    owner: dict[int, ast.FunctionDef | ast.AsyncFunctionDef] = {}

    def visit(node: ast.AST, current: ast.FunctionDef | ast.AsyncFunctionDef | None) -> None:
        for child in ast.iter_child_nodes(node):
            inner = child if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else current
            if current is not None or inner is not None:
                owner[id(child)] = inner  # type: ignore[assignment]
            visit(child, inner)

    visit(tree, None)
    return owner


def _is_carried(expr: ast.expr) -> bool:
    return isinstance(expr, ast.Attribute) and (
        expr.attr == "audit_action"
        or (expr.attr == "action" and isinstance(expr.value, ast.Attribute) and expr.value.attr == "audit")
    )


class Scanner:
    def __init__(self) -> None:
        self.modules = _load()
        self.owners = {name: _enclosing_functions(module.tree) for name, module in self.modules.items()}
        self._carried: set[str] | None = None

    # -- resolution ------------------------------------------------------------------------------------

    def resolve(
        self,
        expr: ast.expr,
        module: Module,
        function: ast.FunctionDef | ast.AsyncFunctionDef | None,
        depth: int = 0,
    ) -> set[str]:
        if depth > 6:
            raise Unresolved(ast.unparse(expr))
        if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
            return {expr.value}
        if isinstance(expr, ast.JoinedStr):
            first = expr.values[0] if expr.values else None
            if isinstance(first, ast.Constant) and isinstance(first.value, str) and "." in first.value:
                return {first.value.partition(".")[0] + ".*"}
            raise Unresolved(ast.unparse(expr))
        if isinstance(expr, ast.IfExp):
            return self.resolve(expr.body, module, function, depth + 1) | self.resolve(
                expr.orelse, module, function, depth + 1
            )
        if isinstance(expr, ast.Name):
            return self._resolve_name(expr.id, module, function, depth)
        if isinstance(expr, ast.Attribute) and isinstance(expr.value, ast.Name):
            target = _target_module(self.modules, module, expr.value.id)
            if target is not None and expr.attr in target.constants:
                return self.resolve(target.constants[expr.attr], target, None, depth + 1)
        if _is_carried(expr):
            return self.carried_actions()
        raise Unresolved(ast.unparse(expr))

    def _resolve_name(
        self,
        name: str,
        module: Module,
        function: ast.FunctionDef | ast.AsyncFunctionDef | None,
        depth: int,
    ) -> set[str]:
        if function is not None:
            params = [arg.arg for arg in (*function.args.posonlyargs, *function.args.args)]
            kwonly = [arg.arg for arg in function.args.kwonlyargs]
            if name in params or name in kwonly:
                return self._from_callers(function, module, name, depth)
            assigned = [
                node.value
                for node in ast.walk(function)
                if isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == name for t in node.targets)
            ]
            if assigned:
                values: set[str] = set()
                for value in assigned:
                    values |= self.resolve(value, module, function, depth + 1)
                return values
        if name in module.constants:
            return self.resolve(module.constants[name], module, None, depth + 1)
        if name in module.imports:
            source, attr = module.imports[name]
            target = _module_named(self.modules, source)
            if target is not None and attr in target.constants:
                return self.resolve(target.constants[attr], target, None, depth + 1)
        raise Unresolved(name)

    def _from_callers(
        self, function: ast.FunctionDef | ast.AsyncFunctionDef, module: Module, param: str, depth: int
    ) -> set[str]:
        """Every value the callers of a module-level ``function`` pass for ``param``."""
        if module.functions.get(function.name) is not function:
            raise Unresolved(f"{module.rel}:{function.name}({param}) is not a module-level function")
        positional = [arg.arg for arg in (*function.args.posonlyargs, *function.args.args)]
        values: set[str] = set()
        calls = 0
        for caller_name, caller in self.modules.items():
            for node in ast.walk(caller.tree):
                if not isinstance(node, ast.Call) or not self._calls(node, caller, module, function.name):
                    continue
                calls += 1
                owner = self.owners[caller_name].get(id(node))
                argument = self._argument(node, positional, param)
                if argument is None:
                    raise Unresolved(f"{caller.rel}: call of {function.name} without {param}")
                values |= self.resolve(argument, caller, owner, depth + 1)
        if calls == 0:
            raise Unresolved(f"{module.rel}:{function.name} has no caller")
        return values

    def _calls(self, call: ast.Call, caller: Module, module: Module, name: str) -> bool:
        func = call.func
        if isinstance(func, ast.Name):
            if caller is module:
                return func.id == name
            return caller.imports.get(func.id) == (module.dotted, name)
        if isinstance(func, ast.Attribute) and func.attr == name and isinstance(func.value, ast.Name):
            return _target_module(self.modules, caller, func.value.id) is module
        return False

    @staticmethod
    def _argument(call: ast.Call, positional: list[str], param: str) -> ast.expr | None:
        for keyword in call.keywords:
            if keyword.arg == param:
                return keyword.value
        if param in positional:
            index = positional.index(param)
            if index < len(call.args) and not isinstance(call.args[index], ast.Starred):
                return call.args[index]
        return None

    # -- the sites -------------------------------------------------------------------------------------

    def record_calls(
        self,
    ) -> Iterator[tuple[Module, ast.FunctionDef | ast.AsyncFunctionDef | None, ast.Call]]:
        for name, module in self.modules.items():
            for node in ast.walk(module.tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "record"
                    and isinstance(node.func.value, ast.Name)
                    and _target_module(self.modules, module, node.func.value.id) is not None
                    and _target_module(self.modules, module, node.func.value.id)
                    is self.modules["glowderma.core.audit"]
                ):
                    yield module, self.owners[name].get(id(node)), node

    def carried_actions(self) -> set[str]:
        """Actions carried on objects instead of passed directly (``x.audit_action``,
        ``result.audit.action``): every ``audit_action=`` argument (``core.admin_actions.done``,
        ``commerce.state`` rules) and the fifth argument of a notifications ``Transition(...)``."""
        if self._carried is not None:
            return self._carried
        values: set[str] = set()
        self._carried = values  # a carried value that is itself carried resolves to what is known so far
        for name, module in self.modules.items():
            for node in ast.walk(module.tree):
                if not isinstance(node, ast.Call):
                    continue
                owner = self.owners[name].get(id(node))
                for keyword in node.keywords:
                    if keyword.arg == "audit_action":
                        values |= self.resolve(keyword.value, module, owner)
                if (
                    isinstance(node.func, ast.Name) and node.func.id == "Transition" and len(node.args) >= 5  # noqa: PLR2004 - Transition(name, from, to, actor, audit_action)
                ):
                    values |= self.resolve(node.args[4], module, owner)
        return values

    def entity_name_actions(self) -> set[str]:
        """``professionals.services.editable_rows.save_row`` / ``delete_row``:
        ``<ENTITY_NAMES value>.created|updated|deleted``."""
        module = self.modules["glowderma.professionals.services.editable_rows"]
        names = module.constants["ENTITY_NAMES"]
        assert isinstance(names, ast.Dict)
        return {
            f"{value.value}.{verb}"
            for value in names.values
            if isinstance(value, ast.Constant) and isinstance(value.value, str)
            for verb in ("created", "updated", "deleted")
        }


# Sites whose action is not a literal, a constant, a parameter chain or a carried ``audit_action``:
# (module, function) → how the test enumerates what they can write. A new unresolvable site fails the scan
# until it is listed here.
DECLARED_SITES = {
    ("glowderma/professionals/services/editable_rows.py", "save_row"): "entity_names",
    ("glowderma/professionals/services/editable_rows.py", "delete_row"): "entity_names",
}


def test_every_audited_action_is_classified() -> None:
    scanner = Scanner()
    found: dict[str, set[str]] = {}
    unresolved: list[str] = []
    seen_declared: set[tuple[str, str]] = set()
    calls = 0
    for module, function, call in scanner.record_calls():
        calls += 1
        keyword = next((k for k in call.keywords if k.arg == "action"), None)
        site = f"{module.rel}:{call.lineno}"
        if keyword is None:
            unresolved.append(f"{site}: audit.record without action=")
            continue
        key = (module.rel, function.name if function is not None else "")
        try:
            values = scanner.resolve(keyword.value, module, function)
        except Unresolved as exc:
            if key not in DECLARED_SITES:
                unresolved.append(f"{site}: {ast.unparse(keyword.value)} ({exc})")
                continue
            seen_declared.add(key)
            values = scanner.entity_name_actions()
        for value in values:
            found.setdefault(value, set()).add(site)
    assert calls > 100  # the scan really walked the code base  # noqa: PLR2004
    assert not unresolved, "audit.record sites the scan cannot follow:\n" + "\n".join(unresolved)
    assert seen_declared == set(DECLARED_SITES), "stale DECLARED_SITES entries"
    unclassified = {
        action: sorted(sites)
        for action, sites in found.items()
        if not audit_scopes.is_classified(action.replace(".*", ".any"))
    }
    assert not unclassified, (
        "audited actions no rule of core.audit_scopes names (they would be superuser-only by rule 5): "
        f"{unclassified}"
    )
    # sanity: the scan reaches the wrappers and the carried actions
    for action in (
        "console.login_failed",
        "ops_access.granted",
        "ops.outbox_requeued",
        "notification.draft_deleted",
        "service.deleted",
        "customer.created_by_staff",
        "customer.logout",
        "pos.qr_resolved",
        "appointment.*",
        "staff.created",
        "auth.login_failed",
        "export.downloaded",
    ):
        assert action in found, action


def test_scan_rejects_an_unclassified_action() -> None:
    assert not audit_scopes.is_classified("brand_new.thing")
    assert audit_scopes.is_classified("auth.anything")
