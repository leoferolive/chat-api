# Plano: Quality Gate para `chat-api`

**Data:** 2026-05-11
**Branch:** `quality-gate-plan`
**Worktree:** `/home/leoferolive/projetos/chat-api-wt-quality-gate`

---

## Goal

Instalar um quality gate objetivo e ratcheted em `chat-api` que falha o build
quando métricas regridem, no espírito do post da Codeminer42 "Pare de ler código
de IA, comece a medi-lo". O ponto central não é revisar IA linha-a-linha: é
**medir saída**, gerar tabela ✓/✗ e tratar a baseline da primeira execução como
piso (ratchet — só sobe).

`chat-api` é o repositório com o **maior gap** entre os quatro projetos do
ecossistema: hoje não tem cobertura de testes, não tem type checking, não tem
formatter check, não tem análise de complexidade ciclomática, não tem
varredura de segurança e não tem pre-commit hook. O plano fecha essas seis
lacunas em **ordem de impacto** (cobertura e tipos primeiro) e termina com um
script único `make quality` que a IA é instruída a rodar antes de commitar via
`CLAUDE.md` raiz.

**O plano não cobre:**

- Bugs de runtime, race conditions em código async, vazamento de tokens via
  prompts adversariais, intenção de negócio. Essas categorias continuam
  exigindo revisão humana e testes de integração. Reconhecido na seção
  "Limitações reconhecidas".
- Mutation testing (mutmut, cosmic-ray). Para um projeto pequeno (~1500 linhas
  de testes) o ruído supera o sinal e o custo de runtime na CI é alto.

## Architecture

O gate é uma **composição de ferramentas existentes** invocadas por um único
ponto de entrada (`scripts/quality.sh` mais um `Makefile` fino). Cada dimensão
roda como subprocess independente; o script captura exit code, imprime linha
✓/✗ e falha agregadamente no final.

```
make quality
  ├─ ruff check app tests              (lint)
  ├─ ruff format --check app tests     (format)
  ├─ pyright                            (types — gradual)
  ├─ pytest --cov=app --cov-fail-under=N (testes + cobertura)
  ├─ bandit -r app -c pyproject.toml   (SAST Python)
  └─ pip-audit --strict                 (vulns em deps)
```

A CI (`.github/workflows/ci.yml`) executa o mesmo script. Pre-commit hooks
disparam um subconjunto rápido (ruff lint+format, bandit) localmente antes do
push.

Thresholds vivem em três lugares e **devem ficar em sincronia**:

1. `pyproject.toml` — config de ferramentas (limite de complexidade no ruff,
   `fail_under` do coverage, exclusões do bandit).
2. `scripts/quality.sh` — fallback caso a ferramenta não suporte threshold
   nativo.
3. `docs/quality-gate.md` — documentação de referência.

## Tech Stack

- **Python 3.12**, `uv` (gerenciador de pacotes e runner). Sem `pip install`
  direto.
- **pytest 8.3 + pytest-asyncio + respx** (já instalado).
- **pytest-cov** (novo, dev-dep).
- **ruff 0.6+** (já instalado; expandir regras).
- **pyright** (novo, dev-dep via `uv add --dev pyright`). Escolhido sobre mypy
  por (a) velocidade no projeto async-heavy, (b) inferência melhor com
  pydantic v2, (c) modo gradual mais ergonômico (`strict` por path).
- **bandit** (novo, dev-dep).
- **pip-audit** (novo, dev-dep). Preferido sobre `safety` porque pip-audit usa
  o banco oficial do PyPA e não exige conta.
- **pre-commit** (novo, dev-dep — instalado como pacote do projeto, não via
  `uv tool install`, para que toda contribuição use a mesma versão pinada em
  `uv.lock`).

## Justificativa dos Thresholds

Os números do post original (Rails: linha ≥95%, branch ≥90%, complexidade
≤6, mutation ≥69,5%) **não se transferem** para um backend Python I/O-heavy
de LLM. Razões:

