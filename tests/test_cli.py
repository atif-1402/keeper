"""End-to-end CLI behaviour: dry-run default, apply, restore, exit codes."""

import json

from keeper.cli import main


def test_scan_without_path_fails_cleanly(capsys):
    assert main([]) == 2
    assert "give me a folder" in capsys.readouterr().err


def test_bad_path_fails_cleanly(tmp_path, capsys):
    assert main([str(tmp_path / "nope")]) == 2
    assert "is not a folder" in capsys.readouterr().err


def test_dry_run_moves_nothing(messy_dir, capsys):
    before = sorted(p.name for p in messy_dir.iterdir())
    assert main([str(messy_dir), "--no-model"]) == 0
    after = sorted(p.name for p in messy_dir.iterdir())
    assert before == after
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "KEEP" in out


def test_apply_then_restore(messy_dir, capsys):
    before = sorted(p.name for p in messy_dir.iterdir())
    assert main([str(messy_dir), "--apply", "--no-model"]) == 0
    assert "_keeper_quarantine" in sorted(p.name for p in messy_dir.iterdir())
    assert main([str(messy_dir), "--restore"]) == 0
    assert sorted(p.name for p in messy_dir.iterdir()) == before


def test_report_file_written(messy_dir, tmp_path, capsys):
    report = tmp_path / "report.md"
    assert main([str(messy_dir), "--report", str(report), "--no-model"]) == 0
    content = report.read_text()
    assert "# Keeper report" in content
    assert "KEEP" in content or "keeper" in content.lower()


def test_tidy_folder_is_fine(tmp_path, capsys):
    (tmp_path / "one.txt").write_text("nothing else here at all")
    assert main([str(tmp_path), "--no-model"]) == 0
    assert "No duplicate clusters" in capsys.readouterr().out


def test_no_model_and_model_only_conflict(messy_dir, capsys):
    assert main([str(messy_dir), "--no-model", "--model-only"]) == 2
    assert "contradict" in capsys.readouterr().err


def test_model_only_fails_cleanly_without_ollama(messy_dir, monkeypatch, capsys):
    monkeypatch.setattr("keeper.cli.model_available", lambda *_a, **_k: False)
    assert main([str(messy_dir), "--model-only"]) == 1
    assert "Ollama is not running" in capsys.readouterr().err
    # ...and nothing was moved.
    assert "_keeper_quarantine" not in [p.name for p in messy_dir.iterdir()]


def test_no_eta_flag_still_works(messy_dir):
    assert main([str(messy_dir), "--no-model", "--no-eta"]) == 0


def test_fmt_duration():
    from keeper.cli import _fmt_duration

    assert _fmt_duration(0) == "0s"
    assert _fmt_duration(9) == "9s"
    assert _fmt_duration(75) == "1m15s"
    assert _fmt_duration(3600) == "1h00m"
    assert _fmt_duration(3700) == "1h01m"


# --- --json machine-readable output -------------------------------------------

def test_json_output_is_pure_json(messy_dir, capsys):
    assert main([str(messy_dir), "--no-model", "--json"]) == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)  # stdout must be ONLY the JSON document
    assert data["keeper"]
    assert data["files"] > 0
    assert data["clusters"] >= 1
    assert data["clutter_bytes"] > 0
    assert data["applied"] is False
    first = data["decisions"][0]
    for key in ("cluster", "copies", "keep", "reason", "confidence",
                "method", "escalated", "losers", "loser_bytes"):
        assert key in first


def test_json_with_apply_reports_quarantine(messy_dir, capsys):
    assert main([str(messy_dir), "--apply", "--no-model", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["applied"] is True
    assert "_keeper_quarantine" in data["quarantine"]
    # ...and restore still works after a JSON apply.
    assert main([str(messy_dir), "--restore"]) == 0


def test_json_restore_reports_count(messy_dir, capsys):
    assert main([str(messy_dir), "--apply", "--no-model"]) == 0
    capsys.readouterr()
    assert main([str(messy_dir), "--restore", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["restored"] > 0
    assert data["error"] is None


# --- clutter report -----------------------------------------------------------

def test_dry_run_names_the_clutter(messy_dir, capsys):
    assert main([str(messy_dir), "--no-model"]) == 0
    out = capsys.readouterr().out
    assert "duplicate clutter" in out


def test_fmt_bytes_units():
    from keeper.report import fmt_bytes

    assert fmt_bytes(0) == "0 B"
    assert fmt_bytes(912) == "912 B"
    assert fmt_bytes(1536) == "1.5 KB"
    assert fmt_bytes(2362232012) == "2.2 GB"
    assert fmt_bytes(1024 ** 4) == "1.0 TB"


def test_markdown_report_mentions_clutter(messy_dir, tmp_path):
    report = tmp_path / "r.md"
    assert main([str(messy_dir), "--no-model", "--report", str(report)]) == 0
    assert "duplicate clutter" in report.read_text()
