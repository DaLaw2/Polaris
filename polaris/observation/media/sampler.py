"""Unified image sampling for a folder of images.

Intelligent sampling strategy that picks representative pages:
- Always includes the cover (most representative for style/color)
- Skips non-content pages (blank, tiny, mostly-white)
- Stratified sampling across the work's page range
- Adaptive sample count based on work size
"""

import math
from collections.abc import Iterator
from pathlib import Path

import numpy as np
from PIL import Image

from polaris.observation.domain import Measurement, Sample, SampleStream

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}

SAMPLE_FLOOR = 8
SAMPLES_PER_ROOT = 2.0
MIN_FILE_SIZE = 30_000
MIN_DIMENSION = 400
BLANK_THRESHOLD = 0.95
NEAR_WHITE = 240
NEAR_BLACK = 15
TAIL_SKIP = 0.95
ZONE_SPLITS = [0.25, 0.65, 1.0]
ZONE_WEIGHTS = [0.2, 0.5, 0.3]

_SHIPPED: dict[str, float] = {
    "sample:floor": SAMPLE_FLOOR,
    "sample:per_root": SAMPLES_PER_ROOT,
    "sample:min_file_bytes": MIN_FILE_SIZE,
    "sample:min_dimension": MIN_DIMENSION,
    "sample:blank_ratio": BLANK_THRESHOLD,
    "sample:near_white": NEAR_WHITE,
    "sample:near_black": NEAR_BLACK,
    "sample:tail_skip": TAIL_SKIP,
    "sample:zone_early": ZONE_SPLITS[0],
    "sample:zone_middle": ZONE_SPLITS[1],
    "sample:weight_early": ZONE_WEIGHTS[0],
    "sample:weight_middle": ZONE_WEIGHTS[1],
    "sample:weight_late": ZONE_WEIGHTS[2],
}


def apply_settings(p: dict[str, float]) -> None:
    """Make the sampling numbers, as read, this build's defaults.

    Set once per scan by `calibration.load_settings`: sampling happens on
    a decoder thread with no database near it.

    The last zone always ends at the body's end, so only the two interior
    boundaries are rows; a third would let a row say the sampler stops
    partway through a work for no stated reason.
    """
    global SAMPLE_FLOOR, SAMPLES_PER_ROOT, MIN_FILE_SIZE, MIN_DIMENSION
    global BLANK_THRESHOLD, NEAR_WHITE, NEAR_BLACK, TAIL_SKIP
    global ZONE_SPLITS, ZONE_WEIGHTS

    SAMPLE_FLOOR = int(p["sample:floor"])
    SAMPLES_PER_ROOT = p["sample:per_root"]
    MIN_FILE_SIZE = int(p["sample:min_file_bytes"])
    MIN_DIMENSION = int(p["sample:min_dimension"])
    BLANK_THRESHOLD = p["sample:blank_ratio"]
    NEAR_WHITE = p["sample:near_white"]
    NEAR_BLACK = p["sample:near_black"]
    TAIL_SKIP = p["sample:tail_skip"]
    ZONE_SPLITS = [p["sample:zone_early"], p["sample:zone_middle"], 1.0]
    ZONE_WEIGHTS = [p["sample:weight_early"], p["sample:weight_middle"],
                    p["sample:weight_late"]]


def sample_budget(pages: int, floor: int, per_root: float) -> int:
    """How many pages to look at, given how many there are.

    Neither number has a default: they were fitted on one collection, so a
    signature carrying them would be this module hiding a row.
    """
    if pages <= floor:
        return max(pages, 0)
    return min(pages, round(floor + per_root * math.sqrt(pages)))


def content_key(folder: Path) -> str | None:
    """A fingerprint of what is in the folder, not of where the folder is.

    The files inside do not change when the folder is renamed, so their
    names and sizes identify the work and the path does not.

    Deliberately not a hash of the pixels: it has to be cheap enough to run
    on every work of every scan, and the directory listing has already been
    read by the time it is called.
    """
    import hashlib

    entries = []
    for p in collect_images(folder):
        try:
            entries.append(f"{p.name}:{p.stat().st_size}")
        except OSError:
            continue
    if not entries:
        return None
    h = hashlib.sha1("\n".join(sorted(entries)).encode("utf-8", "replace"))
    return f"img:{h.hexdigest()}"


def collect_images(folder: Path) -> list[Path]:
    """Collect all images from a folder and one level of subdirectories.

    Returns sorted list of image paths.
    """
    all_images = []
    try:
        for f in sorted(folder.iterdir()):
            if f.is_file() and f.suffix.lower() in IMAGE_EXTENSIONS:
                all_images.append(f)
            elif f.is_dir():
                for sf in sorted(f.iterdir()):
                    if sf.is_file() and sf.suffix.lower() in IMAGE_EXTENSIONS:
                        all_images.append(sf)
    except PermissionError:
        return []
    return all_images


