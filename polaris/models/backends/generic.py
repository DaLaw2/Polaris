"""One backend for every model a definition describes."""

from __future__ import annotations

import numpy as np

from .. import definition as definitions
from .. import preprocess
from .base import (
    DEFAULT_BATCH, SampleScores, TagThreshold, batches, top_scores, views_of)
from .session import open_onnx_model


def select_output(outputs, wanted: str) -> str:
    """The name of the output `wanted` means: a name, "#<index>", or
    "widest" (the largest fixed last dimension)."""
    if wanted == "widest":
        return max(outputs, key=lambda o: o.shape[-1]
                   if isinstance(o.shape[-1], int) else -1).name
    if wanted.startswith("#"):
        return outputs[int(wanted[1:])].name
    if wanted not in {o.name for o in outputs}:
        raise ValueError(f"the model has no output {wanted!r}")
    return wanted


class DefinedBackend:
    """A `TaggerBackend` built from a `Definition`."""

    def __init__(self, d: definitions.Definition,
                 batch_size: int = DEFAULT_BATCH, half: bool = True,
                 provider: str | None = None):
        self.definition = d
        self.name = d.name
        self._batch_size = batch_size
        self._half = half
        self._provider = provider
        self._model = None
        self._fetch = definitions.fetch

    def load(self) -> None:
        if self._model is not None:
            return
        d = self.definition
        for extra in d.source.extra:
            self._fetch(d, extra)
        self.model_path = self._fetch(d, d.source.model)
        model = open_onnx_model(self.model_path, self._provider,
                                half=self._half)

        shape = model.get_inputs()[0].shape
        nhwc = shape[-1] == 3
        side = shape[1] if nhwc else shape[2]
        self._prepare = preprocess.chain(
            [(s.op, s.params) for s in d.preprocess],
            side if isinstance(side, int) and side > 0 else None, nhwc)
        self._input = model.get_inputs()[0].name
        outputs = model.get_outputs()
        self._outputs = [select_output(outputs, d.outputs.scores)]
        if d.outputs.embedding:
            self._outputs.append(select_output(outputs, d.outputs.embedding))

        _, self._names, cats = definitions.vocabulary(d, self._fetch)
        order = list(dict.fromkeys(d.vocabulary.categories.values()))
        by = np.asarray([c or "" for c in cats])
        self._idx = [(c, np.where(by == c)[0]) for c in order]
        self._model = model

    def unload(self) -> None:
        self._model = None

    def published_thresholds(self) -> list[TagThreshold]:
        return definitions.published_thresholds(self.definition, self._fetch)

    def prepare(self, image) -> np.ndarray:
        self.load()
        return self._prepare(views_of(image))

    def _predict(self, arrays):
        self.load()
        preds, embs = [], []
        for chunk, real in batches(arrays, self._batch_size):
            out = self._model.run(self._outputs, {self._input: np.stack(chunk)})
            p = out[0][:real]
            if self.definition.outputs.activation == "sigmoid":
                p = 1.0 / (1.0 + np.exp(-p))
            preds.append(p)
            if len(out) > 1:
                embs.append(out[1][:real])
        return (np.concatenate(preds, axis=0),
                np.concatenate(embs, axis=0) if embs else None)

    def score_prepared(
        self,
        arrays: list[np.ndarray],
        top_k: int = 300,
        keep: frozenset[str] = frozenset(),
    ) -> list[SampleScores]:
        if not arrays:
            return []
        preds, embs = self._predict(arrays)
        width = preds.shape[1]
        idx = [(c, i[i < width]) for c, i in self._idx]
        return [SampleScores(
                    scores=[s for c, i in idx for s in (
                        top_scores(self._names, p, i, c, len(i))
                        if c == "rating" else
                        top_scores(self._names, p, i, c, top_k, keep))],
                    embedding=(embs[n].astype(np.float32).tolist()
                               if embs is not None else None))
                for n, p in enumerate(preds)]
