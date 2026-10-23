"""End-to-end CLI behaviour: dry-run default, apply, restore, exit codes."""

import argparse
import json

import pytest

from keeper.cli import (main, parse_clusters, parse_exts, parse_size)


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


# --- value parsers -------------------------------------------------------------

def test_parse_size():
    assert parse_size("500") == 500
    assert parse_size("10k") == 10240
    assert parse_size("10K") == 10240
    assert parse_size("2M") == 2 * 1024 * 1024
    assert parse_size("1G") == 1024 ** 3
    assert parse_size("2.5M") == int(2.5 * 1024 * 1024)
    with pytest.raises(argparse.ArgumentTypeError):
        parse_size("huge")


def test_parse_exts():
    assert parse_exts("pdf,.JPG,docx") == {".pdf", ".jpg", ".docx"}
    with pytest.raises(argparse.ArgumentTypeError):
        parse_exts(" , ")


def test_parse_clusters():
    assert parse_clusters("1,3,5") == {1, 3, 5}
    assert parse_clusters("2-4") == {2, 3, 4}
    assert parse_clusters("1,3-5,7") == {1, 3, 4, 5, 7}
    with pytest.raises(argparse.ArgumentTypeError):
        parse_clusters("0")
    with pytest.raises(argparse.ArgumentTypeError):
        parse_clusters("abc")
    with pytest.raises(argparse.ArgumentTypeError):
        parse_clusters("5-1")


# --- targeting: --ext, --min-size, --include, --exclude -------------------------

def _run(messy_dir, capsys, *extra) -> dict:
    """Dry run in JSON mode and return the parsed document."""
    assert main([str(messy_dir), "--no-model", "--json", *extra]) == 0
    return json.loads(capsys.readouterr().out)


def test_ext_flag_narrows_the_run(messy_dir, capsys):
    baseline = _run(messy_dir, capsys)
    narrowed = _run(messy_dir, capsys, "--ext", "pdf")

    assert narrowed["clusters"] < baseline["clusters"]
    for dec in narrowed["decisions"]:
        assert dec["keep"].endswith(".pdf")
        assert all(loser.endswith(".pdf") for loser in dec["losers"])


def test_min_size_flag_can_empty_a_scan(messy_dir, capsys):
    data = _run(messy_dir, capsys, "--min-size", "100k")
    assert data["files"] == 0
    assert data["clusters"] == 0
    assert data["losers"] == 0


def test_exclude_flag_drops_matching_files(messy_dir, capsys):
    data = _run(messy_dir, capsys, "--exclude", "passport*")
    touched = [item for dec in data["decisions"]
               for item in [dec["keep"], *dec["losers"]]]
    assert touched
    assert not any("passport" in item for item in touched)


def test_include_flag_keeps_only_matches(messy_dir, capsys):
    data = _run(messy_dir, capsys, "--include", "rental*")
    touched = [item for dec in data["decisions"]
               for item in [dec["keep"], *dec["losers"]]]
    assert touched
    assert all(item.endswith(".txt") for item in touched)
    assert all("rental" in item for item in touched)


def test_filters_are_advertised_in_the_banner(messy_dir, capsys):
    assert main([str(messy_dir), "--no-model", "--ext", "pdf"]) == 0
    out = capsys.readouterr().out
    assert "ext .pdf" in out


# --- cluster selection: --only / --skip ----------------------------------------

def test_only_selects_one_cluster_and_keeps_its_number(messy_dir, capsys):
    baseline = _run(messy_dir, capsys)
    numbers = [d["cluster"] for d in baseline["decisions"]]
    assert len(numbers) >= 2
    target = numbers[-1]

    data = _run(messy_dir, capsys, "--only", str(target))
    assert [d["cluster"] for d in data["decisions"]] == [target]

    expected = baseline["decisions"][-1]
    got = data["decisions"][0]
    assert got["keep"] == expected["keep"]
    assert got["losers"] == expected["losers"]


def test_skip_drops_a_cluster_and_keeps_the_rest_numbered(messy_dir, capsys):
    baseline = _run(messy_dir, capsys)
    numbers = [d["cluster"] for d in baseline["decisions"]]

    data = _run(messy_dir, capsys, "--skip", str(numbers[0]))
    assert [d["cluster"] for d in data["decisions"]] == numbers[1:]


