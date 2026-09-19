"""Frame sampling for video works.

A page count bounds an image work: 30 pages or 200, the whole thing is one
work with one artist and one setting. A duration does not bound a video
the same way, so a fixed frame count is wrong at both ends of any range of
runtimes.

A video does not run out of content at any particular n, and a single-shot
clip saturates at a handful of frames whatever n is. Both ends are handled
by deduplicating after sampling rather than by choosing n cleverly, so n
is a budget rather than a measurement, and it is spent proportionally to
duration — the cheapest predictor of distinct-scene count a container will
give up without being demuxed.

Every number that budget is spent by was priced against one library's
distribution of runtimes, so each is a row in `derivation_params` and
`calibration.load_settings` reads them.

Frames come from wherever the encoder placed a keyframe near the target
timestamp, not from an exact frame index: exact seeking has to decode
forward from the preceding keyframe, and buys nothing, since a frame
1/30 s away from the one requested shows the same scene.
"""

from pathlib import Path

import numpy as np
from PIL import Image

from polaris.observation.domain import Measurement, Sample, SampleStream

VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".wmv", ".mov", ".flv", ".ts",
    ".m4v", ".webm", ".rmvb", ".mpg", ".mpeg",
}

FRAME_BUDGET_BASE = 8
FRAME_BUDGET_PER_MINUTE = 6.0
FRAME_BUDGET_MIN = 4

FRAME_BUDGET_MAX: int | None = None

EDGE_MARGIN = 0.03

SCENE_DISTANCE = 12.0

SIGNATURE_SIZE = 32

FRAME_MAX_SIZE: int | None = 1280

BLANK_THRESHOLD = 0.95
NEAR_WHITE = 240
NEAR_BLACK = 15

_SHIPPED: dict[str, float] = {
    "video:budget_base": FRAME_BUDGET_BASE,
    "video:budget_per_minute": FRAME_BUDGET_PER_MINUTE,
    "video:budget_min": FRAME_BUDGET_MIN,
    "video:budget_max": 0.0,
    "video:edge_margin": EDGE_MARGIN,
    "video:scene_distance": SCENE_DISTANCE,
    "video:frame_max_size": FRAME_MAX_SIZE,
    "sample:blank_ratio": BLANK_THRESHOLD,
    "sample:near_white": NEAR_WHITE,
    "sample:near_black": NEAR_BLACK,
}


def apply_settings(p: dict[str, float]) -> None:
    """Make the frame-budget numbers, as read, this build's defaults.

    Set once per scan by `calibration.load_settings`, for the same reason
    as `sampler.apply_settings`: decoding happens on a thread with no
    database near it.

    `video:budget_max` says 0 for "no cap", because the column is NOT NULL
    and a row is how an installation asks for one.
    """
    global FRAME_BUDGET_BASE, FRAME_BUDGET_PER_MINUTE, FRAME_BUDGET_MIN
    global FRAME_BUDGET_MAX, EDGE_MARGIN, SCENE_DISTANCE
    global BLANK_THRESHOLD, NEAR_WHITE, NEAR_BLACK, FRAME_MAX_SIZE

    FRAME_BUDGET_BASE = int(p["video:budget_base"])
    FRAME_BUDGET_PER_MINUTE = p["video:budget_per_minute"]
    FRAME_BUDGET_MIN = int(p["video:budget_min"])
    cap = int(p["video:budget_max"])
    FRAME_BUDGET_MAX = cap if cap > 0 else None
    EDGE_MARGIN = p["video:edge_margin"]
    SCENE_DISTANCE = p["video:scene_distance"]
    side = int(p["video:frame_max_size"])
    FRAME_MAX_SIZE = side if side > 0 else None
    BLANK_THRESHOLD = p["sample:blank_ratio"]
    NEAR_WHITE = p["sample:near_white"]
    NEAR_BLACK = p["sample:near_black"]


def frame_budget(duration_s: float, base: int, per_minute: float,
                 minimum: int, maximum: int | None) -> int:
    """How many frames to sample from a video of this length.

    Nothing here has a default. Each of these was priced against one
    library's distribution of runtimes, so a signature carrying them would
    be this module hiding a row; the budget a video actually got is stored
    as a measurement, so the choice stays auditable afterwards.
    """
    if duration_s <= 0:
        return minimum
    n = base + per_minute * (duration_s / 60.0)
    if maximum is not None:
        n = min(n, maximum)
    return max(minimum, round(n))


def _budget(duration_s: float) -> int:
    """`frame_budget` under the numbers `apply_settings` last set."""
    return frame_budget(duration_s, FRAME_BUDGET_BASE,
                        FRAME_BUDGET_PER_MINUTE, FRAME_BUDGET_MIN,
                        FRAME_BUDGET_MAX)


def _signature(frame) -> np.ndarray:
    """Greyscale thumbnail, scaled by libswscale inside the decoder.

    Given `_view`'s output rather than the decoded frame, so the scale it
    asks for starts from a bounded picture.
    """
    small = frame.reformat(
        width=SIGNATURE_SIZE, height=SIGNATURE_SIZE, format="gray8"
    )
    return small.to_ndarray().astype(np.float32)


def _is_blank(sig: np.ndarray, blank_ratio: float, near_white: float,
              near_black: float) -> bool:
    """Whether a frame is near enough to all white or all black to skip."""
    n = sig.size
    return (np.count_nonzero(sig > near_white) / n > blank_ratio
            or np.count_nonzero(sig < near_black) / n > blank_ratio)


