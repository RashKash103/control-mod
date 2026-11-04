"""Writes the fireworks-tech-graph specs for the README workflow diagram (light + dark).

Render with https://github.com/yizhiyanhua-ai/fireworks-tech-graph (see docs/diagrams/README.md).
"""

import json
from pathlib import Path

HERE = Path(__file__).parent

LIGHT = {
    "style": 1, "suffix": "light",
    "game": ("#eff6ff", "#3b82f6"), "cli": ("#fff7ed", "#f97316"), "mod": ("#ecfdf5", "#10b981"),
    "blend": ("#faf5ff", "#8b5cf6"),
}
DARK = {
    "style": 2, "suffix": "dark",
    "game": ("#0f172a", "#38bdf8"), "cli": ("#111827", "#f97316"), "mod": ("#0f172a", "#34d399"),
    "blend": ("#111827", "#a855f7"),
}


def spec(t: dict) -> dict:
    def node(id_, kind, x, y, label, sub, colors, w=180, h=60, **extra):
        fill, stroke = t[colors]
        n = {"id": id_, "kind": kind, "x": x, "y": y, "width": w, "height": h, "label": label,
             "sublabel": sub, "fill": fill, "stroke": stroke, "flat": True}
        n.update(extra)
        return n

    def arrow(id_, s, d, sp, dp, flow, label, **extra):
        a = {"id": id_, "source": s, "target": d, "source_port": sp, "target_port": dp, "flow": flow, "label": label}
        a.update(extra)
        return a

    return {
        "schema_version": 1,
        "mode": "architecture",
        "text_policy": "strict",
        "template_type": "architecture",
        "style": t["style"],
        "width": 960,
        "height": 690,
        "title": "control-mod workflow",
        "subtitle": "game archives to Blender and back, installed as removable patch archives",
        "containers": [
            {"id": "game", "x": 40, "y": 110, "width": 880, "height": 130, "label": "Game"},
            {"id": "cli", "x": 40, "y": 280, "width": 880, "height": 130, "label": "CLI"},
            {"id": "mod", "x": 40, "y": 450, "width": 880, "height": 130, "label": "Mod"},
        ],
        "nodes": [
            node("archives", "rect", 70, 150, "Game archives", ".bin/.rmdp + .packmeta", "game"),
            node("control", "rect", 395, 150, "Control", "patches override originals", "game"),
            node("patch", "rect", 720, 150, "Patch archives", "ep100-001-* + .packmeta", "game"),
            node("add", "rect", 70, 320, "add", "export model + textures", "cli"),
            node("build", "rect", 395, 320, "build", "check + rebuild files", "cli"),
            node("install", "rect", 720, 320, "install", "merge enabled mods", "cli"),
            node("package", "rect", 70, 490, "Model package", "PNG textures, geometry", "mod"),
            node("blend", "double_rect", 395, 490, ".blend", "you edit here", "blend"),
            node("built", "rect", 720, 490, "Mod build", "models + textures", "mod"),
        ],
        "arrows": [
            arrow("read", "archives", "add", "bottom", "top", "read", "read"),
            arrow("export", "add", "package", "bottom", "top", "data", "export"),
            arrow("import", "package", "blend", "right", "left", "data", "import"),
            arrow("edit", "blend", "build", "top", "bottom", "control", "save + build"),
            arrow("write", "build", "built", "right", "left", "write", "write"),
            arrow("merge", "built", "install", "top", "bottom", "control", "enable"),
            arrow("install-patch", "install", "patch", "top", "bottom", "write", "write"),
            arrow("load", "patch", "control", "left", "right", "read", "load"),
        ],
        "legend_orientation": "horizontal",
        "legend_x": 48,
        "legend_y": 615,
        "legend_locked": True,
        "legend": [
            {"flow": "read", "label": "read game data"},
            {"flow": "data", "label": "model data"},
            {"flow": "control", "label": "you run"},
            {"flow": "write", "label": "writes files"},
        ],
        "footer": "originals are never modified; uninstall removes only the patch archives control-mod wrote",
        "footer_x": 48,
        "footer_y": 665,
    }


if __name__ == "__main__":
    for t in (LIGHT, DARK):
        out = HERE / f"workflow-{t['suffix']}.json"
        out.write_text(json.dumps(spec(t), indent=1) + "\n")
        print(out)
