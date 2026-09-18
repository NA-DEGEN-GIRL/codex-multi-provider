"""Cached account views and explicit Codex-only quota collection via llm-usage."""
import asyncio
from datetime import datetime, timezone
from pathlib import Path
import sys
import threading


_REFRESH_BUDGET = 45.0
_MIN_START_BUDGET = 35.0
_CLEANUP_GRACE = 1.0
_REFRESH_LOCK = threading.Lock()
_ERROR_MESSAGES = {
    'cli_missing': 'Codex CLI를 찾지 못했습니다.',
    'not_authenticated': '이 계정의 Codex 로그인이 필요합니다.',
    'unsupported': '설치된 CLI에서 사용량 조회를 지원하지 않습니다.',
    'unsafe_profile_config': '계정 설정을 안전하게 읽을 수 없습니다.',
    'auth_env_conflict': '계정별 인증과 현재 실행 환경 설정이 충돌합니다.',
    'startup_required': 'llm-usage에서 이 계정의 Codex 초기 준비를 완료한 뒤 새로고침하세요.',
    'timeout': '사용량 조회 시간이 초과되었습니다. 저장된 값은 참고용입니다.',
    'batch_deferred': '이번 조회 시간 안에 시작하지 못했습니다. 다시 새로고침하면 순서가 바뀝니다.',
    'command_failed': '이 계정의 사용량 조회를 시작하지 못했습니다. 다른 조회가 실행 중일 수 있습니다.',
    'invalid_response': '사용량 응답 형식을 확인하지 못했습니다.',
    'network': '사용량 서버에 연결하지 못했습니다.',
    'expired_snapshot': '저장된 사용량의 갱신 시점이 지났습니다.',
    'profile_error': '등록된 계정의 로컬 설정을 확인하지 못했습니다.',
    'internal': '사용량을 읽거나 저장하지 못했습니다.',
}


def _value(item):
    return getattr(item, 'value', item)


def _safe_error(error):
    """Do not forward arbitrary cached/provider error text to state or logs."""
    if error is None:
        return None
    raw_code = error.get('code') if isinstance(error, dict) else getattr(error, 'code', error)
    code = _value(raw_code)
    if not isinstance(code, str) or code not in _ERROR_MESSAGES:
        code = 'internal'
    return {'code': code, 'message': _ERROR_MESSAGES[code]}


def _usage(snapshot, *, cached=True):
    usage = dict(windows=[], observed_at=None, freshness='unknown', error=None)
    if snapshot is None:
        return usage
    at = datetime.now(timezone.utc)
    windows = [w.to_dict() for w in snapshot.windows if not w.is_expired(at)]
    freshness = _value(snapshot.freshness)
    usage.update(windows=windows,
                 observed_at=snapshot.observed_at.isoformat() if snapshot.observed_at else None,
                 error=_safe_error(snapshot.error))
    if windows:
        if cached:
            usage['freshness'] = 'stale' if freshness == 'stale' or snapshot.error else 'cached'
        else:
            usage['freshness'] = freshness if freshness in ('live', 'cached', 'stale') else 'unknown'
    return usage


