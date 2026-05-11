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

run_check "ruff lint"       uv run ruff check app tests
run_check "ruff format"     uv run ruff format --check app tests
run_check "pyright"         uv run pyright
run_check "pytest+coverage" uv run pytest -q
run_check "bandit"          uv run bandit -c pyproject.toml -r app -ll -ii
run_check "pip-audit"       pip_audit_check

echo
echo "======================================"
echo "         QUALITY GATE — chat-api"
echo "======================================"
printf "%-20s %s\n" "Dimensão" "Resultado"
echo "--------------------------------------"
for name in "ruff lint" "ruff format" "pyright" "pytest+coverage" "bandit" "pip-audit"; do
    symbol="✓"
    [[ "${RESULTS[$name]}" == "FAIL" ]] && symbol="✗"
    printf "%-20s %s\n" "$name" "$symbol"
done
echo "======================================"

exit $FAILED
