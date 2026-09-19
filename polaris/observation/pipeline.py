"""Observing a work: sampling it, scoring it, measuring it.

Ends at a `WorkObservation`: the samples taken, the unthresholded scores
each backend returned for each of them, and what was measured. Nothing is
decided here — what a work's tags, rating, colour and type *are* is
computed in SQL when something asks. See derivation/schema.py.

The only place medium is consulted is `_stream`. After it, a page and a
video frame are the same kind of thing.

Speed and size:
- Concurrent model execution via threading (ONNX ↔ PyTorch overlap)
- A pool of decoder threads reads ahead of the GPU
- Frames, not works, are what is held: a work is streamed a frame at a
  time through a bounded pool, so a very long video costs no more to observe
  than a 30-second one
"""

import os
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from polaris import config
from polaris.models import definition
from polaris.models.backends.base import BackendConfig
from polaris.models.backends.generic import DefinedBackend
from polaris.models.tagger import RAW_TOP_K, Tagger
from polaris.observation.media.color import page_color_ratio

from .domain import Measurement, WorkObservation

DECODE_WORKERS = min(6, os.cpu_count() or 1)

PREPARED_FRAMES = 192

SCORE_FRAMES = 96


@dataclass
class WorkPlan:
    """One work to observe: every backend unless `only` names some, and
    freshly sampled unless `samples` gives the recorded ones to decode again."""

    target: Path
    only: frozenset[str] | None = None
    samples: list | None = None


class _WorkState:
    """One work in flight, from streamed frames to a finished record.

    Frames are scored in whatever company they arrive in, so a work's
    scores come back in pieces from another thread and out of order. This
    is where the pieces are collected and where "finished" is decided:
    every analysed frame accounted for, and the stream that produces them
    ended.
    """

    def __init__(self, plan: WorkPlan):
        self.plan = plan
        self.target = plan.target
        self.obs: WorkObservation | None = None
        self.rows: dict[int, dict] = {}
        self.expected = -1
        self.scored = 0
        self.finished = threading.Event()
        self._lock = threading.Lock()

    def absorb(self, rows: dict[int, dict]) -> None:
        """Take the scores for some of this work's frames."""
        with self._lock:
            for ordinal, per_backend in rows.items():
                self.rows.setdefault(ordinal, {}).update(per_backend)
            self.scored += len(rows)
            self._check()

    def close(self, expected: int, error: str | None = None) -> None:
        """No more frames are coming; `expected` were queued.

        Called exactly once per work however that work ended, including
        from a `finally`: a work that never closes is a caller that waits
        for it forever.
        """
        with self._lock:
            if self.expected >= 0:
                return
            self.expected = expected
            if error and self.obs is not None and not self.obs.error:
                self.obs.error = error
            self._check()

    def _check(self) -> None:
        if self.expected >= 0 and self.scored >= self.expected:
            self.finished.set()


def _no_collection(target: Path) -> str:
    """Why a work under no configured root is not stored."""
    return (f"no collection: {target} is under no configured root. "
            f"Add one from the settings drawer, or with "
            f"PUT /api/collections/NAME")


def configure(version: dict) -> BackendConfig:
    """A stored model version as the pipeline runs it: its definition's
    text, at its precision, with the definition's defaults."""
    if version.get("source_text") is None:
        raise ValueError(f"{version['backend']} version {version['id']} "
                         "has no definition")
    d = definition.parse(version["source_text"])
    return BackendConfig(
        backend=DefinedBackend(d, half=version["precision"] == "fp16"),
        category_priority=dict(d.defaults.roles),
        concurrent=d.defaults.concurrent, max_images=d.defaults.max_images)


