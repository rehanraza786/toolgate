"""The MCP server Copilot talks to: hot tools directly, everything else via search_tools/call_tool."""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from pathlib import Path

import anyio
import mcp_types as types
from mcp import Client, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.stdio import stdio_server
from mcp.server.subscriptions import InMemorySubscriptionBus, ListenHandler
from mcp.shared.subscriptions import ToolsListChanged
from mcp.shared._httpx_utils import create_mcp_http_client
from mcp.shared.exceptions import MCPError
from mcp_types import CONNECTION_CLOSED

from . import __version__
from .config import (Unresolved, adopt_new, candidate_files, home, is_toolgate, load_env_file, load_jsonc,
                     load_store, resolve, servers_in, watched_signature)
from .usage import Usage, matches, pick_hot

log = logging.getLogger("toolgate")

INSTRUCTIONS = (
    "Only this team's frequently used tools are loaded directly. If none of them fits the task, "
    "call search_tools with a few keywords, then run the match with call_tool."
)

CALL_TOOL = types.Tool(
    name="call_tool",
    description="Run a tool found with search_tools. Pass its exact name and its arguments object.",
    input_schema={
        "type": "object",
        "properties": {"name": {"type": "string"}, "arguments": {"type": "object"}},
        "required": ["name"],
    },
)


def words(s: str) -> list[str]:
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", s or "")
    return re.findall(r"[a-z0-9]+", s.lower())


def trim(s: str | None, n: int) -> str | None:
    if not s or not n or len(s) <= n:
        return s
    return s[: n - 1].rstrip() + "…"


def leaf(e: BaseException) -> BaseException:
    while getattr(e, "exceptions", None):  # unwrap ExceptionGroup
        e = e.exceptions[0]
    return e


def text_result(text: str, error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)], is_error=error)


class Upstream:
    def __init__(self, name: str, cfg: dict, workspace: str, on_tools):
        self.name, self.cfg, self.workspace, self.on_tools = name, cfg, workspace, on_tools
        self.tools: list[types.Tool] = []
        self.client: Client | None = None
        self.error: str | None = None
        self.ready = anyio.Event()
        self._broken = anyio.Event()
        self.scope = anyio.CancelScope()

    def stop(self) -> None:
        self.scope.cancel()

    def _transport(self, c: dict):
        if c["type"] == "stdio":
            env = dict(os.environ)
            if c.get("envFile"):
                env.update(load_env_file(Path(c["envFile"])))
            env.update({k: str(v) for k, v in (c.get("env") or {}).items()})
            cwd = c.get("cwd") or (self.workspace if Path(self.workspace).is_dir() else None)
            return StdioServerParameters(command=c["command"], args=c.get("args", []), env=env, cwd=cwd)
        headers = c.get("headers") or {}
        if c["type"] == "sse":
            return sse_client(c["url"], headers=headers)
        return streamable_http_client(c["url"], http_client=create_mcp_http_client(headers=headers))

    async def run(self) -> None:
        with self.scope:
            await self._loop()

    async def _loop(self) -> None:
        delay = 2
        while True:
            try:
                cfg = resolve(self.cfg, self.workspace)
                async with Client(self._transport(cfg)) as client:
                    tools, cursor = [], None
                    while True:
                        page = await client.list_tools(cursor=cursor)
                        tools += page.tools
                        cursor = page.next_cursor
                        if not cursor:
                            break
                    self.tools, self.client, self.error, delay = tools, client, None, 2
                    self._broken = anyio.Event()
                    self.on_tools(self)
                    self.ready.set()
                    log.info("%s: connected, %d tools", self.name, len(tools))
                    await self._broken.wait()
            except Unresolved as e:
                self.error = str(e)
                log.warning("%s: %s", self.name, e)
                self.ready.set()
                return
            except Exception as e:  # noqa: BLE001 - keep the gateway up whatever one server does
                if self._broken.is_set():  # we asked for this reconnect
                    self.error = None
                    delay = 0.5
                else:
                    e = leaf(e)
                    self.error = f"{type(e).__name__}: {e}"
                log.warning("%s: %s (retrying in %ss)", self.name, self.error, delay)
            finally:
                self.client = None
            self.ready.set()
            await anyio.sleep(delay)
            delay = min(delay * 2, 120)

    async def call(self, tool: str, args: dict) -> types.CallToolResult:
        with anyio.move_on_after(60):
            await self.ready.wait()
        with anyio.move_on_after(20):  # mid-reconnect after a crash
            while self.client is None and self.error is None:
                await anyio.sleep(0.2)
        if not self.client:
            raise RuntimeError(f"server '{self.name}' is not connected ({self.error or 'starting'})")
        try:
            return await self.client.call_tool(tool, args)
        except Exception as e:
            closed = isinstance(e, (anyio.ClosedResourceError, anyio.BrokenResourceError, anyio.EndOfStream, OSError))
            if closed or (isinstance(e, MCPError) and e.code == CONNECTION_CLOSED):
                self.client = None
                self._broken.set()  # run() reconnects; not retried here in case the call had side effects
                raise RuntimeError(f"server '{self.name}' disconnected and is restarting; try again") from e
            raise


