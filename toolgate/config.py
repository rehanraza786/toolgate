"""Find Copilot MCP configs, import their servers, resolve ${...} variables."""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path

DEFAULT_SETTINGS = {
    "max_hot": 25,          # most tools exposed directly (pinned ones always are)
    "min_calls": 2,         # calls within the window before a tool is promoted
    "window_days": 14,      # usage older than this is ignored
    "pinned": [],           # "server/tool" globs always exposed, e.g. "lean-ctx/*"
    "blocked": [],          # "server/tool" globs never exposed or callable
    "bootstrap": "search",  # no usage yet: "search" = fallback only, "all" = expose everything
    "max_description_chars": 0,  # 0 = keep full tool descriptions
    "startup_wait_seconds": 20,
    "auto_adopt": True,     # take over servers added to a Copilot config that already lists toolgate
}


def home() -> Path:
    p = Path(os.environ.get("TOOLGATE_HOME", Path.home() / ".toolgate"))
    p.mkdir(parents=True, exist_ok=True)
    return p


def servers_file() -> Path:
    return home() / "servers.json"


# ---------- JSON with comments (VS Code files) ----------

def load_jsonc(path: Path) -> dict:
    text = path.read_text(encoding="utf-8-sig")
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1]); i += 1
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True; out.append(c)
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
            continue
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end == -1 else end + 2
            continue
        else:
            out.append(c)
        i += 1
    cleaned = re.sub(r",(\s*[}\]])", r"\1", "".join(out))
    return json.loads(cleaned) if cleaned.strip() else {}


# ---------- where Copilot keeps MCP configs ----------

def _vscode_user_dirs() -> list[Path]:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library/Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return [base / app / "User" for app in ("Code", "Code - Insiders", "VSCodium", "Cursor")]


def candidate_files(cwd: Path | None = None) -> list[tuple[Path, str, bool]]:
    """(path, kind, user_level). kind: vscode | mcpServers | settings."""
    cwd = cwd or Path.cwd()
    found: list[tuple[Path, str, bool]] = []
    for d in _vscode_user_dirs():
        found.append((d / "mcp.json", "vscode", True))
        found += [(p, "vscode", True) for p in sorted(d.glob("profiles/*/mcp.json"))]
        found.append((d / "settings.json", "settings", True))
    found.append((Path.home() / ".copilot" / "mcp-config.json", "mcpServers", True))   # Copilot CLI
    found.append((Path.home() / ".mcp.json", "mcpServers", True))                      # Visual Studio
    jb = (Path(os.environ.get("LOCALAPPDATA", "")) if sys.platform == "win32"
          else Path.home() / ".config") / "github-copilot" / "intellij" / "mcp.json"
    found.append((jb, "vscode", True))                                               # JetBrains
    found.append((cwd / ".vscode" / "mcp.json", "vscode", False))                     # workspace
    found.append((cwd / ".mcp.json", "mcpServers", False))
    return [f for f in found if f[0].is_file()]


def servers_in(data: dict, kind: str) -> dict:
    if kind == "settings":
        return (data.get("mcp") or {}).get("servers") or {}
    return data.get("servers") or data.get("mcpServers") or {}


def is_toolgate(entry: dict) -> bool:
    blob = " ".join([str(entry.get("command", ""))] + [str(a) for a in entry.get("args", [])])
    return "toolgate" in blob


def normalize(entry: dict) -> dict:
    e = dict(entry)
    e.pop("tools", None)  # Copilot CLI allowlist; toolgate decides instead
    t = e.get("type")
    if "command" in e:
        e["type"] = "stdio"
    elif t not in ("sse", "http"):
        e["type"] = "http"
    return e


# ---------- import ----------

def load_store() -> dict:
    f = servers_file()
    store = json.loads(f.read_text()) if f.exists() else {}
    store.setdefault("servers", {})
    store.setdefault("inputs", [])
    store.setdefault("settings", {})
    for k, v in DEFAULT_SETTINGS.items():
        store["settings"].setdefault(k, v)
    return store


