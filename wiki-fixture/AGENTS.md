# wiki-fixture / AGENTS.md (noise)

This file deliberately lives **outside** the `wiki/` subtree so that local
runs mirror the production volume layout (init container clones the full
`leoferolive-wiki` repo into the volume — `AGENTS.md`, `README.md`, `raw/`
all sit next to the real wiki root).

The retriever MUST ignore this file. If you ever see it cited as a wiki
page, the scoping in `app/wiki_loader.py` is broken.
