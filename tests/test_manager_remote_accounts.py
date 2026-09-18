import copy,sys,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
root=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(root/'scripts'),str(root.parent/'llm-usage/src')]
from remote_helpers import account_alias
from llm_usage.models import Provider
from llm_usage.profiles import ProfileError


class Registry:
    def __init__(self,accounts):self.accounts=accounts
    def find(self,provider,alias):return next((p for p in self.accounts if p.provider==provider and p.alias==alias),None)


class FakeStore:
    def __init__(self,accounts):self.registry=Registry(accounts);self.before_mutation=lambda:None
    def load_registry(self):return copy.deepcopy(self.registry)
    def mutate_registry(self,operation):
        self.before_mutation()
        candidate=copy.deepcopy(self.registry)
        result=operation(candidate);self.registry=candidate
        return result


class AliasTests(unittest.TestCase):
    def account(self,identity,alias):return SimpleNamespace(id=identity,alias=alias,provider=Provider.CODEX)
    def invoke(self,store,expected='correct',alias='04'):
        with patch.object(account_alias,'fingerprint',side_effect=lambda p:p.id):
            return account_alias.execute({'account_fingerprint':expected,'alias':alias,'action':'sync'},store)

    def test_identity_matches_even_when_old_alias_differs(self):
        store=FakeStore([self.account('correct','old')])
        value=self.invoke(store)
        self.assertEqual(store.registry.accounts[0].alias,'04')
        self.assertTrue(value['identity_matched']);self.assertTrue(value['changed'])

    def test_equal_alias_is_not_an_identity_match(self):
        store=FakeStore([self.account('wrong','04')])
        self.assertEqual(self.invoke(store)['state'],'not_found')
        self.assertEqual(store.registry.accounts[0].id,'wrong')

    def test_alias_conflict_preserves_both_accounts(self):
        store=FakeStore([self.account('correct','old'),self.account('wrong','04')])
        with self.assertRaises(ProfileError):self.invoke(store)
        self.assertEqual([p.alias for p in store.registry.accounts],['old','04'])

    def test_identity_is_rechecked_under_registry_write_lock(self):
        store=FakeStore([self.account('correct','old')])
        store.before_mutation=lambda:setattr(store.registry.accounts[0],'id','changed')
        with self.assertRaises(ValueError):self.invoke(store)
        self.assertEqual(store.registry.accounts[0].alias,'old')
