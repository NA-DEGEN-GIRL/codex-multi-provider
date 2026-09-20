# Resume model settings, revision 69

## Diagnosis

Revision 68's live trace still has startup input stalls: the observed maximum
input wait was 480 ms and event-to-paint duration 616 ms. During the following
90–180 second interval, observed input waits reached 19 ms and event duration
80 ms; quiet intervals are not evidence about typing latency. The renderer
liveness median in the first 90 seconds was 6 ms, but its maximum was 2,507 ms.
Different startup activity prevents treating these as a controlled benchmark.

The startup shell log also contains eight `thread/read` failures (`-32600`)
and one profile config-write failure (`-32603`). Native desktop logging identifies
missing persisted records for background hydration requests; subsequent config
writes succeeded. These are separate from the effort restoration regression.
There were no SSH failure entries in that observed shell log. This does not
establish the health of every remote connection.

A separate reported regression was confirmed in durable turn contexts: the
task used Ultra before restarting and Extra High after reopening. The profile's
default remained Extra High. `desktop_profile_resume.cjs` always injected a
provider override to keep execution on the chosen account/provider. Native
`has_model_resume_override` treats that as an explicit model choice and skips
restoring the persisted task model and reasoning effort, even for the same
provider.

## Correction

The adapter keeps provider routing enforced, reads fresh task metadata without
transcript turns, and explicitly carries the saved model/effort when the task
and current profile use the same provider. A potentially stale sidebar cache
cannot decide the provider or overwrite a newer saved choice. Explicit caller
model/effort overrides retain their existing precedence. Cross-provider opens
continue using the destination profile's model and effort. Missing metadata
retains the conservative destination-profile fallback.

This does not modify the active conversation, overwrite profile defaults, or
guess which previously lost selection the user now wants. Re-selecting an
effort after installing this build establishes the durable value for subsequent
reopens.

## Verification

The JavaScript regression failed against the previous adapter and passes for
both main and renderer registrations, including same-provider reopening,
explicit choices, stale cached provider identities, cross-provider requests,
SSH routing, forks without explicit choices and caller-object immutability.

`scripts/test_profile_resume_settings_headless.py` runs the published native
runtime with isolated homes and loopback responses, closes it, then starts a
fresh process against the same fixture home:

| Resume request | Returned model | Returned effort |
| --- | --- | --- |
| Previous redundant provider override | Profile default | Extra High |
| Fixed adapter, same provider | Saved task model | Ultra |
| Fixed adapter, explicit new selection | Explicit model | Low |

The fixture runs no desktop window and makes no real model requests. It validates
native resume settings; model-specific translation from UI effort labels to
wire effort values is outside this persistence contract. User accounts, history
and running processes are not touched.

JavaScript's 19 desktop fixtures and 32 Python desktop/original-bundle tests
passed. The first full Windows build stopped on an existing z-order adjacency
assertion after a delayed native show; an isolated repeat passed with unchanged
window code. This intermittent window-test failure is not counted as fixed by
the model/effort change.

A second complete release build passed all Windows self-tests: native viewport,
window controls, responsiveness instrumentation, notifications, notes, layout,
profile ordering and personal skills. The release pointer now selects revision
69; running user windows were not restarted.
