"""Exact desktop effort-picker compatibility; never open a real desktop."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core.desktop_reasoning_ui import patch


class DesktopReasoningTests(unittest.TestCase):
    def fixture(self, binding):
        return (b'function picker({isCustomModelProvider:s=!1,models:c,useHiddenModels:l}){'
                b'let e=o?r.supportedReasoningEfforts:r.supportedReasoningEfforts.filter(({reasoningEffort:e})=>e!==`ultra`);'
                b'return e.filter(({reasoningEffort:e})=>' + binding + b'(e)&&i.has(e))}')

    def test_verified_bindings_preserve_effort_validation_and_native_gating(self):
        for binding in (b'gj', b'WXn', b'UXn', b'vw'):
            with self.subTest(binding=binding):
                result = patch(self.fixture(binding))
                self.assertIn(b'let e=o||s?', result)
                self.assertIn(binding + b'(e)&&(s||i.has(e))', result)
                self.assertIn(b'e!==`ultra`', result)

    def test_26_917_picker_shape_is_patched_once(self):
        # 26.917 renamed only the minified helpers (O9n/k9n/UXn -> wVn/TVn/vw).
        source = (b'function vw(e){return e===`none`||e===`minimal`||e===`low`||e===`medium`||e===`high`||e===`xhigh`||'
                  b'e===`max`||e===`ultra`||e===`persistent`}'
                  b'function wVn({additionalAvailableModels:e,authMethod:t,availableModels:n,defaultModel:r,'
                  b'enabledReasoningEfforts:i,hasConfiguredModelCatalog:a,includeUltraReasoningEffort:o,'
                  b'isCustomModelProvider:s=!1,models:c,useHiddenModels:l}){let u=[],d=null;return c.forEach(r=>{'
                  b'if(TVn({additionalAvailableModels:e,authMethod:t,availableModels:n,hasConfiguredModelCatalog:a,'
                  b'isCustomModelProvider:s,model:r,useHiddenModels:l})){let e=o?r.supportedReasoningEfforts:'
                  b'r.supportedReasoningEfforts.filter(({reasoningEffort:e})=>e!==`ultra`),n=(t===`copilot`?'
                  b'[e.find(e=>e.reasoningEffort===`medium`)??{reasoningEffort:`medium`,description:`medium effort`}]:e)'
                  b'.filter(({reasoningEffort:e})=>vw(e)&&i.has(e)),a={...r,supportedReasoningEfforts:n};u.push(a)}}),'
                  b'{models:u,defaultModel:d}}')
        result = patch(source)
        self.assertEqual(result.count(b'let e=o||s?r.supportedReasoningEfforts:'), 1)
        self.assertEqual(result.count(b'=>vw(e)&&(s||i.has(e))),a={...r,'), 1)
        self.assertEqual(len(result), len(source) + len(b'||s') + len(b'(s||)'))

    def test_unknown_or_duplicate_picker_is_rejected(self):
        for source in (self.fixture(b'unknown'), self.fixture(b'UXn')*2, self.fixture(b'vw')*2,
                       self.fixture(b'UXn') + self.fixture(b'vw')):
            with self.subTest(source=source[-40:]), self.assertRaises(ValueError):
                patch(source)
        with self.assertRaisesRegex(ValueError, 'not verified'):
            patch(self.fixture(b'unknown'))
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            patch(self.fixture(b'UXn') + self.fixture(b'vw'))


if __name__ == '__main__':
    unittest.main()