1. **Cobertura 95% é fantasia em LLM service.** `app/llm_router.py` chama
   provedores via LiteLLM, faz fallback entre `mock/primary` e
   `mock/secondary` e streama via SSE. Caminhos de erro de rede, timeout de
   provedor, e modos degradados são exercitados por integração com provedor
   real — não por unit test. Tentar cobrir 95% leva a mocks tão profundos
   que o teste vira espelho da implementação (e morre a cada refactor).
2. **`from __future__ import annotations` em 12/13 arquivos** indica autor
   pensa em tipos mas não tem checker — pyright vai pegar fruta baixa
   imediatamente. Vale o investimento.
3. **Complexidade ≤6 é agressivo demais** para handlers FastAPI que fazem
   validação de input, autorização e dispatch num único corpo. Começar em
   ≤10 e descer no ratchet.

Thresholds iniciais (baseline da primeira execução vira piso; só sobe):

| Dimensão               | Inicial      | Justificativa                                       |
| ---------------------- | ------------ | --------------------------------------------------- |
| Cobertura de linhas    | **65%**      | I/O-heavy + LLM/SSE; honesto pra começar            |
| Cobertura de branches  | **55%**      | Coverage.py mede mal branches em async generators   |
| Complexidade McCabe    | **≤ 10**     | C901; descer pra 8 depois de 1 mês                  |
| Max-args por função    | **≤ 7**      | PLR0913; FastAPI deps inflam contagem               |
| Max-statements         | **≤ 50**     | PLR0915                                             |
| Pyright erros          | **0**        | Em modo `basic` global, `strict` por path (ver §)   |
| Bandit findings HIGH   | **0**        | LOW/MEDIUM viram baseline (`bandit.baseline.json`)  |
| pip-audit vulns        | **0 HIGH**   | MEDIUM tolerado se sem fix disponível               |
| Formatter diff         | **0 linhas** | `ruff format --check` é binário                     |

**Política de ratchet:** quando uma métrica numérica melhora em `main`, o
threshold sobe ao novo valor automaticamente via job pós-merge que escreve
em `.quality/baseline.json`. Nunca cai. Documentado em
`docs/quality-gate.md`.

## File Structure

```
chat-api/
├── CLAUDE.md                            (NOVO — instrução pra IA)
├── Makefile                             (NOVO — target `quality`)
├── pyproject.toml                       (EDITADO — deps, ruff, pytest, bandit, pyright)
├── .pre-commit-config.yaml              (NOVO)
├── .quality/
│   └── baseline.json                    (NOVO — gerado na primeira run; commitado)
├── bandit.baseline.json                 (NOVO — findings tolerados)
├── scripts/
│   └── quality.sh                       (NOVO — orquestra tudo e imprime tabela)
├── docs/
│   ├── quality-gate.md                  (NOVO — dimensões, thresholds, limitações)
│   └── superpowers/plans/
│       └── 2026-05-11-quality-gate.md   (este arquivo)
└── .github/workflows/
    └── ci.yml                           (EDITADO — chama `make quality`)
```

---

## Ordem das Tarefas

A ordenação reflete **maior gap primeiro**: cobertura e tipos antes de
complexidade, segurança e automação. Cada tarefa é independente e
mergeável isoladamente.

1. Cobertura com pytest-cov + ratchet
2. Type checking com pyright (modo gradual)
3. Formatter check (ruff format)
4. Complexidade ciclomática (C901, PLR0913, PLR0915)
5. Bandit (SAST) + pip-audit (vulns de deps)
6. Pre-commit hooks
7. Script unificado `make quality` + tabela ✓/✗
8. `CLAUDE.md` raiz e `docs/quality-gate.md`
9. Integração na CI

---

## Tarefa 1 — Cobertura com `pytest-cov`

**Objetivo:** começar a medir cobertura de linhas e branches; falhar o build
abaixo de 65% / 55%; preparar terreno para o ratchet.

### Steps

- [ ] Adicionar dep:

      uv add --dev pytest-cov

