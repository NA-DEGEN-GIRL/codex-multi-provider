# Large SSH responses and transport diagnostics (revision 83)

## Failure mode

The account-binding WebSocket bridge allowed only 32 MiB per message, while the
native app-server client accepts 128 MiB (`app-server-client/src/remote.rs`).
Loading a large history response could therefore end an authenticated SSH
transport with exit code 125. The desktop reconnected and could request the same
response again. This is distinct from the SSH maintenance gates fixed in 82.

A read-only probe of an affected paginated task reproduced the transport
failure with `thread/turns/list(limit=8, sortDirection=desc, itemsView=full)`:
the response frame was 52,499,329 bytes, exceeding the bridge's 32 MiB limit but
within the native client's 128 MiB. This is the request shape used by the
renderer refresh. The original live trace did not retain the failed method, so
the probe establishes a reproducible failure, not an exact request trace.

A separate full-history probe (`thread/read(includeTurns=true)`) announced a
1,079,648,643-byte response. Such a response remains unsupported by the native
client and must not be confused with the bounded turn-page failure. The real
desktop regression uses native pagination and verifies zero full-history reads.
Metadata-only reads and turn/item pages also succeeded on the affected task.
No rollout contents or credentials were retained in the diagnostic report.

The output writer also had a 64 MiB queue limit. Raising the parser limit alone
would still reject one legal response larger than the writer's entire budget.

## Change

- Use a shared 128 MiB message limit, preserving configurable smaller bounds,
  cumulative fragment validation, authentication filtering and protocol checks.
- Bound each writer to two maximum frames including headers. This admits a legal
  large response without introducing unbounded queues or blocking under the
  protocol lock when the opposite direction still needs to drain.
- Avoid retaining an unnecessary copy of large data frames during decoding.
- Log the first transport failure with an allowlisted stage, reason and error
  type, plus a bounded numeric errno where available. Never log message bodies,
  credentials, arbitrary exception text or WebSocket payload bytes.

The change does not modify histories or restart remote listeners. A new local
SSH adapter process must load the updated files before the fix takes effect.

## Verification

The real Windows SSH bootstrap/pump fixture now authenticates, transfers a
65 MiB response intact, successfully answers a following request on the same
connection, then exits normally. It crosses both former limits. Codec tests
cover oversized headers, fragmented messages, smaller configured limits, masked
control frames and credential filtering. Malformed protocol fixtures still
terminate only their own local transport and emit sanitized diagnostics.

For the same affected remote task, the exact 52,499,329-byte turn page failed
under the old 32 MiB bridge and succeeded under the new bridge. A following
metadata read succeeded on that connection, which then stayed open for 20
seconds. The remote listener's PID, start time, boot identity and revision were
unchanged. Only the disposable diagnostic transport was closed.

The isolated real-desktop fixture verifies pagination, live cross-profile
transcript updates, draft preservation, and archive/restore propagation without
external model calls. Its obsolete assertion on the main-process transcript was
replaced with a catalog check: only the renderer owns visible turn history.
