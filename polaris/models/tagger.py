"""Content tagging with pluggable backends.

Runs one or more backends over a batch of images. `score_flat` keeps
every backend's unthresholded output separately, which is what the scan
stores.

Adding a model is one uploaded definition; nothing here
changes.
"""

from PIL import Image

from polaris.models.backends.base import (
    BackendConfig, ImageViews, SampleScores)

RAW_TOP_K = 300


class Tagger:
    """Multi-backend tagger.

    Backends arrive as `BackendConfig`, one per stored model version.
    """

    def __init__(self, backends: list[BackendConfig]):
        self._configs = backends
        self._loaded = False

    def ensure_loaded(self) -> None:
        """Load all backends (idempotent)."""
        if self._loaded:
            return
        for cfg in self._configs:
            cfg.backend.load()
        self._loaded = True

    def unload(self) -> None:
        """Let go of every backend's session."""
        for cfg in self._configs:
            cfg.backend.unload()
        self._loaded = False

    def prepare_one(self, image: Image.Image, index: int = 0,
                    only: frozenset[str] | None = None) -> dict:
        """One image as each backend wants it, `index` applying the caps and
        `only` naming the backends wanted when not all of them are.

        Resizing and normalising is CPU work with nothing to wait for, so
        it belongs wherever the image was decoded rather than on the thread
        that is blocked on the GPU. The backends share one `ImageViews`, so
        a resize two of them need is done once.
        """
        self.ensure_loaded()
        views = ImageViews(image)
        return {cfg.backend.name: cfg.backend.prepare(views)
                for cfg in self._configs
                if not (cfg.max_images and index >= cfg.max_images)
                and (only is None or cfg.backend.name in only)}

    def score_flat(
        self,
        per_backend: dict[str, list],
        top_k: int = RAW_TOP_K,
        keep: frozenset[str] = frozenset(),
    ) -> dict[str, list[SampleScores]]:
        """Each backend's rows for its own list of prepared images.

        Unthresholded and kept separate per backend: once one backend's
        opinion has overwritten another's there is no way back, so both are
        stored and which wins is decided at read time. The lists are not
        assumed to describe the same images in the same order: a backend
        that was given fewer of them gets fewer rows back, and the caller
        keeps the correspondence.
        """
        if not any(per_backend.values()):
            return {}
        return self._run_backends(per_backend, top_k, keep)

    def _run_backends(
        self,
        prepared: dict[str, list],
        top_k: int,
        keep: frozenset[str],
    ) -> dict[str, list[SampleScores]]:
        """One run of every backend over its own images, keyed by name. A
        backend that fails is left out."""
        self.ensure_loaded()

        per_backend: dict[str, list[SampleScores]] = {}

        def _run(cfg) -> list[SampleScores]:
            batch = prepared.get(cfg.backend.name, [])
            return list(cfg.backend.score_prepared(batch, top_k=top_k, keep=keep))

        concurrent = [c for c in self._configs if c.concurrent]
        sequential = [c for c in self._configs if not c.concurrent]

        if concurrent:
            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(max_workers=len(concurrent)) as pool:
                futures = {c.backend.name: pool.submit(_run, c) for c in concurrent}
                for name, fut in futures.items():
                    try:
                        per_backend[name] = fut.result()
                    except Exception as e:
                        print(f"  [WARN] {name} scoring failed: {e}")

        for cfg in sequential:
            try:
                per_backend[cfg.backend.name] = _run(cfg)
            except Exception as e:
                print(f"  [WARN] {cfg.backend.name} scoring failed: {e}")

        return per_backend
