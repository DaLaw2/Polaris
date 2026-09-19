"""Preprocessing steps a model definition names, and the chain that runs them.

Each step is an entry in `OPS`: the function, the checks for its
parameters, and its stage. Image steps work on the PIL image, one convert
step turns it into float32 HWC, array steps work on that. NCHW or NHWC is
the model's, applied last.

A chain stores every intermediate on the frame's `ImageViews` under the
steps that made it, so two models whose chains begin alike compute that
beginning once per frame.
"""

from __future__ import annotations

import numpy as np
from PIL import Image

INTERPOLATION = {
    "nearest": Image.NEAREST,
    "bilinear": Image.BILINEAR,
    "bicubic": Image.BICUBIC,
    "lanczos": Image.LANCZOS,
    "box": Image.BOX,
    "hamming": Image.HAMMING,
}


def _rgb(v):
    if not (isinstance(v, list) and len(v) == 3
            and all(type(c) is int and 0 <= c <= 255 for c in v)):
        raise ValueError("expected three integers 0-255")
    return v


def _triple(v):
    if not (isinstance(v, list) and len(v) == 3
            and all(type(c) in (int, float) for c in v)):
        raise ValueError("expected three numbers")
    return [float(c) for c in v]


def _size(v):
    if v == "model" or (type(v) is int and v > 0):
        return v
    raise ValueError('expected a positive integer or "model"')


def _choice(*names):
    def check(v):
        if v not in names:
            raise ValueError(f"expected one of {', '.join(names)}")
        return v
    return check


def pad_square(image, fill):
    """Centred on a square of `fill`, the side being the longer edge."""
    w, h = image.size
    if w == h:
        return image
    side = max(w, h)
    square = Image.new("RGB", (side, side), tuple(fill))
    square.paste(image, ((side - w) // 2, (side - h) // 2))
    return square


def resize(image, size, interpolation):
    """`size` x `size`; an image already that size is left alone."""
    if image.size == (size, size):
        return image
    return image.resize((size, size), INTERPOLATION[interpolation])


def to_tensor(image):
    return np.asarray(image, dtype=np.float32) / 255.0


def to_array(image):
    return np.asarray(image, dtype=np.float32)


def channel_order(x, order):
    return x[:, :, ::-1] if order == "bgr" else x


def normalize(x, mean, std):
    return (x - np.asarray(mean, dtype=np.float32)) / np.asarray(std, dtype=np.float32)


OPS = {
    "pad_square": (pad_square, {"fill": _rgb}, "image"),
    "resize": (resize, {"size": _size,
                        "interpolation": _choice(*INTERPOLATION)}, "image"),
    "to_tensor": (to_tensor, {}, "convert"),
    "to_array": (to_array, {}, "convert"),
    "channel_order": (channel_order, {"order": _choice("rgb", "bgr")}, "array"),
    "normalize": (normalize, {"mean": _triple, "std": _triple}, "array"),
}


def _frozen(v):
    return tuple(v) if isinstance(v, list) else v


def chain(steps, model_size: int | None, nhwc: bool):
    """A function from an `ImageViews` to the model's input array.

    `steps` are (op, params) pairs; a resize to "model" takes
    `model_size`, which must then be known.
    """
    fns, keys, key = [], [], ()
    for op, params in steps:
        params = dict(params)
        if params.get("size") == "model":
            if model_size is None:
                raise ValueError("resize to the model's size, but its input "
                                 "size is not fixed")
            params["size"] = model_size
        key = key + ((op, tuple(sorted((k, _frozen(v))
                                       for k, v in params.items()))),)
        fns.append((OPS[op][0], params))
        keys.append(key)

    def run(views) -> np.ndarray:
        memo = views.memo
        start, x = 0, None
        for i in range(len(keys) - 1, -1, -1):
            if keys[i] in memo:
                start, x = i + 1, memo[keys[i]]
                break
        else:
            x = views.rgb()
        for i in range(start, len(keys)):
            fn, params = fns[i]
            x = memo[keys[i]] = fn(x, **params)
        return x if nhwc else x.transpose(2, 0, 1)

    return run
