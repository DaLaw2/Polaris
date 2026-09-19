"""Model definitions: one TOML file per model, parsed into a frozen spec.

A definition says where the weights come from, how an image becomes the
model's input, which output holds the scores, how to read the vocabulary
and what the model publishes about its own cutoffs. Every field is
checked, and an error names the field it is about.

Vocabulary formats and published-threshold kinds are entries in
`VOCABULARY` and `THRESHOLDS`: the reader, and the fields it takes.
"""

from __future__ import annotations

import csv
import dataclasses
import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import preprocess
from .backends.base import ALL_CATEGORIES, TagThreshold

ROLES = ALL_CATEGORIES + ("embedding",)
REQUIRED = object()


class DefinitionError(ValueError):
    """A definition that cannot be used, with the field it failed on."""


@dataclass(frozen=True)
class Source:
    model: str
    repo: str | None = None
    path: str | None = None
    revision: str | None = None
    subfolder: str = ""
    extra: tuple[str, ...] = ()


@dataclass(frozen=True)
class Step:
    op: str
    params: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Outputs:
    scores: str
    activation: str = "none"
    embedding: str = ""


@dataclass(frozen=True)
class Vocabulary:
    file: str
    format: str
    categories: dict[str, str]
    name: str | int | None = None
    category: str | int | None = None
    space_to_underscore: bool = False
    repo: str | None = None
    revision: str | None = None


@dataclass(frozen=True)
class Thresholds:
    kind: str
    file: str = ""
    threshold: str = ""
    f1: str = ""
    names: str = ""
    value: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class Defaults:
    roles: dict[str, int]
    concurrent: bool = True
    max_images: int | None = None
    enabled_by_default: bool = False
    fills_primary_roles: bool = False


@dataclass(frozen=True)
class Definition:
    name: str
    source: Source
    preprocess: tuple[Step, ...]
    outputs: Outputs
    vocabulary: Vocabulary
    defaults: Defaults
    thresholds: Thresholds | None = None
    note: str = ""


def _type(kind, what):
    def check(v):
        if not isinstance(v, kind) or (kind is int and isinstance(v, bool)):
            raise ValueError(f"expected {what}")
        return v
    return check


_str = _type(str, "a string")
_bool = _type(bool, "true or false")
_dict = _type(dict, "a table")
_list = _type(list, "a list of tables")


def _nonempty(v):
    if not _str(v):
        raise ValueError("expected a non-empty string")
    return v


def _positive(v):
    if type(v) is not int or v < 1:
        raise ValueError("expected a positive integer")
    return v


def _column(v):
    if (type(v) is int and v >= 0) or (isinstance(v, str) and v):
        return v
    raise ValueError("expected a column name or index")


def _strings(v):
    if not (isinstance(v, list) and all(isinstance(s, str) and s for s in v)):
        raise ValueError("expected a list of strings")
    return tuple(v)


def _mapping(check_key, check_value, what):
    def check(v):
        if not isinstance(v, dict) or not v:
            raise ValueError(f"expected a table of {what}")
        for k, x in v.items():
            try:
                check_key(k)
                check_value(x)
            except ValueError as e:
                raise ValueError(f"{k}: {e}") from None
        return {k: check_value(x) for k, x in v.items()}
    return check


def _among(names):
    def check(v):
        if v not in names:
            raise ValueError(f"expected one of {', '.join(names)}")
        return v
    return check


def _probability(v):
    if type(v) not in (int, float) or not 0 <= v <= 1:
        raise ValueError("expected a number from 0 to 1")
    return float(v)


def _output(v):
    _nonempty(v)
    if v.startswith("#") and not v[1:].isdigit():
        raise ValueError('expected a name, "#<index>" or "widest"')
    return v


def _table(raw, path, fields) -> dict:
    """`raw` checked against `fields` (key -> (check, default)); unknown
    keys and missing required ones are errors."""
    if not isinstance(raw, dict):
        raise DefinitionError(f"{path}: expected a table")

    def where(key):
        return f"{path}.{key}" if path else key

    for key in raw:
        if key not in fields:
            raise DefinitionError(f"{where(key)}: unknown field")
    out = {}
    for key, (check, default) in fields.items():
        if key not in raw:
            if default is REQUIRED:
                raise DefinitionError(f"{where(key)}: required")
            out[key] = default
            continue
        try:
            out[key] = check(raw[key])
        except ValueError as e:
            raise DefinitionError(f"{where(key)}: {e}") from None
    return out


