# Polaris

Polaris tags and searches a local image and video library. It samples pages
and frames, scores them with ONNX tagging models, and stores the scores in
PostgreSQL. A web interface is used to register folders, run scans, correct
the results and search.

It is a personal, single-user tool. It has been used on Windows with an
NVIDIA GPU only. The web interface is in Traditional Chinese.

## What it does

- **Collections.** Folders are registered as collections. A folder of images
  is one work; a video file is one work.
- **Scanning.** A scan job picks pages from image works and frames from video
  works, runs the models of a model profile on them, and stores the top
  scores per category for every page. Only models that have no scores for a
  work yet are run.
- **Model profiles.** A profile lists model versions and the roles they fill
  (general, character, copyright, artist, rating, embedding), with a score
  cutoff per role and the derivation parameters. One profile is active; other
  maintained profiles are kept up to date alongside it.
- **Claims.** A person can add a value to a work, remove one a model gave, or
  set a single-valued field such as the work type. Claims are kept across
  rescans.
- **Vocabulary.** Concepts (artists, characters, series, tags) with aliases,
  Chinese display names, and series for characters. Aliases can be limited
  to search.
- **Search.** By tag, artist, series and other fields; by visual similarity
  (pgvector, exact search over work vectors); or both combined.
- **Duplicate check.** Folders or videos outside the library are compared
  with it, by content and by vector distance, without being added.
- **Model definitions.** A model is a TOML file. A definition can be uploaded
  in the web interface and built as a job at FP32 or FP16.

## How data is stored

Tables fall into two groups.

- **Authoritative:** what a scan observed (samples, per-page scores, page
  embeddings, measurements) and what a person said (claims, concepts,
  aliases, parameters). These cannot be recomputed.
- **Derived** (schema `derived`): work tags, work search rows, work vectors,
  published thresholds. These are recomputed from the authoritative tables
  by SQL functions.

Changing a threshold, an alias or a claim re-derives the affected works. No
model is run again. Re-deriving a library of about ten thousand works for one
profile takes about 20 minutes.

## Models

The repository includes example definitions in `examples/models/` for these
models:

| Name | Source | Roles |
|---|---|---|
| animetimm | `animetimm/eva02_large_patch14_448.dbv4-full` | general, character, rating, embedding (1024-d) |
| canary | `yasamari/wd-eva02-tagger-2026-canary-onnx-with-embeddings` | character |
| cl_tagger | `cella110n/cl_tagger_v2` | character, copyright |
| mldanbooru | `deepghs/ml-danbooru-onnx` | general |
| eva02 | `SmilingWolf/wd-eva02-large-tagger-v3` | general, character, rating |
| camie | `deepghs/camie_tagger_onnx` | artist |
| pixai | `deepghs/pixai-tagger-v0.9-onnx` | general, character |

A new install has no models. A model is available once its definition is
uploaded (模型 → 模型定義 → 上傳) and built. Weights are downloaded from
Hugging Face at the pinned revision into `models/` (created on first use,
not part of the repository). Each model has its own license; check it before
use.

The definitions replaced one Python class per model. On 206 pages the
definitions produced bit-identical input arrays and identical scores to the
classes they replaced.

### Adding a model

A definition describes an ONNX model with one image input and a multi-label
score output. Example, shortened from `examples/models/canary.toml`:

```toml
name = "canary"
note = "Character only."

[source]
repo = "yasamari/wd-eva02-tagger-2026-canary-onnx-with-embeddings"
revision = "dfb8803a5931276375c2247621e590484a111d22"
model = "model.onnx"

[[preprocess]]
op = "pad_square"
fill = [255, 255, 255]

[[preprocess]]
op = "resize"
size = "model"
interpolation = "bicubic"

[[preprocess]]
op = "to_tensor"

[[preprocess]]
op = "channel_order"
order = "bgr"

[[preprocess]]
op = "normalize"
mean = [0.5, 0.5, 0.5]
std = [0.5, 0.5, 0.5]

[outputs]
scores = "output"

[vocabulary]
file = "selected_tags.csv"
format = "csv"
name = "name"
category = "category"
categories = { "4" = "character" }

[thresholds]
kind = "constant"
value = { character = 0.6094 }

[defaults]
roles = { character = 10 }
```