- [ ] Editar `pyproject.toml` — adicionar bloco:

      [tool.coverage.run]
      branch = true
      source = ["app"]
      omit = [
          "app/__init__.py",
          "app/main.py",   # bootstrap, coberto por smoke test
      ]

      [tool.coverage.report]
      precision = 1
      show_missing = true
      skip_covered = false
      exclude_lines = [
          "pragma: no cover",
          "raise NotImplementedError",
          "if TYPE_CHECKING:",
          "if __name__ == .__main__.:",
          "\\.\\.\\.",
      ]

      [tool.coverage.html]
      directory = ".coverage_html"

- [ ] Editar `[tool.pytest.ini_options]` em `pyproject.toml`:

      [tool.pytest.ini_options]
      asyncio_mode = "auto"
      testpaths = ["tests"]
      filterwarnings = ["ignore::DeprecationWarning"]
      addopts = [
          "--cov=app",
          "--cov-branch",
          "--cov-report=term-missing",
          "--cov-report=xml",
          "--cov-fail-under=65",
      ]

- [ ] Adicionar `.coverage`, `.coverage_html/`, `coverage.xml` ao `.gitignore`.

- [ ] Rodar localmente:

      uv sync --all-extras
      uv run pytest -q

      Esperado: pytest roda, imprime tabela de cobertura por arquivo, e
      ou passa (≥65%) ou falha com mensagem clara. Anotar valor real no
      PR como baseline.

- [ ] Criar `.quality/baseline.json` com o valor observado:

      {
        "coverage_line_pct": <valor real, ex 71.3>,
        "coverage_branch_pct": <valor real>,
        "updated_at": "2026-05-11"
      }

- [ ] Commit:

      git add pyproject.toml uv.lock .gitignore .quality/baseline.json
      git commit -m "test: adicionar pytest-cov com gate de 65% linhas / 55% branches"

### Saída esperada

```
---------- coverage: platform linux, python 3.12 ----------
Name                      Stmts   Miss Branch BrPart  Cover   Missing
---------------------------------------------------------------------
app/config.py                42      1     12      2  94.4%   ...
app/llm_router.py           185     32     48      8  78.1%   ...
...
TOTAL                       820    178    220     38  72.4%

Required test coverage of 65.0% reached. Total coverage: 72.4%
```

---

## Tarefa 2 — Type Checking com `pyright`

**Objetivo:** zero erros pyright em `app/` no nível `basic`, com paths
selecionados em `strict`. Não usar `strict = true` global no dia 1 — leva
a centenas de erros e desencoraja adoção.

### Adoção gradual de pyright (estratégia)

Adoção em **três anéis concêntricos**, do mais puro ao mais sujo:

**Anel 1 — `strict`** (zero compromissos):

- `app/models.py` — só Pydantic, sem I/O.
- `app/config.py` — pydantic-settings; tipos triviais.
- `app/prompt.py` — funções puras de formatação.

**Anel 2 — `basic`** (default, deve passar):

- `app/db.py`, `app/guards.py`, `app/metrics.py`, `app/user_identity.py`,
  `app/wiki_loader.py`, `app/router.py`, `app/sse.py`.

**Anel 3 — exceções pontuais** com `# pyright: ignore[<rule>]` por linha:

- `app/llm_router.py` — LiteLLM publica `litellm-stubs` parcial; esperar
  `reportUnknownMemberType` em chamadas a `litellm.completion`.
- `app/main.py` — Starlette/FastAPI middleware decorator às vezes precisa
  de `reportUntypedFunctionDecorator`.
- `tests/` — modo `basic`, `reportPrivateUsage = false`.

**Política sobre stubs ausentes:** `reportMissingTypeStubs = "warning"`
(não erro). Não adicionamos `py.typed` em deps de terceiros nem mantemos
stubs locais — custo de manutenção alto. Preferimos `# pyright: ignore`
localizado e justificado por comentário.

### Steps

- [ ] Adicionar dep:

      uv add --dev pyright