class Gateway:
    def __init__(self, store: dict):
        self.settings = store["settings"]
        self.usage = Usage(home() / "usage.db")
        self.cache_file = home() / "tools_cache.json"
        try:
            raw = json.loads(self.cache_file.read_text())
            self.cache = {s: [types.Tool.model_validate(t) for t in ts] for s, ts in raw.items()}
        except Exception:  # noqa: BLE001
            self.cache = {}
        workspace = os.environ.get("TOOLGATE_WORKSPACE", "")
        if not workspace or workspace.startswith("${"):
            workspace = os.getcwd()
        self.ups = {n: Upstream(n, c, workspace, self._save_cache) for n, c in store["servers"].items()}
        self.listed: set[tuple[str, str]] = set()
        self._started = False
        self.bus = InMemorySubscriptionBus()
        self.workspace = workspace
        self.session = None  # last client session, for change notifications outside a request
        self._dirty = False
        self.tg = None

    # ----- catalog -----
    def _save_cache(self, up: Upstream | None = None) -> None:
        if up is not None:
            self.cache[up.name] = up.tools
            self._dirty = self._started  # a new/reconnected server changes the catalog
        data = {s: [t.model_dump(by_alias=True, exclude_none=True, mode="json") for t in ts]
                for s, ts in self.cache.items() if s in self.ups}
        self.cache_file.write_text(json.dumps(data))

    def catalog(self) -> dict[str, tuple[Upstream, types.Tool]]:
        pairs = [(u, t) for u in self.ups.values() for t in (u.tools or self.cache.get(u.name, []))
                 if not matches(u.name, t.name, self.settings["blocked"])]
        seen: dict[str, int] = {}
        for _, t in pairs:
            seen[t.name] = seen.get(t.name, 0) + 1
        out = {}
        for u, t in pairs:
            exposed = t.name if seen[t.name] == 1 else f"{u.name}__{t.name}"
            out[re.sub(r"[^A-Za-z0-9_-]", "_", exposed)[:64]] = (u, t)
        return out

    def hot_keys(self, cat) -> set[tuple[str, str]]:
        s = self.settings
        return pick_hot([(u.name, t.name) for u, t in cat.values()],
                        self.usage.counts(s["window_days"]), s, self.usage.total() > 0)

    def lookup(self, name: str, cat):
        if name in cat:
            return cat[name]
        for sep in ("/", "__", "."):
            if sep in name:
                srv, _, tool = name.partition(sep)
                for u, t in cat.values():
                    if u.name == srv and t.name == tool:
                        return u, t
        hits = [v for v in cat.values() if v[1].name == name]
        return hits[0] if len(hits) == 1 else None

    async def _wait_startup(self) -> None:
        if self._started:
            return
        with anyio.move_on_after(self.settings["startup_wait_seconds"]):
            for u in self.ups.values():
                await u.ready.wait()
        self._started = True

    # ----- search -----
    def search(self, query: str, limit: int) -> dict:
        cat = self.catalog()
        q = words(query)
        if not q:
            listing: dict[str, list[str]] = {}
            for exp, (u, _) in cat.items():
                listing.setdefault(u.name, []).append(exp)
            return {"servers": listing, "hint": "search again with keywords to get input schemas"}
        scored = []
        for exp, (u, t) in cat.items():
            name_w, desc_w, srv_w = set(words(t.name)), set(words(t.description or "")), set(words(u.name))
            score = 0
            for w in q:
                if w in name_w:
                    score += 3
                elif len(w) >= 3 and any(n.startswith(w) or w.startswith(n) for n in name_w if len(n) >= 3):
                    score += 2
                if w in desc_w:
                    score += 1
                if w in srv_w:
                    score += 2
            if score:
                scored.append((score, exp, u, t))
        scored.sort(key=lambda x: -x[0])
        return {"tools": [{"name": exp, "server": u.name, "description": trim(t.description, 400),
                           "inputSchema": t.input_schema} for _, exp, u, t in scored[:limit]],
                "next": "run one with call_tool(name, arguments)"}

    def search_tool(self) -> types.Tool:
        counts: dict[str, int] = {}
        for u, _ in self.catalog().values():
            counts[u.name] = counts.get(u.name, 0) + 1
        servers = ", ".join(f"{n} ({c})" for n, c in counts.items()) or "none connected yet"
        return types.Tool(
            name="search_tools",
            description=(f"Find tools that are not loaded directly. Servers: {servers}. "
                         "Returns matching tool names with input schemas; run one with call_tool. "
                         "An empty query lists every tool name."),
            input_schema={"type": "object", "properties": {
                "query": {"type": "string", "description": "keywords, e.g. 'merge request comments'"},
                "limit": {"type": "integer", "default": 8}}},
        )

    # ----- MCP handlers -----
    async def on_list_tools(self, ctx, params) -> types.ListToolsResult:
        self.session = ctx.session
        await self._wait_startup()
        cat = self.catalog()
        hot = self.hot_keys(cat)
        n = self.settings["max_description_chars"]
        tools = [t.model_copy(update={"name": exp, "description": trim(t.description, n)})
                 for exp, (u, t) in cat.items() if (u.name, t.name) in hot]
        self.listed = {(u.name, t.name) for u, t in cat.values() if (u.name, t.name) in hot}
        log.info("listing %d of %d tools directly", len(tools), len(cat))
        return types.ListToolsResult(tools=tools + [self.search_tool(), CALL_TOOL])

    async def on_call_tool(self, ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
        self.session = ctx.session
        args = params.arguments or {}
        if params.name == "search_tools":
            await self._wait_startup()
            return text_result(json.dumps(self.search(str(args.get("query", "")), int(args.get("limit") or 8))))
        if params.name == "call_tool":
            target, targs, via = str(args.get("name", "")), args.get("arguments") or {}, "fallback"
            if isinstance(targs, str):
                try:
                    targs = json.loads(targs)
                except ValueError:
                    return text_result("'arguments' must be a JSON object", error=True)
        else:
            target, targs, via = params.name, args, "direct"

        cat = self.catalog()
        hit = self.lookup(target, cat)
        if not hit:
            return text_result(f"Unknown tool '{target}'. Use search_tools to find the right name.", error=True)
        up, tool = hit
        start = time.monotonic()
        try:
            result = await up.call(tool.name, targs)
            ok = not result.is_error
        except Exception as e:  # noqa: BLE001
            result, ok = text_result(f"{up.name}/{tool.name} failed: {e}", error=True), False
        self.usage.record(up.name, tool.name, ok, int((time.monotonic() - start) * 1000), via)

        key = (up.name, tool.name)
        if key not in self.listed and key in self.hot_keys(cat):
            self.listed.add(key)
            await self.announce()
        return result

    # ----- servers added / removed while running -----
    def reconcile(self, store: dict) -> bool:
        self.settings = store["settings"]
        want, changed = store["servers"], False
        for name in list(self.ups):
            if name not in want or want[name] != self.ups[name].cfg:
                self.ups.pop(name).stop()
                self.cache.pop(name, None)
                log.info("%s: removed", name)
                changed = True
        for name, cfg in want.items():
            if name not in self.ups:
                up = self.ups[name] = Upstream(name, cfg, self.workspace, self._save_cache)
                self.tg.start_soon(up.run)
                log.info("%s: added", name)
                changed = True
        if changed:
            self._save_cache()
        return changed

    async def watch(self, interval: float = 3) -> None:
        last = watched_signature()
        while True:
            await anyio.sleep(interval)
            sig = watched_signature()
            if sig != last:
                store = load_store()
                if store["settings"]["auto_adopt"]:
                    names = adopt_new(store)
                    if names:
                        log.info("adopted from Copilot config: %s", ", ".join(names))
                if self.reconcile(load_store()):
                    self._dirty = True
                last = watched_signature()  # include our own writes
            if self._dirty:
                self._dirty = False
                await self.announce()

    async def announce(self) -> None:
        """Tell the client to re-list tools. Clients that ignore this pick it up on restart."""
        log.info("tool list changed")
        try:
            await self.bus.publish(ToolsListChanged())  # 2026-07-28+ clients (subscriptions/listen)
        except Exception:  # noqa: BLE001
            pass
        try:
            if self.session is not None:
                await self.session.send_tool_list_changed()  # older clients
        except Exception:  # noqa: BLE001
            pass


def _warn_direct_servers(store: dict) -> None:
    for path, kind, _ in candidate_files():
        try:
            names = [n for n, e in servers_in(load_jsonc(path), kind).items()
                     if isinstance(e, dict) and not is_toolgate(e)]
        except Exception:  # noqa: BLE001
            continue
        new = [n for n in names if n not in store["servers"]]
        if new:
            log.info("not managed yet (run `toolgate import --rewrite`): %s in %s", ", ".join(new), path)


async def serve() -> None:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="[toolgate] %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    for k, v in load_env_file(home() / ".env").items():
        os.environ.setdefault(k, v)
    store = load_store()
    if store["settings"]["auto_adopt"] and adopt_new(store):
        store = load_store()
    _warn_direct_servers(store)
    gw = Gateway(store)
    server = Server("toolgate", version=__version__, instructions=INSTRUCTIONS,
                    on_list_tools=gw.on_list_tools, on_call_tool=gw.on_call_tool,
                    on_subscriptions_listen=ListenHandler(gw.bus))
    async with anyio.create_task_group() as tg:
        gw.tg = tg
        for up in gw.ups.values():
            tg.start_soon(up.run)
        tg.start_soon(gw.watch)
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options(NotificationOptions(tools_changed=True)))
        tg.cancel_scope.cancel()
