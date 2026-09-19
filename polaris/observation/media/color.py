"""How saturated a page is. Not what that means.

`page_color_ratio` is stored per sample. The three cutoffs that turn a set
of those into full_color / partial_color / grayscale are rows in
`derivation_params`, applied by `derive_works` in derivation/schema.py when
something reads — so moving one does not mean re-reading every page.
"""

import numpy as np
from PIL import Image

SATURATION_THRESHOLD = 0.15

_SHIPPED: dict[str, float] = {"color:saturation": SATURATION_THRESHOLD}


def apply_settings(p: dict[str, float]) -> None:
    """Make `color:saturation`, as read, this build's default.

    Set once per scan by `calibration.load_settings`, for the same reason
    as `sampler.apply_settings`: pages are measured on a decoder thread with
    no database near it.
    """
    global SATURATION_THRESHOLD

    SATURATION_THRESHOLD = p["color:saturation"]


def page_color_ratio(image: Image.Image,
                     saturation_threshold: float | None = None) -> float:
    """Fraction of pixels with meaningful saturation.

    One number per sample, stored as it is. What counts as a colour page,
    and how many colour pages make a colour work, are questions for the
    read side — see `color:page_ratio`, `color:full` and `color:gray` in
    `derivation_params`.

    None reads `color:saturation` as last loaded, resolved here rather than
    in the signature: a default argument binds before `apply_settings` can
    have run.
    """
    if saturation_threshold is None:
        saturation_threshold = SATURATION_THRESHOLD

    img = image.convert("RGB")
    if img.width > 256:
        ratio = 256 / img.width
        img = img.resize((256, int(img.height * ratio)), Image.BILINEAR)

    arr = np.array(img, dtype=np.float32) / 255.0
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    cmax = np.maximum(np.maximum(r, g), b)
    cmin = np.minimum(np.minimum(r, g), b)
    diff = cmax - cmin

    with np.errstate(invalid="ignore"):
        saturation = np.where(cmax > 0, diff / cmax, 0)

    color_pixels = np.sum(saturation > saturation_threshold)
    total_pixels = saturation.size

    return float(color_pixels / total_pixels)
