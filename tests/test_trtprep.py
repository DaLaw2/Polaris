"""python tests/test_trtprep.py"""
import os
import tempfile
from pathlib import Path

import numpy as np
import onnx.parser
import onnxruntime as ort

from polaris import config
from polaris.models.backends import session, trtprep
from polaris.observation import calibration

EPS = 1e-5
HEAD = '<ir_version: 10, opset_import: ["" : 17, "com.microsoft" : 1]>\n'


def _model(text, **init):
    m = onnx.parser.parse_model(HEAD + text)
    m.graph.initializer.extend(onnx.numpy_helper.from_array(v, k) for k, v in init.items())
    return m


def _run(model, x):
    s = ort.InferenceSession(model.SerializeToString(), providers=["CPUExecutionProvider"])
    return s.run(None, {"x": x})[0]


def test_provider_choice() -> None:
    """TensorRT only where the machine has it, whatever the wheel lists."""
    was = os.environ.pop("POLARIS_ONNX_PROVIDER", None)
    try:
        got = session.preferred_provider()
        want = [trtprep.PROVIDER] if trtprep.available() else [
            "CUDAExecutionProvider", "CPUExecutionProvider"]
        assert got in want, got
        print(f"  ok  preferred provider here: {got}")
    finally:
        if was is not None:
            os.environ["POLARIS_ONNX_PROVIDER"] = was
    if trtprep.PROVIDER not in ort.get_available_providers():
        print("  --  this wheel has no TensorRT compiled in; wheel-vs-machine unchecked")
        return
    dirs, cdll = config.TENSORRT_LIBRARY_DIRS, trtprep.ctypes.CDLL
    try:
        config.TENSORRT_LIBRARY_DIRS = []
        trtprep.ctypes.CDLL = lambda *a, **k: (_ for _ in ()).throw(OSError("absent"))
        assert trtprep.available() is False
    finally:
        config.TENSORRT_LIBRARY_DIRS, trtprep.ctypes.CDLL = dirs, cdll
    print("  ok  listed by the wheel, absent from the machine: not available")


def test_run_records_the_engine_built() -> None:
    was = trtprep._in_use
    try:
        trtprep._in_use = False
        assert not trtprep.in_use() and "tensorrt" not in calibration.observation_params()
        trtprep.mark_in_use()
        assert calibration.observation_params()["tensorrt"] is True
    finally:
        trtprep._in_use = was
    print("  ok  the params follow the session, not the intention")


def test_gelu_rewrite() -> None:
    x = np.linspace(-8, 8, 4096, dtype=np.float32).reshape(1, -1)
    model = _model("g (float[1,4096] x) => (float[1,4096] y)\n"
                   "{ a = Abs(x)  g = com.microsoft.Gelu(a)  y = Neg(g) }")
    before = _run(model, x)
    trtprep._rewrite_gelu(model)
    ops = [n.op_type for n in model.graph.node]
    assert not [n for n in model.graph.node if n.domain], ops
    assert ops[0] == "Abs" and ops[-1] == "Neg", ops
    worst = np.abs(_run(model, x) - before).max()
    assert worst < 1e-6, worst
    print(f"  ok  standard ops reproduce Gelu to {worst:.2e} over [-8, 8]")


def test_layernorm_substitution() -> None:
    """On a mean-zero input the standard LayerNorm is the simplified one."""
    rng = np.random.default_rng(0)
    raw = rng.normal(0, 3, (2, 7, 64)).astype(np.float32)
    y = raw - raw.mean(axis=-1, keepdims=True)
    scale = rng.normal(1, 0.2, 64).astype(np.float32)
    model = _model("ln (float[2,7,64] x) => (float[2,7,64] y)\n"
                   f"{{ y = LayerNormalization<axis=-1, epsilon={EPS}>(x, s) }}", s=scale)

    def simplified(v):
        return scale * v / np.sqrt((v ** 2).mean(axis=-1, keepdims=True) + EPS)
    worst = np.abs(_run(model, y) - simplified(y)).max()
    assert worst < 2e-5, worst
    assert np.abs(_run(model, y + 5.0) - simplified(y + 5.0)).max() > 0.1
    print(f"  ok  mean-zero: the two agree to {worst:.2e}; shifted: they do not")


def test_shape_floats_fold() -> None:
    x = np.random.default_rng(1).normal(0, 1, (8, 16)).astype(np.float32)
    model = _model("scaled (float[8,16] x) => (float[8,16] y)\n"
                   "{ shape = Shape(x)  dim = Gather<axis=0>(shape, one)  "
                   "dimf = Cast<to=1>(dim)  scale = Sqrt(dimf)  y = Div(x, scale) }",
                   one=np.array(1, np.int64))
    before = _run(model, x)
    with tempfile.TemporaryDirectory() as tmp:
        src, dst = Path(tmp) / "m.onnx", Path(tmp) / "folded.onnx"
        onnx.save(model, str(src))
        assert trtprep._fold_shape_constants(src, dst) == dst
        folded = onnx.load(str(dst))
        assert trtprep._fold_shape_constants(dst, Path(tmp) / "twice.onnx") is None
    assert [n.op_type for n in folded.graph.node] == ["Div"]
    assert np.array_equal(_run(folded, x), before)
    print("  ok  Shape -> Sqrt folds to a constant, bit-identical; nothing to fold twice")


test_provider_choice()
test_run_records_the_engine_built()
test_gelu_rewrite()
test_layernorm_substitution()
test_shape_floats_fold()
print("all trtprep checks passed")
