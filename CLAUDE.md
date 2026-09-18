# CLAUDE.md — chat-api

## Quality gate (obrigatório antes de qualquer commit)

Antes de criar commit nesta repo, **rodar**:

    make quality

(ou `bash scripts/quality.sh` se `make` não estiver disponível).

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
