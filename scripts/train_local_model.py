"""Prepare, fine-tune, evaluate and export a local extraction model. Run with python -m scripts.train_local_model."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from training.data import BASE_MODEL, load_examples, prepare, synthetic_examples, write_jsonl  # noqa: E402


def checked_dataset(path: Path) -> tuple[dict, dict]:
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    from engine.extraction import VERIFIER_VERSION
    from engine.local_ai import LOCAL_PROMPT_VERSION
    from engine.prompts import get_prompt

    versions = {
        "local_prompt_version": LOCAL_PROMPT_VERSION,
        "resume_prompt_version": get_prompt("resume_extraction").version,
        "jd_prompt_version": get_prompt("jd_extraction").version,
        "verifier_version": VERIFIER_VERSION,
    }
    if any(manifest.get(key) != value for key, value in versions.items()):
        raise ValueError("Extraction instructions changed; prepare a new dataset")
    splits = {}
    seen_groups: set[str] = set()
    seen_documents: set[str] = set()
    for name in ("train", "validation", "test"):
        file = path / f"{name}.jsonl"
        if hashlib.sha256(file.read_bytes()).hexdigest() != manifest["split_sha256"][name]:
            raise ValueError("Prepared dataset changed; prepare a new experiment")
        rows = load_examples(file)
        groups = {row.group_id for row in rows}
        documents = {hashlib.sha256(row.document.encode()).hexdigest() for row in rows}
        if seen_groups & groups or seen_documents & documents:
            raise ValueError("A group or document appears in multiple splits")
        seen_groups.update(groups)
        seen_documents.update(documents)
        splits[name] = rows
    return manifest, splits


def tokenized_example(example, tokenizer, max_length: int) -> dict:
    # Non-thinking format matches the production Ollama provider. Only JSON completion receives loss.
    messages = example.messages()
    prefix = tokenizer.apply_chat_template(
        messages[:2], tokenize=False, add_generation_prompt=True, enable_thinking=False
    )
    prompt_ids = tokenizer(prefix, add_special_tokens=False)["input_ids"]
    answer_ids = tokenizer(messages[2]["content"], add_special_tokens=False)["input_ids"] + [tokenizer.eos_token_id]
    if len(prompt_ids) + len(answer_ids) > max_length:
        raise ValueError(
            "Training example exceeds max-length; raise the limit or shorten it. No examples were truncated."
        )
    return {
        "input_ids": prompt_ids + answer_ids,
        "attention_mask": [1] * (len(prompt_ids) + len(answer_ids)),
        "labels": [-100] * len(prompt_ids) + answer_ids,
    }


def training_dependencies():
    # Training is optional and never imports torch in API/worker processes or dataset preparation.
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    try:
        import torch
        from peft import LoraConfig, get_peft_model
        from transformers import (
            AutoConfig,
            AutoModelForCausalLM,
            AutoTokenizer,
            DataCollatorForSeq2Seq,
            Trainer,
            TrainingArguments,
        )
    except ImportError:
        raise ValueError("Install requirements-training.txt in a separate training virtual environment") from None
    return (
        torch,
        LoraConfig,
        get_peft_model,
        AutoConfig,
        AutoModelForCausalLM,
        AutoTokenizer,
        DataCollatorForSeq2Seq,
        Trainer,
        TrainingArguments,
    )


def train(args) -> dict:
    manifest, splits = checked_dataset(args.dataset)
    plan = {
        "base_model": BASE_MODEL,
        "requested_revision": args.revision,
        "counts": {key: len(value) for key, value in splits.items()},
        "synthetic_only": manifest["synthetic_only"],
        "method": "LoRA",
        "loss": "JSON completion only",
        "max_length": args.max_length,
        "epochs": args.epochs,
        "paid_api_calls": 0,
    }
    if args.dry_run:
        return {**plan, "status": "prepared_only", "weights_trained": False}
    torch, LoraConfig, get_peft_model, AutoConfig, AutoModel, AutoTokenizer, Collator, Trainer, Arguments = (
        training_dependencies()
    )
    torch.manual_seed(42)
    if not torch.cuda.is_available() and not args.allow_cpu:
        raise ValueError(
            "Training requires an NVIDIA CUDA GPU by default. Use --dry-run here, "
            "or --allow-cpu on a machine with enough RAM."
        )
    if args.output.exists():
        raise ValueError("Training output already exists; choose a new experiment directory")
    # Resolve main once, then load model and tokenizer from the identical immutable commit.
    config = AutoConfig.from_pretrained(BASE_MODEL, revision=args.revision, trust_remote_code=False)
    revision = config._commit_hash
    tokenizer = AutoTokenizer.from_pretrained(BASE_MODEL, revision=revision, trust_remote_code=False)
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
    tokenized = {
        name: [tokenized_example(row, tokenizer, args.max_length) for row in splits[name]]
        for name in ("train", "validation")
    }
    gpu = torch.cuda.is_available()
    bf16 = gpu and torch.cuda.is_bf16_supported()
    dtype = torch.bfloat16 if bf16 else torch.float16 if gpu else torch.float32
    model = AutoModel.from_pretrained(BASE_MODEL, revision=revision, torch_dtype=dtype, trust_remote_code=False)
    model.config.use_cache = False
    model = get_peft_model(
        model,
        LoraConfig(
            task_type="CAUSAL_LM",
            r=8,
            lora_alpha=16,
            lora_dropout=0.05,
            target_modules=["q_proj", "v_proj"],
            bias="none",
        ),
    )
    model.enable_input_require_grads()
    args.output.mkdir(parents=True, exist_ok=False)
    run = {**plan, "base_revision": revision, "dataset_manifest": manifest, "status": "started", "seed": 42}
    (args.output / "training-run.json").write_text(json.dumps(run, indent=2) + "\n", encoding="utf-8")
    trainer = Trainer(
        model=model,
        args=Arguments(
            output_dir=str(args.output),
            per_device_train_batch_size=1,
            per_device_eval_batch_size=1,
            gradient_accumulation_steps=8,
            gradient_checkpointing=True,
            gradient_checkpointing_kwargs={"use_reentrant": False},
            num_train_epochs=args.epochs,
            learning_rate=1e-4,
            bf16=bf16,
            fp16=gpu and not bf16,
            eval_strategy="epoch",
            save_strategy="epoch",
            save_total_limit=2,
            load_best_model_at_end=True,
            metric_for_best_model="eval_loss",
            greater_is_better=False,
            report_to="none",
            push_to_hub=False,
            seed=42,
            data_seed=42,
            logging_steps=10,
            dataloader_num_workers=0,
            use_cpu=not gpu,
            optim="adamw_torch",
        ),
        train_dataset=tokenized["train"],
        eval_dataset=tokenized["validation"],
        data_collator=Collator(tokenizer=tokenizer, padding=True, label_pad_token_id=-100),
    )
    trainer.train()
    trainer.save_model(str(args.output / "adapter"))
    tokenizer.save_pretrained(args.output / "adapter")
    metrics = trainer.evaluate()
    run.update(status="trained", metrics=metrics)
    (args.output / "training-run.json").write_text(json.dumps(run, indent=2) + "\n", encoding="utf-8")
    return {
        "status": "trained",
        "adapter": str(args.output / "adapter"),
        "synthetic_only": manifest["synthetic_only"],
        "metrics": metrics,
    }


def evaluate(args) -> dict:
    from engine.config import Settings
    from engine.extraction import verify_candidate, verify_job_requirements
    from engine.llm import LLMError
    from engine.local_ai import OllamaProvider
    from engine.prompts import fence_untrusted, get_prompt
    from engine.schemas import CandidateExtraction

    manifest, splits = checked_dataset(args.dataset)
    if args.output.exists():
        raise ValueError("Evaluation output exists; choose a new report path")
    settings = Settings(_env_file=None, LLM_PROVIDER="ollama", OLLAMA_CHAT_MODEL=args.model, LLM_TIMEOUT_S=args.timeout)
    provider = OllamaProvider(settings)
    digest = provider.model_digest(args.model)
    settings.ollama_chat_digest = digest
    counts = {
        "examples": 0,
        "schema_valid": 0,
        "exact_labels": 0,
        "predicted_skills": 0,
        "gold_skills": 0,
        "matched_skills": 0,
        "skill_quotes": 0,
        "grounded_skill_quotes": 0,
        "dropped_items": 0,
        "failures": 0,
    }
    for row in splits[args.split]:
        counts["examples"] += 1
        label = row.label()
        names = (
            (lambda data: {skill.name.casefold() for skill in data.skills})
            if row.kind == "resume"
            else (
                lambda data: {
                    (key, skill.name.casefold(), tuple(a.casefold() for a in skill.alternatives))
                    for key in ("required_skills", "preferred_skills")
                    for skill in getattr(data, key)
                }
            )
        )
        gold = names(label)
        counts["gold_skills"] += len(gold)
        prompt = get_prompt("resume_extraction" if row.kind == "resume" else "jd_extraction")
        try:
            raw = provider.parse(
                system=prompt.system,
                user=prompt.render_user(document=fence_untrusted(row.document)),
                schema=type(label),
            ).parsed
        except LLMError:
            counts["failures"] += 1
            continue
        counts["schema_valid"] += 1
        counts["exact_labels"] += raw.model_dump() == label.model_dump()
        skills = raw.skills if isinstance(raw, CandidateExtraction) else [*raw.required_skills, *raw.preferred_skills]
        counts["skill_quotes"] += len(skills)
        counts["grounded_skill_quotes"] += sum(bool(s.evidence) and s.evidence in row.document for s in skills)
        verified, verification = (
            verify_candidate(raw, row.document) if row.kind == "resume" else verify_job_requirements(raw, row.document)
        )
        predicted = names(verified)
        counts["predicted_skills"] += len(predicted)
        counts["matched_skills"] += len(predicted & gold)
        counts["dropped_items"] += len(verification.dropped)
    total = counts["examples"]
    report = {
        "model": args.model,
        "model_digest": digest,
        "split": args.split,
        "dataset_sha256": manifest["split_sha256"][args.split],
        "synthetic_only": manifest["synthetic_only"],
        "counts": counts,
        "schema_valid_rate": counts["schema_valid"] / total,
        "exact_label_rate": counts["exact_labels"] / total,
        "skill_precision": counts["matched_skills"] / counts["predicted_skills"]
        if counts["predicted_skills"]
        else None,
        "skill_recall": counts["matched_skills"] / counts["gold_skills"] if counts["gold_skills"] else None,
        "exact_skill_quote_rate": counts["grounded_skill_quotes"] / counts["skill_quotes"]
        if counts["skill_quotes"]
        else None,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def merge(args) -> dict:
    run = json.loads((args.experiment / "training-run.json").read_text(encoding="utf-8"))
    if run["status"] != "trained":
        raise ValueError("This experiment has no completed training checkpoint")
    if args.output.exists():
        raise ValueError("Merged output exists; choose a new directory")
    torch, _, _, _, AutoModel, AutoTokenizer, *_ = training_dependencies()
    from peft import PeftModel

    model = AutoModel.from_pretrained(
        run["base_model"], revision=run["base_revision"], torch_dtype=torch.float32, trust_remote_code=False
    )
    model = PeftModel.from_pretrained(model, args.experiment / "adapter").merge_and_unload()
    model.save_pretrained(args.output, safe_serialization=True, max_shard_size="2GB")
    AutoTokenizer.from_pretrained(args.experiment / "adapter", trust_remote_code=False).save_pretrained(args.output)
    # Ollama infers the Qwen3 architecture/template from the merged model's config and tokenizer.
    (args.output / "Modelfile").write_text(
        "FROM .\nPARAMETER temperature 0\nPARAMETER seed 42\nPARAMETER num_ctx 16384\nPARAMETER num_predict 2048\n",
        encoding="utf-8",
    )
    (args.output / "training-run.json").write_text(json.dumps(run, indent=2) + "\n", encoding="utf-8")
    return {"status": "merged", "output": str(args.output), "synthetic_only": run["synthetic_only"]}


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description=__doc__)
    commands = cli.add_subparsers(dest="command", required=True)
    sample = commands.add_parser("synthetic", help="Write synthetic examples without any AI API call")
    sample.add_argument("--output", type=Path, required=True)
    prep = commands.add_parser("prepare", help="Validate labels and split by person/document group")
    prep.add_argument("--input", type=Path, required=True)
    prep.add_argument("--output", type=Path, required=True)
    prep.add_argument("--seed", type=int, default=42)
    fit = commands.add_parser("train", help="Fine-tune Qwen3-1.7B with a LoRA adapter")
    fit.add_argument("--dataset", type=Path, required=True)
    fit.add_argument("--output", type=Path, required=True)
    fit.add_argument("--revision", default="main")
    fit.add_argument("--epochs", type=float, default=2)
    fit.add_argument("--max-length", type=int, default=4096)
    fit.add_argument("--dry-run", action="store_true")
    fit.add_argument("--allow-cpu", action="store_true")
    score = commands.add_parser("evaluate", help="Evaluate a local Ollama model against held-out labels")
    score.add_argument("--dataset", type=Path, required=True)
    score.add_argument("--model", required=True)
    score.add_argument("--split", choices=["validation", "test"], default="test")
    score.add_argument("--timeout", type=float, default=600)
    score.add_argument("--output", type=Path, required=True)
    export = commands.add_parser("merge", help="Merge adapter and pinned base weights for Ollama import")
    export.add_argument("--experiment", type=Path, required=True)
    export.add_argument("--output", type=Path, required=True)
    return cli


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "synthetic":
            if args.output.exists():
                raise ValueError("Synthetic output exists; choose a new path")
            args.output.parent.mkdir(parents=True, exist_ok=True)
            rows = synthetic_examples()
            write_jsonl(args.output, [row.model_dump() for row in rows])
            result = {"examples": len(rows), "synthetic_only": True, "weights_trained": False}
        elif args.command == "prepare":
            result = prepare(args.input, args.output, args.seed)
        elif args.command == "train":
            if args.epochs <= 0 or args.max_length < 128:
                raise ValueError("Epochs must be positive and max-length must be at least 128")
            result = train(args)
        elif args.command == "evaluate":
            result = evaluate(args)
        else:
            result = merge(args)
        print(json.dumps(result, indent=2))
        return 0
    except (ValueError, OSError, KeyError) as exc:
        # OSError filenames can contain identifying data; avoid printing them.
        message = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        print(f"Training setup failed: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
