# Quality Gate — chat-api

Quality gate objetivo e ratcheted, inspirado no post da Codeminer42
"Pare de ler código de IA, comece a medi-lo". O objetivo não é revisar
IA linha-a-linha: é **medir saída**, gerar tabela ✓/✗ e tratar a
baseline da primeira execução como piso (só sobe).

## Como rodar localmente

    make quality

Equivalente direto (caso `make` não exista):

    bash scripts/quality.sh

A saída termina com uma tabela. Todas as linhas devem mostrar `✓`:

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

## Dimensões e thresholds atuais

| Dimensão               | Threshold inicial | Onde está configurado                                          |
| ---------------------- | ----------------- | -------------------------------------------------------------- |
| Cobertura de linhas    | ≥ 65%             | `pyproject.toml` (`--cov-fail-under`)                          |
| Cobertura de branches  | (medida)          | `[tool.coverage.run] branch = true`                            |
| Complexidade McCabe    | ≤ 10              | `[tool.ruff.lint.mccabe] max-complexity`                       |
| Max-args por função    | ≤ 7               | `[tool.ruff.lint.pylint] max-args`                             |
| Max-statements         | ≤ 50              | `[tool.ruff.lint.pylint] max-statements`                       |
| Pyright erros          | 0                 | `[tool.pyright] typeCheckingMode = "basic"` + `strict = [...]` |
| Bandit findings HIGH   | 0                 | `bandit -ll -ii` no gate; baseline em `bandit.baseline.json`   |
| pip-audit vulns        | 0 conhecidas      | `pip-audit --strict --disable-pip --no-deps`                   |
| Formatter diff         | 0 linhas          | `ruff format --check`                                          |

A baseline numérica corrente vive em `.quality/baseline.json`.

## Política de tipos (pyright em 3 anéis)

Anel 1 — `strict` (zero compromissos):

- `app/models.py`
- `app/config.py`
- `app/prompt.py`

Anel 2 — `basic` (default, deve passar):

- `app/db.py`, `app/guards.py`, `app/metrics.py`,
  `app/user_identity.py`, `app/wiki_loader.py`, `app/router.py`,
  `app/sse.py`.

Anel 3 — exceções pontuais por linha com `# pyright: ignore[<rule>] # razão`:

- `app/llm_router.py` — LiteLLM publica stubs parciais.
- `app/main.py` — handler do slowapi tem assinatura mais estreita que
  `ExceptionHandler` do FastAPI.

Em `tests/` mantemos modo `basic` com `reportPrivateUsage = false`,
`reportUnknownMemberType = "none"`, e algumas categorias degradadas para
`warning` (BaseSettings kwargs).

## Como suprimir uma regra

Por **linha** (preferido):

    foo = bar()  # noqa: PLR0913 — handler legítimo com muitas deps via Depends()
    obj.x = y    # pyright: ignore[reportArgumentType]  # razão clara

**Não suprimir em massa** (no nível de arquivo ou global). Toda
supressão exige comentário com a razão.

## Ratchet automático

Quando uma métrica numérica melhora em `main`, o workflow
`.github/workflows/quality-ratchet.yml` atualiza
`.quality/baseline.json` e commita direto na branch `main`. Threshold
**só sobe** — se a próxima execução estiver abaixo do novo piso, o gate
falha.

> **Atenção:** o workflow tem permissão `contents: write` e faz `git
> push` em `main`. Antes de habilitar em produção, decidir se prefere
> abrir PR de ratchet ao invés de commit direto.

## pytest fora do pre-commit

`pytest` **não roda** no pre-commit hook — é caro. Roda só em
`make quality` (local sob demanda) e na CI. Pre-commit captura ruff,
bandit e pyright nos arquivos staged.

## Limitações reconhecidas

O gate **não cobre**:

1. **Bugs lógicos de negócio.** Pyright e cobertura não sabem o que o
   código deveria fazer.
2. **Race conditions em código async.** Particularmente relevante em
   `app/llm_router.py` (cancelamento de stream SSE quando cliente
   desconecta) e em uso concorrente de aiosqlite.
3. **Vazamento de prompts via injeção adversarial.** Ataque puramente
   de conteúdo; nenhuma ferramenta estática detecta. Mitigado por
   design em `app/guards.py`.
4. **Secrets em runtime / env vars erradas.** Bandit pega secrets em
   código-fonte; não pega `SESSION_SECRET` fraco em produção.
5. **Regressões de performance.** Cobertura passa, build verde,
   latência p99 dobra. Requer benchmark contínuo separado.
6. **Drift de comportamento de LLM provider.** LiteLLM muda formato,
   provedor muda modelo default — testes com `respx` continuam verdes,
   produção quebra. Mitigado por integração com `mock/primary` em CI e
   smoke test pós-deploy.
7. **Acessibilidade / qualidade subjetiva da resposta do bot.** Fora do
   escopo de qualquer gate estático.
8. **Licença de dependências transitivas.** pip-audit foca em CVE.
   Pode evoluir com `pip-licenses` no futuro.

**Por design, o gate prefere falsos negativos a falsos positivos no
dia 1.** Thresholds folgados, baseline tolera findings existentes, modo
`basic` em vez de `strict`. À medida que a baseline sobe pelo ratchet,
o gate fica mais rigoroso sozinho.