def test_only_and_skip_compose(messy_dir, capsys):
    baseline = _run(messy_dir, capsys)
    numbers = [d["cluster"] for d in baseline["decisions"]]

    data = _run(messy_dir, capsys,
                "--only", f"{numbers[0]},{numbers[1]}",
                "--skip", str(numbers[0]))
    assert [d["cluster"] for d in data["decisions"]] == [numbers[1]]


def test_unknown_cluster_number_is_a_usage_error(messy_dir, capsys):
    assert main([str(messy_dir), "--no-model", "--json", "--only", "999"]) == 2
    assert "do not exist" in capsys.readouterr().err


def test_markdown_report_keeps_original_cluster_numbers(messy_dir, tmp_path, capsys):
    baseline = _run(messy_dir, capsys)
    target = baseline["decisions"][-1]["cluster"]

    report = tmp_path / "numbered.md"
    assert main([str(messy_dir), "--no-model", "--only", str(target),
                 "--report", str(report)]) == 0
    text = report.read_text()
    assert f"| {target} |" in text
    assert f"**Cluster {target}**" in text


# --- --fail-on-clutter ----------------------------------------------------------

def test_fail_on_clutter_exits_3(messy_dir):
    assert main([str(messy_dir), "--no-model", "--fail-on-clutter"]) == 3


def test_fail_on_clutter_exits_0_when_tidy(tmp_path):
    (tmp_path / "solo.txt").write_text("only one file here\n")
    assert main([str(tmp_path), "--no-model", "--fail-on-clutter"]) == 0


def test_without_the_flag_clutter_still_exits_0(messy_dir):
    assert main([str(messy_dir), "--no-model"]) == 0


# --- --quiet --------------------------------------------------------------------

def test_quiet_prints_nothing(messy_dir, capsys):
    assert main([str(messy_dir), "--no-model", "--quiet"]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_quiet_still_prints_json(messy_dir, capsys):
    assert main([str(messy_dir), "--no-model", "--quiet", "--json"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out)["clusters"] >= 1


def test_quiet_still_writes_the_report(messy_dir, tmp_path, capsys):
    report = tmp_path / "quiet.md"
    assert main([str(messy_dir), "--no-model", "--quiet",
                 "--report", str(report)]) == 0
    assert report.exists()


# --- --interactive guards -------------------------------------------------------

def test_interactive_needs_a_terminal(messy_dir, capsys, monkeypatch):
    import sys
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert main([str(messy_dir), "--no-model", "--interactive"]) == 2
    assert "needs a terminal" in capsys.readouterr().err


def test_interactive_conflicts_with_json(messy_dir, capsys):
    assert main([str(messy_dir), "--no-model", "--interactive", "--json"]) == 2
    assert "--json" in capsys.readouterr().err


def test_interactive_conflicts_with_quiet(messy_dir, capsys):
    assert main([str(messy_dir), "--no-model", "--interactive", "--quiet"]) == 2
    assert "--quiet" in capsys.readouterr().err


# --- --max-hash-mb --------------------------------------------------------------

def test_negative_max_hash_is_a_usage_error(messy_dir, capsys):
    assert main([str(messy_dir), "--max-hash-mb", "-5"]) == 2
    assert "cannot be negative" in capsys.readouterr().err


def test_zero_max_hash_means_hash_everything(messy_dir, capsys):
    data = _run(messy_dir, capsys, "--max-hash-mb", "0")
    # Every identical cluster still resolves, and nothing errored out.
    assert data["clusters"] >= 1
    assert data["files"] >= 1


# --- closed pipe ----------------------------------------------------------------

def test_a_closed_pipe_does_not_traceback(messy_dir, monkeypatch):
    import sys

    class _ClosedPipe:
        """stdout whose reader went away — what `keeper ... | head` hits."""

        def write(self, text):
            raise BrokenPipeError()

        def flush(self):
            raise BrokenPipeError()

        def fileno(self):
            raise OSError("captured stdout has no file descriptor")

        def isatty(self):
            return False

    monkeypatch.setattr(sys, "stdout", _ClosedPipe())
    assert main([str(messy_dir), "--no-model"]) == 0
