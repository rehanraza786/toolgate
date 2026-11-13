"""toolgate CLI: import | serve | stats"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .config import home, load_store, remove_server, run_import


def stats() -> None:
    from .usage import Usage, pick_hot

    store = load_store()
    s = store["settings"]
    usage = Usage(home() / "usage.db")
    counts = usage.counts(s["window_days"])
    try:
        cache = json.loads((home() / "tools_cache.json").read_text())
    except FileNotFoundError:
        cache = {}
    from .usage import matches
    cache = {srv: [t for t in ts if not matches(srv, t["name"], s["blocked"])]
             for srv, ts in cache.items() if srv in store["servers"]}
    keys = [(srv, t["name"]) for srv, ts in cache.items() for t in ts]
    hot = pick_hot(keys, counts, s, usage.total() > 0)

    def tokens(tools) -> int:
        return len(json.dumps(tools)) // 4

    all_tools = [t for ts in cache.values() for t in ts]
    hot_tools = [t for srv, ts in cache.items() for t in ts if (srv, t["name"]) in hot]
    print(f"servers: {len(store['servers'])}   tools known: {len(all_tools)}   calls logged: {usage.total()}")
    print(f"tool definitions per request: ~{tokens(all_tools):,} tokens without toolgate, "
          f"~{tokens(hot_tools) + 350:,} with it\n")
    print(f"{'calls':>5}  {'hot':<3}  tool   (last {s['window_days']} days)")
    for (srv, tool), n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"{n:>5}  {'yes' if (srv, tool) in hot else '':<3}  {srv}/{tool}")
    pinned_only = sorted(k for k in hot if k not in counts)
    for srv, tool in pinned_only:
        print(f"{0:>5}  yes  {srv}/{tool}  (pinned)")
    missing = [n for n in store["servers"] if n not in cache]
    if missing:
        print(f"\nnot connected yet (no tool list cached): {', '.join(missing)}")


def main() -> None:
    p = argparse.ArgumentParser(prog="toolgate", description="Usage-aware MCP proxy for Copilot")
    sub = p.add_subparsers(dest="cmd", required=True)
    imp = sub.add_parser("import", help="find MCP servers in your Copilot configs and manage them with toolgate")
    imp.add_argument("--from", dest="extra", action="append", default=[], type=Path, help="another mcp.json to read")
    imp.add_argument("--rewrite", action="store_true", help="replace them with a single toolgate entry (keeps .bak)")
    imp.add_argument("--dry-run", action="store_true", help="only show what would be imported")
    sub.add_parser("serve", help="run the MCP server (Copilot starts this)")
    sub.add_parser("stats", help="show usage and which tools are exposed")
    sub.add_parser("list", help="list managed servers")
    rm = sub.add_parser("remove", help="stop managing a server (a running toolgate drops it within seconds)")
    rm.add_argument("name")
    a = p.parse_args()

    if a.cmd == "import":
        run_import(a.extra, a.rewrite, a.dry_run)
    elif a.cmd == "serve":
        import anyio

        from .gateway import serve
        anyio.run(serve)
    elif a.cmd == "list":
        store = load_store()
        try:
            cache = json.loads((home() / "tools_cache.json").read_text())
        except FileNotFoundError:
            cache = {}
        for name, cfg in store["servers"].items():
            where = cfg.get("url") or " ".join([cfg.get("command", "")] + cfg.get("args", []))
            tools = f"{len(cache[name])} tools" if name in cache else "not connected yet"
            print(f"{name:<24} {tools:<18} {where}")
    elif a.cmd == "remove":
        print(f"removed {a.name}" if remove_server(a.name) else f"no server named {a.name}")
    else:
        stats()


if __name__ == "__main__":
    main()
