"""Measuring the shape of a folder, without deciding what it means.

This module used to answer "is this a CG set?" with a boolean, computed
from six thresholds, and that boolean was all that reached the database —
so changing any threshold meant re-walking every folder on disk.

It measures now and does not decide. Whether a work is a comic, a CG set
or a single illustration is a query over these numbers plus the tagger's
`comic` score, and every cutoff involved is a row in `derivation_params`.
See `derive_works` in derivation/schema.py.
"""

import re
from pathlib import Path

import numpy as np
from PIL import Image


def cg_features(folder: Path) -> dict:
    """What a folder of images looks like, as numbers.

    These are the four things the old `detect_cg_set` compared against six
    thresholds before collapsing them into a boolean. Storing only the
    boolean meant re-walking every folder on disk to change any of them.

    Returns a dict with `n_images`, `sequential_ratio`, `aspect_std` and
    `avg_file_size`, plus the gap statistics the numbering check uses.
    Missing values are None rather than a default, because "we could not
    measure the aspect ratios" and "the aspect ratios were consistent" are
    different facts.

    The two numbers left here decide nothing about a work: 20 is how many
    images a standard deviation is estimated from, and 3 is the fewest
    numbered files that have two gaps between them. Every cutoff these
    measurements are then compared against is a `cg:` or `type:` row.
    """
    from .sampler import collect_images

    images = collect_images(folder)
    n = len(images)
    out: dict = {
        "n_images": n,
        "sequential_ratio": None,
        "mean_gap": None,
        "max_gap": None,
        "aspect_std": None,
        "aspect_samples": 0,
        "avg_file_size": None,
    }
    if not images:
        return out

    numeric = [int(m.group(1)) for img in images
               if (m := re.search(r'(\d+)$', img.stem))]
    out["sequential_ratio"] = len(numeric) / n
    if len(numeric) >= 3:
        s = sorted(numeric)
        gaps = [s[i + 1] - s[i] for i in range(len(s) - 1)]
        if gaps:
            out["mean_gap"] = sum(gaps) / len(gaps)
            out["max_gap"] = max(gaps)

    sample = images if n <= 20 else [images[int(i * n / 20)] for i in range(20)]
    ratios = []
    for img_path in sample:
        try:
            with Image.open(img_path) as img:
                w, h = img.size
                if h > 0:
                    ratios.append(w / h)
        except Exception:
            continue
    out["aspect_samples"] = len(ratios)
    if ratios:
        out["aspect_std"] = float(np.std(ratios))

    try:
        out["avg_file_size"] = sum(i.stat().st_size for i in images) / n
    except OSError:
        pass

    return out
