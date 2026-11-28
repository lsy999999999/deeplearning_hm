"""Global journal is append-only, idempotent and safe against changed history."""

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "record_iterations", ROOT / "scripts/record_iterations.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_reimport_is_idempotent_and_changed_source_preserves_old_journal(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(module, "ROOT", tmp_path)
    output = tmp_path / "journal.md"
    entries = [("run/one", "meta_decision", {"accepted": False}, "source.json")]
    assert module.append_entries(entries, output) == 1
    original = output.read_bytes()
    assert module.append_entries(entries, output) == 0
    assert output.read_bytes() == original
    with pytest.raises(ValueError, match="source changed"):
        module.append_entries(
            [("run/one", "meta_decision", {"accepted": True}, "source.json")], output
        )
    assert output.read_bytes() == original
    assert (
        module.append_entries(
            [("run/two", "meta_decision", {"accepted": True}, "source2.json")], output
        )
        == 1
    )
    assert output.read_bytes().startswith(original)


def test_preserved_demo_artifact_hashes_match_originals():
    manifest = ROOT / "docs/demo_archive/originals-1ad8a37.json"
    if not manifest.exists():
        pytest.skip("Preservation manifest is generated in the publishing checkout")
    for name, expected in json.loads(manifest.read_text())["files"].items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == expected, name


def test_export_cannot_overwrite_a_preserved_demo(tmp_path, monkeypatch):
    import importlib

    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    exporter = importlib.import_module("export_evolution")
    monkeypatch.setattr(exporter, "ROOT", tmp_path)
    output = tmp_path / "results/evolution-view.html"
    output.parent.mkdir()
    output.write_text("ORIGINAL DEMO")
    manifest = tmp_path / "docs/demo_archive/originals-1ad8a37.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps({"files": {"results/evolution-view.html": "original hash"}})
    )
    with pytest.raises(ValueError, match="preserved demonstration"):
        exporter.export([], output)
    assert output.read_text() == "ORIGINAL DEMO"
