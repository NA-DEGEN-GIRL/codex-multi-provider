import sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from manager_core.native_usage import normalize


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
