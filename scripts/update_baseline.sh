#!/usr/bin/env bash
# Ratchet automático de cobertura.
#
# 1. Roda pytest+cobertura.
# 2. Lê line-rate/branch-rate de coverage.xml.
# 3. Compara com .quality/baseline.json.
# 4. Se subiu, regrava o JSON (e o workflow commita em main).
#    Nunca diminui — falhas de cobertura já são travadas pelo
#    --cov-fail-under no pytest.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BASELINE="$ROOT/.quality/baseline.json"
COVERAGE_XML="$ROOT/coverage.xml"

cd "$ROOT"

# Rodar pytest (vai gerar coverage.xml por causa do addopts).
uv run pytest -q

uv run python - <<'PY'
import json
import sys
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

baseline_path = Path(".quality/baseline.json")
coverage_path = Path("coverage.xml")

tree = ET.parse(coverage_path)
root = tree.getroot()
line_pct = round(float(root.get("line-rate", "0")) * 100, 2)
branch_pct = round(float(root.get("branch-rate", "0")) * 100, 2)

current = json.loads(baseline_path.read_text())
prev_line = float(current.get("coverage_line_pct", 0))
prev_branch = float(current.get("coverage_branch_pct", 0))

new_line = max(prev_line, line_pct)
new_branch = max(prev_branch, branch_pct)

if new_line == prev_line and new_branch == prev_branch:
    print(f"baseline unchanged (line={prev_line}, branch={prev_branch})")
    sys.exit(0)

current["coverage_line_pct"] = new_line
current["coverage_branch_pct"] = new_branch
current["updated_at"] = date.today().isoformat()
baseline_path.write_text(json.dumps(current, indent=2) + "\n")
print(f"baseline updated: line {prev_line} -> {new_line}, branch {prev_branch} -> {new_branch}")
PY
