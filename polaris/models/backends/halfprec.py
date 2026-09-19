"""A half-precision copy of a model's graph, converted once and kept.

Faster on the taggers this project runs, and it roughly halves what a
loaded session holds.

`keep_io_types` leaves the graph taking and returning float32, so nothing
that calls a backend knows this happened. What it changes is the answer:
against the shipped per-tag thresholds it moves a small number of tags.
That makes it an observation parameter, recorded on the run beside the
batch size.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from polaris import config

CACHE_DIR = config.MODELS_DIR / "fp16"


def cached_path(source: str | Path) -> Path:
    """Where the half-precision copy of `source` lives.

    Named after the file it came from plus a digest of that file's
    identity, so a model that changes upstream converts again rather than
    being served a copy of the previous one.
    """
    source = Path(source)
    stat = source.stat()
    digest = hashlib.sha1(
        f"{source.resolve()}:{stat.st_size}".encode("utf-8", "replace")
    ).hexdigest()[:12]
    return CACHE_DIR / f"{source.stem}-{digest}.fp16.onnx"


def half_precision(source: str | Path) -> Path:
    """`source` with its weights in float16, converting on first use.

    Falls back to the original path if the conversion cannot be made:
    a slower scan is a better outcome than a failed one, and every
    backend can run the graph it was given.
    """
    source = Path(source)
    out = cached_path(source)
    if out.exists():
        return out

    try:
        model = _convert(source)
    except Exception as e:
        print(f"  [WARN] float16 conversion of {source.name} failed, "
              f"using float32 ({e})")
        return source

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".partial")
    import onnx

    onnx.save(model, str(tmp))
    tmp.replace(out)
    return out


def _convert(source: Path):
    """The float16 graph, repaired into something onnxruntime will load.

    The converter emits one cast per consumer of a blocked tensor and
    gives them all the same output name, which is not single assignment,
    and it inserts them wherever it likes rather than in topological
    order. Both are rejected at load time, so both are fixed here.
    """
    import onnx
    from onnxruntime.transformers.float16 import convert_float_to_float16
    from onnxruntime.transformers.onnx_model import OnnxModel

    model = convert_float_to_float16(
        onnx.load(str(source)), keep_io_types=True, disable_shape_infer=True)

    produced: dict[str, tuple] = {}
    kept = []
    for node in model.graph.node:
        signature = (node.op_type, tuple(node.input))
        clashes = [o for o in node.output if o in produced]
        if clashes and all(produced[o] == signature for o in clashes):
            continue
        for o in node.output:
            produced[o] = signature
        kept.append(node)
    del model.graph.node[:]
    model.graph.node.extend(kept)

    seen: set[str] = set()
    for i, node in enumerate(model.graph.node):
        if not node.name or node.name in seen:
            node.name = f"{node.op_type}_{i}"
        seen.add(node.name)

    wrapper = OnnxModel(model)
    wrapper.topological_sort()
    return wrapper.model
