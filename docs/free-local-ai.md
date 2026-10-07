# Free local AI for resume screening

The project supports three independent choices: extraction (`llm` or `rules`), matching (`embeddings` or `tfidf`),
and OCR (`tesseract`, `none`, or Azure when using the Azure provider). Use `LLM_PROVIDER=ollama` for local AI.
This selection does not fall back to Azure when the local server is unavailable, even if Azure keys exist.

## Models and hardware

- `shortlist-local:v1`: a project configuration of pretrained [Qwen3 1.7B](https://ollama.com/library/qwen3:1.7b),
  approximately 1.4 GB of quantized weights, with Apache-2.0 licensing. See `models/shortlist-local.Modelfile`.
- [Nomic Embed Text v1.5](https://ollama.com/library/nomic-embed-text:v1.5): approximately 274 MB of embedding
  weights. Long inputs are chunked without truncation, averaged with length weights, and normalized.
- English Tesseract weights through the pinned PyMuPDF dependency: OCR runs locally in an isolated process.

An 8 GB RAM laptop with integrated graphics can use these models on its CPU. Extraction takes longer than a
cloud model, especially on the first request. The setup downloads several GB including the Windows runtime;
allow at least 8 GB free disk space. The local profile uses one worker, one concurrent request, and one loaded
model. Model switching between extraction and embeddings adds loading time.

## Windows setup

From the project root:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe scripts/local_ai.py setup
if (!(Test-Path .env)) { Copy-Item .env.local-ai.example .env }
```

For an existing `.env`, preserve its database, blob, and authentication settings. Copy the AI, OCR, timeout,
and concurrency settings from `.env.local-ai.example` into it. Both API and worker read the same `.env`.

Start PostgreSQL and Azurite using the [README setup](../README.md#development-setup), then apply migrations:

```powershell
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m scripts.check_setup
```

Keep these in separate terminals:

```powershell
.\.venv\Scripts\python.exe scripts/local_ai.py serve
```

```powershell
.\.venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

```powershell
.\.venv\Scripts\python.exe -m backend.worker
```

Start the dashboard using the [frontend README](../frontend/README.md). Stop the model server with Ctrl+C.
The helper disables Ollama cloud features and binds to loopback. It does not create an account or install a
Windows startup service. If you already run Ollama, setup reuses that server and its model directory; configure
it with `OLLAMA_NO_CLOUD=1` and one parallel request, or stop it and use this helper's `serve` command.
With an installed Ollama CLI, the helper preserves `OLLAMA_MODELS` or uses the normal user model directory;
the portable Windows runtime uses `.cache/local-ai/models`.

Linux/macOS: install [Ollama](https://ollama.com/download) first, then run the same Python helper commands.
The automatic portable runtime installation is Windows-only.

## Checks and failures

```powershell
.\.venv\Scripts\python.exe scripts/local_ai.py status
.\.venv\Scripts\python.exe scripts/local_ai.py smoke
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
```

The smoke check uses synthetic repository samples and the real installed models. It verifies extraction,
job analysis, and embedding responses; the ordinary test suite stays offline and uses mocked HTTP responses.
Smoke success is not a measured accuracy guarantee.

The helper records installed model digests in `.cache/local-ai/installed-models.json`. Each durable screening
run stores its actual chat/embedding digests, context and output limits, requirements, weights and reference
date in its snapshot. A changed model is rejected when executing the original run. Normal setup keeps
installed tags; `setup --rebuild` deliberately refreshes them and requires new runs afterwards.

The gateway sends a JSON schema and validates the result before the existing evidence checks. Its versioned
local instructions include an exact-quote example for comma-separated skill lists. Skill evidence is also
constrained to actual source lines by the local decoder; claim verification still runs afterwards. Unsupported
facts are discarded. Invalid JSON, incomplete output, oversized prompts, OCR timeouts and missing weights
produce reprocessing errors. A missing local server/model returns 503 when creating a screening run.
Nothing automatically switches to paid AI or silently scores a failed extraction with rules.

`OLLAMA_NUM_CTX=16384` reserves room for the extraction schema and output. A conservative UTF-8 byte budget
rejects very long resumes instead of letting the runtime silently drop their text. Increase it on a machine
with more RAM, or review a shorter document. OCR defaults to English, 30 pages, 200 DPI, a page pixel limit,
and a 120-second hard timeout. To add a language, download its Tesseract weights and set `TESSERACT_LANGUAGE`
(for example `eng+hin`) and `TESSERACT_DATA_DIR`.

Local usage retains token/page counts and records AI API cost as zero. This does not estimate electricity or
hardware cost. Semantic similarities use `ollama-embeddings` in explanations, so Azure embedding calibration
thresholds are not incorrectly applied to a different model.

## Speed profiles

Measured on an Intel i3-N305 laptop (8 cores, 8 GB RAM, no GPU) with the sample resumes:

| Profile | `.env` | Time per resume |
| --- | --- | --- |
| Fast (no chat model) | `EXTRACTION_MODE=rules`, `SEMANTIC_BACKEND=embeddings`, `EXPLANATION_MODE=template` | about 2–3 s |
| Local model, template explanations | `EXTRACTION_MODE=llm`, `EXPLANATION_MODE=template` | about 3.5 min |
| Local model, model explanations | `EXTRACTION_MODE=llm`, `EXPLANATION_MODE=llm` | about 5–6 min |

Most local-model time is writing the extraction JSON (about 7 tokens/s on this CPU) and reading the prompt
(about 25 tokens/s). Rules extraction reads clearly structured resumes (headed sections, dated roles, a skills
list) well; the local model is more tolerant of unusual layouts. Both use the same evidence checks, knockouts
and scoring. Several workers do not help on a CPU, and loading both models at once needs free RAM the laptop
usually lacks. A GPU is the main hardware speed-up for the local model.

## Moving to a faster computer

The model server uses an NVIDIA GPU (CUDA) automatically, and AMD/Intel GPUs through Vulkan where the driver
supports it. No code change is needed; the gains come from the GPU and from these settings.

Recommended: an NVIDIA GPU with 6 GB or more video memory, 16 GB RAM, and an up-to-date graphics driver.
Without a GPU, a newer 8–16 core CPU with 16 GB RAM helps only moderately.

1. Copy the project folder, excluding `.venv`, `frontend/node_modules`, `.cache/pgdata` and `.cache/local-ai`.
   Those are rebuilt or reinstalled on the new computer.
2. Follow [Windows setup](#windows-setup) above. `setup` downloads the runtime and models again.
3. In `.env`, use the GPU profile:

   ```ini
   EXTRACTION_MODE=llm
   EXPLANATION_MODE=llm
   OLLAMA_NUM_PARALLEL=2
   OLLAMA_MAX_LOADED_MODELS=2
   OLLAMA_KEEP_ALIVE=30m
   WORKER_CONCURRENCY=2
   ```

   Keep `OLLAMA_NUM_PARALLEL` equal to `WORKER_CONCURRENCY`. Two loaded models avoid unloading the chat model
   for every embedding request. Raise both to 4 only with 12 GB or more video memory.
4. Start `scripts/local_ai.py serve`, run one screening (or `smoke`), then check where the models run:

   ```powershell
   .\.venv\Scripts\python.exe scripts/local_ai.py status
   ```

   `shortlist-local:v1: GPU` means the GPU is used. `CPU` means the driver or GPU was not detected; a split
   such as `60% GPU / 40% CPU` means the video memory is too small for the settings, so lower the values in step 3.

To move existing data, back up PostgreSQL with `pg_dump` and copy the blob container; otherwise start with a new
workspace. Keep the same `JWT_SECRET` and `PII_HASH_KEY` when moving data, or existing logins and contact
searches stop working.

## Training your own weights later

The current model is pretrained; the project adds prompts, schemas, evidence verification and screening
logic. Truly training or fine-tuning your own model needs consented, labeled resume/JD examples, a held-out
evaluation set and suitable compute. Start by measuring extraction precision/recall and evidence failures
against expert-reviewed labels, then compare any fine-tuned model on the same held-out set before switching
production runs. Training from scratch is not practical on an 8 GB CPU laptop.

The project now includes a synthetic-data preparation and LoRA fine-tuning workflow for the existing
Qwen3 model. See [model-training.md](model-training.md) for commands, dataset contracts, compute
requirements, held-out evaluation and deployment through a separate Ollama tag.
