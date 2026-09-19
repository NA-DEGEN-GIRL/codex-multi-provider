# SSH revisions, provider reasoning and role discovery

SSH preparation can publish a new profile definition while its previous remote
listener is still alive. Both native start and maintenance previously rejected
the mismatch, blocking the very operation needed to apply the new settings.
Maintenance now discovers the actual running descriptor and process identity,
then requests idle-only shutdown of that exact process under its lock before
starting the intended definition. Ordinary native start remains revision-strict
so a late request cannot replace a newer runtime with old settings. Busy work
and unverified exits remain untouched. A changed revision is never presented
as already applied.

Background preparation also compares the profile generation, selected models,
render options and previous binding before committing its result. An explicitly
requested, validated saved host can reach preparation even if its UI declaration
was lost. A separate one-shot recovery queue restores reviewed missing host
declarations only during inactive preparation, with a backup and a comparison of
connection preferences. It does not make deleted hosts reappear on every start.

Generated agent TOML files now include their own name, description and developer
instructions. Depending only on a parent config description left standalone
directory discovery with malformed roles. Old manager-generated revisions are
archived outside the discovery directory; custom roles remain intact. The
native test fixture reproduces the missing-description warnings with the old
format and verifies both declared and standalone loading after repair, without
any model calls.

Foreign Responses reasoning with plaintext `reasoning_text` cannot be replayed
as OpenAI input reasoning with nonempty content. This affected automatic remote
compaction of a fork resumed with a GPT model. The repair belongs at the outbound
request boundary, preserving the stored conversation and plaintext context while
excluding the foreign provider's opaque reasoning state. Native encrypted-only
reasoning and requests sent to external providers retain their original handling.

See [Native fork note copies](native-fork-note-copies.md) for the independent
memo inheritance fix and its first-access snapshot semantics. Running apps keep
their loaded runtime until a normal close and reopen; building a new release is
not evidence that an existing remote connection has already recovered.
