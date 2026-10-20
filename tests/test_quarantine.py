"""Quarantine round-trip: apply moves losers, restore brings them back byte-for-byte."""

import json

from keeper.decide import decide
from keeper.extract import extract_text
from keeper.group import cluster
from keeper.quarantine import QDIR, GroupDecision, apply_quarantine, restore
from keeper.scan import scan


def _decisions(root):
    facts = scan(root)
    for f in facts:
        f.text, f.status = extract_text(f.path)
    return [
        GroupDecision(files=c, decision=decide(c, use_model=False))
        for c in cluster(facts)
    ]


def snapshot(root):
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and not str(p.relative_to(root)).startswith(QDIR)
    }


def test_apply_moves_only_losers_and_keeps_keeper(messy_dir):
    before = snapshot(messy_dir)
    groups = _decisions(messy_dir)
    manifest_path = apply_quarantine(messy_dir, groups)

    assert manifest_path and manifest_path.exists()
    for gd in groups:
        assert gd.decision.keep.path.exists(), "keeper must stay in place"
        for loser in gd.losers:
            assert not loser.path.exists(), "loser should have moved"
            assert any(
                p.name.startswith(loser.name)
                for p in (messy_dir / QDIR).rglob("*")
                if p.is_file()
            )
    # Nothing was lost: every original byte is either in place or quarantined.
    moved = json.loads(manifest_path.read_text())
    assert moved["groups"]
    quarantined = {
        str(p.relative_to(messy_dir)): p.read_bytes()
        for p in (messy_dir / QDIR).rglob("*")
        if p.is_file() and p.name != "manifest.json" and p.name != "restore.sh"
    }
    for gd in groups:
        for loser in gd.losers:
            assert before[loser.rel] in [v for v in quarantined.values()]


def test_restore_roundtrip(messy_dir):
    before = snapshot(messy_dir)
    apply_quarantine(messy_dir, _decisions(messy_dir))
    restored, error = restore(messy_dir)

    assert error is None
    assert restored == sum(len(g.losers) for g in _decisions(messy_dir))
    assert snapshot(messy_dir) == before, "folder must be byte-identical to the start"
    assert not (messy_dir / QDIR).exists()


def test_restore_without_quarantine_is_a_safe_noop(tmp_path):
    restored, error = restore(tmp_path)
    assert restored == 0
    assert "no keeper quarantine" in error


def test_restore_sh_is_generated(messy_dir):
    apply_quarantine(messy_dir, _decisions(messy_dir))
    script = messy_dir / QDIR / "restore.sh"
    assert script.exists()
    content = script.read_text()
    assert content.startswith("#!/usr/bin/env bash")
    assert "mv -n" in content
