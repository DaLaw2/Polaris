"""python tests/test_definitions.py"""
import json
import os
import tempfile
from pathlib import Path

import numpy as np
import onnx.parser
from PIL import Image

os.environ["POLARIS_ONNX_PROVIDER"] = "CPUExecutionProvider"

from polaris.models import definition, preprocess  # noqa: E402
from polaris.models.backends.base import BackendConfig, ImageViews, top_scores  # noqa: E402
from polaris.models.backends.generic import DefinedBackend  # noqa: E402
from polaris.models.tagger import Tagger  # noqa: E402

VOCAB = ('file = "tags.csv"\nformat = "csv"\nname = "name"\ncategory = "category"\n'
         'categories = { "0" = "general", "4" = "character", "9" = "rating" }')
JSON_VOCAB = ('file = "vocab.json"\nformat = "json_idx"\n'
              'categories = { General = "general", Character = "character" }')
BASE = f"""name = "tiny"
source = {{ repo = "test/tiny", model = "m.onnx" }}
preprocess = [{{op = "pad_square", fill = [255, 255, 255]}},
              {{op = "resize", size = "model", interpolation = "bicubic"}}, {{op = "to_tensor"}}]
outputs = {{ scores = "logits", activation = "sigmoid", embedding = "#0" }}
[defaults]
roles = {{ general = 1 }}
[vocabulary]
{VOCAB}
"""
NAMES = ["a b", "cat", "char-a", "general", "explicit"]


def _image(w=37, h=23, mode="RGB", seed=0):
    arr = np.random.default_rng(seed).integers(0, 256, (h, w, 3), dtype=np.uint8)
    return Image.fromarray(arr).convert(mode)


def _refused(fn, *args) -> str:
    """The message of the ValueError `fn(*args)` must raise."""
    try:
        fn(*args)
    except ValueError as e:
        return str(e)
    raise AssertionError(f"accepted: {args}")


def test_parser_names_the_field() -> None:
    definition.parse(BASE)
    for old, new, path in (
            ('name = "tiny"', 'name = "tiny"\ncolour = 1', "colour"),
            ('"bicubic"', '"cubic"', "preprocess[1].interpolation"),
            ('op = "to_tensor"', 'op = "to_pixels"', "preprocess[2].op"),
            ("fill = [255, 255, 255]", "fill = [255, 255]", "preprocess[0].fill"),
            ('size = "model"', "size = -4", "preprocess[1].size"),
            ('op = "to_tensor"', 'op = "to_tensor", scale = 2', "preprocess[2].scale"),
            ('op = "pad_square", fill = [255, 255, 255]',
             'op = "normalize", mean = [0, 0, 0], std = [1, 1, 1]', "preprocess[0].op"),
            ('"sigmoid"', '"softmax"', "outputs.activation"),
            ('scores = "logits"', 'scores = "#x"', "outputs.scores"),
            ('"4" = "character"', '"4" = "person"', "vocabulary.categories"),
            ('format = "csv"', 'format = "xml"', "vocabulary.format"),
            ('name = "name"\n', "", "vocabulary.name"),
            ("general = 1", "general = true", "defaults.roles"),
            (', model = "m.onnx"', "", "source.model"),
            ("[vocabulary]", '[thresholds]\nkind = "constant"\nvalue = {artist=0.5}\n[vocabulary]',
             "thresholds.value.artist"),
            ("[vocabulary]", '[thresholds]\nkind = "npz"\nthreshold = "t"\n[vocabulary]',
             "thresholds.file"),
            (BASE, "name = [", "not TOML")):
        why = _refused(definition.parse, BASE.replace(old, new))
        assert why.startswith(path), f"wanted {path!r}, got {why}"
    found = sorted((Path(__file__).parents[1] / "examples" / "models").glob("*.toml"))
    assert found
    json.dumps([definition.as_json(definition.load(p)) for p in found])
    print(f"  ok  bad fields name their path; all {len(found)} example definitions parse")


