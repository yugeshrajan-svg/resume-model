# Fine-tuning the free local extraction model

The dashboard accepts **large resume selections, including more than 500 files**. Three files upload concurrently; registrations
and extraction jobs are persisted as each small batch succeeds. Resume processing runs through
the existing PostgreSQL queue, at the configured `WORKER_CONCURRENCY`. The laptop's local model
profile uses one worker. Upload capacity comes from this queue; fine-tuning improves extraction
behavior and does not make 500 model calls run simultaneously.

The list shows 25 files per page, with overall registration progress and per-file retry actions.
Keep the browser tab open until uploads finish. After registration, the worker can continue even
when the browser closes. File bytes that have not uploaded cannot be restored after a reload;
registration retries reuse their uploaded blobs. All candidates still need consent to be screened.
Use **Clear waiting files** to remove a large unregistered selection in one action; registered
resumes remain saved. Larger selections stay queued and continue automatically in small batches.

## What is available now

`scripts/train_local_model.py` prepares datasets, fine-tunes the existing **Qwen/Qwen3-1.7B** using
[Hugging Face LoRA](https://huggingface.co/docs/peft/v0.18.0/en/conceptual_guides/lora), evaluates an
Ollama model, and merges trained adapters for local import. Training targets **professional facts
and exact evidence quotes**, using the same prompts and schemas as inference. Scores, knockouts,
consent checks and recruiter decisions continue to use the existing application logic.

The starter generator creates **120 synthetic examples**, including resume layout variants,
instruction-injection text, missing information and job descriptions. Labels are constructed from
templates and checked with the actual evidence verifier. They are demonstration data for checking
the pipeline; they do not establish accuracy on real resumes. The active model is still pretrained.

Preparation and dry-run need only the normal backend environment. They make no model/API calls.
The training libraries are optional and belong in a separate environment. Public model downloads
and local inference require no paid AI key. Hardware, storage and electricity still have costs.

## Prepare and check the synthetic dataset

From the project root:

```powershell
.\.venv\Scripts\python.exe -m scripts.train_local_model synthetic --output .cache/model-training/synthetic.jsonl
.\.venv\Scripts\python.exe -m scripts.train_local_model prepare --input .cache/model-training/synthetic.jsonl --output .cache/model-training/prepared
.\.venv\Scripts\python.exe -m scripts.train_local_model train --dataset .cache/model-training/prepared --output .cache/model-training/experiment --dry-run
```

Choose new output paths for another experiment. Commands refuse to overwrite existing datasets
or experiments. Dataset preparation is deterministic for a given source and seed.

This starter produces 97 training, 13 validation and 10 held-out test rows with seed 42. Resume
variants and repeated job descriptions share groups. Each group stays in one split. The manifest
records source/split SHA256 hashes, prompt and verifier versions, and whether all rows are synthetic.
Training refuses data or instructions that have changed since preparation. Test rows are excluded
from training and checkpoint selection.

## Fine-tune on a suitable computer

The current 8 GB CPU laptop is suitable for dataset preparation and its existing inference profile.
Full Qwen3 training is substantially more demanding. Plan to use a CUDA GPU with roughly 12 GB
VRAM and at least 16 GB system RAM for the default non-quantized LoRA configuration; actual memory
depends on sequence length and driver. This is a starting estimate, not a measured hardware guarantee.
The command checks CUDA before downloading weights. `--allow-cpu` is available on a machine with
sufficient RAM, but CPU training is slow.

Create an isolated environment; the API/worker do not need these libraries:

```powershell
python -m venv .cache/model-training/venv
# Install the CUDA build matching the training machine; cu128 is an example for a compatible driver.
.\.cache\model-training\venv\Scripts\python.exe -m pip install torch==2.9.1 --index-url https://download.pytorch.org/whl/cu128
.\.cache\model-training\venv\Scripts\python.exe -m pip install -e . -r requirements-training.txt
.\.cache\model-training\venv\Scripts\python.exe -m scripts.train_local_model train --dataset .cache/model-training/prepared --output .cache/model-training/experiment
```

Use the [PyTorch installation selector](https://pytorch.org/get-started/locally/) for your platform and
driver instead of assuming the example CUDA build fits your computer. For CPU installation use
`https://download.pytorch.org/whl/cpu` and explicitly pass `--allow-cpu` when training.

The trainer resolves the Hugging Face base-model revision once, uses that same immutable revision
for weights and tokenizer, and records it. It trains a rank-8 adapter on attention `q_proj`/`v_proj`
with JSON-completion-only loss, seed 42, gradient checkpointing and two epochs. User/system prompt
tokens are masked out. Oversized examples fail before training; labels are never silently truncated.
The best checkpoint is selected using validation loss. No dataset is pushed to a model hub, and
external experiment reporting is disabled. Loss is a pipeline metric, not an accuracy claim.

## Import and evaluate the result

Merge on a computer with enough RAM; this step loads the full base weights in float32:

```powershell
.\.cache\model-training\venv\Scripts\python.exe -m scripts.train_local_model merge --experiment .cache/model-training/experiment --output .cache/model-training/merged
```

Import the merged Safetensors model under a **new tag**, as described in the
[Ollama import guide](https://docs.ollama.com/import):

```powershell
# Use your installed Ollama executable; the portable executable is under .cache/local-ai/runtime.
ollama create shortlist-trained:experiment1 --quantize q4_K_M -f .cache/model-training/merged/Modelfile
```

If your Ollama build cannot import that architecture, convert the merged weights to GGUF using
[llama.cpp](https://github.com/ggml-org/llama.cpp), quantize there, and use `FROM /path/to/model.gguf`.
This path imports complete merged weights instead of assuming that Ollama accepts a Qwen3 LoRA
adapter directly. Verify the imported model's template and run the normal extraction smoke check.

Compare both tags on the **same held-out dataset**:

```powershell
.\.venv\Scripts\python.exe -m scripts.train_local_model evaluate --dataset .cache/model-training/prepared --model shortlist-local:v1 --output .cache/model-training/baseline-test.json
.\.venv\Scripts\python.exe -m scripts.train_local_model evaluate --dataset .cache/model-training/prepared --model shortlist-trained:experiment1 --output .cache/model-training/trained-test.json
```

Reports contain aggregated schema validity, exact-label rate, verified skill precision/recall,
exact skill quote rate, dropped claims, failures, the model digest and dataset hash. They omit
resume text. Failures remain in the denominator and missed gold skills count against recall.
Evaluating 10 examples with the chat model can take many minutes on this laptop. A fine-tuned model
must also pass the project's golden evaluation and representative human-reviewed tests; an
improvement on these repetitive synthetic templates alone is insufficient to select a production model.

After validating an actual trained model, select its new tag explicitly:

```ini
LLM_PROVIDER=ollama
OLLAMA_CHAT_MODEL=shortlist-trained:experiment1
EXTRACTION_MODE=llm
```

Restart the API and worker to pick up the configuration and create a new screening run. Existing
runs stay pinned to their original model digests; changing a tag cannot silently alter a retry.

## Replace examples with reviewed data

Use one JSON object per line:

```json
{"id":"resume-opaque-001","group_id":"person-opaque-001","kind":"resume","data_usage":"approved","document":"[NAME]\nSkills: Python","expected":{"skills":[{"name":"Python","evidence":"Skills: Python"}],"jobs":[],"projects":[],"education":[],"certifications":[],"location":null,"notice_period_days":null,"summary":null}}
```

`kind` is `resume` or `jd`. `expected` must match `CandidateExtraction` or
`ExtractedJobRequirements` exactly. Do not supply hiring decisions or scores as labels. Correct
the full professional extraction manually, including role dates, nulls and verbatim quotes. Skill
quotes must be complete source lines, matching the local decoder's evidence constraints.

Use at least 10 groups. Give every document/version for the same person the same opaque `group_id`;
use shared groups for versions of the same job posting. Identical source text with different groups
or labels is rejected. Use varied roles, file layouts, lengths, scans and missing-information cases
in the real evaluation set; more repetitions of one template do not establish generalization.

Keep real data and generated weights under ignored `.cache/model-training/` or an access-controlled
external directory. Record permission to use each resume for model improvement before marking it
`approved`; this marker is a dataset attestation, not an automatic consent lookup. Remove candidate
names, contact details and unrelated personal/protected attributes before annotation. Preparation
rejects obvious contacts and unsupported extraction fields, but requires human review for complete
de-identification. It reads explicit files and does not automatically export the production database.

Keep the original baseline and separate validation/test sets. Review errors and evidence failures
before changing the application model. Retention and erasure plans must cover the training corpus
and trained artifacts as well as the application's records.
