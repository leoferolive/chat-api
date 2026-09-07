"""Regression tests for the coverage ratchet enforced by scripts/quality.sh.

These exercise the real `ratchet_coverage_check` function (via `source`,
not a re-implementation) so a future edit that breaks or removes the
enforcement — the exact P1 reported against this file — fails CI.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

QUALITY_SH = Path(__file__).resolve().parent.parent / "scripts" / "quality.sh"


def _run_ratchet_check(
    tmp_path: Path, *, line_rate: float, branch_rate: float
) -> subprocess.CompletedProcess[str]:
    (tmp_path / ".quality").mkdir()
    (tmp_path / ".quality" / "baseline.json").write_text(
        json.dumps({"coverage_line_pct": 93.39, "coverage_branch_pct": 71.59})
    )
    (tmp_path / "coverage.xml").write_text(
        f'<?xml version="1.0" ?><coverage line-rate="{line_rate}" branch-rate="{branch_rate}"></coverage>'
    )
    script = f'set -uo pipefail\nsource "{QUALITY_SH}"\nratchet_coverage_check\nexit $?\n'
    return subprocess.run(
        ["bash", "-c", script],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )


def test_ratchet_passes_when_coverage_matches_baseline(tmp_path: Path) -> None:
    result = _run_ratchet_check(tmp_path, line_rate=0.9339, branch_rate=0.7159)
    assert result.returncode == 0
    assert "Ratchet OK" in result.stdout


def test_ratchet_fails_on_large_coverage_regression(tmp_path: Path) -> None:
    # Baseline is 93.39%/71.59% line/branch; this only stays above the
    # fixed --cov-fail-under=65 floor, which is exactly the silent
    # regression the P1 finding described.
    result = _run_ratchet_check(tmp_path, line_rate=0.70, branch_rate=0.50)
    assert result.returncode == 1
    assert "Ratchet falhou" in result.stdout
    assert "line   70.0% < baseline 93.39%" in result.stdout
    assert "branch 50.0% < baseline 71.59%" in result.stdout


def test_ratchet_tolerates_small_drop_within_tolerance(tmp_path: Path) -> None:
    # 0.3pp under baseline is inside the 0.5pp tolerance band.
    result = _run_ratchet_check(tmp_path, line_rate=0.9309, branch_rate=0.7129)
    assert result.returncode == 0
    assert "Ratchet OK" in result.stdout


def test_ratchet_fails_when_coverage_xml_missing(tmp_path: Path) -> None:
    (tmp_path / ".quality").mkdir()
    (tmp_path / ".quality" / "baseline.json").write_text(
        json.dumps({"coverage_line_pct": 93.39, "coverage_branch_pct": 71.59})
    )
    script = f'set -uo pipefail\nsource "{QUALITY_SH}"\nratchet_coverage_check\nexit $?\n'
    result = subprocess.run(
        ["bash", "-c", script],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "coverage.xml não encontrado" in result.stdout


def test_ratchet_fails_when_baseline_missing(tmp_path: Path) -> None:
    (tmp_path / "coverage.xml").write_text(
        '<?xml version="1.0" ?><coverage line-rate="0.9339" branch-rate="0.7159"></coverage>'
    )
    script = f'set -uo pipefail\nsource "{QUALITY_SH}"\nratchet_coverage_check\nexit $?\n'
    result = subprocess.run(
        ["bash", "-c", script],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "baseline.json não encontrado" in result.stdout