def test_preprocessing() -> None:
    tall, square = _image(20, 50), _image(30, 30)
    ref = Image.new("RGB", (50, 50), (1, 2, 3))
    ref.paste(tall, (15, 0))
    assert np.array_equal(np.asarray(preprocess.pad_square(tall, [1, 2, 3])), np.asarray(ref))
    for name, flag in preprocess.INTERPOLATION.items():
        assert np.array_equal(np.asarray(preprocess.resize(tall, 16, name)),
                              np.asarray(tall.resize((16, 16), flag))), name
    assert preprocess.resize(square, 30, "bicubic") is square
    assert preprocess.pad_square(square, [0, 0, 0]) is square
    x = np.asarray(tall).astype(np.float32)
    assert np.array_equal(t := preprocess.to_tensor(tall), x / 255) and t.dtype == np.float32
    assert np.array_equal(preprocess.to_array(tall), x)
    assert np.array_equal(preprocess.channel_order(x, "bgr"), x[:, :, [2, 1, 0]])
    assert preprocess.channel_order(x, "rgb") is x
    got = preprocess.normalize(x, [0.1, 0.2, 0.3], [0.4, 0.5, 0.6])
    assert got.dtype == np.float32 and np.array_equal(
        got, (x - np.float32([0.1, 0.2, 0.3])) / np.float32([0.4, 0.5, 0.6]))
    calls, saved = {}, dict(preprocess.OPS)
    try:
        for op, (fn, checks, stage) in saved.items():
            def counted(x, _fn=fn, _op=op, **kw):
                calls[_op] = calls.get(_op, 0) + 1
                return _fn(x, **kw)
            preprocess.OPS[op] = (counted, checks, stage)
        pad = ("pad_square", {"fill": [255, 255, 255]})
        size = ("resize", {"size": 16, "interpolation": "bicubic"})
        a = preprocess.chain([pad, size, ("to_tensor", {}),
                              ("normalize", {"mean": [.5] * 3, "std": [.5] * 3})], None, False)
        b = preprocess.chain([pad, size, ("to_tensor", {}),
                              ("channel_order", {"order": "bgr"})], None, False)
        c = preprocess.chain([pad, ("resize", {"size": 16, "interpolation": "lanczos"}),
                              ("to_array", {})], None, True)
        views = ImageViews(_image())
        a(views), b(views), c(views), a(views)
    finally:
        preprocess.OPS.clear()
        preprocess.OPS.update(saved)
    assert calls == {"pad_square": 1, "resize": 2, "to_tensor": 1, "normalize": 1,
                     "channel_order": 1, "to_array": 1}, calls
    steps = [("resize", {"size": "model", "interpolation": "bilinear"}), ("to_tensor", {})]
    views = ImageViews(_image(mode="L"))
    nchw, nhwc = (preprocess.chain(steps, 12, last)(views) for last in (False, True))
    assert nchw.shape == (3, 12, 12) and np.array_equal(nchw, nhwc.transpose(2, 0, 1))
    _refused(preprocess.chain, steps, None, False)
    print("  ok  every op matches numpy/PIL; shared steps run once; NCHW equals NHWC")


def _files(tmp: Path, seen: list):
    """The vocabulary and threshold files, and a fetcher that serves them."""
    (tmp / "tags.csv").write_text(
        "id,name,category,thr,f1\n0,a b,0,0.4,0.8\n1,cat,0,x,0.5\n"
        "2,char-a,4,0.7,bad\n3,general,9,0.3,0.9\n4,explicit,9,1.5,0.1\n", encoding="utf-8")
    (tmp / "vocab.json").write_text(json.dumps(
        {"num_tags": 3, "idx_to_tag": {"0": "a b", "1": "char-a", "2": "e"},
         "tag_to_category": {"a b": "General", "char-a": "Character", "e": "Meta"}}))
    names = np.array(["a b", "char-a", "e"], dtype=object)
    np.savez(tmp / "m.npz", thr=np.float32([0.2, 0.9, 0.5]), f1=np.float32([0.1, 0.2, 0.3]),
             names=names)
    np.savez(tmp / "bad.npz", thr=np.float32([0.2, 0.9, 0.5]), names=names[[1, 0, 2]])

    def get(d, filename, repo=None, revision=None):
        seen.append((filename, repo, revision))
        return str(tmp / filename)
    return get


def test_vocabularies_and_thresholds() -> None:
    with tempfile.TemporaryDirectory() as t:
        get = _files(Path(t), seen := [])
        cats = ["general", "general", "character", "rating", "rating"]
        assert definition.vocabulary(definition.parse(BASE), get)[1:] == (NAMES, cats)
        text = BASE.replace('name = "name"', "name = 1").replace('"category"', "2")
        text += "space_to_underscore = true"
        raw, names, got = definition.vocabulary(definition.parse(text), get)
        assert raw[0] == "a b" and names[0] == "a_b" and got == cats
        abe = ["a b", "char-a", "e"]
        assert definition.vocabulary(definition.parse(BASE.replace(VOCAB, JSON_VOCAB)), get) \
            == (abe, abe, ["general", "character", None])
        seen.clear()
        _, names, got = definition.vocabulary(definition.parse(BASE.replace(
            VOCAB.split("\n", 3)[3], 'categories = { "*" = "general" }\n'
            'repo = "other/tags"\nrevision = "abc"')), get)
        assert got == ["general"] * 5 and seen == [("tags.csv", "other/tags", "abc")]

        def published(text, k):
            d = definition.parse(text.replace("[vocabulary]", f"[thresholds]\n{k}\n[vocabulary]"))
            return [(r.category, r.tag, r.threshold, r.f1 and round(r.f1, 3))
                    for r in definition.published_thresholds(d, get)]
        assert published(BASE, 'kind = "csv"\nthreshold = "thr"\nf1 = "f1"') == [
            ("general", "a b", 0.4, 0.8), ("character", "char-a", 0.7, None),
            ("rating", "general", 0.3, 0.9)]
        npz = 'kind = "npz"\nthreshold = "thr"\nnames = "names"\nfile = '
        json_base = BASE.replace(VOCAB, JSON_VOCAB)
        assert published(json_base, npz + '"m.npz"\nf1 = "f1"') == [
            ("general", "a b", np.float32(0.2).item(), 0.1),
            ("character", "char-a", np.float32(0.9).item(), 0.2)]
        assert "vocabulary order" in _refused(published, json_base, npz + '"bad.npz"')
        assert published(BASE, 'kind = "constant"\nvalue = { character = 0.6 }') == [
            ("character", "char-a", 0.6, None)]
    print("  ok  csv, json and another repo's vocabularies; csv, npz and constant thresholds")