class Accounts:
    def __init__(self, root):
        self.root = Path(root)
        source = self.root.parent / 'llm-usage/src'
        if source.is_dir() and str(source) not in sys.path:
            sys.path.insert(0, str(source))

    @staticmethod
    def _storage():
        try:
            from llm_usage.storage import StateStore
            return StateStore()
        except ImportError:
            raise RuntimeError('llm-usage의 Python 모듈을 찾을 수 없습니다.') from None

    @staticmethod
    def _dependencies():
        try:
            from llm_usage.storage import StateStore
            from llm_usage.providers.codex import CodexUsageAdapter
            return StateStore, CodexUsageAdapter
        except ImportError:
            raise RuntimeError('llm-usage의 Codex 사용량 조회 모듈을 찾을 수 없습니다.') from None

    def list(self):
        try:
            store = self._storage()
            registry = store.load_registry()
            output = []
            for account in registry.accounts:
                if _value(account.provider) != 'codex':
                    continue
                usage = dict(windows=[], observed_at=None, freshness='unknown', error=None)
                try:
                    snapshot = store.load_snapshot(account.id)
                    if snapshot and (snapshot.account_id != account.id or _value(snapshot.provider) != 'codex'):
                        raise ValueError('Mismatched cached account.')
                    usage = _usage(snapshot)
                except (OSError, ValueError, RuntimeError, AttributeError, TypeError):
                    usage['error'] = _safe_error('internal')
                output.append(dict(id=account.id, alias=account.alias, home=str(account.profile_dir),
                                   usage=usage, source='llm-usage'))
            return output
        except (OSError, ValueError):
            raise RuntimeError('llm-usage의 계정 목록을 읽지 못했습니다.') from None

    def sync(self, manager_store):
        return self._sync_accounts(manager_store, self.list())

    @staticmethod
    def _sync_accounts(manager_store, accounts):
        for account in accounts:
            manager_store.add_profile(account['alias'], account['id'], account['home'])
        by_id = {a['id']: a for a in accounts}
        def sync(data):
            for profile in data['profiles']:
                # A legacy usage ID is an import reference, not proof that the
                # account later logged into this Windows profile is the same
                # account. Windows-owned aliases and native credentials remain
                # authoritative until an explicit identity match is established.
                if profile.get('auth_mode') == 'native' or profile.get('alias_authority') == 'manager':
                    continue
                uid = profile.get('usage_account_id')
                if not uid:
                    continue
                account = by_id.get(uid)
                profile['account_missing'] = account is None
                if account:
                    profile['alias'] = account['alias']
                    from .native_usage import newer
                    profile['usage'] = newer(profile.get('usage'), account['usage'])
            return dict(accounts=len(accounts), message='llm-usage의 계정 목록과 저장된 사용량을 확인했습니다.')
        return manager_store.mutate(sync)

    @staticmethod
    def _failure(store, profile, code):
        try:
            snapshot = store.load_snapshot(profile.id)
            if snapshot and (snapshot.account_id != profile.id or _value(snapshot.provider) != 'codex'):
                snapshot = None
            usage = _usage(snapshot)
        except (OSError, ValueError, RuntimeError, AttributeError, TypeError):
            usage = _usage(None)
        usage['error'] = _safe_error(code)
        usage['freshness'] = 'stale' if usage['windows'] else 'unknown'
        return {'id': profile.id, 'usage': usage}

    async def _collect_codex(self, store, adapter, profiles):
        """Mirror llm-usage's safe Codex scheduling without invoking other adapters."""
        ids = tuple(p.id for p in profiles)
        first = store.advance_codex_collection_cursor(ids)
        ordered = list(profiles)
        if first in ids:
            start = ids.index(first)
            ordered = ordered[start:] + ordered[:start]
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _REFRESH_BUDGET
        results = []
        for profile in ordered:
            try:
                if loop.time() >= deadline:
                    results.append(self._failure(store, profile, 'batch_deferred'))
                    continue
                identities, error = adapter.startup_precheck(profile)
                if error is not None:
                    results.append(self._failure(store, profile, error))
                    continue
                if not identities or not store.codex_startup_is_ready(
                        profile.id, binary_identity=identities[0], state_identity=identities[1]):
                    results.append(self._failure(store, profile, 'startup_required'))
                    continue
                remaining = deadline - loop.time()
                if remaining < min(_MIN_START_BUDGET, _REFRESH_BUDGET):
                    results.append(self._failure(store, profile, 'batch_deferred'))
                    continue
                timeout = min(_REFRESH_BUDGET, remaining)
                async with asyncio.timeout(timeout + _CLEANUP_GRACE):
                    result = await adapter.collect(profile, store=store, timeout=timeout,
                                                   now=datetime.now(timezone.utc))
                if result.account_id != profile.id or _value(result.provider) != 'codex':
                    results.append(self._failure(store, profile, 'invalid_response'))
                    continue
                results.append({'id': profile.id, 'usage': _usage(result, cached=False)})
            except TimeoutError:
                results.append(self._failure(store, profile, 'timeout'))
            except Exception:
                # Never include collector exceptions: they may include API data,
                # identity or authentication-bearing paths.
                results.append(self._failure(store, profile, 'internal'))
        by_id = {r['id']: r for r in results}
        return [by_id[p.id] for p in profiles]

    def refresh_live(self, manager_store):
        """Explicit quota-only refresh; returns sanitized data and updates manager state.

        The adapter owns account/rateLimits/read, per-profile collector exclusion,
        app-server cleanup and successful snapshot persistence. This method never
        calls its warm/login APIs or creates an inference turn.
        """
        if not _REFRESH_LOCK.acquire(blocking=False):
            raise RuntimeError('사용량 새로고침이 이미 실행 중입니다.')
        try:
            StoreType, AdapterType = self._dependencies()
            store = StoreType()
            registry = store.load_registry()
            native_ids={p.get('usage_account_id') for p in manager_store.read()['profiles'] if p.get('auth_mode')=='native' or p.get('removed_at')}
            profiles = [p for p in registry.accounts if _value(p.provider) == 'codex' and p.id not in native_ids]
            if profiles:
                adapter = AdapterType()
                with store.bounded_snapshot_persistence(lock_wait_budget=0.25):
                    results = asyncio.run(self._collect_codex(store, adapter, profiles))
            else:
                results = []
            by_id = {r['id']: r['usage'] for r in results}
            accounts = [dict(id=p.id, alias=p.alias, home=str(p.profile_dir), usage=by_id[p.id], source='llm-usage')
                        for p in profiles]
            self._sync_accounts(manager_store, accounts)
            refreshed = sum(r['usage']['freshness'] == 'live' for r in results)
            failed = sum(r['usage']['error'] is not None for r in results)
            deferred = sum((r['usage']['error'] or {}).get('code') == 'batch_deferred' for r in results)
            return dict(accounts=len(profiles), refreshed=refreshed, failed=failed, deferred=deferred,
                        results=results, message=f'Codex 계정 {len(profiles)}개 중 {refreshed}개의 최신 사용량을 읽었습니다.')
        except Exception:
            raise RuntimeError('사용량 새로고침을 완료하지 못했습니다. 잠시 후 다시 시도하세요.') from None
        finally:
            _REFRESH_LOCK.release()