def find_work_folders(base: Path, max_depth: int = 5) -> list[Path]:
    """Find every work folder under `base`, at whatever depth it sits.

    A folder is a work if it directly contains images. Descent stops there,
    so a work's own subfolders (chapters, extras, bonus) stay part of that
    work rather than being counted as separate works.

    Hidden folders are skipped at every level. Only evaluation walks a
    tree; a scan reads the works a person registered.
    """
    works: list[Path] = []

    def _has_images(d: Path) -> bool:
        try:
            return any(
                f.is_file() and f.suffix.lower() in IMAGE_EXTENSIONS
                for f in d.iterdir()
            )
        except (PermissionError, OSError):
            return False

    def _walk(d: Path, depth: int) -> None:
        if depth > max_depth:
            return
        if _has_images(d):
            works.append(d)
            return
        try:
            children = sorted(d.iterdir())
        except (PermissionError, OSError):
            return
        for child in children:
            if child.is_dir() and not child.name.startswith("."):
                _walk(child, depth + 1)

    _walk(base, 0)
    return works


def _page_rejection(path: Path, min_file_size: int) -> str | None:
    """Why this file is not a page of the work, judged from its size before
    any page is chosen, or None if it is one."""
    try:
        return "too_small" if path.stat().st_size < min_file_size else None
    except OSError:
        return "decode_error"


def _blank_ratios(image: Image.Image, near_white: float,
                  near_black: float) -> tuple[float, float]:
    """How much of a downsampled page is near-white and near-black.

    The numbers rather than the verdict: storing only `skip_reason='blank'`
    threw away the measurement, so moving `sample:blank_ratio` meant
    decoding every page again to find out which ones would now pass.

    64x64 is the resolution the ratio is estimated at, which is a fact
    about estimating a fraction and not about anybody's pages.
    """
    thumb = image.resize((64, 64), Image.BILINEAR)
    arr = np.array(thumb)

    if arr.ndim == 3:
        gray = arr.mean(axis=2)
    else:
        gray = arr.astype(float)

    total = gray.size
    return (float(np.sum(gray > near_white) / total),
            float(np.sum(gray < near_black) / total))


def _zones(candidates: list[Path], n: int, include_cover: bool,
           per_root: float) -> list[tuple[list[Path], list[int]]]:
    """The candidate pages split into zones, each with the indices chosen
    in it: the cover alone, then early, middle and late pages of the body
    with the tail skipped."""
    total = len(candidates)
    n = sample_budget(total, n, per_root)
    if total <= n:
        return [(candidates, list(range(total)))]

    zones: list[tuple[list[Path], list[int]]] = []
    if include_cover:
        zones.append((candidates[:1], [0]))
    start = 1 if include_cover else 0
    end = max(start + 1, int(total * TAIL_SKIP))
    body = candidates[start:end]
    remaining_n = n - start
    if not body or remaining_n <= 0:
        return zones
    if len(body) <= remaining_n:
        zones.append((body, list(range(len(body)))))
        return zones

    exact = [remaining_n * w for w in ZONE_WEIGHTS]
    counts = [int(x) for x in exact]
    leftover = remaining_n - sum(counts)
    if leftover > 0:
        by_remainder = sorted(
            range(len(exact)),
            key=lambda i: (-(exact[i] - counts[i]), -ZONE_WEIGHTS[i]))
        for i in range(leftover):
            counts[by_remainder[i % len(by_remainder)]] += 1

    for z, (split_end, count) in enumerate(zip(ZONE_SPLITS, counts)):
        split_start = ZONE_SPLITS[z - 1] if z > 0 else 0.0
        pages = body[int(len(body) * split_start):int(len(body) * split_end)]
        if count <= 0 or not pages:
            continue
        if len(pages) <= count:
            zones.append((pages, list(range(len(pages)))))
        else:
            step = len(pages) / count
            zones.append((pages, [min(int((i + 0.5) * step), len(pages) - 1)
                                  for i in range(count)]))
    return zones


def _candidates(folder: Path) -> tuple[list[Path], list[Path],
                                       list[tuple[Path, str]]]:
    """Every image, the ones that may be chosen, and the ones rejected by
    size. A filter that takes everything is trusted less than the folder."""
    all_images = collect_images(folder)
    candidates, rejected = [], []
    for p in all_images:
        reason = _page_rejection(p, MIN_FILE_SIZE)
        if reason is None:
            candidates.append(p)
        else:
            rejected.append((p, reason))
    if not candidates:
        return all_images, all_images, []
    return all_images, candidates, rejected


def sample_folder_images(folder: Path, n: int | None = None,
                         include_cover: bool = True,
                         per_root: float | None = None) -> list[Path]:
    """The pages sampling would choose first, before any is decoded."""
    _, candidates, _ = _candidates(folder)
    zones = _zones(candidates, SAMPLE_FLOOR if n is None else n,
                   include_cover,
                   SAMPLES_PER_ROOT if per_root is None else per_root)
    return [pages[i] for pages, chosen in zones for i in chosen]


