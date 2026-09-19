"""Shared types for tagger backends.

`RawScore` / `SampleScores` are what a scan stores. `TaggerBackend` is
the protocol every backend implements, and `BackendConfig` is one
backend's standing in a pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

import numpy as np
from PIL import Image

ALL_CATEGORIES = ("general", "character", "copyright", "artist", "rating")

DEFAULT_BATCH = 8


def batches(images: list, size: int):
    """`images` in chunks of exactly `size`, the last one padded.

    Yields `(chunk, real)`; only the first `real` entries are the
    caller's, the rest repeat the last one and their output is discarded.

    Padding rather than a short final chunk because a session re-plans
    when the input shape changes, and a work's last chunk is a different
    size in almost every work: measured, letting it vary costs more than
    the padding wastes by a wide margin. Speed is the whole of the reason.
    The size does not change what a model says about an image -- measured
    across seven batch shapes on both backends, bit-identical.
    """
    for i in range(0, len(images), size):
        chunk = images[i:i + size]
        real = len(chunk)
        if real < size:
            chunk = chunk + [chunk[-1]] * (size - real)
        yield chunk, real


class ImageViews:
    """One decoded image, and the resized copies of it that backends share.

    Each backend used to convert and resize the image for itself. animetimm
    and canary want the identical white-padded bicubic 448, so the second
    one to ask gets the first one's array. `convert` lets this stand in for
    the PIL image in a backend that asks for nothing else.
    """

    def __init__(self, image: Image.Image):
        self._image = image
        self.memo: dict = {}

    def _get(self, key, make):
        if key not in self.memo:
            self.memo[key] = make()
        return self.memo[key]

    def rgb(self) -> Image.Image:
        return self._get("rgb", lambda: self._image if self._image.mode == "RGB"
                         else self._image.convert("RGB"))

    def convert(self, mode: str) -> Image.Image:
        return self.rgb() if mode == "RGB" else self._image.convert(mode)


def views_of(image) -> ImageViews:
    return image if isinstance(image, ImageViews) else ImageViews(image)


@dataclass(slots=True, frozen=True)
class TagThreshold:
    """Where a model says one of its own tags should be cut.

    `f1` is what the model reports it achieves at that cutoff, and is not
    a threshold: it says how much to believe this tag at all, which is a
    separate decision from where to cut it. None when the model publishes
    a cutoff without a score for it.
    """

    category: str
    tag: str
    threshold: float
    f1: float | None = None


@dataclass(slots=True)
class RawScore:
    """One backend's unthresholded opinion about one tag on one image.

    This is what gets stored; no cutoff has been applied to it.
    """

    category: str
    tag: str
    score: float


@dataclass
class SampleScores:
    """Everything one backend observed about one image, before any cutoff.

    Scores and embedding travel together because they come out of the same
    forward pass; returning only the scores would force a second one.
    """

    scores: list[RawScore] = field(default_factory=list)
    embedding: list[float] | None = None


def top_scores(
    names: list[str],
    preds,
    indices,
    category: str,
    top_k: int,
    keep: frozenset[str] = frozenset(),
    floor: float = 0.01,
) -> list[RawScore]:
    """The top `top_k` tags of one category, plus anything in `keep`.

    Storing every tag of every sample is not affordable — one image against
    EVA02's vocabulary is 10,861 numbers — and almost all of it is noise
    well under any cutoff anyone would choose. `top_k` bounds the cost;
    `keep` is the escape hatch that makes it safe: a tag someone has
    curated is stored no matter how far down it ranked, so adding it to the
    vocabulary later does not require the GPU to run again.

    `floor` trims the deep tail, which is most of the volume. At 0.01 it
    sits an order of magnitude below any cutoff anyone would operate at, so
    it bounds storage without bounding what a later threshold change can
    reach. Set it to 0 to keep everything the top-k selects.
    """
    import numpy as np

    if len(indices) == 0:
        return []

    scores = preds[indices]
    k = min(top_k, len(indices))
    chosen = set(np.argpartition(scores, -k)[-k:].tolist()) if k > 0 else set()

    if floor > 0:
        chosen = {j for j in chosen if scores[j] >= floor}

    if keep:
        chosen.update(
            j for j in range(len(indices)) if names[indices[j]] in keep
        )

    return [
        RawScore(category, names[indices[j]], float(scores[j]))
        for j in chosen
    ]


class TaggerBackend(Protocol):
    """Protocol for tagger backends. Implement name, load(), prepare() and score_prepared()."""

    name: str

    def load(self) -> None:
        """Download model and initialize inference session.

        Called once before the first batch. Must be idempotent.
        """
        ...

    def unload(self) -> None:
        """Drop the inference session, freeing the device memory it held.

        `load()` builds it again on the next batch, so this is a cost
        decision rather than a lifecycle one: a worker with nothing queued
        has no reason to hold a card.
        """
        ...

    def prepare(self, image: Image.Image) -> "np.ndarray":  # noqa: F821
        """One image as this model wants it: resized, normalised, CHW.

        Split out from `score_prepared` because it is CPU work that has
        nothing to wait for. Kept together they run on whichever thread is
        blocked on the GPU; kept apart the sampler can do them while the
        GPU is busy with the batch before.
        """
        ...

    def score_prepared(
        self,
        arrays: list["np.ndarray"],  # noqa: F821
        top_k: int = 300,
        keep: frozenset[str] = frozenset(),
    ) -> list[SampleScores]:
        """Unthresholded scores for the output of `prepare`, one per array.

        No cutoff is applied, so a threshold can change after the fact.
        """
        ...

    def published_thresholds(self) -> list[TagThreshold]:
        """What the model publishes about where to cut each of its tags.

        A property of the model, read off the files it ships, so it must
        not need `load()` or a GPU. Optional: a model that publishes
        nothing has no such method, and `getattr` is how callers ask.

        The distinction is what the derived layer runs on. A backend that
        answers here is judged tag by tag on numbers its authors measured;
        one that stays silent gets a single cutoff for everything it says,
        and is outranked wherever a publishing backend knows the same tag.
        """
        ...


@dataclass
class BackendConfig:
    """Configuration for one tagger backend in the pipeline.

    Attributes:
        backend: The tagger backend instance.
        category_priority: category → priority. A category absent here is
            not taken from this backend at all; higher wins where two
            contribute the same tag. "embedding" is a pseudo-category
            saying whose vector the work gets.
        concurrent: False for a backend too slow or too greedy to share
            the GPU with the others.
        max_images: How many of the batch to send. None means all.
    """

    backend: TaggerBackend
    category_priority: dict[str, int]
    concurrent: bool = True
    max_images: int | None = None
