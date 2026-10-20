"""Decision logic: identical rule, model path, and the safe rule fallback."""

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
