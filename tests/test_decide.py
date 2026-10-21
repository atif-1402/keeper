"""Decision logic: identical rule, model path, and the safe rule fallback."""

import json
import urllib.error

from keeper.decide import decide
from keeper.extract import extract_text
from keeper.scan import scan


def _facts(root):
    facts = scan(root)
    for f in facts:
        f.text, f.status = extract_text(f.path)
    return facts


def _cluster_named(root, needle):
    for c in cluster_of(root):
        if any(needle in f.name for f in c):
            return c
    raise AssertionError(f"no cluster containing {needle!r}")


def cluster_of(root):
    from keeper.group import cluster
    return cluster(_facts(root))


def test_identical_group_never_calls_the_model(messy_dir):
    group = _cluster_named(messy_dir, "passport")
    dec = decide(group, use_model=False)
    assert dec.method == "identical"
    assert dec.confidence == 1.0
    assert dec.keep.sha256 == group[0].sha256


def test_rule_fallback_prefers_readable_and_newest(messy_dir):
    group = _cluster_named(messy_dir, "rental")
    dec = decide(group, use_model=False)
    assert dec.method == "rule"
    # The signed, newest copy should win under the fallback rule.
    assert dec.keep.name == "rental_agreement_final_v2(1).txt"


def test_rule_fallback_handles_unreadable_files(messy_dir):
    # Strip text from the newest copy: fallback must prefer a readable one.
    group = _cluster_named(messy_dir, "rental")
    newest = max(group, key=lambda f: f.mtime)
    newest.text, newest.status = "", "no_text_layer"
    dec = decide(group, use_model=False)
    assert dec.method == "rule"
    assert dec.keep.readable


def test_model_verdict_when_ollama_is_up(messy_dir):
    from keeper.decide import model_available

    group = _cluster_named(messy_dir, "rental")
    dec = decide(group, model="gemma3:1b")
    if not model_available():
        # Offline in CI: must degrade to the rule path, never crash.
        assert dec.method == "rule"
        return
    # Method may be "model" (verdict accepted) or "rule" (model flipped and
    # Keeper's sanity-check overrode it) — never anything else, never a crash.
    assert dec.method in {"model", "rule"}
    assert dec.keep in group
    assert dec.reason
    assert 0.0 <= dec.confidence <= 1.0


def test_model_picks_sane_answer_on_signed_copy(messy_dir):
    from keeper.decide import model_available

    if not model_available():
        import pytest
        pytest.skip("Ollama not running")
    group = _cluster_named(messy_dir, "rental")
    dec = decide(group, model="gemma3:1b")
    # The signed, newest version is objectively the keeper.
    assert dec.keep.name == "rental_agreement_final_v2(1).txt"


def test_model_is_skipped_when_nothing_is_readable(tmp_path, monkeypatch):
    """Zips/images/videos have no text: the model must never be consulted."""
    from keeper.group import cluster

    (tmp_path / "app-data.zip").write_bytes(b"PK\x03\x04 payload one")
    (tmp_path / "app-data (2).zip").write_bytes(b"PK\x03\x04 payload two")

    def boom(*_args, **_kwargs):
        raise AssertionError("_ask_model must not run for unreadable clusters")

    monkeypatch.setattr("keeper.decide._ask_model", boom)
    groups = cluster(_facts(tmp_path))
    assert len(groups) == 1
    dec = decide(groups[0], use_model=True)
    assert dec.method == "rule"
    assert "no readable text" in dec.reason


# --- schema-enforced verdicts -------------------------------------------------

class _FakeResponse:
    def __init__(self, data):
        self._data = json.dumps(data).encode()

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _answer(keep, reason="picks this one", confidence=0.9):
    return {"message": {"content": json.dumps(
        {"keep": keep, "reason": reason, "confidence": confidence}
    )}}


