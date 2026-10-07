"""Select a headless-validated experimental runtime for future profile launches."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from manager_core.runtime_build import load_release
from manager_core.runtime_migrations import require_compatible
from manager_core.store import atomic_json

ROOT = Path(__file__).resolve().parents[1]
RUNTIMES = 'artifacts/manager-runtime'
CROSS_BUILD = 'linux-cross-xwin'

SHARED_CHECKS = frozenset({'canonical_list', 'both_profiles_can_open', 'selected_credentials_used',
    'effort_applied_to_requests', 'same_record_contains_both_replies',
    'source_credentials_and_config_unchanged', 'cold_resume_keeps_effort', 'cold_resume_keeps_title',
    'paged_history_available', 'cold_resume_keeps_model', 'live_membership_matches_list',
    'live_membership_preserves_observer', 'live_name_uses_canonical_id',
    'live_removal_uses_canonical_id', 'activity_remains_observable'})


def release_problem(release):
    """Why this release must never become the active runtime, or None.

    Revision 118 activated a cross build whose migrations were never compared
    with the live stores (it had no 'migrations' key), and every profile exited
    at startup. An empty list would make that comparison just as vacuous.
    """
    if release.get('build_profile') != 'release':
        return 'Only release builds can be activated (build_profile: %r).' % (release.get('build_profile'),)
    migrations = release.get('migrations')
    if not isinstance(migrations, list) or not migrations:
        return ('The release records no embedded migrations, so the live stores cannot be checked '
                'against it. Stage it again.')
    if release.get('build_origin') == CROSS_BUILD:
        checks = release.get('pe_checks')
        executables = [name[:-4] for name in release.get('files', {}) if name.endswith('.exe')]
        if (not isinstance(checks, dict) or not executables
                or any(not isinstance(checks.get(name), dict) for name in executables)):
            return 'A cross-built release needs recorded PE checks for every executable.'
    return None


def known_bad(root):
    """Entries of known-bad.json; an unreadable list refuses rather than allows."""
    path = Path(root) / RUNTIMES / 'known-bad.json'
    if not path.is_file():
        return []
    try:
        entries = json.loads(path.read_text(encoding='utf-8-sig')).get('releases')
        if not isinstance(entries, list):
            raise ValueError
    except (OSError, ValueError, AttributeError):
        raise ValueError('known-bad.json is unreadable; fix or remove it first.') from None
    return [entry for entry in entries if isinstance(entry, dict)]


def known_bad_entry(root, release):
    """The known-bad entry naming this release folder or this exact codex.exe."""
    name = Path(release.get('runtime') or '').parent.name
    for entry in known_bad(root):
        if ((entry.get('sha256') and entry['sha256'] == release.get('sha256'))
                or (entry.get('release') and entry['release'] == name)):
            return entry
    return None


def require_activatable(root, release):
    """Gate shared by activation and rollback, before any pointer changes."""
    problem = release_problem(release)
    if problem is None and (entry := known_bad_entry(root, release)) is not None:
        problem = 'This runtime is listed in known-bad.json (%s).' % (entry.get('reason') or 'no reason recorded')
    if problem is not None:
        raise ValueError(problem)
    # Stores may have been migrated since staging; re-check right before the switch.
    try:
        require_compatible(release['migrations'], root)
    except RuntimeError as error:
        raise ValueError(str(error)) from None


def replace_current(root, release):
    """Point future launches at `release` and return the replaced release.

    The replaced runtime becomes previous.json only while its files still
    verify: a pointer to a deleted or changed release is no fallback.
    """
    pointer = Path(root) / RUNTIMES / 'current.json'
    replaced = None
    if pointer.is_file():
        try:
            replaced = load_release(root, pointer)
        except (OSError, ValueError, KeyError, TypeError, RuntimeError):
            replaced = None
    if replaced is not None:
        atomic_json(pointer.with_name('previous.json'), replaced)
    atomic_json(pointer, release)
    return replaced


def activate(candidate, evidence, root=ROOT, *, canonical_evidence=None):
    root = Path(root).resolve()
    candidate = Path(candidate).resolve(strict=True)
    evidence = Path(evidence).resolve(strict=True)
    if not candidate.is_relative_to(root / RUNTIMES / 'releases'):
        raise ValueError('Candidate must belong to the manager runtime releases directory.')
    if not evidence.is_relative_to(root / 'artifacts/results'):
        raise ValueError('Evidence must belong to this workspace.')
    release = load_release(root, candidate)
    require_activatable(root, release)
    report = json.loads(evidence.read_text(encoding='utf-8-sig'))
    transfers = report.get('transfers', [])
    checks = report.get('checks', {})
    shared = release.get('capabilities', {}).get('shared_record_execution') is True
    behavior_verified = (report.get('validation_kind') == 'shared_record_execution_v1'
        and SHARED_CHECKS <= checks.keys() and report.get('history_mode') == 'paginated') if shared else (
        len(transfers) == 2 and all(item.get('writer_release_verified') is True
            and item.get('binding_reloaded') is True and item.get('root_thread_id') == report.get('parent_thread_id')
            for item in transfers))
    if (report.get('status') != 'PASS' or not report.get('finished_at')
            or report.get('runtime_sha256') != release['sha256']
            or not checks or any(value is not True for value in checks.values())
            or not behavior_verified):
        raise ValueError('Completed same-runtime behavior validation is required.')
    if release.get('capabilities', {}).get('canonical_record_storage'):
        if canonical_evidence is None:
            raise ValueError('Canonical storage migration and four-client validation is required.')
        canonical_evidence = Path(canonical_evidence).resolve(strict=True)
        if not canonical_evidence.is_relative_to(root / 'artifacts/results'):
            raise ValueError('Canonical storage evidence must belong to this workspace.')
        canonical_report = json.loads(canonical_evidence.read_text(encoding='utf-8-sig'))
        required = {'original_and_three_profiles_same_unique_ids', 'project_membership_shared',
            'old_profile_body_migrated', 'imported_history_index_rebuilt', 'managed_context_refresh', 'original_context_refresh',
            'profile_to_profile_context_refresh', 'each_profile_uses_selected_account',
            'chosen_model_effort_and_account', 'rename_visible_in_original',
            'section_order_pagination_supported', 'ancestry_supported', 'no_profile_rollout_copies',
            'all_processes_stay_alive', 'last_model_effort_after_reopen', 'durable_ordinals_increase'}
        verified = canonical_report.get('checks', {})
        if (canonical_report.get('status') != 'PASS' or not canonical_report.get('finished_at')
                or canonical_report.get('validation_kind') != 'canonical_storage_v1'
                or canonical_report.get('runtime_sha256') != release['sha256']
                or not required <= verified.keys() or any(value is not True for value in verified.values())):
            raise ValueError('Completed same-runtime canonical storage validation is required.')
        release['canonical_storage_evidence'] = str(canonical_evidence)
    release.update(validation='experimental-headless-shared-editing' if shared else 'experimental-headless-handoff', evidence=str(evidence),
                   activated_at=datetime.now(timezone.utc).isoformat(),
                   full_workspace_regression='not_passed',
                   gui_acceptance='not_completed')
    replace_current(root, release)
    return dict(runtime=release['runtime'], sha256=release['sha256'],
                validation=release['validation'], running_profiles_restarted=False)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--canonical-evidence', type=Path)
    args = parser.parse_args()
    print(json.dumps(activate(args.candidate, args.evidence, canonical_evidence=args.canonical_evidence)))
