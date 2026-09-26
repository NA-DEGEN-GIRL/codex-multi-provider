//! Cross-profile record-change hub (stage 2a, service side only).
//!
//! Profile adapters keep writing `record-signals/<profile>.desktop.json` and
//! runtime proxies `<profile>.json`: bounded, retained, content-free
//! invalidations `[thread id, writer seq, host, kind]`. The hub folds every
//! writer's new entries into one ordered, compacted journal so a client can
//! long-poll "what changed since my cursor" instead of every profile scanning
//! every peer file. Only ids, hosts, kinds and writer identities are handled:
//! never titles, prompts, transcripts or credentials.
use crate::windows;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, BTreeSet, HashMap, HashSet, VecDeque},
    io::{self, Read},
    path::{Path, PathBuf},
    sync::{
        Mutex, MutexGuard, PoisonError,
        atomic::{AtomicBool, AtomicU64, Ordering},
    },
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
use tokio::sync::watch;

/// Record-only pipe connections, separate from the 32 management slots:
/// 8 profiles × (adapter + runtime proxy) poll and publish, with headroom.
pub const MAX_CONNECTIONS: usize = 48;
const SCAN_MS: u64 = 250;
const IDLE_SCAN_MS: u64 = 1000;
const IDLE_AFTER_MS: u64 = 60_000;
const QUIET_MS: u64 = 750;
// A thread that streams keeps re-reporting; never starve it behind the window.
const MAX_DELAY_MS: u64 = 3000;
const PERSIST_MS: u64 = 1000;
/// While only plain changes (streaming turns) are undelivered.
const STREAM_PERSIST_MS: u64 = 3000;
const LAZY_PERSIST_MS: u64 = 30_000;
const EXPIRY_CHECK_MS: u64 = 60_000;
const TOMBSTONE_MS: u64 = 30 * 24 * 60 * 60 * 1000;
const MAX_THREADS: usize = 4096;
const MAX_PENDING: usize = 8192;
// 4096 entries with 256-byte hosts are about 1.7 MB: a larger file is not a
// writer's, and parsing one into a `Value` would cost ten times its size.
const MAX_FILE_BYTES: u64 = 4 * 1024 * 1024;
const MAX_JOURNAL_BYTES: u64 = 64 * 1024 * 1024;
const MAX_FILE_CHANGES: usize = 4096;
const MAX_DIRECTORY_ENTRIES: usize = 4096;
const MAX_WRITER_FILES: usize = 128;
const MAX_WRITERS: usize = 256;
const WRITER_GENERATIONS: usize = 4;
const MAX_ORIGINS: usize = 64;
const MAX_HOST_BYTES: usize = 256;
const MAX_POLL_EVENTS: usize = 512;
const MAX_WAIT_MS: u64 = 25_000;
const MAX_POLL_HOSTS: usize = 64;
pub const MAX_PUBLISH_EVENTS: usize = 256;
/// Per-connection publish budget over any sliding one-second window.
pub const PUBLISH_REQUESTS: usize = 20;
pub const PUBLISH_EVENTS: usize = 2000;
/// Service-wide publish budget across all record connections: bounds the
/// time publishes can hold the hub lock on the async runtime.
pub const GLOBAL_PUBLISH_REQUESTS: usize = 200;
pub const GLOBAL_PUBLISH_EVENTS: usize = 8000;
const PUBLISH_WINDOW: Duration = Duration::from_secs(1);
/// Consecutive panicking ticks after which the hub stops and hands every
/// client back to its own catch-up.
const MAX_CONSECUTIVE_PANICS: u32 = 3;
const MAX_SAFE_INTEGER: u64 = (1 << 53) - 1;
const JOURNAL_VERSION: u64 = 1;
const INVALID: &str = "기록 동기화 요청 형식이 올바르지 않습니다.";

pub fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| d.as_millis() as u64)
}

fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(PoisonError::into_inner)
}

fn new_epoch() -> String {
    uuid::Uuid::new_v4().to_string()
}

fn thread_key(host: &str, id: &str) -> String {
    format!("{host}\0{id}")
}

fn is_uuid(text: &str) -> bool {
    text.len() == 36
        && text.bytes().enumerate().all(|(i, b)| {
            if matches!(i, 8 | 13 | 18 | 23) {
                b == b'-'
            } else {
                b.is_ascii_hexdigit()
            }
        })
}

fn valid_host(host: &str) -> bool {
    !host.is_empty() && host.len() <= MAX_HOST_BYTES && !host.chars().any(char::is_control)
}

/// A deletion or archive: once it stands, a thread's earlier changes are moot
/// to every client (an archived thread ignores ordinary changes).
fn closes(kind: Kind) -> bool {
    matches!(kind, Kind::Deleted | Kind::Archived)
}

fn valid_generation(generation: &str) -> bool {
    !generation.is_empty() && generation.len() <= 128
}

fn updated_at(value: &Value) -> Option<Value> {
    match value {
        Value::Number(n) if n.as_f64().is_some_and(|v| v.is_finite() && v >= 0.0) => {
            Some(value.clone())
        }
        Value::String(s) if !s.is_empty() && s.len() <= 64 && !s.chars().any(char::is_control) => {
            Some(value.clone())
        }
        _ => None,
    }
}

