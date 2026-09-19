"""Frame size, sampler settings as rows, and page replacement.

    POLARIS_TEST_DSN=postgresql://.../polaris_test python tests/test_media.py
"""
import asyncio
import inspect
import os
import tempfile
from pathlib import Path

from _common import dsn

DSN = dsn()

import asyncpg  # noqa: E402
import av  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from polaris.observation import calibration  # noqa: E402
from polaris.observation.media import color, sampler, video  # noqa: E402
from polaris.shared import tuning  # noqa: E402

W, H = 640, 360


def clip(path: Path, frames: int = 20) -> None:
    with av.open(str(path), "w") as out:
        s = out.add_stream("mpeg4", rate=10)
        s.width, s.height, s.pix_fmt = W, H, "yuv420p"
        x = np.linspace(0, 255, W, dtype=np.float32)
        for i in range(frames):
            arr = np.repeat(((x[None, :] + i * 37) % 256).astype(np.uint8), H, 0)
            rgb = np.stack([arr, np.roll(arr, 7, 1), 255 - arr], axis=-1)
            out.mux(s.encode(av.VideoFrame.from_ndarray(rgb, format="rgb24")))
        out.mux(s.encode())


def page(path: Path, side: int = 400) -> None:
    Image.frombytes("RGB", (side, side), os.urandom(side * side * 3)).save(path)


def test_frame_size(tmp: Path) -> None:
    clip(path := tmp / "clip.mp4")
    with av.open(str(path)) as c:
        frame = next(c.decode(c.streams.video[0]))
    view = video._view(frame, 320)
    assert (view.width, view.height, view.format.name) == (320, 180, "rgb24"), view
    assert all(video._view(f, cap).width == f.width
               for f, cap in ((frame, None), (frame, 0), (view, 320)))
    was = video.FRAME_MAX_SIZE
    try:
        video.FRAME_MAX_SIZE = 160
        frames = video.sample_video_frames(path, n=4, deduplicate=False)
        assert frames and all(max(f.size) == 160 for f in frames), frames
        small = video.sample_video_frames(path, n=2, deduplicate=False, max_size=80)
        assert all(max(f.size) == 80 for f in small)
        assert calibration.observation_params()["video"]["frame_max_size"] == 160
        video.FRAME_MAX_SIZE = None
        assert "frame_max_size" not in calibration.observation_params()["video"]
    finally:
        video.FRAME_MAX_SIZE = was
    print("  ok  a frame is bounded once, aspect kept; max_size overrides; no cap is not recorded")


def test_pure_numbers() -> None:
    wanted = set(sampler._SHIPPED) | set(video._SHIPPED) | set(color._SHIPPED)
    missing = sorted(n for n in wanted if n not in tuning.GLOBAL_PARAMS
                     or f"('{n}'," not in tuning.SEED_SQL)
    assert not missing, f"no seed row for {missing}"
    for fn in (sampler.sample_budget, sampler._page_rejection,
               sampler._blank_ratios, video.frame_budget, video._is_blank):
        assert all(p.default is inspect.Parameter.empty
                   for p in inspect.signature(fn).parameters.values()), fn.__name__
    assert [sampler.sample_budget(100, 8, 2.0), sampler.sample_budget(100, 8, 4.0),
            sampler.sample_budget(5, 8, 2.0)] == [28, 48, 5]
    assert [video.frame_budget(600, 8, 6.0, 4, None), video.frame_budget(600, 8, 6.0, 4, 20),
            video.frame_budget(0, 8, 6.0, 4, None)] == [68, 20, 4]
    print(f"  ok  {len(wanted)} numbers seeded; pure functions and budgets take them as arguments")


def test_replacement_keeps_ordinals(tmp: Path) -> None:
    folder = tmp / "replace"
    folder.mkdir()
    for i in range(20):
        page(folder / f"{i:03}.png")
    chosen = sampler.sample_folder_images(folder, n=4)
    page(chosen[1], side=300)
    got = list(sampler.stream_image_work(folder, n=4).samples)
    assert [s.ordinal for s in got] == list(range(len(got))), got
    assert [s.source_relpath for s in got if s.skip_reason == "too_small"] == [chosen[1].name]
    kept = {s.source_relpath for s in got if s.analyzed}
    assert len(kept) == len(chosen) and chosen[1].name not in kept and kept - {
        p.name for p in chosen}, kept
    print(f"  ok  a rejected page is replaced; {len(got)} ordinals, no gap")


async def test_rows_move_settings() -> None:
    names = ["sample:min_file_bytes", "video:scene_distance", "color:saturation",
             "video:budget_max"]
    conn = await asyncpg.connect(DSN)
    kept = await conn.fetch("SELECT name, value FROM derivation_params "
                            "WHERE name = ANY($1::text[])", names)
    before = (sampler.MIN_FILE_SIZE, video.SCENE_DISTANCE, color.SATURATION_THRESHOLD)
    try:
        for name, value in zip(names, (1234, 7.5, 0.42, 0)):
            await tuning.set_param(conn, name, value)
        await calibration.load_settings(conn)
        assert (sampler.MIN_FILE_SIZE, video.SCENE_DISTANCE,
                color.SATURATION_THRESHOLD, video.FRAME_BUDGET_MAX) == (1234, 7.5, 0.42, None)
        await tuning.set_param(conn, "video:budget_max", 96)
        await calibration.load_settings(conn)
        assert video.FRAME_BUDGET_MAX == 96
        await conn.execute("DELETE FROM derivation_params WHERE name = ANY($1::text[])", names)
        await calibration.load_settings(conn)
        assert (sampler.MIN_FILE_SIZE, video.SCENE_DISTANCE,
                color.SATURATION_THRESHOLD) == before and video.FRAME_BUDGET_MAX is None
        print("  ok  rows move sampler, video and colour; budget_max 0 is no cap; no rows falls back")
    finally:
        for r in kept:
            await tuning.set_param(conn, r["name"], r["value"])
        await conn.close()


with tempfile.TemporaryDirectory() as t:
    test_frame_size(Path(t))
    test_pure_numbers()
    test_replacement_keeps_ordinals(Path(t))
asyncio.run(test_rows_move_settings())
print("all media checks passed")