def test_payload_carries_verdict_schema(messy_dir, monkeypatch):
    from keeper import decide as decide_mod

    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["payload"] = json.loads(request.data)
        group_names = _cluster_named(messy_dir, "rental")
        newest = max(group_names, key=lambda f: f.mtime)
        return _FakeResponse(_answer(newest.name))

    monkeypatch.setattr("keeper.decide.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("keeper.decide.model_available", lambda *a, **k: True)
    dec = decide(_cluster_named(messy_dir, "rental"))
    assert captured["payload"]["format"] == decide_mod.VERDICT_SCHEMA
    assert captured["payload"]["format"]["required"] == ["keep", "reason", "confidence"]
    assert dec.method == "model"


def test_old_ollama_schema_rejection_retries_with_loose_json(messy_dir, monkeypatch):
    """Ollama < 0.5 answers schema objects with a 400; Keeper must retry once
    with the string form instead of giving up."""
    from keeper.decide import VERDICT_SCHEMA

    calls = []

    def fake_urlopen(request, timeout=None):
        payload = json.loads(request.data)
        calls.append(payload["format"])
        if isinstance(payload["format"], dict):
            raise urllib.error.HTTPError(
                "http://localhost:11434/api/chat", 400, "Bad Request", None, None
            )
        group = _cluster_named(messy_dir, "rental")
        newest = max(group, key=lambda f: f.mtime)
        return _FakeResponse(_answer(newest.name))

    monkeypatch.setattr("keeper.decide.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("keeper.decide.model_available", lambda *a, **k: True)
    dec = decide(_cluster_named(messy_dir, "rental"))
    assert calls == [VERDICT_SCHEMA, "json"]
    assert dec.method == "model"


def test_reordered_json_keys_still_parse(messy_dir, monkeypatch):
    """The Ollama chat path may alphabetically re-sort schema keys — parsing
    is by key lookup, so any order works."""
    group = _cluster_named(messy_dir, "rental")
    newest = max(group, key=lambda f: f.mtime)
    # Alphabetical order: confidence, keep, reason — not our declared order.
    content = ('{"confidence": 0.91, "keep": "%s", '
               '"reason": "newest signed copy"}' % newest.name)
    monkeypatch.setattr("keeper.decide.model_available", lambda *a, **k: True)
    monkeypatch.setattr(
        "keeper.decide._ask_model",
        lambda *a, **k: json.loads(content),
    )
    dec = decide(group)
    assert dec.method == "model"
    assert dec.keep is newest


# --- confidence escalation ----------------------------------------------------

def _low_then_high(keep_name):
    answers = [
        {"keep": keep_name, "reason": "unsure maybe", "confidence": 0.35},
        {"keep": keep_name, "reason": "checked again, this one", "confidence": 0.85},
    ]
    return answers


def test_low_confidence_triggers_second_look(messy_dir, monkeypatch):
    group = _cluster_named(messy_dir, "rental")
    newest = max(group, key=lambda f: f.mtime)
    calls = []
    answers = _low_then_high(newest.name)

    def fake_ask(_group, _model, *_a, **kw):
        calls.append(kw)
        return answers[len(calls) - 1]

    monkeypatch.setattr("keeper.decide.model_available", lambda *a, **k: True)
    monkeypatch.setattr("keeper.decide._ask_model", fake_ask)
    dec = decide(group)
    assert len(calls) == 2
    assert calls[1].get("revisit") is True, "second look must be the rich one"
    assert dec.method == "model"
    assert dec.escalated is True
    assert dec.confidence >= 0.6


def test_two_low_passes_fall_back_to_rules(messy_dir, monkeypatch):
    group = _cluster_named(messy_dir, "rental")
    newest = max(group, key=lambda f: f.mtime)

    def fake_ask(*_a, **_k):
        return {"keep": newest.name, "reason": "hmm", "confidence": 0.3}

    monkeypatch.setattr("keeper.decide.model_available", lambda *a, **k: True)
    monkeypatch.setattr("keeper.decide._ask_model", fake_ask)
    dec = decide(group)
    assert dec.method == "rule"
    assert "unsure after two looks" in dec.reason
    assert dec.escalated is True
    assert dec.keep.readable  # rules still pick the sensible keeper


def test_confident_answer_never_escalates(messy_dir, monkeypatch):
    group = _cluster_named(messy_dir, "rental")
    newest = max(group, key=lambda f: f.mtime)
    calls = []

    def fake_ask(*_a, **_k):
        calls.append(1)
        return {"keep": newest.name, "reason": "clear", "confidence": 0.9}

    monkeypatch.setattr("keeper.decide.model_available", lambda *a, **k: True)
    monkeypatch.setattr("keeper.decide._ask_model", fake_ask)
    dec = decide(group)
    assert len(calls) == 1
    assert dec.escalated is False
    assert dec.method == "model"
