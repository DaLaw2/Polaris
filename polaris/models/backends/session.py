"""Opening an ONNX model: graph optimisations on, CUDA ahead of CPU.

Which graph is opened follows from which provider will run it. CUDA gets
the float16 copy `halfprec` makes; TensorRT gets the fused graph
`trtprep` makes and does its own float16, because the two arrive at
different numbers and each is only right for its own backend.
"""

import os

from onnxruntime import (
    GraphOptimizationLevel,
    InferenceSession,
    SessionOptions,
    get_available_providers,
)

from . import halfprec, trtprep


def _cuda_provider() -> str:
    """CUDA when it is there, CPU when it is not."""
    if "CUDAExecutionProvider" in get_available_providers():
        return "CUDAExecutionProvider"
    return "CPUExecutionProvider"


def preferred_provider() -> str:
    """TensorRT when the machine has it, then CUDA, then CPU.

    TensorRT changes what a model says slightly against float32, so it
    is recorded on the run. It is preferred rather than merely offered
    because it is also faster, and a machine without it simply never sees
    this branch.
    """
    if trtprep.available():
        return trtprep.PROVIDER
    return _cuda_provider()


def open_onnx_model(path: str, provider: str | None = None,
                    half: bool = False) -> InferenceSession:
    """A session over the model at `path`, in float16 when `half`.

    TensorRT runs in float16 only, so a float32 session goes to CUDA.
    `POLARIS_ONNX_PROVIDER` overrides the choice, which is how a machine
    with a GPU is asked to score on the CPU anyway, and how TensorRT is
    refused on a machine that has it.
    """
    chosen = provider or os.environ.get("POLARIS_ONNX_PROVIDER") \
        or preferred_provider()

    options = SessionOptions()
    options.graph_optimization_level = GraphOptimizationLevel.ORT_ENABLE_ALL

    if chosen == trtprep.PROVIDER:
        graph = trtprep.trt_graph(path) if half else None
        if graph is not None:
            session = InferenceSession(str(graph), options,
                                       providers=trtprep.providers(graph))
            if session.get_providers()[0] == trtprep.PROVIDER:
                trtprep.mark_in_use()
            return session
        chosen = _cuda_provider()

    if half:
        path = str(halfprec.half_precision(path))
    if chosen == "CPUExecutionProvider":
        options.intra_op_num_threads = os.cpu_count() or 1

    providers = [chosen]
    if "CPUExecutionProvider" not in providers:
        providers.append("CPUExecutionProvider")

    return InferenceSession(str(path), options, providers=providers)
