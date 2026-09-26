import sys,unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from manager_core.native_usage import STALE_AFTER, newer, normalize, presentation


class UsageTests(unittest.TestCase):
    def test_codex_bucket_wins_and_no_identity_is_retained(self):
        result=normalize({'secret':'fixture','rateLimits':{'primary':{'usedPercent':99}},
                          'rateLimitsByLimitId':{'codex':{'primary':{'usedPercent':23,'windowDurationMins':300,'resetsAt':123}}}})
        self.assertEqual(result['windows'],[{'label':'5시간','used_percent':23,'remaining_percent':77,'resets_at':123}])
        self.assertNotIn('secret',result)

    def test_missing_and_invalid_windows_are_not_zero_usage(self):
        for value in (None,True,float('nan'),float('inf'),'0'):
            result=normalize({'rateLimits':{'primary':{'usedPercent':value}}})
            self.assertEqual(result['windows'],[])
            self.assertEqual(result['freshness'],'unknown')

    def test_out_of_range_values_are_bounded(self):
        result=normalize({'rateLimits':{'primary':{'usedPercent':103},'secondary':{'usedPercent':-5}}})
        self.assertEqual([w['remaining_percent'] for w in result['windows']],[0,100])

    def test_reset_credits_are_counted_without_credit_identity(self):
        result=normalize({'rateLimits':{'primary':{'usedPercent':10,'windowDurationMins':10080,'resetsAt':500}},
                          'rateLimitResetCredits':{'availableCount':2,'credits':[
                              {'id':'secret-credit-id','status':'available','expiresAt':900},
                              {'id':'other','status':'redeemed','expiresAt':100},
                              {'id':'third','status':'available','expiresAt':700}]}})
        self.assertEqual(result['reset_credits'],{'available':2,'expires_at':700})
        self.assertNotIn('secret-credit-id',str(result))
        self.assertEqual(normalize({'rateLimits':{'primary':{'usedPercent':1}}})['reset_credits'],None)

    def test_zero_reset_credits_are_known_with_or_without_details(self):
        for details in (None, []):
            with self.subTest(details=details):
                result=normalize({'rateLimits':{'primary':{'usedPercent':10}},
                                  'rateLimitResetCredits':{'availableCount':0,'credits':details}})
                self.assertEqual(result['reset_credits'],{'available':0,'expires_at':None})
        self.assertIsNone(normalize({'rateLimits':{'primary':{'usedPercent':10}}})['reset_credits'])

    def test_newer_zero_reset_credits_replace_old_positive_count(self):
        older={'windows':[{'label':'주간','used_percent':50,'remaining_percent':50,'resets_at':1}],
               'observed_at':'2026-09-18T00:00:00+00:00','reset_credits':{'available':2,'expires_at':9}}
        latest={'windows':[{'label':'주간','used_percent':60,'remaining_percent':40,'resets_at':1}],
                'observed_at':'2026-09-18T01:00:00+00:00','reset_credits':{'available':0,'expires_at':None}}
        self.assertEqual(newer(older,latest),latest)
        self.assertEqual(newer(latest,older),latest)
        self.assertEqual(older['reset_credits'],{'available':2,'expires_at':9})

    def test_usage_turns_stale_only_after_a_missed_refresh_cycle(self):
        from manager_core.usage_refresh import UsageRefresh
        self.assertEqual(STALE_AFTER,2*UsageRefresh.INTERVAL)
        def shown(seconds):
            observed=(datetime.now(timezone.utc)-timedelta(seconds=seconds)).isoformat()
            return presentation({'windows':[{'remaining_percent':50}],'observed_at':observed,'freshness':'live'})
        self.assertEqual(shown(UsageRefresh.INTERVAL+60)['freshness'],'live')  # next probe due, not missed
        self.assertEqual(shown(STALE_AFTER+60)['freshness'],'stale')

    def test_newer_snapshot_keeps_credit_count_from_older_reply(self):
        older={'windows':[{'label':'주간','used_percent':50,'remaining_percent':50,'resets_at':1}],
               'observed_at':'2026-09-18T00:00:00+00:00','reset_credits':{'available':1,'expires_at':9}}
        newer_reply={'windows':[{'label':'주간','used_percent':60,'remaining_percent':40,'resets_at':1}],
                     'observed_at':'2026-09-18T01:00:00+00:00','reset_credits':None}
        merged=newer(older,newer_reply)
        self.assertEqual(merged['windows'][0]['used_percent'],60)
        self.assertEqual(merged['reset_credits'],{'available':1,'expires_at':9})
