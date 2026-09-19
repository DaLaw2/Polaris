"""The constants this build observes with."""

from __future__ import annotations

from polaris.shared import tuning


async def load_settings(conn) -> None:
    """Read what sampler, video and colour observe with, and apply it.

    Set once per scan by whoever opened the connection: sampling and
    decoding happen on threads with no database near them.
    """
    from polaris.observation.media import color, sampler, video

    for mod in (sampler, video, color):
        mod.apply_settings(await tuning.params(conn, mod._SHIPPED))


def observation_params(**overrides) -> dict:
    """The constants this build observes with, as recorded on a run. What
    each model runs with is the caller's, as overrides."""
    import inspect

    from polaris.models import tagger
    from polaris.models.backends import base, trtprep
    from polaris.observation.media import color, sampler, video

    p = {
        "raw_top_k": tagger.RAW_TOP_K,
        "score_floor": inspect.signature(
            base.top_scores).parameters["floor"].default,
        "keep_tags": [],
        "saturation_threshold": color.SATURATION_THRESHOLD,
        "inference_batch": base.DEFAULT_BATCH,
        "sampler": {
            "min_file_size": sampler.MIN_FILE_SIZE,
            "min_dimension": sampler.MIN_DIMENSION,
            "blank_threshold": sampler.BLANK_THRESHOLD,
            "near_white": sampler.NEAR_WHITE,
            "near_black": sampler.NEAR_BLACK,
            "sample_floor": sampler.SAMPLE_FLOOR,
            "samples_per_root": sampler.SAMPLES_PER_ROOT,
            "tail_skip": sampler.TAIL_SKIP,
            "zone_splits": sampler.ZONE_SPLITS,
            "zone_weights": sampler.ZONE_WEIGHTS,
            "decode_draft": True,
        },
        "video": {
            "budget_base": video.FRAME_BUDGET_BASE,
            "budget_per_minute": video.FRAME_BUDGET_PER_MINUTE,
            "budget_min": video.FRAME_BUDGET_MIN,
            "budget_max": video.FRAME_BUDGET_MAX,
            "edge_margin": video.EDGE_MARGIN,
            "scene_distance": video.SCENE_DISTANCE,
        },
    }
    if video.FRAME_MAX_SIZE:
        p["video"]["frame_max_size"] = video.FRAME_MAX_SIZE
    if trtprep.in_use():
        p["tensorrt"] = True
    p.update(overrides)
    return p