def save_store(store: dict) -> None:
    servers_file().write_text(json.dumps(store, indent=2))


def toolgate_entry(input_ids: list[str]) -> dict:
    entry = {"type": "stdio", "command": sys.executable, "args": ["-m", "toolgate", "serve"]}
    env = {f"TOOLGATE_INPUT_{i}": "${input:" + i + "}" for i in input_ids}
    env["TOOLGATE_WORKSPACE"] = "${workspaceFolder}"
    entry["env"] = env
    return entry


def run_import(extra: list[Path], rewrite: bool, dry_run: bool) -> None:
    store = load_store()
    sources = candidate_files() + [(p, "vscode", False) for p in extra]
    input_defs = {i["id"]: i for i in store["inputs"] if "id" in i}
    moved: dict[Path, tuple[str, dict, list[str], bool]] = {}
    added = 0

    for path, kind, user_level in sources:
        try:
            data = load_jsonc(path)
        except Exception as e:  # noqa: BLE001
            print(f"  skip {path}: {e}")
            continue
        servers = servers_in(data, kind)
        names = []
        for name, entry in servers.items():
            if not isinstance(entry, dict) or is_toolgate(entry):
                continue
            names.append(name)
            if name in store["servers"]:
                continue
            store["servers"][name] = normalize(entry)
            added += 1
            print(f"  + {name:<24} from {path}")
        for i in data.get("inputs", []) if kind != "settings" else []:
            input_defs.setdefault(i.get("id"), i)
        if names:
            moved[path] = (kind, data, names, user_level)

    store["inputs"] = list(input_defs.values())
    print(f"\n{added} new server(s); {len(store['servers'])} total in {servers_file()}")
    if dry_run:
        return
    save_store(store)
    if not rewrite:
        print("\nNext: run `toolgate import --rewrite` to replace them with toolgate in your Copilot config,")
        print("or add this server entry yourself:\n")
        print(json.dumps({"toolgate": toolgate_entry([i for i in input_defs])}, indent=2))
        return
    _rewrite(moved)


def _referenced_inputs(obj) -> set[str]:
    return set(re.findall(r"\$\{input:([^}]+)\}", json.dumps(obj)))


def _rewrite(moved: dict) -> None:
    if not moved:
        print("\nNothing to rewrite: your Copilot configs only list toolgate.")
        return
    store = load_store()
    needed = sorted(_referenced_inputs(store["servers"]))
    defs = [i for i in store["inputs"] if i.get("id") in needed]
    user_vscode_done = user_cli_done = False

    for path, (kind, data, names, is_user) in moved.items():
        if kind == "settings":
            print(f"  ! {path}: remove the 'mcp.servers' block by hand ({', '.join(names)})")
            continue
        backup = path.with_suffix(path.suffix + ".toolgate.bak")
        if not backup.exists():
            shutil.copy2(path, backup)
        key = "servers" if "servers" in data else "mcpServers"
        for n in names:
            data[key].pop(n, None)
        if is_user and key == "servers" and not user_vscode_done:
            data[key]["toolgate"] = toolgate_entry(needed)
            ids = {i.get("id") for i in data.get("inputs", [])}
            data["inputs"] = data.get("inputs", []) + [d for d in defs if d.get("id") not in ids]
            user_vscode_done = True
        elif is_user and key == "mcpServers" and not user_cli_done:
            e = toolgate_entry([]); e["env"].pop("TOOLGATE_WORKSPACE"); e["tools"] = ["*"]
            data[key]["toolgate"] = e
            user_cli_done = True
        path.write_text(json.dumps(data, indent=2))
        print(f"  ~ rewrote {path} (backup: {backup.name})")

    if not user_vscode_done and any(m[0] == "vscode" for m in moved.values()):
        target = next((d for d in _vscode_user_dirs() if d.is_dir()), _vscode_user_dirs()[0]) / "mcp.json"
        data = load_jsonc(target) if target.exists() else {}
        data.setdefault("servers", {})["toolgate"] = toolgate_entry(needed)
        ids = {i.get("id") for i in data.get("inputs", [])}
        data["inputs"] = data.get("inputs", []) + [d for d in defs if d.get("id") not in ids]
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            shutil.copy2(target, target.with_suffix(".json.toolgate.bak"))
        target.write_text(json.dumps(data, indent=2))
        print(f"  ~ added toolgate to {target}")
    print("\nDone. Restart MCP servers in Copilot (Command Palette → MCP: List Servers → toolgate → Restart).")


