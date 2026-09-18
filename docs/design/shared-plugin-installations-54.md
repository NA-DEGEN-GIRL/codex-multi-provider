# Shared plugin installations

The manager watches registered local homes and profiles for installed plugins.
It publishes shared plugin packages through a manager-owned local marketplace,
`codex-manager-shared`, and materializes them before a new profile starts. The
manager checks for changes every two seconds; an unchanged inventory causes no
config, package or signal writes. Opening the synced original app also performs
a reconciliation. Continuous propagation requires the manager service to run.

Copying remote installations into another account's remote cache is insufficient:
native account reconciliation can delete packages absent from that account's
remote installation list. The separate local marketplace survives that cleanup.
A profile with its own native installation uses it instead of a duplicate mirror.

Plugin manifests, app declarations, hooks and local capabilities remain intact.
Account authentication, OAuth grants, mutable plugin data and remote installation
markers remain outside the mirror. The receiving account still needs its own
service connection and normal native authorization/trust checks. Sharing an
installation does not authorize another account to access its services.

The source installation's removal unpublishes its shared mirrors. Tombstones
prevent old copies from silently recreating it. A profile's explicit disable or
mirror removal is retained locally. Version updates and detected same-version
republication refresh the mirror; idle scans do not recopy the package tree.

Writes and recursive cleanup are confined to manager-owned directories. Linked
paths are rejected, damaged registry state is preserved, and unrelated TOML
settings are retained. The sync revision is published after package/config
changes commit. Running desktop clients use that revision to reload plugin and
skill caches and invalidate the native queries without navigating or reloading
the conversation window.

Validation uses an isolated native app-server plus a hidden desktop with a
synthetic plugin. The desktop fixture passed 25 checks: install, disable and
remove each caused one native cache reload and one renderer query invalidation;
the synthetic skill appeared and disappeared accordingly. The draft, document
load identity and route remained unchanged. These checks use native query
subscriptions rather than automating the marketplace's install/uninstall UI.
Account-backed GitHub authorization is not exercised by these isolated tests.
