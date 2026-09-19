"""Where things are: roots, model cache, CUDA libraries.

Nothing here has a default that means something on one machine only —
where the library lives and how to reach the database are environment
variables, and their absence is an error rather than a guess.
"""

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent


def _load_dotenv() -> None:
    """Read `.env` at the project root, if there is one.

    A real environment variable wins: the file is the fallback, so one
    command can point at another database without editing it.
    """
    path = PROJECT_ROOT / ".env"
    if not path.exists():
        return
    from dotenv import load_dotenv

    load_dotenv(path, override=False)


_load_dotenv()

MODELS_DIR = Path(os.environ.get(
    "POLARIS_MODELS_DIR",
    str(PROJECT_ROOT / "models"),
))
MODELS_DIR.mkdir(parents=True, exist_ok=True)
os.environ["HF_HUB_CACHE"] = str(MODELS_DIR)


def _register_cuda_libraries() -> list[str]:
    """Let onnxruntime-gpu find cuDNN and cuBLAS shipped in wheels.

    On Windows a DLL is found only if its directory is on the search path,
    and the CUDA libraries onnxruntime needs are not installed system-wide
    — they arrive inside `nvidia/*/bin` in site-packages, or `torch/lib`
    on a machine that has torch for its own reasons. Without this, every
    model fails one Conv node at a time with `cuDNN is unavailable`, which
    looks like a broken model rather than an unfound library.

    Returns the directories registered, for diagnostics.
    """
    if not hasattr(os, "add_dll_directory"):
        return []

    import site

    roots: list[Path] = []
    for base in {*site.getsitepackages(), site.getusersitepackages()}:
        p = Path(base)
        roots.append(p / "torch" / "lib")
        nvidia = p / "nvidia"
        if nvidia.is_dir():
            roots.extend(d / "bin" for d in nvidia.iterdir() if d.is_dir())

    registered = []
    for d in roots:
        if not d.is_dir():
            continue
        if not (any(d.glob("cudnn64_*.dll")) or any(d.glob("cublas64_*.dll"))):
            continue
        try:
            os.add_dll_directory(str(d))
        except OSError:
            continue
        os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")
        registered.append(str(d))
    return registered


def _register_tensorrt() -> list[str]:
    """Let onnxruntime find a TensorRT that was installed by hand.

    TensorRT does not arrive in a wheel, so its location is this machine's
    business and comes from `POLARIS_TENSORRT_DIR` -- the directory
    holding `nvinfer_10.dll`, or the release root above it. The returned
    handles are kept: `add_dll_directory`'s entry is removed when its
    handle is collected, which shows up as onnxruntime simply not offering
    the provider.
    """
    root = os.environ.get("POLARIS_TENSORRT_DIR", "")
    if not root or not hasattr(os, "add_dll_directory"):
        return []

    registered = []
    for d in (Path(root), Path(root) / "bin", Path(root) / "lib"):
        if not d.is_dir() or not any(d.glob("nvinfer*.dll")):
            continue
        try:
            _TENSORRT_HANDLES.append(os.add_dll_directory(str(d)))
        except OSError:
            continue
        os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")
        registered.append(str(d))
    return registered


_TENSORRT_HANDLES: list = []

CUDA_LIBRARY_DIRS = _register_cuda_libraries()
TENSORRT_LIBRARY_DIRS = _register_tensorrt()

DATABASE_DSN = os.environ.get("POLARIS_DSN") or ""


def require_dsn() -> str:
    """The database DSN, or a clear instruction if it is not configured."""
    if not DATABASE_DSN:
        raise RuntimeError(
            "POLARIS_DSN is not set. Point it at a PostgreSQL database with "
            "the pgvector extension, e.g.\n"
            "  POLARIS_DSN=postgresql://user:pass@localhost:5432/polaris")
    return DATABASE_DSN


@dataclass(frozen=True)
class Collection:
    """One root of the library.

    A root and a name, and nothing else: a directory segment means
    whatever `term_map` says it means, and a segment nobody has mapped
    means nothing, which is why no exclusion list is needed.

    `searchable` is what "indexed but out of the way" means — the
    collection is stored, browsable and searchable when asked for by
    name, but never included in a search that did not name it.

    """

    name: str
    root: Path
    searchable: bool = True


_collections_cache: list[Collection] = []
_resolved_roots: list[tuple[Path, Collection]] = []


async def load_collections(conn) -> list[Collection]:
    """Read the configured roots and make them this process's collections.

    Set once by whoever opened the connection: a scan resolves a work's
    collection on a decoder thread with no database near it.
    """
    global _collections_cache, _resolved_roots
    rows = await conn.fetch(
        "SELECT name, root, searchable FROM collections "
        "ORDER BY ordinal, name")
    _collections_cache = [
        Collection(r["name"], Path(r["root"]), r["searchable"])
        for r in rows
    ]
    roots = []
    for c in _collections_cache:
        try:
            roots.append((c.root.resolve(), c))
        except OSError:
            continue
    roots.sort(key=lambda rc: len(rc[0].parts), reverse=True)
    _resolved_roots = roots
    return _collections_cache


def collections() -> list[Collection]:
    """Every configured collection, as last loaded.

    Empty until `load_collections` has run. An empty list means nobody has
    said what this installation holds, not that it holds nothing.
    """
    return _collections_cache


def searchable_collections() -> list[str] | None:
    """Which collections a search covers when the caller names none.

    Every one marked searchable, and None — meaning no narrowing at all —
    when nothing is configured or nothing is marked.

    Not "the first one". `ordinal` orders a list for a person to read;
    `searchable` is the flag whose whole job is this answer, and reading
    `ordinal` here left it decorative: an installation could mark two
    collections searchable and still have plain searches cover one.
    """
    names = [c.name for c in _collections_cache if c.searchable]
    return names or None


def collection_for(path: Path | str) -> Collection | None:
    """Which collection a path belongs to, or None if no root holds it.

    The deepest matching root wins, so a collection nested inside another
    one is attributed to itself rather than to its parent.

    None is an answer, not a failure: a path under no configured root
    belongs to no collection, and the caller says so rather than filing it
    under whichever name happened to be first.
    """
    p = Path(path).resolve()
    for root, c in _resolved_roots:
        if p == root or root in p.parents:
            return c
    return None