def _replacement(i: int, size: int, taken: set[int]) -> int | None:
    """The next page of the zone nobody chose, or failing that the previous."""
    for j in (*range(i + 1, size), *range(i - 1, -1, -1)):
        if j not in taken:
            return j
    return None


def stream_image_work(folder: Path, n: int | None = None) -> "SampleStream":
    """Pick pages from a folder and say what happened to each one.

    A chosen page that turns out too small, blank or undecodable is recorded
    with its reason and replaced by another page of the same zone. Ordinals
    run in the order samples are yielded; files rejected by size before
    choosing come last.
    """
    all_images, candidates, rejected = _candidates(folder)
    measurements = [Measurement("page_census", {
        "files": len(all_images), "candidates": len(candidates)})]
    if not all_images:
        return SampleStream(measurements, iter(()))

    zones = _zones(candidates, SAMPLE_FLOOR if n is None else n, True,
                   SAMPLES_PER_ROOT)
    index_of = {p: i for i, p in enumerate(all_images)}

    def relpath(path: Path) -> str:
        try:
            return path.relative_to(folder).as_posix()
        except ValueError:
            return path.name

    def page(path: Path, ordinal: int) -> "Sample":
        sample = Sample(medium="image", source_relpath=relpath(path),
                        position=float(index_of[path]), ordinal=ordinal)
        try:
            img = Image.open(path)
            w, h = img.size
            measurements.append(Measurement(
                "page_pixels", {"width": w, "height": h}, ordinal))
            if min(w, h) < MIN_DIMENSION:
                sample.skip_reason = "too_small"
                return sample
            img.draft("RGB", (1024, 1024))
            img = img.convert("RGB")
            white, black = _blank_ratios(img, NEAR_WHITE, NEAR_BLACK)
            measurements.append(Measurement(
                "blank_ratio", {"white": white, "black": black}, ordinal))
            if white > BLANK_THRESHOLD or black > BLANK_THRESHOLD:
                sample.skip_reason = "blank"
                return sample
            sample.image = img
        except Exception:
            sample.skip_reason = "decode_error"
        return sample

    def pages() -> "Iterator[Sample]":
        ordinal = 0
        for zone, chosen in zones:
            taken = set(chosen)
            for k in chosen:
                while k is not None:
                    sample = page(zone[k], ordinal)
                    ordinal += 1
                    analyzed = sample.analyzed
                    yield sample
                    if analyzed:
                        break
                    k = _replacement(k, len(zone), taken)
                    if k is not None:
                        taken.add(k)

        for path, reason in rejected:
            if reason == "too_small":
                try:
                    measurements.append(Measurement(
                        "file_bytes", path.stat().st_size, ordinal))
                except OSError:
                    pass
            yield Sample(medium="image", source_relpath=relpath(path),
                         position=float(index_of[path]), ordinal=ordinal,
                         skip_reason=reason)
            ordinal += 1

    return SampleStream(measurements, pages())


def reload_images(folder: Path, samples: list) -> "Iterator":
    """Decode already-recorded page samples again, for models that have not
    scored them. A page that no longer decodes comes back not analyzed."""
    for sample in samples:
        try:
            img = Image.open(folder / sample.source_relpath)
            img.draft("RGB", (1024, 1024))
            sample.image = img.convert("RGB")
        except Exception:
            sample.skip_reason = "decode_error"
        yield sample


def load_pil_images(
    paths: list[Path],
    skip_blank: bool = True,
    min_dimension: int | None = None,
    max_size: int = 1024,
) -> list[Image.Image]:
    """Load image paths as PIL Images, skipping failures and junk.

    Filters:
    - Failed loads are silently skipped
    - Images smaller than min_dimension on their short side are skipped
    - Blank pages (mostly white/black) are optionally skipped

    Resolution:
    - Pages are handed on at whatever resolution they decoded at. Each
      backend resizes once, from this, to the size it was trained on.
      Resizing here as well would mean two resamplings, the second one
      working from an image already stripped of detail.

    Args:
        paths: List of image file paths.
        skip_blank: Whether to skip blank pages.
        min_dimension: Minimum pixel dimension on the shorter side. None
            reads `sample:min_dimension` as last loaded.
        max_size: Maximum longest-side dimension (0 = no resize). A ceiling
            above every backend's own input size, so it is a fact about
            models rather than about a library.

    Returns:
        List of PIL Images (RGB).
    """
    if min_dimension is None:
        min_dimension = MIN_DIMENSION

    images = []
    for p in paths:
        try:
            img = Image.open(p)

            w, h = img.size
            if min(w, h) < min_dimension:
                continue

            img = img.convert("RGB")

            if max_size and max(w, h) > max_size:
                scale = max_size / max(w, h)
                new_w, new_h = int(w * scale), int(h * scale)
                img = img.resize((new_w, new_h), Image.LANCZOS)

            if skip_blank:
                white, black = _blank_ratios(img, NEAR_WHITE, NEAR_BLACK)
                if white > BLANK_THRESHOLD or black > BLANK_THRESHOLD:
                    continue

            images.append(img)
        except Exception:
            continue
    return images
