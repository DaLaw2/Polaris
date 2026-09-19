"""A TensorRT-ready copy of a model's graph, converted once and kept.

TensorRT is worth 2.33x on the pair of taggers this project runs, and it
cannot have the graph as exported. Both exports decompose LayerNorm into
`ReduceMean/Sub/Pow/Sqrt/Div`, and on animetimm 55 of those 73 squares
exceed float16's 65,504 ceiling -- the largest is 108,309,696. What saves
the shipping float16 path is not the converter's block list but
onnxruntime's own fusion, whose `LayerNormalization` kernel accumulates in
float32. That fusion runs *after* execution-provider partitioning, so
TensorRT never sees it: it lowers the graph it was given, overflows, and
compounds the error across every block.

So the fusion is done here, ahead of time, and the result is handed over
as a graph TensorRT can take whole. Four things have to be true for that:

1. The `LayerNormalization` fusion happens, but the two rewrites that emit
   `com.microsoft` ops do not -- those are unparseable, and a graph
   holding them is cut into an engine per island, which costs both memory
   and speed.
2. No `com.microsoft.Gelu` survives; it is rewritten with `Erf`.
3. No `SimplifiedLayerNormalization` survives. It sits in the *default*
   domain, so a check for foreign domains does not see it, and its input
   is already mean-subtracted, which is what makes the standard op an
   equivalent substitution rather than an approximation.
4. Shapes are inferred again and the opset is at least 17, or the parser
   refuses the model it was just handed.
5. No float is computed from a tensor shape. The 2026 WD canary derives
   each block's attention scale from `Shape`; onnxruntime places that on
   the CPU, the saved graph carries 24 `MemcpyFromHost`, and TensorRT cuts
   it into 25 engines at 11.8 GB. Folded into constants first: 1 engine.
6. A graph over protobuf's 2 GB (cl_tagger_v2, 2.2 GB) is optimised and
   shape-inferred through files, and `SkipLayerNormFusion` and
   `MatmulTransposeFusion` are off -- they emit
   `com.microsoft.SkipLayerNormalization` and `FusedMatMul` there.

Measured against float32, this moves a few more tags than the shipping
float16 path, and each of them sits close to its own threshold.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
from pathlib import Path

from polaris import config

PROVIDER = "TensorrtExecutionProvider"

CACHE_DIR = config.MODELS_DIR / "trt"
ENGINE_DIR = CACHE_DIR / "engines"

_DISABLED = ("MatMulScaleFusion;QuickGeluFusion;BiasGeluFusion;"
             "SkipLayerNormFusion;MatmulTransposeFusion")

_OPSET = 17

_PROTO_LIMIT = 2 ** 31 - 2 ** 24


_in_use = False


def available() -> bool:
    """Whether TensorRT will actually load, not whether it was compiled in.

    `get_available_providers` answers the second question: it lists the
    providers the wheel was built with, so it says TensorRT on a machine
    that has never had TensorRT installed. Asking it alone would make
    every such machine convert a graph, build nothing, and fall back to
    CUDA -- so the runtime itself is probed. It is found either because
    `POLARIS_TENSORRT_DIR` pointed `config` at it or because it is
    already on the loader's path.
    """
    from onnxruntime import get_available_providers

    if PROVIDER not in get_available_providers():
        return False
    if config.TENSORRT_LIBRARY_DIRS:
        return True
    for name in ("nvinfer_10.dll", "nvinfer.dll",
                 "libnvinfer.so.10", "libnvinfer.so"):
        try:
            ctypes.CDLL(name)
            return True
        except OSError:
            continue
    return False


def in_use() -> bool:
    """Whether a session in this process actually got TensorRT.

    Not the same question as `available`, and it is this one a run
    records: a graph that will not convert falls back to CUDA, and a run
    claiming an engine it never built would be describing someone else's
    scan. `mark_in_use` is called from `session.open_onnx_model` when the
    session is really there.
    """
    return _in_use


def mark_in_use() -> None:
    global _in_use
    _in_use = True


def cached_path(source: str | Path) -> Path:
    """Where the TensorRT-ready copy of `source` lives.

    The recipe is in the digest as well as the file, so changing which
    fusions are switched off converts again rather than serving a graph
    made by the previous rules.
    """
    source = Path(source)
    stat = source.stat()
    digest = hashlib.sha1(
        f"{source.resolve()}:{stat.st_size}:{_DISABLED}:{_OPSET}:fold"
        .encode("utf-8", "replace")
    ).hexdigest()[:12]
    return CACHE_DIR / f"{source.stem}-{digest}.trt.onnx"


def trt_graph(source: str | Path) -> Path | None:
    """`source` as a graph TensorRT can take whole, converting on first use.

    None when the conversion cannot be made, and the caller must then not
    use TensorRT at all. This is the one place `halfprec`'s "fall back to
    the graph as exported" would be wrong: TensorRT *will* run the graph
    as exported, at full speed, silently overflowing float16 -- measured
    at 109% of animetimm's tags changed. A slower scan beats a failed one,
    but a wrong one beats neither.
    """
    source = Path(source)
    out = cached_path(source)
    if out.exists():
        return out

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    staged = CACHE_DIR / f"{source.stem}.staged.onnx"
    try:
        model = _convert(source, staged)
    except Exception as e:
        print(f"  [WARN] TensorRT conversion of {source.name} failed, "
              f"scoring on CUDA instead ({e})")
        return None
    finally:
        for leftover in CACHE_DIR.glob(staged.name.split(".onnx")[0] + "*"):
            leftover.unlink(missing_ok=True)

    import onnx

    tmp = out.with_suffix(".partial")
    onnx.save(model, str(tmp), save_as_external_data=True,
              all_tensors_to_one_file=True, location=out.name + ".data",
              size_threshold=1024)
    tmp.replace(out)
    return out


def providers(graph: str | Path) -> list:
    """The provider list for `graph`, with its engine cache alongside.

    The cache directory's parents are created here because TensorRT only
    creates the leaf, and a missing parent is not an error: the provider
    fails to load and onnxruntime falls back to CUDA with one log line.
    """
    cache = ENGINE_DIR / Path(graph).stem
    cache.mkdir(parents=True, exist_ok=True)
    return [
        (PROVIDER, {
            "trt_fp16_enable": True,
            "trt_engine_cache_enable": True,
            "trt_engine_cache_path": str(cache),
            "trt_timing_cache_enable": True,
            "trt_timing_cache_path": str(ENGINE_DIR),
        }),
        "CUDAExecutionProvider",
        "CPUExecutionProvider",
    ]


def _size(source: Path) -> int:
    data = source.with_name(source.name + ".data")
    return source.stat().st_size + (data.stat().st_size if data.exists() else 0)


def _convert(source: Path, staged: Path):
    """The fused graph, with every op TensorRT does not parse removed."""
    import onnx
    import onnxruntime as ort

    big = _size(source) > _PROTO_LIMIT
    folded = _fold_shape_constants(
        source, staged.with_name(staged.stem + ".folded.onnx"))

    options = ort.SessionOptions()
    options.graph_optimization_level = \
        ort.GraphOptimizationLevel.ORT_ENABLE_EXTENDED
    options.add_session_config_entry(
        "optimization.disable_specified_optimizers", _DISABLED)
    if big:
        options.add_session_config_entry(
            "session.optimized_model_external_initializers_file_name",
            staged.name + ".data")
        options.add_session_config_entry(
            "session.optimized_model_external_initializers_min_size_in_bytes",
            "1024")
    options.optimized_model_filepath = str(staged)
    ort.InferenceSession(str(folded or source), options,
                         providers=["CUDAExecutionProvider"])

    model = onnx.load(str(staged))
    for node in model.graph.node:
        if node.op_type == "SimplifiedLayerNormalization":
            node.op_type = "LayerNormalization"
    _rewrite_gelu(model)

    for entry in model.opset_import:
        if (entry.domain or "ai.onnx") == "ai.onnx" and entry.version < _OPSET:
            entry.version = _OPSET

    left = {n.op_type for n in model.graph.node if n.domain}
    if left:
        raise ValueError(f"ops TensorRT cannot parse remain: {sorted(left)}")

    if not big:
        try:
            model = onnx.shape_inference.infer_shapes(
                model, strict_mode=False, data_prop=True)
        except Exception:
            pass
        return model

    unshaped = staged.with_name(staged.stem + ".shape.onnx")
    inferred = staged.with_name(staged.stem + ".inferred.onnx")
    onnx.save(model, str(unshaped), save_as_external_data=True,
              all_tensors_to_one_file=True, location=unshaped.name + ".data",
              size_threshold=1024)
    del model
    try:
        onnx.shape_inference.infer_shapes_path(
            str(unshaped), str(inferred), strict_mode=False, data_prop=True)
        return onnx.load(str(inferred))
    except Exception:
        return onnx.load(str(unshaped))


def _fold_shape_constants(source: Path, out: Path) -> Path | None:
    """`source` with every float computed only from shapes made a constant.

    A node is shape-only when all its inputs are initializers, constants or
    other shape-only outputs, `Shape` itself included. The float tensors
    where such a chain meets real data are evaluated once on the CPU at
    batch `DEFAULT_BATCH` -- the only batch `base.batches` ever sends -- and
    written in as initializers. None when there is nothing to fold, and
    when any input dimension past the batch is not fixed: a float derived
    from a free height or width is only a constant for one image size.
    Also None over 2 GB, which cannot be evaluated in memory; cl_tagger_v2
    has 4 such floats and builds one engine without folding them.
    """
    import numpy as np
    import onnx
    import onnxruntime as ort
    from onnx import numpy_helper

    from .base import DEFAULT_BATCH

    model = onnx.load(str(source), load_external_data=False)
    g = model.graph
    if any(d.dim_value <= 0 for i in g.input
           for d in i.type.tensor_type.shape.dim[1:]):
        return None
    fixed ={i.name for i in g.initializer} | {
        o for n in g.node if n.op_type == "Constant" for o in n.output}
    shape_only = set(fixed)
    for n in g.node:
        if n.op_type == "Shape" or (
                n.input and all(i == "" or i in shape_only for i in n.input)):
            shape_only.update(n.output)

    typed = onnx.shape_inference.infer_shapes(model).graph
    elem = {v.name: v.type.tensor_type.elem_type
            for v in [*typed.value_info, *typed.output, *typed.input]}
    floats = (onnx.TensorProto.FLOAT, onnx.TensorProto.FLOAT16,
              onnx.TensorProto.DOUBLE)
    frontier = sorted({
        i for n in g.node if not set(n.output) <= shape_only for i in n.input
        if i in shape_only and i not in fixed and elem.get(i) in floats})
    if not frontier or _size(source) > _PROTO_LIMIT:
        return None

    model = onnx.load(str(source))
    g = model.graph
    if len(g.input) != 1:
        return None
    probe = onnx.ModelProto()
    probe.CopyFrom(model)
    for t in frontier:
        probe.graph.output.append(
            onnx.helper.make_tensor_value_info(t, elem[t], None))
    inp = g.input[0]
    dims = [d.dim_value if d.dim_value > 0 else DEFAULT_BATCH
            for d in inp.type.tensor_type.shape.dim]
    values = ort.InferenceSession(
        probe.SerializeToString(), providers=["CPUExecutionProvider"]
    ).run(frontier, {inp.name: np.zeros(dims, np.float32)})
    del probe

    g.initializer.extend(numpy_helper.from_array(np.asarray(v), t)
                         for t, v in zip(frontier, values))
    nodes = [n for n in g.node if not set(n.output) & set(frontier)]
    outputs = {o.name for o in g.output}
    while True:
        used = {i for n in nodes for i in n.input} | outputs
        alive = [n for n in nodes if any(o in used for o in n.output)]
        if len(alive) == len(nodes):
            break
        nodes = alive
    del g.node[:]
    g.node.extend(nodes)
    used = {i for n in nodes for i in n.input} | outputs
    kept = [i for i in g.initializer if i.name in used]
    del g.initializer[:]
    g.initializer.extend(kept)

    onnx.save(model, str(out))
    return out


def _rewrite_gelu(model) -> None:
    """`com.microsoft.Gelu` as `0.5 x (1 + erf(x / sqrt(2)))`.

    Named plainly by whichever fusion emits it, so there is no optimizer
    to switch off and the node is rewritten instead.
    """
    import numpy as np
    from onnx import helper, numpy_helper

    hit = lambda n: n.domain == "com.microsoft" and n.op_type == "Gelu"  # noqa: E731
    if not any(hit(n) for n in model.graph.node):
        return

    model.graph.initializer.extend([
        numpy_helper.from_array(np.array(0.5, "float32"), "trtprep_half"),
        numpy_helper.from_array(np.array(1.0, "float32"), "trtprep_one"),
        numpy_helper.from_array(
            np.array(1.0 / np.sqrt(2.0), "float32"), "trtprep_isqrt2"),
    ])

    out = []
    for node in model.graph.node:
        if not hit(node):
            out.append(node)
            continue
        x, y, tag = node.input[0], node.output[0], node.name
        out += [
            helper.make_node("Mul", [x, "trtprep_isqrt2"], [tag + "_s"],
                             tag + "_s"),
            helper.make_node("Erf", [tag + "_s"], [tag + "_e"], tag + "_e"),
            helper.make_node("Add", [tag + "_e", "trtprep_one"], [tag + "_a"],
                             tag + "_a"),
            helper.make_node("Mul", [x, tag + "_a"], [tag + "_m"], tag + "_m"),
            helper.make_node("Mul", [tag + "_m", "trtprep_half"], [y],
                             tag + "_h"),
        ]
    del model.graph.node[:]
    model.graph.node.extend(out)