| Section | Fields |
|---|---|
| `[source]` | `repo` and `revision`, or `path` for a local folder; `model`; optional `subfolder`, `extra` (files to fetch first, e.g. external weights) |
| `[[preprocess]]` | image steps `pad_square`, `resize` (`size` = integer or `"model"`); then `to_tensor` (÷255) or `to_array` (0–255); then `channel_order`, `normalize`. NCHW or NHWC follows the model input. |
| `[outputs]` | `scores` and optional `embedding`: an output name, `"#<index>"` or `"widest"`; `activation` = `none` or `sigmoid` |
| `[vocabulary]` | `file`, `format` (`csv` or `json_idx`), `name`/`category` column (name or index), `categories` (key → category; also selects what is kept), `space_to_underscore`, optional `repo`/`revision` for a vocabulary in another repo |
| `[thresholds]` | optional published per-tag thresholds: `kind` = `csv`, `npz` or `constant` |
| `[defaults]` | `roles` (role → priority), `concurrent`, `max_images` |

In the web interface, 模型 → 模型定義 → 上傳 stores the definition, and 建置
runs a build job:

1. **download:** the model and the files the definition names;
2. **validate:** on CPU at FP32, the outputs exist, the score width equals
   the vocabulary length, and preprocessing runs;
3. **convert:** FP32 is used as is; FP16 is converted with TensorRT when it
   is available, otherwise with ONNX float16 conversion;
4. **verify:** up to 32 pages from recent scans are scored. Any NaN or Inf
   fails the build. For FP16, the share of tags that cross the threshold on
   only one of FP16 and CPU FP32 must be at most 4%.

A ready version can then be added to a profile. Scanning with that profile
scores only the works the version has no scores for.

## Requirements

- Windows, an NVIDIA GPU with CUDA
- Docker (PostgreSQL 17 with pgvector)
- Python 3.14 with [uv](https://docs.astral.sh/uv/)
- Node.js
- Optional: TensorRT 10

## Setup

Copy `.env.example` to `.env` and set:

| Variable | Meaning |
|---|---|
| `POLARIS_DSN` | PostgreSQL connection string. Required. |
| `POSTGRES_PASSWORD` | Password for the Docker database, used when its volume is first created |
| `POLARIS_TENSORRT_DIR` | Optional. The TensorRT release directory containing `nvinfer_10.dll`. When set, FP16 models run on TensorRT; otherwise on CUDA. |
| `POLARIS_MODELS_DIR` | Optional. Where model files are stored; default `models/` |
| `POLARIS_ONNX_PROVIDER` | Optional. Forces an ONNX Runtime provider |

Then run `start.cmd`. It installs the Python and web dependencies, starts
Docker and the database, checks the schema, starts the API on port 8000 and
the web page on port 5173, and opens the browser. Logs go to `data\api.log`
and `data\web.log` (`data\` is created locally and not part of the
repository). `stop.cmd` stops the API and the web page.

Without the script:

```bash
uv sync
docker compose up -d
uv run python -m uvicorn polaris.app:app --port 8000
cd web && npm install && npm run dev
```

The scan worker runs as a separate process with the GPU. It is started from
the web interface (掃描), or with `uv run polaris-worker`.

When the code expects a newer schema than the database has, the API refuses
to start and `uv run polaris-migrate` lists what would change;
`polaris-migrate --apply` applies it. Back up the database first.

The API has no authentication and listens on localhost only.

## Command-line tools

| Command | What it does |
|---|---|
| `polaris-worker` | Takes scan, check and build jobs from the queue |
| `polaris-migrate` | Lists or applies pending schema migrations |
| `polaris-curated` | Exports or imports what a scan cannot reproduce (concepts, aliases, claims, parameters), keyed by name instead of id |

## Project layout

```
polaris/
├── shared/        connection pool, schema assembly, migrations, errors
├── catalog/       collections, works and their paths
├── observation/   scanning, sampling, duplicate checks, model builds
├── models/        model definitions, the generic backend, profiles
├── vocabulary/    concepts, aliases, claims
├── derivation/    SQL that derives work tags and search rows
├── search/        tag, similarity and hybrid search
├── jobs/          job queue: claim, lease, heartbeat
├── cli/           worker, migrate, curated
└── app.py         FastAPI application
examples/models/   example model definitions
web/               Vue 3 web interface
tests/             test scripts
```

`tests/test_boundaries.py` checks which packages may import which.

## Tests

Each file in `tests/` is a script run on its own against a scratch database:

```bash
createdb polaris_test
POLARIS_TEST_DSN=postgresql://user:pass@localhost:5432/polaris_test \
    uv run python tests/test_derive.py
dropdb polaris_test
```

They use small generated images and ONNX graphs and do not download models
or need a GPU.

## License

MIT. See [LICENSE](LICENSE).