- [ ] Criar bloco em `pyproject.toml`:

      [tool.pyright]
      include = ["app", "tests"]
      exclude = ["**/__pycache__", ".venv", "build", "dist"]
      pythonVersion = "3.12"
      pythonPlatform = "Linux"
      typeCheckingMode = "basic"
      reportMissingTypeStubs = "warning"
      reportUnknownMemberType = "warning"
      reportUnknownVariableType = "warning"
      reportUnknownArgumentType = "warning"
      reportMissingImports = "error"
      reportGeneralTypeIssues = "error"
      strict = [
          "app/models.py",
          "app/config.py",
          "app/prompt.py",
      ]

      [[tool.pyright.executionEnvironments]]
      root = "tests"
      reportPrivateUsage = false
      reportUnknownMemberType = "none"

- [ ] Primeira execução:

      uv run pyright

      Esperar 5–30 erros reais. Categorias previstas:

      1. Retornos sem anotação → adicionar `-> None`, `-> dict[str, Any]`.
      2. `Optional` implícito → trocar `x: str = None` por `x: str | None = None`.
      3. LiteLLM untyped → `# pyright: ignore[reportUnknownMemberType]  # LiteLLM stubs incompletos`.
      4. Decoradores untyped do slowapi → mesma estratégia.

- [ ] Corrigir erros em `app/` até `uv run pyright` sair com **0 errors**
      (warnings tolerados). Não suprimir em massa.

- [ ] Atualizar CI temporariamente para WARN: rodar `uv run pyright || true`
      no primeiro deploy, depois remover `|| true` quando estabilizar.

- [ ] Commit:

      git add pyproject.toml uv.lock app/
      git commit -m "feat: adotar pyright em modo basic com paths estritos"

### Saída esperada

```
0 errors, 12 warnings, 0 informations
```

---

## Tarefa 3 — Formatter Check (`ruff format`)

**Objetivo:** garantir que todo código em `main` está formatado segundo
`ruff format`. Diff zero é o gate.

### Steps

- [ ] Rodar formatação inicial uma vez (commit separado, sem mudanças
      funcionais):

      uv run ruff format app tests
      git add app tests
      git commit -m "style: formatar codebase com ruff format"

- [ ] Adicionar bloco opcional em `pyproject.toml` (defaults já são bons):

      [tool.ruff.format]
      quote-style = "double"
      indent-style = "space"
      docstring-code-format = true

- [ ] Validar:

      uv run ruff format --check app tests

      Esperado: `<N> files already formatted`, exit 0.

- [ ] Commit:

      git add pyproject.toml
      git commit -m "ci: habilitar ruff format --check no quality gate"

---

## Tarefa 4 — Complexidade Ciclomática (Ruff `C901`, `PLR`)

**Objetivo:** travar regressões em complexidade de função antes de chegarem
em `main`. Limite inicial folgado (≤10) para não bloquear; ratchet desce
depois.

### Steps

- [ ] Editar `[tool.ruff.lint]` em `pyproject.toml`:

      [tool.ruff.lint]
      select = ["E", "F", "W", "I", "B", "UP", "ASYNC", "C90", "PLR0913", "PLR0915"]
      ignore = ["E501"]

      [tool.ruff.lint.mccabe]
      max-complexity = 10

      [tool.ruff.lint.pylint]
      max-args = 7
      max-statements = 50

- [ ] Rodar:

      uv run ruff check app tests

      Categorias previstas:
      - `C901` em `app/llm_router.py` (dispatch de provedor).
      - `PLR0913` em handlers FastAPI com muitas deps via `Depends()`.

- [ ] Para cada violação:
      - Refatorar se for vitória clara (extrair função privada).
      - Caso o handler FastAPI legitimamente precise de 8+ params (deps),
        suprimir linha com `# noqa: PLR0913` e justificativa em comentário.
        Não suprimir no nível arquivo nem global.

- [ ] Commit:

      git add pyproject.toml app/
      git commit -m "refactor: travar complexidade ciclomática em <=10"