def _steps(raw) -> tuple[Step, ...]:
    if not isinstance(raw, list) or not raw:
        raise DefinitionError("preprocess: expected at least one [[preprocess]]")
    steps, stage = [], "image"
    order = {"image": 0, "convert": 1, "array": 2}
    for i, item in enumerate(raw):
        path = f"preprocess[{i}]"
        op = item.get("op") if isinstance(item, dict) else None
        if op not in preprocess.OPS:
            raise DefinitionError(f"{path}.op: expected one of "
                                  f"{', '.join(preprocess.OPS)}")
        _, checks, op_stage = preprocess.OPS[op]
        params = _table(item, path, {"op": (_str, REQUIRED),
                                     **{k: (c, REQUIRED) for k, c in checks.items()}})
        del params["op"]
        if order[op_stage] < order[stage] or (op_stage == stage == "convert"):
            raise DefinitionError(
                f"{path}.op: {op} cannot come after a {stage} step")
        if op_stage == "array" and stage == "image":
            raise DefinitionError(
                f"{path}.op: {op} needs a to_tensor or to_array step first")
        stage = op_stage
        steps.append(Step(op, params))
    if stage == "image":
        raise DefinitionError("preprocess: no to_tensor or to_array step")
    return tuple(steps)


def parse(text: str) -> Definition:
    """The definition in `text`, or `DefinitionError`."""
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise DefinitionError(f"not TOML: {e}") from None

    top = _table(raw, "", {
        "name": (_nonempty, REQUIRED), "note": (_str, ""),
        "source": (_dict, REQUIRED), "preprocess": (_list, REQUIRED),
        "outputs": (_dict, REQUIRED), "vocabulary": (_dict, REQUIRED),
        "thresholds": (_dict, None), "defaults": (_dict, REQUIRED)})

    source = Source(**_table(top["source"], "source", {
        "repo": (_nonempty, None), "path": (_nonempty, None),
        "model": (_nonempty, REQUIRED),
        "revision": (_nonempty, None), "subfolder": (_str, ""),
        "extra": (_strings, ())}))
    if (source.repo is None) == (source.path is None):
        raise DefinitionError("source: expected exactly one of repo or path")
    if source.revision and source.path:
        raise DefinitionError("source.revision: only with source.repo")

    outputs = Outputs(**_table(top["outputs"], "outputs", {
        "scores": (_output, REQUIRED),
        "activation": (_among(("none", "sigmoid")), "none"),
        "embedding": (_str, "")}))

    raw_vocab = top["vocabulary"]
    fmt = raw_vocab.get("format")
    if fmt not in VOCABULARY:
        raise DefinitionError(f"vocabulary.format: expected one of "
                              f"{', '.join(VOCABULARY)}")
    vocab = Vocabulary(**_table(raw_vocab, "vocabulary", {
        "file": (_nonempty, REQUIRED), "format": (_str, REQUIRED),
        "categories": (_mapping(_nonempty, _among(ALL_CATEGORIES),
                                "category names"), REQUIRED),
        "space_to_underscore": (_bool, False),
        "repo": (_nonempty, None), "revision": (_nonempty, None),
        **VOCABULARY[fmt][1]}))
    if vocab.revision and not vocab.repo:
        raise DefinitionError("vocabulary.revision: only with vocabulary.repo")
    if "*" in vocab.categories and len(vocab.categories) > 1:
        raise DefinitionError('vocabulary.categories: "*" maps every tag, '
                              "so it must be the only entry")
    if fmt == "csv" and vocab.category is None and "*" not in vocab.categories:
        raise DefinitionError('vocabulary.categories: without a category '
                              'column the only key is "*"')

    thresholds = None
    if top["thresholds"] is not None:
        kind = top["thresholds"].get("kind")
        if kind not in THRESHOLDS:
            raise DefinitionError(f"thresholds.kind: expected one of "
                                  f"{', '.join(THRESHOLDS)}")
        thresholds = Thresholds(**_table(top["thresholds"], "thresholds", {
            "kind": (_str, REQUIRED), **THRESHOLDS[kind][1]}))
        emitted = set(vocab.categories.values())
        for category in thresholds.value:
            if category not in emitted:
                raise DefinitionError(
                    f"thresholds.value.{category}: not a category the "
                    "vocabulary emits")

    defaults = Defaults(**_table(top["defaults"], "defaults", {
        "roles": (_mapping(_among(ROLES), _positive, "role priorities"),
                  REQUIRED),
        "concurrent": (_bool, True), "max_images": (_positive, None),
        "enabled_by_default": (_bool, False),
        "fills_primary_roles": (_bool, False)}))

    return Definition(
        name=top["name"], note=top["note"], source=source,
        preprocess=_steps(top["preprocess"]), outputs=outputs,
        vocabulary=vocab, thresholds=thresholds, defaults=defaults)


def load(path: str | Path) -> Definition:
    path = Path(path)
    try:
        return parse(path.read_text(encoding="utf-8"))
    except DefinitionError as e:
        raise DefinitionError(f"{path.name}: {e}") from None


def as_json(d: Definition) -> dict:
    """`d` as plain JSON-able data."""
    return json.loads(json.dumps(dataclasses.asdict(d)))


def fetch(d: Definition, filename: str, repo: str | None = None,
          revision: str | None = None) -> str:
    """A local path to `filename`: from `repo` at `revision` when given,
    otherwise from the definition's source, a repo or a local folder."""
    from huggingface_hub import hf_hub_download

    if repo is None and d.source.path:
        path = Path(d.source.path, d.source.subfolder, filename)
        if not path.is_file():
            raise FileNotFoundError(f"{path} does not exist")
        return str(path)
    if repo is None:
        repo, revision, subfolder = (d.source.repo, d.source.revision,
                                     d.source.subfolder or None)
    else:
        subfolder = None
    return hf_hub_download(repo, filename, subfolder=subfolder,
                           revision=revision)