class Pipeline:
    """Turns a work on disk into a record of what was observed about it."""

    def __init__(
        self,
        versions: list[dict],
        keep_tags: frozenset[str] = frozenset(),
        raw_top_k: int = RAW_TOP_K,
    ):
        self.keep_tags = keep_tags
        self.raw_top_k = raw_top_k

        self.tagger = Tagger([configure(v) for v in versions])

        self._initialized = False

    def _ensure_init(self) -> None:
        """Warm up models on first use."""
        if self._initialized:
            return
        self.tagger.ensure_loaded()
        self._initialized = True

    def _stream(self, target: Path, n_samples: int):
        """A work's samples one at a time, whatever medium it is.

        The only branch on medium in the entire write path. After this line
        a page and a video frame are the same kind of thing, and nothing
        downstream — tagging, measuring, storing, aggregating — asks again.

        Returns (stream, content_key).
        """
        from polaris.observation.media.work_type import cg_features

        if target.is_dir():
            from polaris.observation.media.sampler import content_key, stream_image_work

            stream = stream_image_work(target, n=n_samples)
            stream.measurements.append(
                Measurement("cg_features", cg_features(target)))
            return stream, content_key(target)

        from polaris.observation.media.video import stream_video_work

        return stream_video_work(target), _file_content_key(target)

    @staticmethod
    def _mean_embeddings(scores: list[dict]) -> dict[str, list[float]]:
        """L2-normalised mean of each backend's per-sample embeddings."""
        collected: dict[str, list] = {}
        for per_backend in scores:
            for name, seen in per_backend.items():
                if seen.embedding is not None:
                    collected.setdefault(name, []).append(seen.embedding)

        out = {}
        for name, vecs in collected.items():
            arr = np.asarray(vecs, dtype=np.float32).mean(axis=0)
            norm = float(np.linalg.norm(arr))
            if norm > 0:
                arr = arr / norm
            out[name] = arr.tolist()
        return out

    def _stream_work(self, st: "_WorkState", n_samples: int,
                     frames, budget, stop,
                     require_collection: bool = True) -> None:
        """Decode one work into the frame pool, a frame at a time.

        A permit is taken per frame and given back by the scorer, so a
        decoder waits when the pool is full rather than when some number of
        works is reached. Nothing here holds more than the frame it is
        converting.
        """
        target = st.target
        kept = 0
        error = None
        try:
            owner = config.collection_for(target)
            if owner is None and require_collection:
                st.obs = WorkObservation(
                    path=str(target), name=target.name, collection="",
                    error=_no_collection(target))
                return

            obs = WorkObservation(path=str(target), name=target.name,
                                  collection=owner.name if owner else "")
            st.obs = obs
            plan = st.plan
            if plan.samples is None:
                stream, obs.content_key = self._stream(target, n_samples)
                obs.measurements = stream.measurements
                samples = stream.samples
            elif target.is_dir():
                from polaris.observation.media.sampler import reload_images
                samples = reload_images(target, plan.samples)
            else:
                from polaris.observation.media.video import reload_frames
                samples = reload_frames(target, plan.samples)
            for sample in samples:
                obs.samples.append(sample)
                if not sample.analyzed:
                    continue
                img, sample.image = sample.image, None
                if plan.samples is None:
                    obs.measurements.append(Measurement(
                        "color_ratio", page_color_ratio(img), sample.ordinal))
                budget.acquire()
                if stop.is_set():
                    return
                frames.put((st, sample.ordinal,
                            self.tagger.prepare_one(img, kept, plan.only)))
                kept += 1
                del img
        except BaseException as e:
            error = f"sampling: {e}"
            if st.obs is None:
                st.obs = WorkObservation(
                    path=str(target), name=target.name, collection="")
            raise
        finally:
            st.close(kept, error=error)

    def _score_loop(self, frames, budget, score_frames: int) -> None:
        """Score whatever frames are waiting, whoever they belong to."""
        while True:
            first = frames.get()
            if first is None:
                return
            batch = [first]
            stop = False
            while len(batch) < score_frames:
                try:
                    nxt = frames.get_nowait()
                except Exception:
                    break
                if nxt is None:
                    stop = True
                    break
                batch.append(nxt)

            try:
                self._score_frames(batch)
            except BaseException as e:
                for st, ordinal, _ in batch:
                    if st.obs is not None and not st.obs.error:
                        st.obs.error = f"tagging: {e}"
                    st.absorb({ordinal: {}})
            finally:
                for _ in batch:
                    budget.release()
                batch.clear()
            if stop:
                return

    def _score_frames(self, batch: list) -> None:
        """One model run over a batch of frames from any number of works."""
        flat: dict[str, list] = {}
        owner: dict[str, list] = {}
        for st, ordinal, prepared in batch:
            for name, array in prepared.items():
                flat.setdefault(name, []).append(array)
                owner.setdefault(name, []).append((st, ordinal))

        error = None
        try:
            rows = self.tagger.score_flat(
                flat, top_k=self.raw_top_k, keep=self.keep_tags)
        except Exception as e:
            rows, error = {}, f"tagging: {e}"

        got: dict[int, tuple] = {}
        for st, ordinal, _ in batch:
            entry = got.setdefault(id(st), (st, {}))
            entry[1].setdefault(ordinal, {})
        for name, seen in rows.items():
            for (st, ordinal), scores in zip(owner.get(name, ()), seen):
                got[id(st)][1][ordinal][name] = scores

        for st, by_ordinal in got.values():
            if error and st.obs is not None and not st.obs.error:
                st.obs.error = error
            st.absorb(by_ordinal)

    def _finish(self, st: "_WorkState") -> WorkObservation:
        """The record for a work whose frames have all been scored."""
        obs = st.obs
        obs.scores = [st.rows.get(s.ordinal, {}) for s in obs.analyzed_samples]
        if obs.scores and not obs.error and not any(
                seen.scores for per in obs.scores for seen in per.values()):
            obs.error = "tagging: every backend returned no scores"
        obs.embeddings = self._mean_embeddings(obs.scores)
        st.rows.clear()
        return obs

    def observe_batch(
        self,
        targets: "list[Path | WorkPlan]",
        n_samples: int = 8,
        show_progress: bool = True,
        decode_workers: int | None = None,
        prepared_frames: int = PREPARED_FRAMES,
        score_frames: int = SCORE_FRAMES,
        require_collection: bool = True,
    ) -> "Iterator[WorkObservation]":
        """Observe many works, holding a bounded number of frames.

        Frames flow one at a time from a pool of decoders, through at most
        `prepared_frames` permits, to a single thread that scores them in
        runs of up to `score_frames`. What is resident therefore depends on
        neither how long a video is nor how many works are in flight, and a
        work is never assembled in memory before it is scored.

        Works come out in the order they were given. A frame is scored in
        whatever company it arrives in, which changes nothing a model says:
        `backends.batches` pads every run to a fixed batch size.
        """
        import queue
        from concurrent.futures import ThreadPoolExecutor

        self._ensure_init()
        workers = decode_workers or DECODE_WORKERS
        total = len(targets)
        t0 = time.perf_counter()
        done = 0

        states = [_WorkState(t if isinstance(t, WorkPlan) else WorkPlan(t))
                  for t in targets]
        frames: "queue.Queue" = queue.Queue()
        budget = threading.Semaphore(prepared_frames)
        stop = threading.Event()
        pool = ThreadPoolExecutor(max_workers=workers,
                                  thread_name_prefix="sample")
        scorer = threading.Thread(
            target=self._score_loop, args=(frames, budget, score_frames),
            name="score", daemon=True)
        scorer.start()

        window = workers * 2
        upcoming = iter(states)

        def _top_up(n: int) -> None:
            for _ in range(n):
                nxt = next(upcoming, None)
                if nxt is None:
                    return
                pool.submit(self._stream_work, nxt, n_samples, frames,
                            budget, stop, require_collection)

        try:
            _top_up(window)
            for st in states:
                st.finished.wait()
                obs = self._finish(st)
                _top_up(1)
                done += 1
                if show_progress:
                    elapsed = time.perf_counter() - t0
                    rate = done / elapsed if elapsed > 0 else 0
                    eta = (total - done) / rate if rate > 0 else 0
                    n_scores = sum(len(v.scores) for d in obs.scores
                                   for v in d.values())
                    status = obs.error or (
                        f"{len(obs.analyzed_samples)}/{len(obs.samples)}"
                        f" samples, {n_scores} scores")
                    print(f"  [{done}/{total}] {obs.name[:44]:44s} |"
                          f" {status} | {rate:.2f}/s ETA {eta:.0f}s",
                          flush=True)
                yield obs
        finally:
            stop.set()
            frames.put(None)
            for _ in range(prepared_frames):
                budget.release()
            pool.shutdown(wait=False, cancel_futures=True)

    def release(self) -> None:
        """Let go of the models, and of the memory they hold on the card.

        `_ensure_init` loads them again on the next work, so a released
        pipeline is still usable -- it is a pipeline that currently costs
        nothing.
        """
        self.tagger.unload()
        self._initialized = False


def _file_content_key(path: Path) -> str | None:
    """Identity for a one-file work: its size and extension.

    A renamed video keeps both. Not a hash of the bytes: these files run
    to gigabytes and this is computed for every work of every scan.
    Collisions are possible, which is why `identity.upsert` accepts
    a content match only when exactly one work has it.
    """
    import hashlib

    try:
        size = path.stat().st_size
    except OSError:
        return None
    h = hashlib.sha1(f"{size}:{path.suffix.lower()}".encode())
    return f"vid:{h.hexdigest()}"