---

## Tarefa 5 — Bandit (SAST) + pip-audit (vulns de deps)

**Objetivo:** varredura estática de segurança Python. Findings HIGH falham
o build; LOW/MEDIUM viram baseline.

### Steps Bandit

- [ ] Adicionar dep:

      uv add --dev bandit

- [ ] Adicionar config em `pyproject.toml`:

      [tool.bandit]
      exclude_dirs = ["tests", ".venv", "build"]
      skips = [
          "B101",  # assert_used — pytest depende
      ]

- [ ] Primeira execução para gerar baseline:

      uv run bandit -r app -f json -o bandit.baseline.json || true

      Inspecionar `bandit.baseline.json`. Findings esperados em
      `python-jose` (uso de algoritmos JWT) e `slowapi` — revisar
      manualmente e:
      - Verdadeiros positivos → corrigir.
      - Falsos positivos justificados → manter no baseline.

- [ ] Comando de gate (no `quality.sh`):

      uv run bandit -r app -ll -ii

      `-ll` = falha só em severity ≥ LOW, confidence ≥ MEDIUM (ajustar para
      `-lll -iii` se ruidoso). HIGH sempre falha.

- [ ] Commit:

      git add pyproject.toml uv.lock bandit.baseline.json
      git commit -m "sec: adicionar bandit com baseline para chat-api"

### Steps pip-audit

- [ ] Adicionar dep:

      uv add --dev pip-audit

- [ ] Comando de gate:

      uv run pip-audit --strict --disable-pip

      `--disable-pip` evita que pip-audit re-resolva o ambiente;
      ele consome o `uv.lock` via plugin nativo do PyPA.

- [ ] Caso uma vuln MEDIUM sem fix apareça, adicionar exceção temporária
      em arquivo `.pip-audit-ignore` (formato: um GHSA por linha) com data
      de revisão.

- [ ] Commit:

      git add pyproject.toml uv.lock
      git commit -m "sec: pip-audit no quality gate (vulns de deps)"

---

## Tarefa 6 — Pre-commit Hooks

**Objetivo:** roda subconjunto rápido localmente em cada commit. Não
duplica o gate da CI — é um early-warning.

**Decisão:** `pre-commit` instalado como **dev-dep do projeto**
(`uv add --dev pre-commit`), não via `uv tool install`. Justificativa:
versão pinada em `uv.lock` garante que todo dev e a CI usam exatamente o
mesmo binário; `uv tool install` é global e drift entre máquinas. Custo:
1 dep extra; ganho: reprodutibilidade.

### Steps

- [ ] Adicionar dep:

      uv add --dev pre-commit

- [ ] Criar `.pre-commit-config.yaml`:

      repos:
        - repo: https://github.com/astral-sh/ruff-pre-commit
          rev: v0.6.9
          hooks:
            - id: ruff
              args: [--fix]
            - id: ruff-format
        - repo: https://github.com/PyCQA/bandit
          rev: 1.7.10
          hooks:
            - id: bandit
              args: ["-c", "pyproject.toml", "-r", "app"]
              additional_dependencies: ["bandit[toml]"]
        - repo: local
          hooks:
            - id: pyright-changed
              name: pyright (changed files)
              entry: uv run pyright
              language: system
              types: [python]
              pass_filenames: true

      Nota: pytest **não entra** no pre-commit — lento demais. Roda só
      no `make quality` e na CI.

- [ ] Instalar localmente:

      uv run pre-commit install

- [ ] Validar:

      uv run pre-commit run --all-files

- [ ] Commit:

      git add .pre-commit-config.yaml pyproject.toml uv.lock
      git commit -m "chore: adicionar pre-commit com ruff, bandit, pyright"

---

## Tarefa 7 — Script Unificado `make quality`

**Objetivo:** um comando único, com tabela ✓/✗ final clara, igual ao do
post da Codeminer42. Mesmo script roda local e na CI.

### Steps

