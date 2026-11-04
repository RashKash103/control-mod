# Diagrams

The diagrams are made with [fireworks-tech-graph](https://github.com/yizhiyanhua-ai/fireworks-tech-graph),
in a light (Style 1, Flat Icon) and a dark (Style 2, Dark Terminal) version. The README
picks the one matching the reader's GitHub theme with `<picture>` /
`prefers-color-scheme`.

| file | what |
|---|---|
| `make_workflow.py` | writes the specs (`workflow-light.json`, `workflow-dark.json`) — edit this, not the JSON |
| `workflow-*.svg` | rendered diagrams (committed) |

To regenerate, with a checkout of fireworks-tech-graph at `$FTG`:

```bash
uv run python docs/diagrams/make_workflow.py
```

```bash
for v in light dark; do uv run python -I "$FTG/scripts/fireworks.py" render architecture docs/diagrams/workflow-$v.json docs/diagrams/workflow-$v.svg && uv run python -I "$FTG/scripts/fireworks.py" check docs/diagrams/workflow-$v.svg; done
```

The specs use `"text_policy": "strict"`, so rendering fails instead of truncating a label.
