# Persistent development domains and worktrees

Status: code-grounded assessment and proposed design, 2026-10-01. This document
does not mean a domain orchestrator or automatic Git integration has been built.
Existing project grouping, worktrees, files and running tasks are unchanged.

## Intended behavior

A development domain has a stable identity, responsibility, full repository
checkout/worktree, and a persistent **ordinary task** acting as its owner. Main
coordinates work and integration. The user can open the same owner task directly;
Main's requests and the user's instructions belong to that same conversation.
Short-lived subagents remain implementation helpers, not the domain's identity.

For example, one repository may have gameplay, asset tooling and integration
domains, each using a different worktree and branch. Each worktree is a full
checkout, not a copy of only its assigned folder. Responsibility can concern
particular areas, but shared files require coordination. Linked worktrees still
belong under the same Git project; a domain is a separate management concept and
must not revive grouping solely by working-directory name.

Opening a task only views it. It must not silently claim ownership, interrupt its
current run or authorize merging. Changing profile/model changes execution
credentials/settings, not the domain, checkout or canonical task identity.

## What is already implemented

Paths below are relative to the repository root. Assessment includes the existing
uncommitted revisions 98–100 on base `f554498`.

| Need | Existing implementation | Missing behavior |
|---|---|---|
| Continue the same task across profiles | Canonical shared records and `scripts/manager_core/conversation_open.py::open_shortcut`; the shared-record path bypasses legacy ownership handoff | Stable domain identity and responsibility |
| Directly open an owner conversation | `AppTransport.open_conversation` and local/SSH task deep links; shortcuts retain host/thread/source when changing profile | Domain card referencing an ordinary task |
| Notes and user checklists | `manager/service/src/notes.rs`, `TaskKey(host_id, thread_id)`, revision conflicts and recovery drafts | Versioned requirements, decisions and verification evidence |
| Notice changed task records | Record notification hub and `runtime/codex-rs/app-server/src/request_processors/shared_history_resume.rs` reload updated history before idle execution | Semantic domain events and Main's durable cursor |
| Bind a native worktree to an owner | `runtime/codex-rs/worktree/src/lib.rs` and `metadata.rs`, `codex-thread.json` | Domain lifecycle for existing ordinary checkouts as well as managed worktrees |
| Deliver work to an existing ordinary task | Native app-server has `thread/resume`, `turn/start`, `turn/steer` | Narrow manager dispatch API, persistent delivery tracking and admission control |

The manager's `scripts/manager_core/runtime_admin.py` deliberately allowlists
lifecycle/read-only metadata requests. It is not a general model execution
tunnel. The native multi-agent message tool requires an agent in the current
agent tree; it is not arbitrary ordinary-task messaging.

### Existing locks do not prevent simultaneous code changes

`runtime/codex-rs/rollout/src/recorder_shared_append.rs` locks individual durable
record appends. Its lease ends **before inference or tool execution**. Notes CAS,
manager state locks, and legacy profile handoff authority likewise do not lock
repository edits for an entire run.

Git worktree locking prevents administrative pruning/movement; it is not a
source-edit mutex. Linked worktrees have separate HEAD/index/work directories
and share repository objects and refs. See the
[official Git worktree documentation](https://git-scm.com/docs/git-worktree).
Build outputs, ports, temporary files and databases may still collide even when
the source checkouts differ.

## Proposed first implementation

Start by registering an existing checkout and an existing ordinary task. Automatic
creation, splitting, merging and deletion of worktrees can follow after the
dispatch path is reliable. Do not create a new model engine, authentication store
or separate conversation history.

1. **Domain registry and navigation.** Store a stable `domain_id`, name/scope,
   repository identity, host and canonical checkout path, branch/base, owner
   host/thread, preferred profile/model, requirement revision and lifecycle state.
   Keep the registry separate from shortcuts; a domain card reuses shortcut
   navigation and task notes. Changing its preferred profile must not replace
   the owner task or repo identity.
2. **Persistent dispatch.** A narrow `domain.dispatch` resolves the fixed owner
   task and stores an outbox/inbox item before execution. Include message ID,
   requirement revision, expected domain revision, task endpoint and delivery
   state. Resume and start an idle task; queue by default if running. Steering
   requires an explicit intent and matching current turn ID. Do not repeatedly
   resend requests whose delivery outcome is unknown.
3. **Shared requirement and decision events.** Owner activity creates concise
   `requirement_changed`, `decision`, `blocked` and `ready_for_review` records
   with source turn/commit/test references. Main reads new events using a durable
   cursor. Existing record notifications can trigger reading, but are not the
   durable event store. Do not repeatedly copy entire conversations into Main.
4. **One admission path for Main and direct user work.** Sequence execution for
   each domain/checkout, detect conflicting writers and preserve explicit human
   takeover until returned. Viewing or closing the owner window does not change
   control. Direct user requirement changes invalidate assignments and approvals
   tied to an older requirement revision.
5. **Evidence-bound integration.** Associate review readiness with requirement
   revision, base commit, exact candidate commit, tests and results. Main uses
   one integration queue and checks the expected target HEAD. Changed commits,
   requirements or target HEAD require reassessment. Preserve dirty worktrees;
   never resolve a conflict with automatic reset/clean/force removal.

Suggested code boundaries: a new manager domain registry/dispatch module, narrow
routes in `scripts/control_center.py`, a small durable inbox/event store in the
Rust service, and domain cards in the WPF shell. Runtime/proxy changes are needed
where desktop and Main inputs must share admission checks. Merely adding a button
that calls `turn/start` is not the complete feature.

## Identity and recovery requirements

Keep project, domain/workspace, session binding, work item, run, event, verification
and integration IDs distinct. Bind a task using host identity, record namespace,
thread ID and checkout/repository identity, not thread ID alone. Account/model
selection is mutable metadata. Old profile authority fields are not domain IDs.

Use stable operation IDs and recover pending delivery after a service restart.
The presence of `client_user_message_id` on native `turn/start` is not proof of
end-to-end exactly-once delivery. A timeout requires reconciliation against the
target task before retrying. If the original host is unavailable, show pending
or unknown state rather than starting another writer on a guessed path.

## Minimum acceptance scenarios before shipping

- Main's message reaches the registered ordinary owner task and is visible when
  the user opens it directly; profile/model switching retains that task.
- Direct user changes produce an event visible to Main and invalidate stale
  work/approval without copying all history.
- Queued delivery survives manager restart; an uncertain acknowledgement does
  not duplicate the request or start a parallel run.
- User input during Main dispatch is sequenced; explicit takeover is honored
  until returned, including after closing/reopening a window.
- Cross-host identity/path mismatches, dirty checkout changes and unexpected
  target HEAD block integration without losing work.
- Approval of candidate A cannot integrate later candidate B. Tests and evidence
  remain attached to the exact candidate and requirements they checked.

The first useful vertical slice ends at Main dispatch → the same owner task →
direct user update → Main receives its event. Integration automation should ship
only after the admission and stale-evidence cases above pass.