- [ ] Criar `scripts/quality.sh`:

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

      run_check "ruff lint"       uv run ruff check app tests
      run_check "ruff format"     uv run ruff format --check app tests
      run_check "pyright"         uv run pyright
      run_check "pytest+coverage" uv run pytest -q
      run_check "bandit"          uv run bandit -r app -ll -ii
      run_check "pip-audit"       uv run pip-audit --strict --disable-pip

      echo
      echo "======================================"
      echo "         QUALITY GATE — chat-api"
      echo "======================================"
      printf "%-20s %s\n" "Dimensão" "Resultado"
      echo "--------------------------------------"
      for name in "ruff lint" "ruff format" "pyright" "pytest+coverage" "bandit" "pip-audit"; do
          local symbol="✓"
          [[ "${RESULTS[$name]}" == "FAIL" ]] && symbol="✗"
          printf "%-20s %s\n" "$name" "$symbol"
      done
      echo "======================================"

      exit $FAILED

- [ ] `chmod +x scripts/quality.sh`.

- [ ] Criar `Makefile`:

      .PHONY: quality install format lint type test sec audit

      install:
      	uv sync --all-extras

      lint:
      	uv run ruff check app tests

      format:
      	uv run ruff format app tests

      type:
      	uv run pyright

      test:
      	uv run pytest -q

      sec:
      	uv run bandit -r app -ll -ii

      audit:
      	uv run pip-audit --strict --disable-pip

      quality:
      	@bash scripts/quality.sh

- [ ] Validar:

      make quality

      Saída esperada (com tudo verde):

      ======================================
               QUALITY GATE — chat-api
      ======================================
      Dimensão             Resultado
      --------------------------------------
      ruff lint            ✓
      ruff format          ✓
      pyright              ✓
      pytest+coverage      ✓
      bandit               ✓
      pip-audit            ✓
      ======================================

- [ ] Commit:

      git add scripts/quality.sh Makefile
      git commit -m "build: adicionar make quality com tabela do gate"

---

## Tarefa 8 — `CLAUDE.md` raiz e `docs/quality-gate.md`

**Objetivo:** documentar contrato com o agente IA e com humanos.

### Steps

- [ ] Criar `CLAUDE.md` na raiz com no mínimo:

      # CLAUDE.md — chat-api

      ## Quality gate (obrigatório antes de qualquer commit)

      Antes de criar commit nesta repo, **rodar**:

          make quality

      Todas as linhas da tabela final devem mostrar `✓`. Se qualquer uma
      mostrar `✗`, **não commite** — corrija o problema reportado.

      Detalhes em `docs/quality-gate.md`.

      ## Comandos comuns

      - Sync deps: `uv sync --all-extras`
      - Rodar dev server: `uv run uvicorn app.main:app --reload`
      - Testes só: `uv run pytest -q`
      - Lint só: `uv run ruff check app tests`
      - Type só: `uv run pyright`

      ## Política de tipos

      Modo `basic` global, `strict` em `app/models.py`, `app/config.py`,
      `app/prompt.py`. Não desabilitar pyright em arquivos novos sem
      justificativa em comentário `# pyright: ignore[<rule>]  # <razão>`.

      ## Cobertura

      Mínimo 65% linhas / 55% branches. Ratchet sobe automaticamente em
      `main`. Não baixar `--cov-fail-under` para passar o gate.

- [ ] Criar `docs/quality-gate.md` documentando:
      - Tabela das 6 dimensões e thresholds atuais.
      - Como rodar local (`make quality`).
      - Como o ratchet funciona (job pós-merge atualiza
        `.quality/baseline.json`).
      - **Limitações reconhecidas** (ver seção final deste plano).
      - Como suprimir uma regra (em arquivo, com justificativa).

- [ ] Commit:

      git add CLAUDE.md docs/quality-gate.md
      git commit -m "docs: CLAUDE.md raiz e doc do quality gate"

---

## Tarefa 9 — Integração na CI

**Objetivo:** CI passa a usar `make quality` em vez de comandos
individuais. Job pós-merge em `main` atualiza baseline.

