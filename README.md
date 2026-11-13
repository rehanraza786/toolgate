# toolgate

An MCP proxy for GitHub Copilot that learns which tools you actually use and loads only those.

Copilot connects to toolgate as one MCP server. toolgate connects to all your real servers (GitLab, lean-ctx,
Chrome DevTools, …) and shows Copilot:

- **your hot tools**: the ones you've used at least `min_calls` times in the last `window_days`, up to `max_hot`
- **`search_tools`**: finds any other tool by keywords and returns its input schema
- **`call_tool`**: runs a tool found that way

Every call is logged. When a tool reaches the threshold, toolgate promotes it and tells Copilot to reload the
tool list. Tools that stop being used drop out of the window and go back behind search. On day one only the two
fallback tools are loaded (`bootstrap: "search"`), so your hot set builds up from real use.

## Setup (about 2 minutes)

```bash
pip install git+https://github.com/rehanraza786/toolgate    # Python 3.10+
toolgate import --dry-run         # shows every MCP server it found in your Copilot configs
toolgate import --rewrite         # moves them into toolgate and puts toolgate in their place
```

Then restart MCP servers in Copilot (Command Palette → **MCP: List Servers** → toolgate → **Restart**).

`import` reads:

| Client | File |
|---|---|
| VS Code (Stable, Insiders, profiles) | `<user dir>/mcp.json`, plus `mcp.servers` in `settings.json` (remove that one by hand) |
| Workspace | `.vscode/mcp.json`, `.mcp.json` in the current folder |
| Copilot CLI | `~/.copilot/mcp-config.json` |
| Visual Studio | `~/.mcp.json` |
| JetBrains | `github-copilot/intellij/mcp.json` |

Each rewritten file gets a `.toolgate.bak` backup next to it. Secrets that VS Code prompts for (`${input:...}`)
stay in VS Code: toolgate's entry passes them through as `TOOLGATE_INPUT_<id>` environment variables.
For the Copilot CLI or other clients, put them in `~/.toolgate/.env` (`TOOLGATE_INPUT_gitlab_token=...`).

## Adding and removing servers

- **Add**: add the server the way you normally would (VS Code's **MCP: Add Server**, or edit `mcp.json`) in any
  config that already lists toolgate. Within about 3 seconds toolgate takes the server over, removes it from that
  file so it isn't loaded twice, connects it, and makes its tools searchable. If the new server needs a prompted
  secret, restart toolgate once so VS Code can ask for it.
- **Remove**: `toolgate remove <name>`, or delete it from `~/.toolgate/servers.json`. A running toolgate drops it
  within seconds.
- `toolgate list` shows what's managed. Editing a server's entry in `servers.json` restarts that server.

Workspace `.vscode/mcp.json` files are left alone unless you run `toolgate import --rewrite` yourself, because
they're usually committed for the whole team. Servers moved out of a workspace file run in every workspace.

## Seeing what it does

```bash
toolgate stats   # example output below
```
```
servers: 6   tools known: 141   calls logged: 312
tool definitions per request: ~21,400 tokens without toolgate, ~2,300 with it

calls  hot  tool   (last 14 days)
   58  yes  gitlab/get_merge_request
   31  yes  chrome-devtools/take_screenshot
   ...
```

The token numbers are estimates (about 4 characters per token) from the cached tool definitions.

## Settings (`~/.toolgate/servers.json` → `"settings"`)

| Key | Default | Meaning |
|---|---|---|
| `max_hot` | 25 | most tools loaded directly (pinned ones always are) |
| `min_calls` | 2 | successful calls within the window before a tool is promoted |
| `window_days` | 14 | usage older than this is ignored, so unused tools fall back out |
| `pinned` | `[]` | `"server/tool"` globs that are always loaded, e.g. `"lean-ctx/*"` |
| `blocked` | `[]` | globs that are never loaded or callable, e.g. `"*/delete_*"` |
| `bootstrap` | `"search"` | with no usage yet: `"search"` loads only the fallback tools, `"all"` loads everything |
| `max_description_chars` | 0 | shorten long tool descriptions (0 keeps them whole) |
| `auto_adopt` | true | take over servers added next to toolgate |

Changes are picked up live.

## Updating

```bash
pip install --upgrade git+https://github.com/rehanraza786/toolgate
```

Then restart toolgate in Copilot. Your servers, settings and usage history live in `~/.toolgate` and are kept.

## Notes

- Promotion is announced both ways: with `notifications/tools/list_changed` for older protocol versions, and over
  `subscriptions/listen` for 2026-07-28 clients. A client that ignores both sees new tools after a restart, and
  `call_tool` works in the meantime.
- If two servers expose a tool with the same name, both get prefixed: `gitlab__search`, `lean-ctx__search`.
- A server that crashes is restarted automatically. The call that hit the crash returns an error rather than
  being retried, in case it already had side effects.
- Remote servers that need a browser OAuth login (for example GitLab's hosted MCP) aren't handled by toolgate
  directly. Wrap them in `mcp-remote`: `"command": "npx", "args": ["-y", "mcp-remote", "https://gitlab.com/api/v4/mcp"]`.
  Servers contributed by VS Code extensions aren't in any `mcp.json`; turn those off in Copilot's tools picker.
- Usage is per person (`~/.toolgate/usage.db`). Point `TOOLGATE_HOME` at a shared folder to pool it.
