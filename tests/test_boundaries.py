"""Which package may import which, lazy imports included; a new edge fails
until it is added to `ALLOWED`.

    python tests/test_boundaries.py
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "polaris"

ALLOWED = {
    "polaris": {"config"},
    "config": set(),
    "app": {"catalog", "config", "derivation", "jobs", "models",
            "observation", "search", "state", "vocabulary"},
    "state": {"search", "shared", "vocabulary"},
    "cli": {"config", "derivation", "jobs", "models", "observation",
            "search", "shared", "vocabulary"},
    "shared": {"config", "models"},
    "catalog": {"derivation"},
    "derivation": set(),
    "jobs": {"derivation"},
    "models": {"config"},
    "observation": {"catalog", "config", "derivation", "jobs", "models",
                    "observation.domain", "observation.media", "shared"},
    "observation.domain": set(),
    "observation.media": {"observation.domain"},
    "search": {"config", "models", "shared"},
    "vocabulary": {"derivation", "search"},
}

ASSEMBLY_MAY_IMPORT = ({f"polaris.{c}.schema" for c in (
    "catalog", "derivation", "jobs", "models", "observation", "vocabulary")}
                       | {"polaris.shared.tuning", "polaris.shared.migrations"})


def module_name(path: Path) -> str:
    parts = list(path.relative_to(ROOT).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


MODULES = {module_name(p): p for p in PKG.rglob("*.py")}


def imports(name: str) -> list[tuple[str, int]]:
    """Every polaris module `name` imports, anywhere in the file."""
    path = MODULES[name]
    package = name if path.name == "__init__.py" else name.rsplit(".", 1)[0]
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            targets = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parts = package.split(".")[:len(package.split(".")) - node.level + 1]
                base = ".".join(parts + ([node.module] if node.module else []))
            targets = [f"{base}.{a.name}" if f"{base}.{a.name}" in MODULES
                       else base for a in node.names]
        else:
            continue
        for t in targets:
            if t == "polaris" or t.startswith("polaris."):
                while t not in MODULES:
                    t = t.rsplit(".", 1)[0]
                found.append((t, node.lineno))
    return found


def context(name: str) -> str:
    parts = name.split(".")
    if len(parts) == 1:
        return "polaris"
    if parts[1] == "observation" and len(parts) > 2 and parts[2] in ("media", "domain"):
        return f"observation.{parts[2]}"
    return parts[1]


def is_api(name: str) -> bool:
    return name.endswith(".api")


def interface(n: str) -> bool:
    return n in ("polaris.app", "polaris.state") or n.startswith("polaris.cli")


def test_imports() -> None:
    """Media is pure, schemas are strings, and the interface is a leaf."""
    for name in MODULES:
        for target, line in imports(name):
            where = f"{name}:{line} imports {target}"
            if name.startswith("polaris.observation.media"):
                assert (target == "polaris.observation.domain"
                        or target.startswith("polaris.observation.media")), where
            if name.endswith(".schema"):
                assert target in (ASSEMBLY_MAY_IMPORT if name == "polaris.shared.schema"
                                  else set()), where
            if not (interface(name) or is_api(name)):
                assert not (interface(target) or is_api(target)), where
    print("  ok  media imports only the domain, schemas import nothing, nothing imports the interface")


def test_edges_are_allowed() -> None:
    for name in MODULES:
        if is_api(name) or name.endswith(".schema"):
            continue
        for target, line in imports(name):
            src, dst = context(name), context(target)
            assert src == dst or dst in ALLOWED.get(src, set()), (
                f"{name}:{line} -> {target}: {src} -> {dst} is not in ALLOWED")
    print("  ok  every context edge outside api and schema is in ALLOWED")


def test_no_cycle() -> None:
    state = {}

    def visit(node: str, path: list[str]) -> None:
        state[node] = "open"
        for nxt in sorted(ALLOWED.get(node, ())):
            assert state.get(nxt) != "open", "ALLOWED has a cycle: " + " -> ".join(path + [node, nxt])
            if nxt not in state:
                visit(nxt, path + [node])
        state[node] = "done"

    for node in sorted(ALLOWED):
        if node not in state:
            visit(node, [])
    print("  ok  the allowed context graph has no cycle")


if __name__ == "__main__":
    test_imports()
    test_edges_are_allowed()
    test_no_cycle()
    print("all boundary checks passed")