### Steps

- [ ] Editar `.github/workflows/ci.yml`:

      name: ci

      on:
        push:
          branches: [main]
        pull_request:

      jobs:
        quality:
          runs-on: ubuntu-latest
          steps:
            - uses: actions/checkout@v4
            - name: Install uv
              uses: astral-sh/setup-uv@v3
              with:
                version: "0.5.x"
            - name: Set up Python
              run: uv python install 3.12
            - name: Sync dependencies
              run: uv sync --all-extras
            - name: Quality gate
              run: make quality
              env:
                ENV: ci
                TURNSTILE_DISABLED: "true"
                LLM_PROVIDERS: "mock/primary,mock/secondary"
                SESSION_SECRET: "ci-secret"
                IP_HASH_SALT: "ci-salt"
            - name: Upload coverage
              if: always()
              uses: actions/upload-artifact@v4
              with:
                name: coverage-xml
                path: coverage.xml

- [ ] Criar `.github/workflows/quality-ratchet.yml` (roda só em push pra
      `main`):

      name: quality-ratchet
      on:
        push:
          branches: [main]
      jobs:
        ratchet:
          runs-on: ubuntu-latest
          permissions:
            contents: write
          steps:
            - uses: actions/checkout@v4
            - uses: astral-sh/setup-uv@v3
            - run: uv python install 3.12
            - run: uv sync --all-extras
            - name: Run coverage, update baseline if higher
              run: bash scripts/update_baseline.sh
            - name: Commit ratchet
              run: |
                if [[ -n "$(git status --porcelain .quality/)" ]]; then
                  git config user.name "quality-ratchet"
                  git config user.email "ratchet@chat-api.local"
                  git add .quality/baseline.json
                  git commit -m "chore(ratchet): subir baseline de cobertura"
                  git push
                fi

- [ ] Criar `scripts/update_baseline.sh` que lê coverage atual, compara
      com `.quality/baseline.json`, e regrava se subiu (nunca se desceu).

- [ ] Commit:

      git add .github/workflows/ scripts/update_baseline.sh
      git commit -m "ci: trocar passos avulsos por make quality + ratchet automático"

---

## Limitações reconhecidas

O quality gate **não cobre** as seguintes categorias. Para essas, revisão
humana e/ou testes de integração com infra real continuam obrigatórios:

1. **Bugs lógicos de negócio.** Pyright e cobertura não sabem o que o
   código deveria fazer.
2. **Race conditions em código async.** Particularmente relevante em
   `app/llm_router.py` (cancelamento de stream SSE quando cliente
   desconecta) e em uso concorrente de aiosqlite. Detectáveis só com teste
   de stress / chaos.
3. **Vazamento de prompts via injeção adversarial.** Ataque puramente de
   conteúdo; nenhuma ferramenta estática detecta. Mitigado por design no
   `app/guards.py`, não no gate.
4. **Secrets em runtime / env vars erradas.** Bandit pega secrets em
   código-fonte; não pega `SESSION_SECRET` fraco em produção.
5. **Regressões de performance.** Cobertura passa, build verde, latência
   p99 dobra. Requer benchmark contínuo separado.
6. **Drift de comportamento de LLM provider.** LiteLLM muda formato,
   provedor muda modelo default — testes com `respx` continuam verdes,
   produção quebra. Mitigado por integração com `mock/primary` em CI e
   smoke test pós-deploy.
7. **Acessibilidade de prompts/respostas, qualidade subjetiva da resposta
   do bot.** Fora do escopo de qualquer gate estático.
8. **Dependências transitivas com licença incompatível.** pip-audit foca
   em CVE, não em licença. Plano futuro pode adicionar `pip-licenses`.

**Por design, o gate prefere falsos negativos a falsos positivos no
dia 1.** Thresholds folgados, baseline tolera findings existentes, modo
`basic` em vez de `strict`. À medida que a baseline sobe pelo ratchet, o
gate fica mais rigoroso sozinho — sem precisar de discussão.