# ---------- live adoption of servers added later ----------

def managed_files() -> list[Path]:
    """User-level Copilot configs that already list toolgate; new servers there get adopted."""
    out = []
    for path, kind, user_level in candidate_files():
        if not user_level or kind == "settings":
            continue
        try:
            entries = servers_in(load_jsonc(path), kind)
        except Exception:  # noqa: BLE001
            continue
        if any(isinstance(e, dict) and is_toolgate(e) for e in entries.values()):
            out.append(path)
    return out


def adopt_new(store: dict) -> list[str]:
    """Move servers that appeared next to toolgate into servers.json. Returns their names."""
    adopted = []
    for path in managed_files():
        try:
            data = load_jsonc(path)
        except Exception:  # noqa: BLE001 - file mid-save; next poll retries
            continue
        key = "servers" if "servers" in data else "mcpServers"
        new = {n: e for n, e in data[key].items() if isinstance(e, dict) and not is_toolgate(e)}
        if not new:
            continue
        backup = path.with_suffix(path.suffix + ".toolgate.bak")
        if not backup.exists():
            shutil.copy2(path, backup)
        for name, entry in new.items():
            store["servers"][name] = normalize(entry)
            data[key].pop(name)
            adopted.append(name)
        if key == "servers":  # VS Code: pass any ${input:...} the new server needs through to toolgate
            tg = next(e for e in data[key].values() if is_toolgate(e))
            env = tg.setdefault("env", {})
            for i in sorted(_referenced_inputs(new)):
                env.setdefault(f"TOOLGATE_INPUT_{i}", "${input:" + i + "}")
            ids = {i.get("id") for i in store["inputs"]}
            store["inputs"] += [i for i in data.get("inputs", []) if i.get("id") not in ids]
        path.write_text(json.dumps(data, indent=2))
    if adopted:
        save_store(store)
    return adopted


def remove_server(name: str) -> bool:
    store = load_store()
    if store["servers"].pop(name, None) is None:
        return False
    save_store(store)
    return True


def watched_signature() -> tuple:
    paths = [servers_file()] + [p for p, kind, user in candidate_files() if user and kind != "settings"]
    return tuple((str(p), p.stat().st_mtime_ns) for p in paths if p.exists())


# ---------- variable resolution at serve time ----------

class Unresolved(Exception):
    pass


def resolve(obj, workspace: str):
    if isinstance(obj, dict):
        return {k: resolve(v, workspace) for k, v in obj.items()}
    if isinstance(obj, list):
        return [resolve(v, workspace) for v in obj]
    if not isinstance(obj, str):
        return obj

    def sub(m: re.Match) -> str:
        kind, _, arg = m.group(1).partition(":")
        if kind == "env":
            return os.environ.get(arg, "")
        if kind == "input":
            for k in (f"TOOLGATE_INPUT_{arg}", arg, arg.upper().replace("-", "_")):
                v = os.environ.get(k)
                if v and not v.startswith("${"):
                    return v
            raise Unresolved(f"needs input '{arg}': restart toolgate in Copilot so it can prompt for it "
                             f"(or set TOOLGATE_INPUT_{arg} in ~/.toolgate/.env)")
        if kind == "workspaceFolder":
            return workspace
        if kind == "userHome":
            return str(Path.home())
        if kind == "pathSeparator":
            return os.sep
        return m.group(0)

    return re.sub(r"\$\{([^}]+)\}", sub, obj)


def load_env_file(path: Path) -> dict[str, str]:
    env = {}
    if path.is_file():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env
