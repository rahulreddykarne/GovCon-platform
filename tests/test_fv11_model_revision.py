"""Embedding identity retains explicit/local behavior and resolves cached aliases."""
from types import SimpleNamespace
from uuid import uuid4

import pytest

from govcon.enrich.embeddings import SentenceTransformerProvider, _fresh, _refresh
from govcon.models import Opportunity


class Model(list):
    def __init__(self, commit=None):
        super().__init__([SimpleNamespace(auto_model=SimpleNamespace(config=SimpleNamespace(_commit_hash=commit)))])
        self.calls = []

    def encode(self, text, normalize_embeddings=True):
        self.calls.append(text)
        return SimpleNamespace(tolist=lambda: [0.2] * 384)


def provider(name, commit=None, revision=None):
    value = SentenceTransformerProvider(name, revision=revision)
    value._model = Model(commit)
    return value


@pytest.mark.parametrize("commit,revision,expected", [("resolved", None, "resolved"),
                                                     (None, "pinned", "pinned"),
                                                     ("resolved", "pinned", "resolved")])
def test_current_explicit_identity_priority(commit, revision, expected):
    assert provider("identity-test", commit, revision).model_version == f"identity-test@{expected}"


def test_current_local_model_content_hash_changes_with_weights(tmp_path):
    (tmp_path / "config.json").write_text("{}")
    weights = tmp_path / "model.safetensors"
    weights.write_bytes(b"first weights")
    first = provider(str(tmp_path)).model_version
    weights.write_bytes(b"second weights")
    second = provider(str(tmp_path)).model_version
    assert first != second and not first.endswith("unversioned")


def test_current_absent_cache_fallback_is_preserved():
    name = f"absent-cache-{uuid4().hex}"
    assert provider(name).model_version == f"{name}@unversioned"


@pytest.mark.parametrize("name", ["all-MiniLM-L6-v2", "sentence-transformers/all-MiniLM-L6-v2"])
def test_cached_alias_uses_the_resolved_snapshot_commit(tmp_path, monkeypatch, name):
    commit = "a" * 40
    calls = []

    def cached(repo_id, filename, **kwargs):
        calls.append((repo_id, filename))
        return str(tmp_path / "snapshots" / commit / "config.json")

    monkeypatch.setattr("huggingface_hub.try_to_load_from_cache", cached)
    value = provider(name)
    assert value.model_version == f"{name}@{commit}"
    assert value.model_version == f"{name}@{commit}"
    assert calls == [("sentence-transformers/all-MiniLM-L6-v2", "config.json")]


def test_cached_alias_revision_change_rebuilds_the_existing_vector(tmp_path, monkeypatch):
    commit = ["a" * 40]
    monkeypatch.setattr("huggingface_hub.try_to_load_from_cache",
                        lambda *args, **kwargs: str(tmp_path / "snapshots" / commit[0] / "config.json"))
    opp = Opportunity(title="Medical gloves", description="Nitrile gloves", raw={})
    first = provider("all-MiniLM-L6-v2")
    _refresh(opp, first)
    assert _fresh(opp, first, "Medical gloves Nitrile gloves")
    commit[0] = "b" * 40
    second = provider("all-MiniLM-L6-v2")
    assert not _fresh(opp, second, "Medical gloves Nitrile gloves"), "the changed model alias still appears fresh"
    _refresh(opp, second)
    assert len(second._model.calls) == 1 and opp.embedding_model.endswith(commit[0])