def _graph(tmp: Path, nhwc: bool) -> dict:
    rng = np.random.default_rng(1)
    w = {n: rng.normal(size=(48, k)).astype(np.float32) for n, k in (("we", 3), ("wl", 5),
                                                                     ("wn", 2))}
    shape = "N,4,4,3" if nhwc else "N,3,4,4"
    m = onnx.parser.parse_model(
        f'<ir_version: 10, opset_import: ["" : 17]>\ntiny (float[{shape}] x) => '
        "(float[N,3] emb, float[N,5] logits, float[N,2] narrow)\n"
        "{ f = Flatten(x)  emb = MatMul(f, we)  logits = MatMul(f, wl)  narrow = MatMul(f, wn) }")
    m.graph.initializer.extend(onnx.numpy_helper.from_array(v, k) for k, v in w.items())
    onnx.save(m, str(tmp / "m.onnx"))
    return w


def _backend(tmp, text):
    b = DefinedBackend(definition.parse(text), batch_size=2, half=False)
    b._fetch = _files(tmp, [])
    return b


def test_backend_scores_like_the_graph() -> None:
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        keep = frozenset({"cat"})
        for nhwc in (False, True):
            w = _graph(tmp, nhwc)
            b = _backend(tmp, BASE)
            arrays = [b.prepare(ImageViews(_image(seed=i))) for i in range(3)]
            assert arrays[0].shape == ((4, 4, 3) if nhwc else (3, 4, 4))
            for a, s in zip(arrays, b.score_prepared(arrays, top_k=1, keep=keep)):
                flat = a.reshape(1, -1)
                probs = 1.0 / (1.0 + np.exp(-(flat @ w["wl"])[0]))
                want = (top_scores(NAMES, probs, np.array([0, 1]), "general", 1, keep)
                        + top_scores(NAMES, probs, np.array([2]), "character", 1, keep)
                        + top_scores(NAMES, probs, np.array([3, 4]), "rating", 2))
                got = {(r.category, r.tag): r.score for r in s.scores}
                assert got.keys() == {(r.category, r.tag) for r in want}
                assert all(abs(got[(r.category, r.tag)] - r.score) < 1e-5 for r in want)
                assert np.allclose(s.embedding, (flat @ w["we"])[0], atol=1e-5)
        print("  ok  sigmoid, NCHW and NHWC, rating kept whole, embedding by #0")

        _graph(tmp, False)
        for wanted, name in (("widest", "logits"), ("#2", "narrow"), ("narrow", "narrow")):
            b = _backend(tmp, BASE.replace('scores = "logits"', f'scores = "{wanted}"'))
            b.load()
            assert b._outputs[0] == name, (wanted, b._outputs)
        _refused(_backend(tmp, BASE.replace('scores = "logits"', 'scores = "nope"')).load)
        print("  ok  outputs by name, #index and widest; a missing name is refused")


class FakeBackend:
    name, _model, loads = "fake", None, 0

    def load(self) -> None:
        if self._model is None:
            self.loads, self._model = self.loads + 1, object()

    def unload(self) -> None:
        self._model = None


def test_tagger_releases_every_backend() -> None:
    backends = [FakeBackend(), FakeBackend()]
    tagger = Tagger([BackendConfig(backend=b, category_priority={}) for b in backends])
    tagger.ensure_loaded(), tagger.ensure_loaded()
    assert [b.loads for b in backends] == [1, 1] and all(b._model for b in backends)
    tagger.unload()
    assert not any(b._model for b in backends), "a backend kept its session"
    tagger.ensure_loaded()
    assert [b.loads for b in backends] == [2, 2]
    print("  ok  loading is idempotent, unloading reaches every backend, and it reloads")


for check in (test_parser_names_the_field, test_preprocessing, test_vocabularies_and_thresholds,
              test_backend_scores_like_the_graph, test_tagger_releases_every_backend):
    check()
print("all definition checks passed")
