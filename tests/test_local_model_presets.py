"""Offline specification boundaries and preset lookup; no inference requests."""
import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core import local_model_presets as presets


class LocalModelPresetTests(unittest.TestCase):
    def test_only_exact_verified_checkpoints_are_available_in_stable_order(self):
        rows = presets.list_presets()
        self.assertEqual([(row['id'], row['model_id']) for row in rows], [
            ('qwen3.8-flash-next', 'Qwen/Qwen3.8-Flash-Next'),
            ('deepseek-v4.1-flash', 'deepseek-ai/DeepSeek-V4.1-Flash'),
            ('glm-5.3-flash', 'zai-org/GLM-5.3-Flash'),
        ])
        self.assertEqual(json.loads(json.dumps(rows, allow_nan=False)), rows)
        for row in rows:
            self.assertEqual(row, presets.get_preset(row['id']))
            self.assertEqual(row['verified_at'], '2026-09-29')
            self.assertTrue(row['sources'])
            self.assertTrue(all(source['url'].startswith('https://') for source in row['sources']))
            self.assertNotIn('base_url', row)
            self.assertNotIn('served_model_name', row)

    def test_qwen_maximum_requires_extension_and_does_not_replace_native_capacity(self):
        row = presets.get_preset('qwen3.8-flash-next')
        self.assertEqual(row['native_context_tokens'], 262144)
        self.assertEqual(row['max_context_tokens'], 1000000)
        self.assertTrue(row['context_extension_required'])
        self.assertFalse(row['context_extension']['applied_in_checkpoint'])
        self.assertEqual(row['context_extension']['factor'], 4.0)

    def test_deepseek_extension_is_already_in_checkpoint_and_glm_needs_no_added_yarn(self):
        deepseek = presets.get_preset('deepseek-v4.1-flash')
        glm = presets.get_preset('glm-5.3-flash')
        for row in (deepseek, glm):
            self.assertEqual(row['native_context_tokens'], 1048576)
            self.assertEqual(row['max_context_tokens'], 1048576)
            self.assertFalse(row['context_extension_required'])
        self.assertEqual(deepseek['context_extension']['original_max_position_embeddings'], 65536)
        self.assertEqual(deepseek['context_extension']['factor'], 16)
        self.assertTrue(deepseek['context_extension']['applied_in_checkpoint'])
        self.assertIsNone(glm['context_extension'])

    def test_logical_max_maps_to_model_specific_native_effort(self):
        rows = presets.list_presets()
        self.assertEqual([row['default_reasoning_effort'] for row in rows], ['max', 'max', 'max'])
        self.assertEqual([row['native_reasoning_effort'] for row in rows], ['xhigh', 100, 'max'])
        self.assertIs(type(rows[1]['native_reasoning_effort']), int)
        self.assertNotIn('max', rows[0]['native_reasoning_levels'])

    def test_recommendations_never_become_hard_output_caps(self):
        qwen, deepseek, glm = presets.list_presets()
        self.assertTrue(all(row['max_output_tokens'] is None for row in (qwen, deepseek, glm)))
        self.assertEqual(qwen['output_recommendation']['reasoning_tokens'], 262144)
        self.assertEqual(qwen['output_recommendation']['final_tokens'], 131072)
        self.assertTrue(qwen['output_recommendation']['requires_separate_budget_support'])
        self.assertEqual(deepseek['output_recommendation']['kind'], 'minimum_generation_budget')
        self.assertEqual(deepseek['output_recommendation']['minimum_generation_tokens'], 262144)
        self.assertIsNone(glm['output_recommendation'])

    def test_aliases_and_unknown_or_malformed_inputs_cannot_select_a_different_model(self):
        invalid = [None, True, 100, [], {}, '', 'max', 'Qwen3.8', 'qwen3.5-flash-next',
                   'qwen3.8-27b', 'deepseek-v4-flash', 'glm-5.3', 'GLM-5.3-Flash',
                   'qwen3.8-flash-next ', '\nqwen3.8-flash-next',
                   'https://example.invalid/v1/private-credential']
        invalid += [row['model_id'] for row in presets.list_presets()]
        invalid += [alias for row in presets.list_presets() for alias in row['aliases']]
        for value in invalid:
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, '^Unknown local model preset ID\\.$'):
                presets.get_preset(value)

    def test_nested_results_are_independent_and_mutations_cannot_change_future_defaults(self):
        first = presets.list_presets()
        first[0]['context_extension']['factor'] = 0
        first[0]['sources'][0]['url'] = 'https://untrusted.invalid'
        first[0]['aliases'].append('different model')
        first[0]['native_context_tokens'] = 8
        second = presets.get_preset('deepseek-v4.1-flash')
        second['native_reasoning_levels']['maximum'] = 5
        self.assertEqual(presets.get_preset('qwen3.8-flash-next')['context_extension']['factor'], 4.0)
        self.assertEqual(presets.get_preset('qwen3.8-flash-next')['native_context_tokens'], 262144)
        self.assertNotIn('different model', presets.get_preset('qwen3.8-flash-next')['aliases'])
        self.assertEqual(presets.get_preset('deepseek-v4.1-flash')['native_reasoning_levels']['maximum'], 100)
        self.assertEqual(presets.list_presets()[0]['sources'][0]['url'], 'https://huggingface.co/Qwen/Qwen3.8-Flash-Next')

    def test_glm_parser_configuration_is_engine_specific(self):
        engines = {row['engine']: row for row in presets.get_preset('glm-5.3-flash')['engine_support']}
        self.assertEqual(engines['vllm']['reasoning_parser'], 'glm47')
        self.assertEqual(engines['sglang']['reasoning_parser'], 'glm45')
        self.assertEqual({row['tool_call_parser'] for row in engines.values()}, {'glm47'})

    def test_import_and_queries_do_not_open_network_connections(self):
        spec = importlib.util.spec_from_file_location('isolated_local_model_presets', presets.__file__)
        module = importlib.util.module_from_spec(spec)
        with patch('socket.create_connection', side_effect=AssertionError('network forbidden')), \
                patch('socket.socket', side_effect=AssertionError('network forbidden')), \
                patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden')):
            spec.loader.exec_module(module)
            self.assertEqual(len(module.list_presets()), 3)
            self.assertEqual(module.get_preset('glm-5.3-flash')['native_reasoning_effort'], 'max')


if __name__ == '__main__':
    unittest.main()
