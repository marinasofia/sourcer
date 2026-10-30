"""Import layering is a design rule, so it is a test.

Layers, lowest first. A module may import from its own layer or below,
never above. The rule keeps the policy and storage code free of entry-point
concerns and makes the dependency direction reviewable in one place.
Modules are named by their dotted path inside the package; a subpackage's
``__init__`` is named by the package itself.
"""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent / "sourcer"

LAYERS: dict[str, int] = {
    # 0: pure configuration and logging
    "config": 0,
    "log": 0,
    "progress": 0,
    "preferences": 0,
    "eligibility": 0,  # the candidate's rules: reads config only
    # 1: infrastructure adapters (model API, storage, boards)
    "llm": 1,
    "claude_cli": 1,
    "sqlitedb": 1,
    "snowdb": 1,
    "db": 1,
    "trace": 1,
    "net": 1,
    "sources": 1,
    "sources._common": 1,
    "sources.greenhouse": 1,
    "sources.lever": 1,
    "sources.ashby": 1,
    "sources.workable": 1,
    "sources.smartrecruiters": 1,
    "sources.careers_page": 1,
    "sources.simplify": 1,
    "sources.rippling": 1,
    "sources.workday": 1,
    "sources.eightfold": 1,
    "sources.oracle": 1,
    "trust": 1,
    # onboarding: collectors and local files sit with the other adapters
    "onboard": 1,
    "onboard.items": 1,
    "onboard.privacy": 1,
    "onboard.store": 1,
    "onboard.backends": 1,
    "onboard.resume": 1,
    "onboard.github": 1,
    "onboard.folders": 1,
    "onboard.linkedin": 1,
    "onboard.collect": 1,
    "onboard.extract": 1,
    "onboard.weights": 1,
    "onboard.web": 1,
    "onboard.links": 1,
    # 2: the pipeline
    "pipeline": 2,
    "pipeline.state": 2,
    "pipeline.source": 2,
    "pipeline.dedupe": 2,
    "pipeline.filters": 2,
    "pipeline.record": 2,
    "pipeline.scoring": 2,
    "pipeline.output": 2,
    "pipeline.graph": 2,
    "digest": 2,
    # 3: entry points and operator tools
    "main": 3,
    "lambda_handler": 3,
    "serve": 3,
    "mcp_server": 3,
    "track": 3,
    "alerts": 3,
    "apply": 3,
    "apply.verify": 3,
    "apply.recheck": 3,
    "apply.tailor": 3,
    "apply.prefill": 3,
    "bot": 3,
    "bot.telegram": 3,
    "discovery": 3,
    "discovery.builtin": 3,
    "discovery.builtin_jobs": 3,
    "discovery.cli": 3,
    "discovery.getro": 3,
    "discovery.hn": 3,
    "discovery.http": 3,
    "discovery.models": 3,
    "discovery.service": 3,
    "discovery.simplify": 3,
    "discovery.yc": 3,
    "probe": 3,
    "orchestrator": 3,
    "rescore": 3,
    "rescreen": 3,
    "restore": 3,
    "export": 3,
    "export_public": 3,
    "export_showcase": 3,
    "score_listings": 3,
    "batch": 3,
    "evaluate": 3,
    "onboard.cli": 3,
    "onboard.sync": 3,
}


def module_name(path: Path) -> str:
    rel = path.relative_to(PACKAGE).with_suffix("")
    return ".".join(p for p in rel.parts if p != "__init__")


def all_modules() -> dict[str, Path]:
    """Every module in the package by dotted name, excluding the package root."""
    return {module_name(p): p for p in PACKAGE.rglob("*.py") if module_name(p)}


def is_module(dotted: str) -> bool:
    target = PACKAGE.joinpath(*dotted.split("."))
    return target.with_suffix(".py").exists() or (target / "__init__.py").exists()


def internal_imports(path: Path) -> set[str]:
    """Dotted names of package modules reached through relative imports."""
    tree = ast.parse(path.read_text())
    own = module_name(path).split(".") if module_name(path) else []
    package = own if path.name == "__init__.py" else own[:-1]
    names: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.ImportFrom) and node.level >= 1):
            continue
        base = package[: max(0, len(package) - (node.level - 1))]
        if node.module:
            names.add(".".join([*base, *node.module.split(".")]))
        else:
            # ``from . import x``: keep only names that are modules, not
            # package attributes such as __version__
            for alias in node.names:
                dotted = ".".join([*base, alias.name])
                if is_module(dotted):
                    names.add(dotted)
    return names


def test_every_module_is_assigned_a_layer() -> None:
    modules = set(all_modules())
    assert modules == set(LAYERS), f"unassigned or stale modules: {modules ^ set(LAYERS)}"


def test_no_module_imports_a_higher_layer() -> None:
    violations = []
    for name, path in all_modules().items():
        layer = LAYERS[name]
        for imported in internal_imports(path):
            if LAYERS.get(imported, 99) > layer:
                violations.append(
                    f"{name} (layer {layer}) imports {imported} (layer {LAYERS.get(imported)})"
                )
    assert not violations, "\n".join(violations)


def test_storage_backends_are_reached_only_through_the_facade() -> None:
    for name, path in all_modules().items():
        if name in ("db", "sqlitedb", "snowdb"):
            continue
        assert not internal_imports(path) & {"sqlitedb", "snowdb"}, f"{name} bypasses sourcer.db"