/// `<uuid>.desktop.json` (Electron adapter) or `<uuid>.json` (runtime proxy)
/// both speak for the same profile; anything else there is not a writer.
fn signal_origin(name: &str) -> Option<String> {
    let stem = name
        .strip_suffix(".desktop.json")
        .or_else(|| name.strip_suffix(".json"))?;
    if stem.len() != 36 {
        return None;
    }
    uuid::Uuid::parse_str(stem).ok().map(|id| id.to_string())
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Kind {
    Changed,
    Deleted,
    Archived,
    Unarchived,
}

impl Kind {
    fn parse(text: &str) -> Option<Self> {
        Some(match text {
            "changed" => Self::Changed,
            "deleted" => Self::Deleted,
            "archived" => Self::Archived,
            "unarchived" => Self::Unarchived,
            _ => return None,
        })
    }
    /// Reports merged into one group: deletion is final and the last
    /// visibility change wins, as in each adapter's own sticky visibility.
    fn merge(self, next: Self) -> Self {
        match (self, next) {
            (Self::Deleted, _) | (_, Self::Deleted) => Self::Deleted,
            (_, Self::Archived | Self::Unarchived) => next,
            (current, Self::Changed) => current,
        }
    }
}

struct Entry {
    id: String,
    seq: u64,
    host: String,
    kind: Kind,
    updated_at: Option<Value>,
}

fn parse_entry(change: &Value) -> Option<Entry> {
    // Adapters accept [id, seq] and [id, seq, host, kind]; a fifth item is an
    // optional updatedAt a newer writer may add.
    let items = change.as_array().filter(|a| matches!(a.len(), 2 | 4 | 5))?;
    // Case variants of one id are one thread (and one group).
    let id = items[0]
        .as_str()
        .filter(|id| is_uuid(id))?
        .to_ascii_lowercase();
    let seq = items[1]
        .as_u64()
        .filter(|s| (1..=MAX_SAFE_INTEGER).contains(s))?;
    let host = items
        .get(2)
        .map_or(Some("local"), Value::as_str)
        .filter(|h| valid_host(h))?;
    let kind = items
        .get(3)
        .map_or(Some(Kind::Changed), |k| k.as_str().and_then(Kind::parse))?;
    Some(Entry {
        id,
        seq,
        host: host.into(),
        kind,
        updated_at: items.get(4).and_then(updated_at),
    })
}

fn parse_signals(bytes: &[u8]) -> Option<(String, Vec<Entry>)> {
    let data: Value = serde_json::from_slice(bytes).ok()?;
    if !matches!(data["version"].as_u64(), Some(1 | 2)) {
        return None;
    }
    let generation = data["generation"]
        .as_str()
        .filter(|g| valid_generation(g))?;
    let changes = data["changes"]
        .as_array()
        .filter(|c| c.len() <= MAX_FILE_CHANGES)?;
    let mut entries: Vec<Entry> = changes.iter().filter_map(parse_entry).collect();
    entries.sort_by_key(|e| e.seq);
    Some((generation.into(), entries))
}

fn read_bounded(path: &Path) -> io::Result<Option<Vec<u8>>> {
    let mut bytes = Vec::new();
    std::fs::File::open(path)?
        .take(MAX_FILE_BYTES + 1)
        .read_to_end(&mut bytes)?;
    Ok((bytes.len() as u64 <= MAX_FILE_BYTES).then_some(bytes))
}

struct Group {
    host: String,
    id: String,
    kind: Kind,
    origins: BTreeSet<String>,
    first: u64,
    last: u64,
    updated_at: Option<Value>,
}

struct Event {
    seq: u64,
    host: String,
    id: String,
    kind: Kind,
    /// Profiles that need this event at no cursor: they sent every report
    /// it carries, including those of the events it replaced.
    origins: Vec<String>,
    /// Profiles that sent the newest report; they need this event only if
    /// their cursor is older than `prior` (they missed what it replaced).
    latest: Vec<String>,
    prior: u64,
    at: u64,
    updated_at: Option<Value>,
}

impl Event {
    fn suppressed_for(&self, profile: &str, cursor: u64) -> bool {
        self.origins.iter().any(|o| o == profile)
            || (cursor >= self.prior && self.latest.iter().any(|o| o == profile))
    }

    fn json(&self) -> Value {
        let mut value = json!({"host":self.host,"id":self.id,"kind":self.kind,"seq":self.seq,"origins":self.origins});
        if let Some(updated) = &self.updated_at {
            value["updatedAt"] = updated.clone();
        }
        value
    }
}

#[derive(Clone, PartialEq, Eq)]
struct Stamp {
    len: u64,
    modified: Option<SystemTime>,
    created: Option<SystemTime>,
}

#[derive(Default)]
struct Writer {
    /// Last ingested seq per writer generation, most recent last.
    cursors: Vec<(String, u64)>,
    stamp: Option<Stamp>,
    failures: u8,
}

enum Outcome {
    Parsed(String, Vec<Entry>),
    Ignored,
    Failed,
}

#[derive(Serialize, Deserialize)]
struct StoredEvent {
    #[serde(rename = "s", default)]
    seq: u64,
    #[serde(rename = "h")]
    host: usize,
    #[serde(rename = "i")]
    id: String,
    #[serde(rename = "k")]
    kind: Kind,
    #[serde(rename = "t")]
    at: u64,
    #[serde(rename = "o")]
    origins: Vec<usize>,
    #[serde(rename = "u", default, skip_serializing_if = "Option::is_none")]
    updated_at: Option<Value>,
    #[serde(rename = "l", default, skip_serializing_if = "Vec::is_empty")]
    latest: Vec<usize>,
    #[serde(rename = "p", default, skip_serializing_if = "is_zero")]
    prior: u64,
}

fn is_zero(value: &u64) -> bool {
    *value == 0
}

/// Compacted journal: hosts and origins are interned so 4096 threads stay
/// small enough to rewrite atomically.
#[derive(Serialize, Deserialize)]
struct Journal {
    version: u64,
    epoch: String,
    head: u64,
    floor: u64,
    #[serde(default)]
    hosts: Vec<String>,
    #[serde(default)]
    origins: Vec<String>,
    #[serde(default)]
    events: Vec<StoredEvent>,
    #[serde(default)]
    pending: Vec<StoredEvent>,
    #[serde(default)]
    writers: BTreeMap<String, Vec<(String, u64)>>,
    /// Written while journal writes were failing: polls may have been
    /// served past this head under this epoch, so it must not resume.
    #[serde(default, skip_serializing_if = "is_false")]
    volatile: bool,
}

fn is_false(value: &bool) -> bool {
    !*value
}

#[derive(Default)]
struct Interner {
    ids: HashMap<String, usize>,
    values: Vec<String>,
}

impl Interner {
    fn id(&mut self, value: &str) -> usize {
        if let Some(&id) = self.ids.get(value) {
            return id;
        }
        self.values.push(value.into());
        self.ids.insert(value.into(), self.values.len() - 1);
        self.values.len() - 1
    }
}

struct State {
    epoch: String,
    /// Last assigned hub sequence number.
    head: u64,
    /// Highest seq removed without a newer event for its thread (eviction or
    /// tombstone expiry). An older cursor may have missed it and must reset.
    floor: u64,
    /// Highest seq a poll may return: already durable in the journal, so a
    /// crash can never make the same epoch hand out a seq twice.
    delivered: u64,
    events: BTreeMap<u64, Event>,
    index: HashMap<String, u64>,
    pending: HashMap<String, Group>,
    /// `pending` keys by arrival order: oldest-first commits and eviction
    /// in O(log n) instead of a scan under the lock.
    queue: BTreeMap<u64, String>,
    arrivals: u64,
    writers: HashMap<String, Writer>,
    /// A new epoch adopts retained writer files as already known: clients
    /// reset and catch up themselves, so replaying them would be a burst.
    baseline: bool,
    /// Journal writes are failing; deliver without durability under a fresh
    /// epoch that an older file on disk can never be mistaken for.
    volatile: bool,
    version: u64,
    saved: u64,
    saved_head: u64,
    last_write: Option<u64>,
    last_expiry: Option<u64>,
    write_failures: u32,
}

impl State {
    fn new(epoch: String, head: u64, floor: u64) -> Self {
        Self {
            epoch,
            head,
            floor,
            delivered: head,
            events: BTreeMap::new(),
            index: HashMap::new(),
            pending: HashMap::new(),
            queue: BTreeMap::new(),
            arrivals: 0,
            writers: HashMap::new(),
            baseline: false,
            volatile: false,
            version: 0,
            saved: 0,
            saved_head: head,
            last_write: None,
            last_expiry: None,
            write_failures: 0,
        }
    }

    fn fresh() -> Self {
        let mut state = Self::new(new_epoch(), 0, 0);
        state.baseline = true;
        state
    }

    fn restore(journal: Journal, now: u64) -> Option<Self> {
        if journal.version != JOURNAL_VERSION
            || journal.epoch.is_empty()
            || journal.epoch.len() > 64
            || journal.floor > journal.head
            || journal.head > MAX_SAFE_INTEGER
            || journal.events.len() > MAX_THREADS
            || journal.pending.len() > MAX_PENDING
            || journal.writers.len() > MAX_WRITERS
            || !journal.hosts.iter().all(|h| valid_host(h))
            || !journal.origins.iter().all(|o| is_uuid(o))
        {
            return None;
        }
        let profiles = |ids: &[usize]| -> Option<Vec<String>> {
            if ids.len() > MAX_ORIGINS {
                return None;
            }
            ids.iter()
                .map(|&o| journal.origins.get(o).cloned())
                .collect()
        };
        let resolve = |stored: &StoredEvent| -> Option<(String, Vec<String>)> {
            if !is_uuid(&stored.id) {
                return None;
            }
            let host = journal.hosts.get(stored.host)?.clone();
            Some((host, profiles(&stored.origins)?))
        };
        let mut state = Self::new(journal.epoch.clone(), journal.head, journal.floor);
        let mut previous = 0;
        for stored in &journal.events {
            let (host, origins) = resolve(stored)?;
            let latest = profiles(&stored.latest)?;
            if stored.seq <= previous || stored.seq > journal.head || stored.prior >= stored.seq {
                return None;
            }
            previous = stored.seq;
            if state
                .index
                .insert(thread_key(&host, &stored.id), stored.seq)
                .is_some()
            {
                return None;
            }
            state.events.insert(
                stored.seq,
                Event {
                    seq: stored.seq,
                    host,
                    id: stored.id.clone(),
                    kind: stored.kind,
                    origins,
                    latest,
                    prior: stored.prior,
                    at: stored.at,
                    updated_at: stored.updated_at.as_ref().and_then(updated_at),
                },
            );
        }
        for stored in &journal.pending {
            let (host, origins) = resolve(stored)?;
            state.arrivals += 1;
            let key = thread_key(&host, &stored.id);
            let group = Group {
                host,
                id: stored.id.clone(),
                kind: stored.kind,
                origins: origins.into_iter().collect(),
                first: now,
                last: now,
                updated_at: stored.updated_at.as_ref().and_then(updated_at),
            };
            state.queue.insert(state.arrivals, key.clone());
            if state.pending.insert(key, group).is_some() {
                return None;
            }
        }
        for (name, cursors) in journal.writers {
            if signal_origin(&name).is_none()
                || cursors.is_empty()
                || cursors.len() > WRITER_GENERATIONS
                || cursors
                    .iter()
                    .any(|(g, s)| !valid_generation(g) || *s > MAX_SAFE_INTEGER)
            {
                return None;
            }
            state.writers.insert(
                name,
                Writer {
                    cursors,
                    ..Writer::default()
                },
            );
        }
        Some(state)
    }

    fn snapshot(&self) -> Vec<u8> {
        let mut hosts = Interner::default();
        let mut origins = Interner::default();
        let mut events = Vec::with_capacity(self.events.len());
        for e in self.events.values() {
            events.push(StoredEvent {
                seq: e.seq,
                host: hosts.id(&e.host),
                id: e.id.clone(),
                kind: e.kind,
                at: e.at,
                origins: e.origins.iter().map(|o| origins.id(o)).collect(),
                updated_at: e.updated_at.clone(),
                latest: e.latest.iter().map(|o| origins.id(o)).collect(),
                prior: e.prior,
            });
        }
        let mut pending = Vec::with_capacity(self.pending.len());
        for g in self.queue.values().filter_map(|key| self.pending.get(key)) {
            pending.push(StoredEvent {
                seq: 0,
                host: hosts.id(&g.host),
                id: g.id.clone(),
                kind: g.kind,
                at: g.first,
                origins: g.origins.iter().map(|o| origins.id(o)).collect(),
                updated_at: g.updated_at.clone(),
                latest: Vec::new(),
                prior: 0,
            });
        }
        let writers = self
            .writers
            .iter()
            .filter(|(_, w)| !w.cursors.is_empty())
            .map(|(name, w)| (name.clone(), w.cursors.clone()))
            .collect();
        serde_json::to_vec(&Journal {
            version: JOURNAL_VERSION,
            epoch: self.epoch.clone(),
            head: self.head,
            floor: self.floor,
            hosts: hosts.values,
            origins: origins.values,
            events,
            pending,
            writers,
            volatile: self.volatile,
        })
        .unwrap_or_default()
    }

    fn ingest(
        &mut self,
        name: &str,
        origin: &str,
        generation: String,
        entries: Vec<Entry>,
        now: u64,
    ) {
        let writer = self.writers.entry(name.into()).or_default();
        let known = writer.cursors.iter().position(|(g, _)| *g == generation);
        let from = known.map_or(0, |i| writer.cursors[i].1);
        let top = entries.iter().map(|e| e.seq).max().unwrap_or(0).max(from);
        if let Some(i) = known {
            writer.cursors.remove(i);
        }
        writer.cursors.push((generation, top));
        if writer.cursors.len() > WRITER_GENERATIONS {
            writer.cursors.remove(0);
        }
        if known.is_none() || top != from {
            self.version += 1;
        }
        if self.baseline {
            return;
        }
        for entry in entries {
            if entry.seq > from {
                self.enqueue(origin, entry, now);
            }
        }
    }

    /// Distinct (host, id) groups these reports would add to `pending`.
    fn new_groups(&self, entries: &[Entry]) -> usize {
        entries
            .iter()
            .map(|e| thread_key(&e.host, &e.id))
            .filter(|key| !self.pending.contains_key(key))
            .collect::<HashSet<_>>()
            .len()
    }

    fn enqueue(&mut self, origin: &str, entry: Entry, now: u64) {
        let key = thread_key(&entry.host, &entry.id);
        if !self.pending.contains_key(&key) && self.pending.len() >= MAX_PENDING {
            // Only file ingestion gets here (publish is refused when full):
            // commit the oldest group early rather than drop a report.
            if let Some((_, oldest)) = self.queue.pop_first()
                && let Some(group) = self.pending.remove(&oldest)
            {
                self.commit(oldest, group, now);
            }
        }
        self.arrivals += 1;
        let order = self.arrivals;
        self.version += 1;
        let group = match self.pending.entry(key) {
            std::collections::hash_map::Entry::Occupied(group) => group.into_mut(),
            std::collections::hash_map::Entry::Vacant(slot) => {
                self.queue.insert(order, slot.key().clone());
                slot.insert(Group {
                    host: entry.host,
                    id: entry.id,
                    kind: entry.kind,
                    origins: BTreeSet::from([origin.to_string()]),
                    first: now,
                    last: now,
                    updated_at: entry.updated_at,
                });
                return;
            }
        };
        // `origins` are the profiles that already know everything the group
        // will say, so a poll never hands it back to them. Being a reporter
        // is not enough.
        let merged = group.kind.merge(entry.kind);
        if closes(entry.kind) && merged != group.kind {
            // A deletion or archive decides the event and makes earlier
            // changes moot: only its reporter knows the result.
            group.origins = BTreeSet::from([origin.to_string()]);
        } else if closes(merged) {
            // The same deletion or archive again: both reporters know it. A
            // plain change or unarchive under it adds no knower.
            if entry.kind == merged && group.origins.len() < MAX_ORIGINS {
                group.origins.insert(origin.into());
            }
        } else {
            // Changes and unarchives, local or remote. Adapters keep
            // reporting an unarchived thread's changes as `unarchived`, and a
            // shared host only broadcasts starts and renames to every
            // connection, not turns: two profiles' reports are two changes,
            // and neither knows the other's. Once mixed, no one knows it all.
            group.origins.retain(|o| o == origin);
        }
        group.kind = merged;
        group.last = now;
        if entry.updated_at.is_some() {
            group.updated_at = entry.updated_at;
        }
    }

    /// Commits groups whose writers went quiet (or that waited too long).
    fn flush(&mut self, now: u64, force: bool) -> usize {
        // In arrival order, so commits keep the order reports first came in.
        let due: Vec<(u64, String)> = self
            .queue
            .iter()
            .filter(|(_, key)| {
                self.pending.get(*key).is_some_and(|g| {
                    force
                        || now < g.first
                        || now >= g.last.saturating_add(QUIET_MS)
                        || now >= g.first.saturating_add(MAX_DELAY_MS)
                })
            })
            .map(|(&order, key)| (order, key.clone()))
            .collect();
        for (order, key) in &due {
            self.queue.remove(order);
            if let Some(group) = self.pending.remove(key) {
                self.commit(key.clone(), group, now);
            }
        }
        due.len()
    }

    fn commit(&mut self, key: String, mut group: Group, now: u64) {
        let (mut latest, mut prior) = (Vec::new(), 0);
        if let Some(&previous) = self.index.get(&key) {
            let old = &self.events[&previous];
            // Deletion is final and an archived task ignores ordinary changes,
            // exactly as every adapter already treats its peers' reports.
            let redundant = matches!(
                (old.kind, group.kind),
                (Kind::Deleted, _) | (Kind::Archived, Kind::Changed | Kind::Archived)
            );
            if redundant {
                return;
            }
            // The replaced event may not have reached every poller: keep what
            // it said. Its updatedAt survives a report without one.
            group.updated_at = group.updated_at.or_else(|| old.updated_at.clone());
            let merged = old.kind.merge(group.kind);
            if !(closes(group.kind) && merged != old.kind) {
                // Changes and unarchives do not supersede what they replace:
                // the new reporters did not necessarily see it. Only profiles
                // that sent both are skipped at every cursor; the newest
                // reporters are skipped once their cursor is past what was
                // replaced.
                latest = group.origins.iter().cloned().collect::<Vec<_>>();
                prior = if !old.latest.is_empty() && latest.iter().all(|o| old.latest.contains(o)) {
                    old.prior
                } else {
                    previous
                };
                group.origins.retain(|o| old.origins.contains(o));
            }
            // Otherwise a deletion or archive decides the event and makes
            // what it replaced moot: as in `enqueue`, its reporters alone
            // know the result.
            group.kind = merged;
            self.events.remove(&previous);
        }
        self.head += 1;
        self.index.insert(key, self.head);
        self.events.insert(
            self.head,
            Event {
                seq: self.head,
                host: group.host,
                id: group.id,
                kind: group.kind,
                origins: group.origins.into_iter().collect(),
                latest,
                prior,
                at: now,
                updated_at: group.updated_at,
            },
        );
        self.version += 1;
        if self.volatile {
            self.delivered = self.head;
        }
        while self.index.len() > MAX_THREADS {
            let Some((seq, oldest)) = self.events.pop_first() else {
                break;
            };
            self.index.remove(&thread_key(&oldest.host, &oldest.id));
            self.floor = self.floor.max(seq);
        }
    }

    fn expire(&mut self, now: u64) {
        if self
            .last_expiry
            .is_some_and(|last| now >= last && now < last + EXPIRY_CHECK_MS)
        {
            return;
        }
        self.last_expiry = Some(now);
        let expired: Vec<u64> = self
            .events
            .values()
            .filter(|e| e.kind == Kind::Deleted && now.saturating_sub(e.at) > TOMBSTONE_MS)
            .map(|e| e.seq)
            .collect();
        for seq in expired {
            if let Some(event) = self.events.remove(&seq) {
                self.index.remove(&thread_key(&event.host, &event.id));
                self.floor = self.floor.max(seq);
                self.version += 1;
            }
        }
    }

    fn rotate_epoch(&mut self) {
        self.epoch = new_epoch();
        self.floor = self.head;
        self.delivered = self.head;
        self.volatile = true;
        self.version += 1;
    }

    fn query(&self, query: &Query, cursor: Option<u64>) -> Value {
        let limit = self.delivered;
        let start = match cursor {
            Some(cursor)
                if query.epoch.as_deref() == Some(self.epoch.as_str())
                    && cursor >= self.floor
                    && cursor <= limit =>
            {
                cursor
            }
            // Unknown epoch, compacted history or a cursor this journal never
            // delivered: the client runs its own catch-up, then polls on.
            _ => {
                return json!({"epoch":self.epoch,"cursor":limit,"reset":true,"events":[],"more":false});
            }
        };
        let mut events = Vec::new();
        let mut last = start;
        let mut more = false;
        if start < limit {
            for (&seq, event) in self.events.range(start + 1..=limit) {
                if event.suppressed_for(&query.profile, start)
                    || query
                        .hosts
                        .as_ref()
                        .is_some_and(|hosts| !hosts.contains(&event.host))
                {
                    continue;
                }
                if events.len() == MAX_POLL_EVENTS {
                    more = true;
                    break;
                }
                last = seq;
                events.push(event.json());
            }
        }
        let cursor = if more { last } else { limit };
        json!({"epoch":self.epoch,"cursor":cursor,"reset":false,"events":events,"more":more})
    }
}

fn load(path: &Path, now: u64) -> (State, &'static str) {
    let bytes = match std::fs::metadata(path) {
        Err(e) if e.kind() == io::ErrorKind::NotFound => return (State::fresh(), "created"),
        Ok(meta) if meta.is_file() && meta.len() <= MAX_JOURNAL_BYTES => std::fs::read(path).ok(),
        _ => None,
    };
    let journal = bytes.and_then(|b| serde_json::from_slice::<Journal>(&b).ok());
    let volatile = journal.as_ref().is_some_and(|j| j.volatile);
    match journal.and_then(|journal| State::restore(journal, now)) {
        // Delivery may have run ahead of this file: resuming its epoch at a
        // lower head would reuse seqs clients already hold. Keep its content
        // and writer cursors, but make every client reset.
        Some(mut state) if volatile => {
            state.epoch = new_epoch();
            state.floor = state.head;
            state.delivered = state.head;
            state.version += 1;
            (state, "rotated")
        }
        Some(state) => (state, "loaded"),
        None => (State::fresh(), "corrupt"),
    }
}

pub struct Query {
    profile: String,
    epoch: Option<String>,
    cursor: Option<u64>,
    hosts: Option<HashSet<String>>,
    wait_ms: u64,
}

impl Query {
    pub fn parse(args: &Value) -> Result<Self, String> {
        let profile = crate::protocol::uuid(args, "profile")?;
        let epoch = match &args["epoch"] {
            Value::Null => None,
            Value::String(epoch) if epoch.len() <= 64 => Some(epoch.clone()),
            _ => return Err(INVALID.into()),
        };
        let cursor = match &args["cursor"] {
            Value::Null => None,
            value => Some(value.as_u64().ok_or(INVALID)?),
        };
        let hosts = match &args["hosts"] {
            Value::Null => None,
            Value::Array(hosts) if hosts.len() <= MAX_POLL_HOSTS => Some(
                hosts
                    .iter()
                    .map(|h| {
                        h.as_str()
                            .filter(|h| valid_host(h))
                            .map(str::to_string)
                            .ok_or(INVALID)
                    })
                    .collect::<Result<HashSet<_>, _>>()?,
            ),
            _ => return Err(INVALID.into()),
        };
        let wait_ms = match &args["wait_ms"] {
            Value::Null => 0,
            value => value.as_u64().ok_or(INVALID)?.min(MAX_WAIT_MS),
        };
        Ok(Self {
            profile,
            epoch,
            cursor,
            hosts,
            wait_ms,
        })
    }
}

/// The service is going away: the client keeps its epoch/cursor and polls
/// the next instance (or falls back to its own catch-up).
pub fn closing(query: &Query) -> Value {
    json!({"closing":true,"epoch":query.epoch,"cursor":query.cursor,"reset":false,"events":[],"more":false})
}

/// Nothing was taken: the writer's own signal file still carries the change.
pub fn publish_closing() -> Value {
    json!({"closing":true,"accepted":0})
}

/// `records.publish`: a writer's own reports sent directly instead of waiting
/// for the next directory scan. The same fields and rules as a signal-file
/// entry, minus the writer sequence (the hub groups by thread, not by seq).
pub struct Publish {
    profile: String,
    entries: Vec<Entry>,
}

impl Publish {
    pub fn parse(args: &Value) -> Result<Self, String> {
        let known = |key: &str| matches!(key, "profile" | "events");
        if !args.as_object().is_some_and(|a| a.keys().all(|k| known(k))) {
            return Err(INVALID.into());
        }
        let profile = crate::protocol::uuid(args, "profile")?;
        let entries = args["events"]
            .as_array()
            .filter(|events| events.len() <= MAX_PUBLISH_EVENTS)
            .and_then(|events| events.iter().map(parse_report).collect::<Option<Vec<_>>>())
            .ok_or(INVALID)?;
        Ok(Self { profile, entries })
    }
}

/// `{host, id, kind, updatedAt?}`, validated exactly like a file entry so a
/// report arriving on both paths lands in the same (host, id) group.
fn parse_report(value: &Value) -> Option<Entry> {
    let report = value.as_object()?;
    let known = |key: &str| matches!(key, "host" | "id" | "kind" | "updatedAt");
    if !report.keys().all(|k| known(k)) {
        return None;
    }
    let host = report.get("host")?.as_str().filter(|h| valid_host(h))?;
    let id = report.get("id")?.as_str().filter(|id| is_uuid(id))?;
    let kind = report.get("kind")?.as_str().and_then(Kind::parse)?;
    let updated_at = match report.get("updatedAt") {
        None | Some(Value::Null) => None,
        Some(value) if value.is_number() => Some(updated_at(value)?),
        Some(_) => return None,
    };
    Some(Entry {
        id: id.to_ascii_lowercase(),
        seq: 0,
        host: host.into(),
        kind,
        updated_at,
    })
}

/// Outcome of `Hub::publish`.
#[derive(Debug, PartialEq, Eq)]
pub enum Published {
    Accepted(usize),
    /// `pending` cannot take these groups without evicting others: nothing
    /// was taken and the writer's signal file still carries them.
    Busy,
    Closing,
}

/// Publish budget: at most `max_requests` requests and `max_events` events
/// admitted in any sliding one-second window.
pub struct Throttle {
    admitted: VecDeque<(Instant, usize)>,
    events: usize,
    max_requests: usize,
    max_events: usize,
}

impl Throttle {
    /// One record connection's budget.
    pub fn connection() -> Self {
        Self::new(PUBLISH_REQUESTS, PUBLISH_EVENTS)
    }

    fn new(max_requests: usize, max_events: usize) -> Self {
        Self {
            admitted: VecDeque::new(),
            events: 0,
            max_requests,
            max_events,
        }
    }

    fn fits(&mut self, events: usize, now: Instant) -> bool {
        while let Some(&(at, count)) = self.admitted.front() {
            if now.saturating_duration_since(at) < PUBLISH_WINDOW {
                break;
            }
            self.admitted.pop_front();
            self.events -= count;
        }
        self.admitted.len() < self.max_requests && self.events + events <= self.max_events
    }

    fn take(&mut self, events: usize, now: Instant) {
        self.admitted.push_back((now, events));
        self.events += events;
    }

    /// This budget alone; the service spends through `Hub::admit_publish`.
    #[cfg(test)]
    pub fn admit(&mut self, events: usize, now: Instant) -> bool {
        // An oversized list is refused by validation, not by the budget.
        let events = events.min(MAX_PUBLISH_EVENTS);
        let fits = self.fits(events, now);
        if fits {
            self.take(events, now);
        }
        fits
    }
}

pub struct Hub {
    root: PathBuf,
    signals: PathBuf,
    journal: PathBuf,
    state: Mutex<State>,
    /// Serializes snapshot+write so an older snapshot never replaces a newer one.
    persist: Mutex<()>,
    wake: watch::Sender<u64>,
    closing: AtomicBool,
    last_poll: AtomicU64,
    worker: Mutex<Option<std::thread::JoinHandle<()>>>,
    /// Service-wide publish budget, shared by every record connection.
    budget: Mutex<Throttle>,
    ticks: AtomicU64,
    panics: AtomicU64,
    /// The worker could not start or kept panicking: the hub is closed and
    /// every client runs its own catch-up.
    failed: AtomicBool,
    #[cfg(test)]
    pub inject_panics: std::sync::atomic::AtomicU32,
}

impl Hub {
    pub fn open(root: &Path) -> Self {
        let directory = root.join("work/control-center");
        let journal = directory.join("record-journal.json");
        let (state, outcome) = load(&journal, now_ms());
        crate::log_event(
            root,
            &json!({"event":"records.journal","state":outcome,"threads":state.events.len(),"cursor":state.head}),
        );
        Self {
            root: root.into(),
            signals: directory.join("record-signals"),
            journal,
            state: Mutex::new(state),
            persist: Mutex::new(()),
            wake: watch::channel(0).0,
            closing: AtomicBool::new(false),
            last_poll: AtomicU64::new(0),
            worker: Mutex::new(None),
            budget: Mutex::new(Throttle::new(
                GLOBAL_PUBLISH_REQUESTS,
                GLOBAL_PUBLISH_EVENTS,
            )),
            ticks: AtomicU64::new(0),
            panics: AtomicU64::new(0),
            failed: AtomicBool::new(false),
            #[cfg(test)]
            inject_panics: std::sync::atomic::AtomicU32::new(0),
        }
    }

    /// Polls the writer directory on a dedicated thread: never on a request
    /// path, never behind admission, gates or the Python backend.
    pub fn start(self: &std::sync::Arc<Self>) {
        let hub = self.clone();
        let spawned = std::thread::Builder::new()
            .name("records-hub".into())
            .spawn(move || {
                let mut consecutive = 0;
                while !hub.closing.load(Ordering::SeqCst) {
                    let ticked = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                        hub.tick(now_ms())
                    }));
                    if ticked.is_ok() {
                        consecutive = 0;
                    } else {
                        consecutive += 1;
                        let panics = hub.panics.fetch_add(1, Ordering::SeqCst) + 1;
                        crate::log_event(
                            &hub.root,
                            &json!({"event":"records.hub","state":"panicked","panics":panics}),
                        );
                        if consecutive >= MAX_CONSECUTIVE_PANICS {
                            hub.fail("panicked");
                            return;
                        }
                    }
                    let busy = !lock(&hub.state).pending.is_empty()
                        || now_ms().saturating_sub(hub.last_poll.load(Ordering::Relaxed))
                            < IDLE_AFTER_MS;
                    std::thread::park_timeout(Duration::from_millis(if busy {
                        SCAN_MS
                    } else {
                        IDLE_SCAN_MS
                    }));
                }
            });
        match spawned {
            Ok(handle) => *lock(&self.worker) = Some(handle),
            Err(_) => self.fail("spawn_failed"),
        }
    }

    /// The hub cannot run: release every poll with `{closing:true}` (and
    /// refuse publishes) so clients fall back to their own catch-up.
    fn fail(&self, reason: &str) {
        self.failed.store(true, Ordering::SeqCst);
        crate::log_event(
            &self.root,
            &json!({"event":"records.hub","state":"failed","reason":reason}),
        );
        self.close();
    }

    fn wake_worker(&self) {
        if let Some(worker) = lock(&self.worker).as_ref() {
            worker.thread().unpark();
        }
    }

    pub fn tick(&self, now: u64) {
        if self.closing.load(Ordering::SeqCst) {
            return;
        }
        self.ticks.fetch_add(1, Ordering::Relaxed);
        self.scan(now);
        let wake = {
            let mut state = lock(&self.state);
            #[cfg(test)]
            if self
                .inject_panics
                .fetch_update(Ordering::SeqCst, Ordering::SeqCst, |n| n.checked_sub(1))
                .is_ok()
            {
                panic!("injected records hub panic");
            }
            let before = state.delivered;
            state.flush(now, false);
            state.expire(now);
            state.delivered != before
        };
        if wake {
            self.notify();
        }
        self.persist(now, false);
    }

    fn scan(&self, now: u64) {
        let mut listing: Vec<(String, String, Stamp)> = match std::fs::read_dir(&self.signals) {
            Ok(directory) => directory
                .take(MAX_DIRECTORY_ENTRIES)
                .filter_map(|entry| {
                    let entry = entry.ok()?;
                    let name = entry.file_name().into_string().ok()?;
                    let origin = signal_origin(&name)?;
                    // Directory metadata: no handle is opened on an unchanged
                    // file, so writers' atomic renames are never contended.
                    let meta = entry.metadata().ok()?;
                    meta.is_file().then(|| {
                        let stamp = Stamp {
                            len: meta.len(),
                            modified: meta.modified().ok(),
                            created: meta.created().ok(),
                        };
                        (name, origin, stamp)
                    })
                })
                .collect(),
            Err(e) if e.kind() == io::ErrorKind::NotFound => Vec::new(),
            Err(_) => return,
        };
        // Over the cap, the most recently written files win: stale files of
        // removed profiles must never starve live writers (directory order
        // is by name).
        if listing.len() > MAX_WRITER_FILES {
            listing.sort_unstable_by(|a, b| b.2.modified.cmp(&a.2.modified));
            listing.truncate(MAX_WRITER_FILES);
        }
        let changed: Vec<(String, String, Stamp)> = {
            let state = lock(&self.state);
            listing
                .iter()
                .filter(|(name, _, stamp)| {
                    state.writers.get(name).and_then(|w| w.stamp.as_ref()) != Some(stamp)
                })
                .cloned()
                .collect()
        };
        let mut outcomes = Vec::with_capacity(changed.len());
        for (name, origin, stamp) in changed {
            let outcome = if stamp.len > MAX_FILE_BYTES {
                Outcome::Ignored
            } else {
                match read_bounded(&self.signals.join(&name)) {
                    Ok(bytes) => match bytes.as_deref().and_then(parse_signals) {
                        Some((generation, entries)) => Outcome::Parsed(generation, entries),
                        None => Outcome::Ignored,
                    },
                    Err(e) if e.kind() == io::ErrorKind::NotFound => continue,
                    Err(_) => Outcome::Failed,
                }
            };
            outcomes.push((name, origin, stamp, outcome));
        }
        let present: HashSet<&str> = listing.iter().map(|(name, _, _)| name.as_str()).collect();
        let mut state = lock(&self.state);
        for (name, writer) in state.writers.iter_mut() {
            if !present.contains(name.as_str()) {
                writer.stamp = None;
            }
        }
        for (name, origin, stamp, outcome) in outcomes {
            match outcome {
                Outcome::Parsed(generation, entries) => {
                    state.ingest(&name, &origin, generation, entries, now)
                }
                // Malformed or oversized: skip until the writer replaces it.
                Outcome::Ignored => {}
                Outcome::Failed => {
                    // Transient sharing violations retry; a persistently
                    // unreadable file waits for its next change.
                    let writer = state.writers.entry(name).or_default();
                    writer.failures = writer.failures.saturating_add(1);
                    if writer.failures >= 3 {
                        writer.stamp = Some(stamp);
                    }
                    continue;
                }
            }
            let writer = state.writers.entry(name).or_default();
            writer.failures = 0;
            writer.stamp = Some(stamp);
        }
        if state.writers.len() > MAX_WRITERS {
            state
                .writers
                .retain(|name, _| present.contains(name.as_str()));
        }
        // Polls held until the writers' files were adopted may answer now.
        let adopted = std::mem::replace(&mut state.baseline, false);
        drop(state);
        if adopted {
            self.notify();
        }
    }

    fn persist(&self, now: u64, force: bool) {
        let _order = lock(&self.persist);
        let (bytes, version, head) = {
            let state = lock(&self.state);
            // Undelivered deletions and visibility changes are written
            // promptly (at most once per second), a stream of plain changes
            // at most every 3 s; cursor-only changes can be replayed, so
            // write lazily. A burst that evicted undelivered events holds
            // resetting polls until delivery passes the floor: write at once.
            let interval = if state.floor > state.delivered {
                0
            } else if state.head != state.saved_head {
                let visibility = state
                    .events
                    .range(state.delivered.saturating_add(1)..)
                    .any(|(_, event)| event.kind != Kind::Changed);
                if visibility {
                    PERSIST_MS
                } else {
                    STREAM_PERSIST_MS
                }
            } else {
                LAZY_PERSIST_MS
            };
            let due = force
                || state
                    .last_write
                    .is_none_or(|last| now < last || now >= last.saturating_add(interval));
            if state.version == state.saved || !due {
                return;
            }
            (state.snapshot(), state.version, state.head)
        };
        let written = windows::atomic(&self.journal, &bytes);
        let mut state = lock(&self.state);
        state.last_write = Some(now);
        let (before, was_volatile) = (state.delivered, state.volatile);
        match written {
            Ok(()) => {
                state.saved = version;
                state.saved_head = head;
                state.write_failures = 0;
                state.delivered = state.delivered.max(head);
                if state.volatile && state.head == head {
                    // The file on disk is marked volatile (a reload would
                    // start a new epoch): replace it with an unmarked one.
                    state.volatile = false;
                    state.version += 1;
                }
            }
            Err(_) => {
                state.write_failures += 1;
                if !state.volatile && state.write_failures >= 2 {
                    state.rotate_epoch();
                }
            }
        }
        let rotated = state.volatile && !was_volatile;
        let wake = rotated || state.delivered != before;
        drop(state);
        if rotated {
            crate::log_event(
                &self.root,
                &json!({"event":"records.journal","state":"write_failed"}),
            );
        }
        if wake {
            self.notify();
        }
    }

    fn notify(&self) {
        self.wake
            .send_modify(|generation| *generation = generation.wrapping_add(1));
    }

    /// Releases every pending poll with `{closing:true}` and stops the worker.
    pub fn close(&self) {
        self.closing.store(true, Ordering::SeqCst);
        self.notify();
        self.wake_worker();
    }

    /// Final shutdown flush: groups still inside their quiet window are
    /// committed so the next service instance delivers them. A failed hub
    /// writes nothing: its last good journal and the writers' own files let
    /// the next instance catch up.
    pub fn finish(&self, now: u64) {
        self.close();
        if self.failed.load(Ordering::SeqCst) {
            return;
        }
        lock(&self.state).flush(now, true);
        self.persist(now, true);
    }

    /// Spends one publish of `events` from both this connection's and the
    /// service-wide budget, or from neither.
    pub fn admit_publish(&self, connection: &mut Throttle, events: usize, now: Instant) -> bool {
        // An oversized list is refused by validation, not by the budget.
        let events = events.min(MAX_PUBLISH_EVENTS);
        let mut global = lock(&self.budget);
        if !connection.fits(events, now) || !global.fits(events, now) {
            return false;
        }
        connection.take(events, now);
        global.take(events, now);
        true
    }

    pub async fn poll(&self, query: Query) -> Value {
        let now = now_ms();
        // Leave the idle (1 s) scan cadence as soon as a client polls again.
        if now.saturating_sub(self.last_poll.swap(now, Ordering::Relaxed)) >= IDLE_AFTER_MS {
            self.wake_worker();
        }
        let mut wake = self.wake.subscribe();
        let deadline = tokio::time::Instant::now() + Duration::from_millis(query.wait_ms);
        let mut cursor = query.cursor;
        loop {
            wake.borrow_and_update();
            if self.closing.load(Ordering::SeqCst) {
                return closing(&query);
            }
            let expired = tokio::time::Instant::now() >= deadline;
            let reply = {
                let state = lock(&self.state);
                // A new epoch adopts the writers' files as known on its
                // first scan. Answered earlier, a reset would start the
                // client's own catch-up before entries the scan then
                // adopts, and those would never be delivered: hold (or,
                // out of time, say `closing` so the client asks again).
                if state.baseline {
                    None
                } else {
                    let reply = state.query(&query, cursor);
                    // An eviction burst moved the floor past delivery: a
                    // reset now would only reset again until the journal
                    // write lands (at once, on the next tick). Hold it.
                    let early = reply["reset"] == true && state.floor > state.delivered;
                    Some((reply, early))
                }
            };
            match reply {
                None if expired => return closing(&query),
                Some((reply, early)) => {
                    if expired
                        || (!early
                            && (reply["reset"] == true
                                || reply["events"].as_array().is_some_and(|e| !e.is_empty())))
                    {
                        return reply;
                    }
                    if !early {
                        // Filtered-out events (own origin, other hosts) are
                        // skipped for good.
                        cursor = reply["cursor"].as_u64();
                    }
                }
                None => {}
            }
            if let Ok(Err(_)) = tokio::time::timeout_at(deadline, wake.changed()).await {
                return closing(&query);
            }
        }
    }

    /// Enqueues a writer's reports into the same (host, id) groups as its
    /// signal file: the same quiet window, maximum delay and merge/commit
    /// rules, and one origin per writer however many paths reported it.
    /// All or nothing: refused while closing or when `pending` would need
    /// an eviction; the writer's signal file still carries the reports.
    pub fn publish(&self, publish: Publish, now: u64) -> Published {
        let accepted = publish.entries.len();
        let was_idle = {
            let mut state = lock(&self.state);
            // Checked under the lock: `finish` sets `closing` before its final
            // flush, so a report is either flushed and journaled or refused.
            if self.closing.load(Ordering::SeqCst) {
                return Published::Closing;
            }
            if state.pending.len() + state.new_groups(&publish.entries) > MAX_PENDING {
                return Published::Busy;
            }
            let was_idle = state.pending.is_empty();
            for entry in publish.entries {
                state.enqueue(&publish.profile, entry, now);
            }
            was_idle
        };
        // Leave the idle (1 s) scan cadence: the quiet window, not the scan
        // interval, bounds delivery. At most once per empty→pending edge.
        if was_idle && accepted > 0 {
            self.wake_worker();
        }
        Published::Accepted(accepted)
    }

    pub fn status(&self) -> Value {
        let (panics, failed) = (
            self.panics.load(Ordering::SeqCst),
            self.failed.load(Ordering::SeqCst),
        );
        let health = match (failed, panics) {
            (true, _) => "failed",
            (false, 0) => "ok",
            (false, _) => "degraded",
        };
        let worker = lock(&self.worker)
            .as_ref()
            .is_some_and(|worker| !worker.is_finished());
        let state = lock(&self.state);
        json!({"cursor":state.delivered,"head":state.head,"floor":state.floor,
               "threads":state.events.len(),"pending":state.pending.len(),
               "writers":state.writers.len(),"volatile":state.volatile,
               "health":health,"panics":panics,"worker":worker,
               "ticks":self.ticks.load(Ordering::Relaxed),
               "closing":self.closing.load(Ordering::SeqCst)})
    }
}
