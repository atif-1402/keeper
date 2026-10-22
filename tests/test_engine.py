"""Engine runner: the shared pipeline both the CLI and GUI sit on."""

from __future__ import annotations

from pathlib import Path

from keeper.engine import MODE_DEFAULT, MODE_MODEL, MODE_RULES, run_pipeline
from keeper.quarantine import QDIR


def test_runs_and_finds_the_fixture_clusters(messy_dir):
    result = run_pipeline(messy_dir, mode=MODE_RULES)
    assert result.files == 10
    assert result.clusters >= 3
    assert result.losers >= 4
    assert result.clutter_bytes > 0
    assert len(result.decisions) == result.clusters


def test_dry_run_never_touches_disk(messy_dir):
    before = sorted(p.name for p in messy_dir.iterdir())
    run_pipeline(messy_dir, mode=MODE_RULES)
    after = sorted(p.name for p in messy_dir.iterdir())
    assert before == after
    assert not (messy_dir / QDIR).exists()


def test_every_decision_has_a_keeper_and_losers(messy_dir):
    result = run_pipeline(messy_dir, mode=MODE_RULES)
    for gd in result.decisions:
        assert gd.decision.keep is not None
        assert gd.decision.reason
        # The keeper is never one of its own losers.
        assert gd.decision.keep.path not in [f.path for f in gd.losers]


def test_rules_mode_never_reports_model_asking(messy_dir):
    result = run_pipeline(messy_dir, mode=MODE_RULES)
    assert result.use_model is False
    assert result.asked == 0


def test_progress_callback_reports_phases(messy_dir):
    seen: list[tuple[str, int, int]] = []

    def progress(text, done, total):
        seen.append((text, done, total))

    run_pipeline(messy_dir, mode=MODE_RULES, progress=progress)
    phases = {t for t, _d, _t in seen}
    assert any("Hashing" in p for p in phases)
    assert any("Extracting" in p for p in phases)
    assert any("Clustering" in p for p in phases)


def test_model_mode_without_ollama_flags_but_still_decides(messy_dir, monkeypatch):
    # Force model_available() -> False: model mode must flag the failure but
    # still return rule decisions rather than crash.
    import keeper.engine as engine
    monkeypatch.setattr(engine, "model_available", lambda *a, **k: False)
    result = run_pipeline(messy_dir, mode=MODE_MODEL)
    assert result.model_error is not None
    assert "Ollama" in result.model_error or "ollama" in result.model_error
    # Decisions still produced via the rule engine.
    assert len(result.decisions) == result.clusters


def test_default_mode_without_ollama_falls_back_and_flags(messy_dir, monkeypatch):
    import keeper.engine as engine
    monkeypatch.setattr(engine, "model_available", lambda *a, **k: False)
    result = run_pipeline(messy_dir, mode=MODE_DEFAULT)
    assert result.model_error is not None
    assert result.use_model is False
    assert len(result.decisions) == result.clusters


def test_result_root_is_the_given_folder(messy_dir):
    result = run_pipeline(messy_dir, mode=MODE_RULES)
    assert result.root == Path(messy_dir)
