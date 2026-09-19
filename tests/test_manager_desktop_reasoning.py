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
        for binding in (b'gj', b'WXn', b'UXn'):
            with self.subTest(binding=binding):
                result = patch(self.fixture(binding))
                self.assertIn(b'let e=o||s?', result)
                self.assertIn(binding + b'(e)&&(s||i.has(e))', result)
                self.assertIn(b'e!==`ultra`', result)

    def test_unknown_or_duplicate_picker_is_rejected(self):
        for source in (self.fixture(b'unknown'), self.fixture(b'UXn')*2):
            with self.assertRaises(ValueError):
                patch(source)


if __name__ == '__main__':
    unittest.main()