def sample_video_frames(
    path: Path,
    n: int | None = None,
    skip_blank: bool = True,
    deduplicate: bool = True,
    max_size: int | None = None,
) -> list[Image.Image]:
    """The frames of a video that survive sampling, as RGB images.

    `stream_video_work` with the rejected samples dropped. A file that will
    not open returns an empty list and a frame that will not decode is
    skipped; neither raises.
    """
    stream = stream_video_work(path, n, skip_blank, deduplicate, max_size)
    return [s.image for s in stream.samples if s.analyzed]


def stream_video_work(
    path: Path,
    n: int | None = None,
    skip_blank: bool = True,
    deduplicate: bool = True,
    max_size: int | None = None,
) -> "SampleStream":
    """Sample frames and say what happened to each one, one at a time.

    The media-neutral counterpart of `sampler.stream_image_work`: same
    `Sample` shape, same contract that a rejected sample is still recorded.

    Yielded rather than returned because the budget is proportional to
    duration and has no cap: a long enough video is priced at thousands
    of frames, which is more decoded image than a machine has. A caller
    that converts and releases each frame holds one at a time whatever the
    video's length.

    `position` is the timestamp in seconds, fractional, which is why
    `work_samples.position` is NUMERIC: a page index and a timestamp are
    the same kind of fact about where in a work something was seen.

    `n` is `video:budget_*` as last loaded when the caller names none, and
    the edge margin and scene distance are always the loaded ones. Read
    here rather than in the signature, because a default argument binds
    before `apply_settings` can have run.
    """
    import av

    edge_margin, scene_distance = EDGE_MARGIN, SCENE_DISTANCE

    measurements: list[Measurement] = []
    rel = path.name

    def nothing(note: dict) -> "SampleStream":
        measurements.append(Measurement("video_probe", note))
        return SampleStream(measurements, iter(()))

    try:
        container = av.open(str(path))
    except Exception:
        return nothing({"opened": False})

    try:
        stream = container.streams.video[0]
    except Exception:
        container.close()
        return nothing({"opened": True, "video_stream": False})

    duration = None
    if stream.duration:
        duration = float(stream.duration * stream.time_base)
    elif container.duration:
        duration = float(container.duration / av.time_base)
    if not duration or duration <= 0:
        container.close()
        return nothing({"opened": True, "duration": None})

    if n is None:
        n = _budget(duration)
    side = FRAME_MAX_SIZE if max_size is None else max_size

    measurements.append(Measurement("duration_s", duration))
    measurements.append(Measurement("frame_budget", n))

    def frames() -> "Iterator[Sample]":
        kept = 0
        with container:
            stream.codec_context.skip_frame = "NONKEY"
            signatures: list[np.ndarray] = []
            span = 1.0 - 2 * edge_margin
            for i in range(n):
                target = duration * (edge_margin + span * i / max(n - 1, 1))
                sample = Sample(
                    medium="video",
                    source_relpath=rel,
                    position=round(target, 3),
                    ordinal=i,
                )
                try:
                    container.seek(int(target / stream.time_base),
                                   stream=stream)
                    got = False
                    for frame in container.decode(stream):
                        got = True
                        if frame.pts is not None:
                            sample.position = round(
                                float(frame.pts * stream.time_base), 3)
                        view = _view(frame, side)
                        sig = _signature(view)
                        if skip_blank and _is_blank(
                                sig, BLANK_THRESHOLD, NEAR_WHITE, NEAR_BLACK):
                            sample.skip_reason = "blank"
                            break
                        if deduplicate and any(
                            np.abs(sig - s).mean() < scene_distance
                            for s in signatures
                        ):
                            sample.skip_reason = "duplicate_scene"
                            break
                        signatures.append(sig)
                        sample.image = view.to_image()
                        break
                    if not got:
                        sample.skip_reason = "decode_error"
                except Exception:
                    sample.skip_reason = "decode_error"

                if sample.analyzed:
                    kept += 1
                yield sample
        measurements.append(Measurement("frames_kept", kept))

    return SampleStream(measurements, frames())


def reload_frames(path: Path, samples: list):
    """Decode already-recorded frame samples again at their timestamps, for
    models that have not scored them. A frame that no longer decodes comes
    back not analyzed."""
    import av

    try:
        container = av.open(str(path))
        stream = container.streams.video[0]
    except Exception:
        for sample in samples:
            sample.skip_reason = "decode_error"
            yield sample
        return
    with container:
        stream.codec_context.skip_frame = "NONKEY"
        for sample in samples:
            try:
                container.seek(int(float(sample.position) / stream.time_base),
                               stream=stream)
                frame = next(container.decode(stream))
                sample.image = _view(frame, FRAME_MAX_SIZE).to_image()
            except Exception:
                sample.skip_reason = "decode_error"
            yield sample


def _view(frame, max_size: int | None):
    """The frame as RGB, no larger than `max_size` on its longest side.

    One conversion, shared. The scale happens inside libswscale, folded
    into the colour conversion that has to happen anyway, and everything
    downstream reads this frame rather than the decoded one -- which is
    the point: a 4K keyframe is scaled once here instead of being carried
    at full size through the signature, the colour ratio and one resize
    per backend.
    """
    w, h = frame.width, frame.height
    if max_size and max(w, h) > max_size:
        scale = max_size / max(w, h)
        return frame.reformat(width=int(w * scale), height=int(h * scale),
                              format="rgb24")
    return frame.reformat(format="rgb24")


def _to_image(frame, max_size: int | None = None) -> Image.Image:
    """`_view`, as a PIL image."""
    return _view(frame, max_size).to_image()
