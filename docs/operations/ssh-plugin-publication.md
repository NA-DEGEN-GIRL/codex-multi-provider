# Publish installed portable plugins to an SSH host

The local manager shares installed plugins among Windows profiles. That watcher
does not publish packages to SSH profile homes. Use the explicit publisher after
the local manager has reconciled its installed plugins:

```powershell
python scripts/sync_ssh_plugins.py --host <ssh-host>
python scripts/sync_ssh_plugins.py --host <ssh-host> --execute
```

Replace `<ssh-host>` with a registered SSH alias. The first command prints a
local plan without contacting SSH. The second publishes the same existing
`codex-manager-shared` packages to every active, registered managed profile on
that alias. `--root` can select another manager checkout. Neither command starts
a model turn, installs a service connection, or changes the manager watcher.
This is a one-time publication. Later Windows plugin installations and updates
do not propagate to SSH automatically; rerun the explicit command to publish
them. Remote profiles registered later also require another publication.

The source is the current manager plugin registry and its published package
tree. The command reuses `plugin_sync._copy_bundle` and the existing
`plugin_sync.edit_config` editor. It validates package identities, SHA-256
digests, file paths and size limits before publication. The remote helper checks
every destination against the selected user's managed profile UUID and refuses
symbolic links, hard-linked files, conflicting unowned mirror directories,
platform binaries, credential-state filenames, and Windows paths or commands in
MCP/plugin/hook declarations. Portable script files with a shebang are executable
on Linux. Static validation does not install their language or system libraries.

Packages are published under a content-addressed local marketplace beneath
`~/.local/share/codex-control-center/shared-plugins/codex-manager-shared/`.
Installed cache copies are recorded separately in each managed `CODEX_HOME`.
Native account-installed versions take priority. A native disable declaration
is preserved even when its package cache is absent. Existing manager disable
settings are preserved, and unrelated configuration and authentication files
are retained. Repeating an unchanged publication does not rewrite its files.
Same-version content changes and new versions replace only receipt-owned cache
directories. This command does not uninstall packages omitted from the source.

Serialize publication with other changes to the same remote `config.toml`.
The editor preserves unrelated TOML, validates its parsed result, compares the
original immediately before atomic replacement, and fails on an observed
collision. There is no shared lock with native configuration writers, so a
concurrent writer can still race that final comparison. Publication is atomic
per file or package, not across every profile. If one profile fails, the output
is an error and earlier profiles may already have been updated; inspect the
cause and explicitly rerun the idempotent command.

## Refresh a running runtime

Through the target host's authenticated native app-server connection, request:

```json
{"method":"skills/list","params":{"cwds":[],"forceReload":true}}
```

The runtime clears plugin and skill caches and reloads configuration. Inspect the
returned skill list and errors for the intended remote working directory. If
MCP declarations changed, the native API also supports
`config/mcpServer/reload` with omitted params, followed by
`mcpServerStatus/list`. These requests belong on the existing authenticated
native connection. The manager's restricted lifecycle admin pipe does not allow
them, and the existing desktop plugin revision listener refreshes local hosts
only. Publication by itself therefore does not promise a refreshed remote
settings screen or new tools in an already running assistant turn.

## Account and runtime boundaries

Plugin manifests and `.app.json` declare capabilities; they are not service
credentials. ChatGPT account access is supplied by the existing SSH auth proxy,
which forwards the selected account's current access token and account ID.
Refresh tokens, connector OAuth grants, Windows keychains, plugin data and
login files are not copied. Hosted connector grants remain scoped to that
account, and profiles using another account need that account's own connection.
An empty `mcp_servers` table does not prove hosted apps are unavailable:
`codex_apps` is provided by the authenticated runtime.

`openai-bundled` and `openai-primary-runtime` are intentionally excluded. Install
the correct platform's official runtime dependencies through their native
runtime mechanism. Windows browser/client capabilities and Windows executable
packages cannot be made usable on Linux by copying their directories. Personal
skills and their native model environments are also configured separately.

## Validation

```powershell
python -m unittest discover -s tests -p test_ssh_plugin_install.py -v
```

Fixtures cover native version priority and disabled declarations, content
updates, idempotency, authentication-file preservation, private/binary/path
rejection, unowned cache conflicts, linked destinations, and the transported
helper with the existing config editor. All 11 fixtures passed on Linux via SSH
using disposable temporary homes. The same Windows run passes 10 fixtures and
skips symbolic-link creation when the OS does not grant that capability. These
fixtures do not exercise paid connector operations or generation.
