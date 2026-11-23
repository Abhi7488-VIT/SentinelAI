"""Static guards for layering and tenant isolation."""

from __future__ import annotations

import ast
import importlib
import pkgutil
from pathlib import Path

import app.models
import app.repositories
from app.db.base import Base
from app.repositories.base import TenantRepository

APP_DIR = Path(__file__).resolve().parents[2] / "app"

# Modules allowed to build SQL directly. Everything else goes through repositories.
_SQL_BUILDERS = {"select", "update", "delete", "insert", "text"}


def _tenant_models() -> set[type[Base]]:
    return {mapper.class_ for mapper in Base.registry.mappers if "org_id" in mapper.local_table.c}


def _tenant_repository_models() -> set[type[Base]]:
    for mod in pkgutil.iter_modules(app.repositories.__path__):
        importlib.import_module(f"app.repositories.{mod.name}")

    def walk(cls: type) -> set[type]:
        out: set[type] = set()
        sub: type
        for sub in cls.__subclasses__():
            out.add(sub)
            out |= walk(sub)
        return out

    subclasses: set[type] = walk(TenantRepository)
    return {sub.model for sub in subclasses if hasattr(sub, "model")}


def test_every_tenant_model_has_a_tenant_repository() -> None:
    missing = _tenant_models() - _tenant_repository_models()
    assert not missing, f"tenant-owned models without a TenantRepository: {missing}"
    assert len(_tenant_models()) >= 3  # sanity: projects, members, audit logs


def _sqlalchemy_builder_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module
            and node.module.startswith("sqlalchemy")
        ):
            found |= {a.name for a in node.names if a.name in _SQL_BUILDERS}
    return found


def test_services_and_routes_do_not_build_sql() -> None:
    """Queries live in repositories, the single place tenant filtering is enforced."""
    offenders = {
        str(p.relative_to(APP_DIR)): names
        for layer in ("services", "api")
        for p in (APP_DIR / layer).rglob("*.py")
        if (names := _sqlalchemy_builder_imports(p))
    }
    assert not offenders, offenders


def _imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found |= {f"{node.module}.{a.name}" for a in node.names}
        elif isinstance(node, ast.Import):
            found |= {a.name for a in node.names}
    return found


def test_only_investigation_tools_create_evidence() -> None:
    """Evidence is created only by deterministic tools (Phase 4 plan §4, invariant 1)."""
    tools_dir = APP_DIR / "investigation" / "tools"
    evidence_mod = APP_DIR / "investigation" / "evidence.py"
    offenders = sorted(
        str(p.relative_to(APP_DIR))
        for p in APP_DIR.rglob("*.py")
        if p != evidence_mod
        and tools_dir not in p.parents
        and any(
            name == "app.investigation.evidence" or name.startswith("app.investigation.evidence.")
            for name in _imports_of(p)
        )
    )
    assert not offenders, offenders
    # Sanity: the tools really do use it (the guard would be vacuous otherwise).
    assert any("app.investigation.evidence" in _imports_of(p) for p in tools_dir.rglob("*.py"))


def test_investigation_engine_does_not_build_sql() -> None:
    offenders = {
        str(p.relative_to(APP_DIR)): names
        for p in (APP_DIR / "investigation").rglob("*.py")
        if (names := _sqlalchemy_builder_imports(p))
    }
    assert not offenders, offenders
