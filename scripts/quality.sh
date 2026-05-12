#!/usr/bin/env bash
# Quality gate orchestrator — imprime tabela ✓/✗ e falha agregadamente.
set -uo pipefail

declare -A RESULTS
FAILED=0

run_check() {
    local name="$1"
    shift
    echo "::group::$name"
    if "$@"; then
        RESULTS[$name]="OK"
    else
        RESULTS[$name]="FAIL"
        FAILED=1
    fi
    echo "::endgroup::"
}

pip_audit_check() {
    # pip-audit não consome uv.lock diretamente; geramos um requirements
    # temporário a partir do lock e auditamos sem deps (já está fechado).
    local tmp rc
    tmp="$(mktemp -t pipaudit-XXXXXX.txt)"
    uv export --format requirements-txt --no-emit-project --no-hashes > "$tmp"
    uv run pip-audit --strict --disable-pip --no-deps -r "$tmp"
    rc=$?
    rm -f "$tmp"
    return $rc
}

ratchet_coverage_check() {
    # Compara coverage.xml com .quality/baseline.json e falha se a
    # cobertura cair mais de TOLERANCE pp abaixo da baseline registrada.
    # Não substitui --cov-fail-under; reforça o ratchet contra regressões
    # silenciosas (ex: baseline em 93% mas cov-fail-under em 65%).
    if [[ ! -f coverage.xml ]]; then
        echo "❌ ratchet: coverage.xml não encontrado (rode pytest antes)."
        return 1
    fi
    if [[ ! -f .quality/baseline.json ]]; then
        echo "❌ ratchet: .quality/baseline.json não encontrado."
        return 1
    fi

    local current_line current_branch baseline_line baseline_branch tolerance
    tolerance=0.5
    current_line=$(python3 -c "import xml.etree.ElementTree as ET; print(round(float(ET.parse('coverage.xml').getroot().get('line-rate'))*100, 2))")
    current_branch=$(python3 -c "import xml.etree.ElementTree as ET; print(round(float(ET.parse('coverage.xml').getroot().get('branch-rate'))*100, 2))")
    baseline_line=$(python3 -c "import json; print(json.load(open('.quality/baseline.json'))['coverage_line_pct'])")
    baseline_branch=$(python3 -c "import json; print(json.load(open('.quality/baseline.json'))['coverage_branch_pct'])")

    RATCHET_LINE_CURRENT="$current_line"
    RATCHET_LINE_BASELINE="$baseline_line"
    RATCHET_BRANCH_CURRENT="$current_branch"
    RATCHET_BRANCH_BASELINE="$baseline_branch"
    RATCHET_TOLERANCE="$tolerance"

    local line_fail branch_fail
    line_fail=$(python3 -c "print(1 if $current_line < ($baseline_line - $tolerance) else 0)")
    branch_fail=$(python3 -c "print(1 if $current_branch < ($baseline_branch - $tolerance) else 0)")

    if [[ "$line_fail" = "1" || "$branch_fail" = "1" ]]; then
        echo "❌ Ratchet falhou:"
        [[ "$line_fail" = "1" ]] && echo "   line   $current_line% < baseline $baseline_line% - tolerance $tolerance%"
        [[ "$branch_fail" = "1" ]] && echo "   branch $current_branch% < baseline $baseline_branch% - tolerance $tolerance%"
        return 1
    fi

    echo "✓ Ratchet OK: line $current_line% >= $baseline_line% - $tolerance%; branch $current_branch% >= $baseline_branch% - $tolerance%"
}

run_check "ruff lint"       uv run ruff check app tests
run_check "ruff format"     uv run ruff format --check app tests
run_check "pyright"         uv run pyright
run_check "pytest+coverage" uv run pytest -q
run_check "ratchet"         ratchet_coverage_check
run_check "bandit"          uv run bandit -c pyproject.toml -r app -ll -ii
run_check "pip-audit"       pip_audit_check

echo
echo "======================================"
echo "         QUALITY GATE — chat-api"
echo "======================================"
printf "%-20s %s\n" "Dimensão" "Resultado"
echo "--------------------------------------"
for name in "ruff lint" "ruff format" "pyright" "pytest+coverage" "ratchet" "bandit" "pip-audit"; do
    symbol="✓"
    [[ "${RESULTS[$name]}" == "FAIL" ]] && symbol="✗"
    printf "%-20s %s\n" "$name" "$symbol"
done
echo "--------------------------------------"
if [[ -n "${RATCHET_LINE_CURRENT:-}" ]]; then
    printf "%-20s line %s%% (baseline %s%%, tol %s%%)\n" \
        "ratchet line"   "$RATCHET_LINE_CURRENT"   "$RATCHET_LINE_BASELINE"   "$RATCHET_TOLERANCE"
    printf "%-20s branch %s%% (baseline %s%%, tol %s%%)\n" \
        "ratchet branch" "$RATCHET_BRANCH_CURRENT" "$RATCHET_BRANCH_BASELINE" "$RATCHET_TOLERANCE"
fi
echo "======================================"

exit $FAILED
