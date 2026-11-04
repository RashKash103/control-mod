"""Workspace configuration (``ctlmod.toml``) and mod-manager state (``.ctlmod/state.json``)."""

from __future__ import annotations

import json
import os
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_NAME = "ctlmod.toml"
DEFAULT_BLENDER_GLOBS = [
    r"C:\Program Files\Blender Foundation\Blender*\blender.exe",
    r"C:\Program Files (x86)\Steam\steamapps\common\Blender\blender.exe",
]


def find_workspace(start: Path | None = None) -> Path:
    """Nearest folder (from ``start`` upwards) containing ``ctlmod.toml``, else ``start``."""
    start = (start or Path.cwd()).resolve()
    env = os.environ.get("CTLMOD_WORKSPACE")
    if env:
        return Path(env).resolve()
    for p in [start, *start.parents]:
        if (p / CONFIG_NAME).exists():
            return p
    return start


def find_blender() -> Path | None:
    env = os.environ.get("CTLMOD_BLENDER")
    if env and Path(env).exists():
        return Path(env)
    which = shutil.which("blender")
    if which:
        return Path(which)
    import glob

    for pattern in DEFAULT_BLENDER_GLOBS:
        hits = sorted(glob.glob(pattern), reverse=True)  # newest version first
        if hits:
            return Path(hits[0])
    return None


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    return json.dumps(str(v))  # JSON string escaping is valid TOML basic-string escaping


def write_toml(path: Path, data: dict, header: str = "") -> None:
    lines = [header] if header else []
    tables = {k: v for k, v in data.items() if isinstance(v, dict)}
    for k, v in data.items():
        if not isinstance(v, dict):
            lines.append(f"{k} = {_toml_value(v)}")
    for name, table in tables.items():
        lines.append(f"\n[{name}]")
        for k, v in table.items():
            if isinstance(v, dict):
                continue
            lines.append(f"{json.dumps(k) if not k.replace('_', '').replace('-', '').isalnum() else k} = {_toml_value(v)}")
        for k, v in table.items():
            if isinstance(v, dict):
                lines.append(f"\n[{name}.{json.dumps(k) if not k.replace('_', '').replace('-', '').isalnum() else k}]")
                for kk, vv in v.items():
                    lines.append(f"{kk} = {_toml_value(vv)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@dataclass
class Config:
    workspace: Path
    game_dir: Path | None = None
    blender: Path | None = None
    patch_number: int = 1  # ep100-000-x is overridden by ep100-<patch_number>-x

    @property
    def mods_dir(self) -> Path:
        return self.workspace / "mods"

    @property
    def state_dir(self) -> Path:
        return self.workspace / ".ctlmod"

    @classmethod
    def load(cls, workspace: Path | None = None) -> "Config":
        ws = find_workspace(workspace)
        cfg = cls(ws)
        p = ws / CONFIG_NAME
        if p.exists():
            data = tomllib.loads(p.read_text(encoding="utf-8"))
            if data.get("game_dir"):
                cfg.game_dir = Path(data["game_dir"])
            if data.get("blender"):
                cfg.blender = Path(data["blender"])
            cfg.patch_number = int(data.get("patch_number", 1))
        return cfg

    def save(self) -> None:
        data = {
            "game_dir": str(self.game_dir or ""),
            "blender": str(self.blender or ""),
            "patch_number": self.patch_number,
        }
        write_toml(self.workspace / CONFIG_NAME, data, "# ctlmod workspace configuration")

    def require_game(self) -> Path:
        from .gamedata import find_game_dir

        g = self.game_dir or find_game_dir()
        if not g or not (Path(g) / "data_packfiles").is_dir():
            raise SystemExit("Game directory not configured. Run: ctlmod setup --game <path to Control>")
        return Path(g)

    def require_blender(self) -> Path:
        b = self.blender or find_blender()
        if not b or not Path(b).exists():
            raise SystemExit("Blender not found. Run: ctlmod setup --blender <path to blender.exe>")
        return Path(b)


@dataclass
class State:
    """Load order and enabled flags of mods, plus what is currently installed."""

    path: Path
    order: list[str] = field(default_factory=list)
    enabled: dict[str, bool] = field(default_factory=dict)
    installed: dict = field(default_factory=dict)

    @classmethod
    def load(cls, cfg: Config) -> "State":
        p = cfg.state_dir / "state.json"
        st = cls(p)
        if p.exists():
            d = json.loads(p.read_text())
            st.order = d.get("order", [])
            st.enabled = d.get("enabled", {})
            st.installed = d.get("installed", {})
        return st

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"order": self.order, "enabled": self.enabled, "installed": self.installed}, indent=1))

    def sync(self, mod_ids: list[str]) -> None:
        """Add new mods (enabled, lowest priority last) and forget deleted ones."""
        for m in mod_ids:
            if m not in self.order:
                self.order.append(m)
                self.enabled.setdefault(m, True)
        self.order = [m for m in self.order if m in mod_ids]
        self.enabled = {k: v for k, v in self.enabled.items() if k in mod_ids}
