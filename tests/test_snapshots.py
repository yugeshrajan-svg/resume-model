from __future__ import annotations

import json

import pytest

from backend.api.schemas import ConsentIn
from backend.snapshots import SnapshotMismatch, engine_fingerprint, freeze_settings, restore_settings, validate_snapshot
from engine.config import Settings


def test_snapshot_omits_credentials_and_resolves_automatic_modes() -> None:
    settings = Settings(
        _env_file=None,
        AZURE_OPENAI_ENDPOINT="https://example.openai.azure.com/",
        AZURE_OPENAI_API_KEY="private-openai-secret",
        AZURE_DOCINTEL_KEY="private-ocr-secret",
        API_KEY="private-api-secret",
        EXTRACTION_MODE="llm",
        SEMANTIC_BACKEND="embeddings",
    )
    saved = freeze_settings(settings)
    assert "private-" not in json.dumps(saved)
    assert saved["extraction_mode"] == "llm" and saved["semantic_backend"] == "embeddings"
    assert saved["azure_openai_chat_deployment"] == settings.azure_openai_chat_deployment


def test_restore_uses_saved_values_over_worker_and_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    saved = freeze_settings(
        Settings(
            _env_file=None,
            EXTRACTION_MODE="rules",
            SEMANTIC_BACKEND="tfidf",
            SKILL_RECENCY_FULL_MONTHS=12,
            AZURE_OPENAI_CHAT_DEPLOYMENT="original-model",
        )
    )
    monkeypatch.setenv("SKILL_RECENCY_FULL_MONTHS", "50")
    worker = Settings(_env_file=None, AZURE_OPENAI_API_KEY="rotated-secret", AZURE_OPENAI_CHAT_DEPLOYMENT="new-model")
    restored = restore_settings(saved, worker)
    assert restored.skill_recency_full_months == 12
    assert restored.azure_openai_chat_deployment == "original-model"
    assert restored.azure_openai_api_key == "rotated-secret"
    assert restored.extraction_mode == "rules" and restored.semantic_backend == "tfidf"


@pytest.mark.parametrize("snapshot", [None, {"format": 0}, {"format": 1, "fingerprint": "old-worker"}])
def test_incompatible_or_legacy_snapshots_are_rejected(snapshot) -> None:
    with pytest.raises(SnapshotMismatch):
        validate_snapshot(snapshot)
    assert validate_snapshot({"format": 1, "fingerprint": engine_fingerprint()})["format"] == 1


@pytest.mark.parametrize("when", ["2099-01-01T00:00:00Z", "2020-01-01T00:00:00"])
def test_invalid_consent_dates_are_rejected(when: str) -> None:
    with pytest.raises(ValueError, match="granted_at"):
        ConsentIn(source="application form", purpose="recruitment", granted_at=when)