def _csv_vocabulary(path, v: Vocabulary):
    with open(path, encoding="utf-8", newline="") as f:
        rows = csv.reader(f)
        header = next(rows)
        name = v.name if isinstance(v.name, int) else header.index(v.name)
        cat = (v.category if isinstance(v.category, int) or v.category is None
               else header.index(v.category))
        pairs = [(r[name], "" if cat is None else r[cat]) for r in rows]
    return [p[0] for p in pairs], [p[1] for p in pairs]


def _json_idx_vocabulary(path, v: Vocabulary):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    names = [data["idx_to_tag"][str(i)] for i in range(data["num_tags"])]
    return names, [data["tag_to_category"][t] for t in names]


VOCABULARY = {
    "csv": (_csv_vocabulary, {"name": (_column, REQUIRED),
                              "category": (_column, None)}),
    "json_idx": (_json_idx_vocabulary, {}),
}


def vocabulary(d: Definition, get=fetch):
    """(raw names, names, category per tag or None when not emitted), in
    output order. `get(d, filename, repo, revision)` supplies files."""
    v = d.vocabulary
    raw, keys = VOCABULARY[v.format][0](get(d, v.file, v.repo, v.revision), v)
    names = [n.replace(" ", "_") for n in raw] if v.space_to_underscore else raw
    every = v.categories.get("*")
    cats = [every or v.categories.get(k) for k in keys]
    return raw, names, cats


def _float_or_none(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _csv_thresholds(d, get, raw, names, cats):
    t, v = d.thresholds, d.vocabulary
    path = get(d, t.file) if t.file else get(d, v.file, v.repo, v.revision)
    with open(path, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    if len(rows) != len(names):
        raise ValueError(f"{t.file or v.file} has {len(rows)} rows, "
                         f"the vocabulary {len(names)}")
    out = []
    for row, name, cat in zip(rows, names, cats):
        threshold = _float_or_none(row[t.threshold])
        if cat and threshold is not None and 0.0 <= threshold <= 1.0:
            out.append(TagThreshold(cat, name, threshold,
                                    _float_or_none(row[t.f1]) if t.f1 else None))
    return out


def _npz_strings(path, key):
    """A string array out of an npz without unpickling anything but the
    array itself."""
    import pickle
    import zipfile

    import numpy as np

    class Arrays(pickle.Unpickler):
        def find_class(self, module, name):
            if (module, name) in {("numpy", "ndarray"), ("numpy", "dtype"),
                                  ("numpy.core.multiarray", "_reconstruct"),
                                  ("numpy._core.multiarray", "_reconstruct")}:
                return super().find_class(module, name)
            raise pickle.UnpicklingError(f"{module}.{name} is not allowed")

    with zipfile.ZipFile(path) as z, z.open(f"{key}.npy") as f:
        if np.lib.format.read_magic(f) == (1, 0):
            np.lib.format.read_array_header_1_0(f)
        else:
            np.lib.format.read_array_header_2_0(f)
        return [str(s) for s in Arrays(f).load()]


def _npz_thresholds(d, get, raw, names, cats):
    import numpy as np

    t = d.thresholds
    path = get(d, t.file)
    data = np.load(path, allow_pickle=False)
    if t.names and _npz_strings(path, t.names) != raw:
        raise ValueError(f"{t.file} is not in vocabulary order")
    thr = data[t.threshold]
    f1 = data[t.f1] if t.f1 else None
    return [TagThreshold(cats[i], names[i], float(thr[i]),
                         float(f1[i]) if f1 is not None else None)
            for i in range(len(names))
            if cats[i] and 0.0 <= thr[i] <= 1.0]


def _constant_thresholds(d, get, raw, names, cats):
    value = d.thresholds.value
    return [TagThreshold(c, n, value[c]) for n, c in zip(names, cats)
            if c in value]


THRESHOLDS = {
    "csv": (_csv_thresholds, {"threshold": (_nonempty, REQUIRED),
                              "f1": (_str, ""), "file": (_str, "")}),
    "npz": (_npz_thresholds, {"file": (_nonempty, REQUIRED),
                              "threshold": (_nonempty, REQUIRED),
                              "f1": (_str, ""), "names": (_str, "")}),
    "constant": (_constant_thresholds, {
        "value": (_mapping(_among(ALL_CATEGORIES), _probability,
                           "category cutoffs"), REQUIRED)}),
}


def published_thresholds(d: Definition, get=fetch) -> list[TagThreshold]:
    """What the model publishes about where to cut its emitted tags."""
    if d.thresholds is None:
        return []
    raw, names, cats = vocabulary(d, get)
    return THRESHOLDS[d.thresholds.kind][0](d, get, raw, names, cats)
