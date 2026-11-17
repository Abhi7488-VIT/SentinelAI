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
