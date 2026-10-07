from __future__ import annotations

import json
from argparse import Namespace

import pytest

from engine.llm import LLMError, LLMResult
from engine.local_ai import OllamaProvider, structured_system_prompt
from engine.prompts import DOC_CLOSE, DOC_OPEN, get_prompt
from engine.schemas import CandidateExtraction, LLMUsage
from scripts.train_local_model import checked_dataset, evaluate, tokenized_example, train
from training.data import load_examples, prepare, split_examples, synthetic_examples, validate_example, write_jsonl


@pytest.fixture
def prepared(tmp_path):
    source = tmp_path / "synthetic.jsonl"
    write_jsonl(source, [row.model_dump() for row in synthetic_examples()])
    output = tmp_path / "prepared"
    prepare(source, output)
    return output


def test_synthetic_labels_pass_real_evidence_verifier():
    examples = synthetic_examples()
    assert len(examples) == 120
    assert {example.kind for example in examples} == {"resume", "jd"}
    for example in examples:
        validate_example(example)


def test_resume_variants_never_leak_into_another_split():
    splits = split_examples(synthetic_examples())
    memberships = [{row.group_id for row in split} for split in splits.values()]
    assert all(memberships)
    assert not memberships[0] & memberships[1]
    assert not memberships[0] & memberships[2]
    assert not memberships[1] & memberships[2]
    assert sum(map(len, splits.values())) == 120
    assert splits == split_examples(synthetic_examples())


@pytest.mark.parametrize(
    "mutation", ["fabricated_quote", "incorrect_notice", "contact", "reserved_fence", "extra_field"]
)
def test_bad_training_rows_are_rejected_without_printing_source(tmp_path, mutation):
    data = synthetic_examples()[0].model_dump()
    secret = "PRIVATE-CANDIDATE-MARKER"
    data["document"] += "\n" + secret
    if mutation == "fabricated_quote":
        data["expected"]["skills"][0]["evidence"] = "Skills: Invented Python"
    elif mutation == "incorrect_notice":
        data["expected"]["notice_period_days"]["days"] = 999
    elif mutation == "contact":
        data["document"] += "\nprivate@example.com"
    elif mutation == "reserved_fence":
        data["document"] += DOC_CLOSE
    else:
        data["expected"]["gender"] = secret
    source = tmp_path / "invalid.jsonl"
    write_jsonl(source, [data])
    with pytest.raises(ValueError, match="Invalid training row 1") as error:
        load_examples(source)
    assert secret not in str(error.value)
    assert "private@example.com" not in str(error.value)


def test_identical_documents_with_different_groups_are_rejected(tmp_path):
    first = synthetic_examples()[0].model_dump()
    second = {**first, "id": "second", "group_id": "different-person"}
    source = tmp_path / "duplicates.jsonl"
    write_jsonl(source, [first, second])
    with pytest.raises(ValueError, match="Identical documents must share a group"):
        load_examples(source)


def test_prepared_data_is_hashed_and_existing_experiments_are_preserved(prepared, tmp_path):
    manifest, splits = checked_dataset(prepared)
    assert manifest["synthetic_only"] is True
    assert sum(manifest["counts"].values()) == 120
    assert all(splits.values())
    with pytest.raises(FileExistsError):
        prepare(tmp_path / "synthetic.jsonl", prepared)
    train_file = prepared / "train.jsonl"
    train_file.write_text(train_file.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Prepared dataset changed"):
        checked_dataset(prepared)


def test_prompt_changes_require_preparing_again(prepared):
    manifest_file = prepared / "manifest.json"
    manifest = json.loads(manifest_file.read_text())
    manifest["local_prompt_version"] = "older-prompt"
    manifest_file.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="Extraction instructions changed"):
        checked_dataset(prepared)


class FakeTokenizer:
    eos_token_id = 255

    def apply_chat_template(self, messages, **kwargs):
        assert kwargs["enable_thinking"] is False
        return "\n".join(message["content"] for message in messages) + "\nassistant:\n"

    def __call__(self, text, **kwargs):
        assert kwargs["add_special_tokens"] is False
        return {"input_ids": list(text.encode())}


def test_training_masks_all_prompt_tokens_and_preserves_entire_answer():
    example = synthetic_examples()[0]
    tokenizer = FakeTokenizer()
    result = tokenized_example(example, tokenizer, max_length=20000)
    answer = [*example.label().model_dump_json().encode(), tokenizer.eos_token_id]
    assert result["labels"][-len(answer) :] == answer
    assert set(result["labels"][: -len(answer)]) == {-100}
    assert len(result["input_ids"]) == len(result["labels"]) == len(result["attention_mask"])
    with pytest.raises(ValueError, match="No examples were truncated"):
        tokenized_example(example, tokenizer, max_length=128)


def test_training_and_inference_use_identical_system_instructions():
    row = synthetic_examples()[0]
    assert row.messages()[0]["content"] == structured_system_prompt(
        get_prompt("resume_extraction").system, CandidateExtraction
    )
    assert row.document not in row.messages()[0]["content"]
    assert row.document in row.messages()[1]["content"]


def test_dry_run_does_not_download_weights_or_import_training_dependencies(prepared, tmp_path, monkeypatch):
    def forbidden():
        pytest.fail("Dry run must not load training packages or download models")

    monkeypatch.setattr("scripts.train_local_model.training_dependencies", forbidden)
    output = tmp_path / "weights"
    result = train(Namespace(dataset=prepared, revision="main", epochs=2, max_length=4096, output=output, dry_run=True))
    assert result["weights_trained"] is False
    assert result["synthetic_only"] is True
    assert result["paid_api_calls"] == 0
    assert not output.exists()


def test_heldout_metrics_include_failed_documents_and_store_no_resume_text(prepared, tmp_path, monkeypatch):
    _, splits = checked_dataset(prepared)
    lookup = {row.document: row for row in splits["test"]}
    failures = 0

    def predict(_self, *, user, **_kwargs):
        nonlocal failures
        document = user.split(DOC_OPEN, 1)[1].rsplit(DOC_CLOSE, 1)[0].strip()
        if not failures:
            failures += 1
            raise LLMError("Synthetic failure")
        return LLMResult(parsed=lookup[document].label(), usage=LLMUsage(provider="ollama", model="test"))

    monkeypatch.setattr(OllamaProvider, "model_digest", lambda *_args: "test-digest")
    monkeypatch.setattr(OllamaProvider, "parse", predict)
    output = tmp_path / "report.json"
    result = evaluate(Namespace(dataset=prepared, split="test", model="test", timeout=30, output=output))
    assert result["counts"]["examples"] == len(splits["test"])
    assert result["counts"]["failures"] == 1
    assert result["schema_valid_rate"] < 1
    assert result["exact_label_rate"] < 1
    assert result["model_digest"] == "test-digest"
    assert result["synthetic_only"] is True
    report = output.read_text()
    assert all(row.document not in report for row in splits["test"])
