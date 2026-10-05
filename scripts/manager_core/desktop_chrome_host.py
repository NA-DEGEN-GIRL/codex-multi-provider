"""Codex Chrome extension native host for managed desktop copies.

Chrome starts the extension host named by the v1 manifest that
HKCU\\Software\\Google\\Chrome\\NativeMessagingHosts\\com.openai.codexextension
points to. The host then reads %LOCALAPPDATA%\\OpenAI\\Codex\\
chrome-native-hosts-v2.json to find a Codex CLI and CODEX_HOME. The desktop
writes both files after its bundled ``chrome`` plugin is installed.

Managed profiles never got that far. Their bundled marketplace is staged under
CODEX_HOME, the manager root may contain '#', and the runtime reads the text
after the last '#' of a marketplace source as a git ref. Registering
``openai-bundled`` failed in every profile, so chrome and computer-use were
never installed. The app already retries its other marketplace adds with a
trailing '#' (an empty ref) after exactly that error; the bundled add gets the
same retry. A runtime that accepts the plain path never sees the retry.

With the add fixed, every managed desktop would register itself: the v1
manifest would follow whichever profile synced last, and v2 would collect one
entry per profile and per managed copy. Only the desktop the manager marks with
``CODEX_MANAGER_CHROME_NATIVE_HOST=1`` registers. Its registration drops every
other entry written by a managed copy of this manager root (older copies and
previous owners). Entries of other installs, such as the official app, stay.
"""

ENV = 'CODEX_MANAGER_CHROME_NATIVE_HOST'

# Present in every desktop build that has the code each patch changes. When a
# marker is present, its patch must apply (or already be applied) exactly once.
_ADD_FAILED = b'bundled_plugins_marketplace_add_failed'
_HOSTS_FILE = b'chrome-native-hosts-v2.json'
_REF_ERROR = b'--ref is only supported for git marketplace sources'

# Bundled marketplace registration (Ra in 26.917): its options binding.
_ADD_VARIANTS = ((b'e',),)
# Native-host sync (26.917): the sync entry g2 and its extension-id helper WS;
# the v2 writer's same-install test w2 and that module's node:path binding s.
_HOST_VARIANTS = ((b'g2', b'WS', b'w2', b's'),
                  (b'kF', b'r.Wr', b'LF', b'g'))  # 26.930 (bootstrap module)


def _add(e):
    before = (b'(await %(e)s.appServerConnection.addMarketplace({source:%(e)s.materializedMarketplace.marketplaceRoot},'
              b'...%(e)s.beforeSendRequest?[%(e)s.beforeSendRequest]:[])).marketplaceName') % {b'e': e}
    # Same recovery as the app's own marketplace add: only the ref error, only
    # for a source that contains '#', and a trailing '#' is an empty ref.
    after = (b'(await(async(c,m,o)=>{try{return await c.addMarketplace({source:m},...o)}catch(r){'
             b'if(typeof m!=`string`||!m.includes(`#`)||!String(r?.message??r).includes(`' + _REF_ERROR + b'`))throw r;'
             b'return await c.addMarketplace({source:`${m}#`},...o)}})(%(e)s.appServerConnection,'
             b'%(e)s.materializedMarketplace.marketplaceRoot,%(e)s.beforeSendRequest?[%(e)s.beforeSendRequest]:[]))'
             b'.marketplaceName') % {b'e': e}
    return before, after


def _gate(sync, extensions):
    before = b'async function %s(e){let t=[...new Set([...e.extensionIds,...%s(e.nativeHostName)])]' % (sync, extensions)
    after = before.replace(b'{let t=', b'{if(process.env.' + ENV.encode() + b'!==`1`)return;let t=', 1)
    return before, after


def _exclusive(same_install, path):
    before = b'entries:[...r.entries.filter(t=>!%s(t,e.resource)),a]' % same_install
    # A managed copy's resources live under <root>/artifacts/managed-desktop.
    after = (b'entries:[...r.entries.filter(t=>!%(w)s(t,e.resource)&&!((m,p)=>typeof m==`string`&&m!==``&&'
             b'typeof p==`string`&&(0,%(s)s.resolve)(p).toLowerCase().startsWith((0,%(s)s.resolve)(m,`artifacts`,'
             b'`managed-desktop`).toLowerCase()+%(s)s.sep))(process.env.CODEX_MANAGER_ROOT,t?.paths?.resourcesPath)),a]'
             ) % {b'w': same_install, b's': path}
    return before, after


def _table():
    rows, guards = [], {}
    for names in _ADD_VARIANTS:
        rows.append(('marketplace_add',) + _add(*names))
    for sync, extensions, same_install, path in _HOST_VARIANTS:
        rows.append(('native_host_gate',) + _gate(sync, extensions))
        rows.append(('native_host_exclusive',) + _exclusive(same_install, path))
        # The anchor does not name the path binding the replacement calls;
        # the module must bind exactly that name to node:path at its top.
        guards[rows[-1][1]] = b'let %s=require("node:path");' % path
    return rows, guards


_PATCHES, _GUARDS = _table()
_REQUIRED = {'marketplace_add': _ADD_FAILED, 'native_host_gate': _HOSTS_FILE, 'native_host_exclusive': _HOSTS_FILE}


def patches_for(data):
    """Exact replacements for one module, as {before: (kind, after)}."""
    found = {}
    for kind, before, after in _PATCHES:
        count = data.count(before)
        if count > 1:
            raise ValueError('Ambiguous desktop Chrome native host implementation.')
        if count and before in _GUARDS and data.count(_GUARDS[before]) != 1:
            raise ValueError('Unverified desktop Chrome native host path binding.')
        if count:
            found[before] = (kind, after)
    if len({kind for kind, _ in found.values()}) != len(found):
        raise ValueError('Ambiguous desktop Chrome native host implementation.')
    return found


class Plan:
    """Collect one archive's Chrome host patches and apply them fail-closed."""

    def __init__(self):
        self.modules = []
        self.markers = set()
        self.applied = set()

    def scan(self, name, item, data):
        self.markers.update(kind for kind, marker in _REQUIRED.items() if marker in data)
        self.applied.update(kind for kind, _, after in _PATCHES if after in data)
        patches = patches_for(data)
        if patches:
            self.modules.append((name, item, data, patches))

    def apply(self, changed):
        """Patch ``changed`` ({name: (item, data)}) in place; return what was patched."""
        kinds = [kind for *_, patches in self.modules for kind, _ in patches.values()]
        if len(kinds) != len(set(kinds)):
            raise ValueError('Ambiguous desktop Chrome native host implementation.')
        missing = sorted(kind for kind in self.markers if kind not in kinds and kind not in self.applied)
        if missing:
            raise ValueError('이 Codex 버전의 Chrome 확장 연결 위치를 확인하지 못했습니다: ' + ', '.join(missing))
        for name, item, data, patches in self.modules:
            current = changed.get(name, (item, data))[1]
            for before, (_, after) in patches.items():
                if current.count(before) != 1:
                    raise ValueError('Ambiguous desktop Chrome native host implementation.')
                current = current.replace(before, after)
            changed[name] = (item, current)
        return sorted(kinds)


def owner(profiles):
    """The profile whose desktop registers Chrome: the first in list order that
    signs in with its own ChatGPT login. API-key and borrowed-login profiles
    have no credentials in their CODEX_HOME for the extension's app-server."""
    for profile in profiles or ():
        if (isinstance(profile, dict) and profile.get('auth_mode') == 'native' and not profile.get('removed_at')
                and not profile.get('view_only') and not profile.get('native_login_pending')
                and not profile.get('account_missing')):
            return profile.get('id')
    return None
