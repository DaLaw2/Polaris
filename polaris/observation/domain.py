"""What a scan saw: samples, the numbers measured, one work's observation."""

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Sample:
    """One image handed to a model, or one that was considered and was not.

    The same shape for both media. For an image work this is a page and
    `position` is its index in the folder; for a video work it is a frame
    and `position` is its timestamp in seconds. Nothing downstream branches
    on `medium` — that is the point of there being one class.

    A rejected sample is still a sample: recording that page 14 was
    skipped as blank is what makes the denominator of any later frequency
    match the pages a model saw.
    """

    medium: str
    source_relpath: str
    position: float
    ordinal: int
    skip_reason: Optional[str] = None
    image: object = None

    @property
    def analyzed(self) -> bool:
        return self.skip_reason is None


@dataclass
class SampleStream:
    """A work's samples one at a time, and what was measured about it.

    A sample carries a decoded image, and the frame budget for a long
    video runs to thousands of them, so a work's samples do not
    necessarily fit in memory together. `measurements` is filled as the
    stream runs and is complete only once `samples` is exhausted.
    """

    measurements: list["Measurement"]
    samples: Iterator["Sample"]


@dataclass
class Measurement:
    """A number that was measured, before anyone decided what it means.

    `sample_ordinal` set means it describes one sample (a page's
    saturation, a frame's rating distribution); None means the whole work
    (page count, video duration, the CG-set features).
    """

    kind: str
    value: object
    sample_ordinal: Optional[int] = None


@dataclass
class WorkObservation:
    """What one scan saw of one work. No conclusions, no thresholds.

    The write-path counterpart of `WorkAnalysis`, which holds answers —
    `rating="explicit"`, `general_tags={"maid": 0.31}` — and not the
    numbers they came from. This holds the numbers.
    """

    path: str
    name: str
    collection: str
    content_key: Optional[str] = None
    samples: list["Sample"] = field(default_factory=list)
    scores: list[dict] = field(default_factory=list)
    measurements: list["Measurement"] = field(default_factory=list)
    embeddings: dict = field(default_factory=dict)
    error: Optional[str] = None

    @property
    def analyzed_samples(self) -> list["Sample"]:
        return [s for s in self.samples if s.analyzed]
