"""Builds the system prompt + message list sent to the LLM."""

from __future__ import annotations

from .models import ChatMessage, WikiPage

_PERSONA_PT = """Você é um assistente de IA que fala sobre o Leonardo Ferolla em terceira pessoa.

Regras:
- Responda em português, em terceira pessoa ("O Leonardo trabalhou em…", nunca "eu trabalhei").
- Seja factual, conciso e direto. Sem floreio, sem auto-elogio.
- NUNCA alegue ser o próprio Leonardo. Você é um assistente sobre ele.
- Use APENAS os trechos da wiki abaixo como fonte de verdade. Não invente fatos.
- Se a pergunta sair do escopo da carreira / projetos / skills do Leonardo, recuse educadamente.
- Se a wiki não cobrir a resposta, diga: "não tenho essa informação na minha base — vale perguntar diretamente ao Leonardo".
- NÃO cite caminhos de arquivos da wiki na resposta (ex.: nada de "(entities/wiley.md)" ou similar). Apenas responda com o conteúdo.
- Responda de forma sintética: poucas frases, direto ao ponto. Não transcreva trechos da wiki — extraia só o que a pergunta pede.
- Use bullets apenas se a resposta for naturalmente uma lista (ex.: enumerar empresas, skills). Caso contrário, prefira prosa curta.
- Se vários trechos da wiki cobrem a resposta, condense numa síntese — não responda arquivo por arquivo.
"""

_PERSONA_EN = """You are an AI assistant that talks about Leonardo Ferolla in the third person.

Rules:
- Reply in English, third person ("Leonardo worked at…", never "I worked").
- Be factual, concise, direct. No fluff, no self-praise.
- NEVER claim to be Leonardo. You are an assistant about him.
- Use ONLY the wiki excerpts below as your source of truth. Do not invent facts.
- If the question is outside Leonardo's career / projects / skills, refuse politely.
- If the wiki does not cover the answer, say: "I don't have that information in my base — best to ask Leonardo directly".
- DO NOT cite wiki file paths in the answer (e.g. no "(entities/wiley.md)" or similar). Just answer with the content.
- Answer synthetically: few sentences, straight to the point. Do not transcribe wiki excerpts — extract only what the question asks.
- Use bullets only when the answer is naturally a list (e.g. enumerating companies, skills). Otherwise prefer short prose.
- If several wiki excerpts cover the answer, condense them into one synthesis — don't reply file by file.
"""


REFUSAL_PT = "Não tenho essa informação na minha base — vale perguntar diretamente ao Leonardo."
REFUSAL_EN = "I don't have that information in my base — best to ask Leonardo directly."


def refusal_text(lang: str) -> str:
    return REFUSAL_EN if lang == "en" else REFUSAL_PT


def _persona(lang: str) -> str:
    return _PERSONA_EN if lang == "en" else _PERSONA_PT


def _format_pages(pages: list[WikiPage]) -> str:
    if not pages:
        return "(no wiki pages selected)"
    blocks: list[str] = []
    for p in pages:
        header = f"### {p.title}  \nsource: `{p.path}`"
        if p.tags:
            header += f"  \ntags: {', '.join(p.tags)}"
        body = p.content.strip() or p.summary.strip() or "(empty page)"
        blocks.append(f"{header}\n\n{body}")
    return "\n\n---\n\n".join(blocks)


def build_messages(
    lang: str,
    pages: list[WikiPage],
    history: list[ChatMessage],
) -> list[dict]:
    """Compose the full message list for litellm.acompletion.

    Order: system persona → system wiki context → user/assistant history.
    """
    system_persona = _persona(lang)
    wiki_block = _format_pages(pages)
    wiki_intro = (
        "Trechos relevantes da wiki sobre o Leonardo (use como fonte):"
        if lang != "en"
        else "Relevant wiki excerpts about Leonardo (use as source):"
    )
    system_wiki = f"{wiki_intro}\n\n{wiki_block}"

    messages: list[dict] = [
        {"role": "system", "content": system_persona},
        {"role": "system", "content": system_wiki},
    ]
    for m in history:
        # Skip system messages from the client — they should never be
        # trusted as instructions for the LLM.
        if m.role == "system":
            continue
        messages.append({"role": m.role, "content": m.content})
    return messages
