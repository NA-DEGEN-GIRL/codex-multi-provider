"""Exact desktop effort-picker compatibility; never open a real desktop."""
from pathlib import Path
import json
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core.desktop_reasoning_ui import patch, patch_composer, upgrade_managed


class DesktopReasoningTests(unittest.TestCase):
    def fixture(self, binding):
        return (b'function picker({isCustomModelProvider:s=!1,models:c,useHiddenModels:l}){'
                b'let e=o?r.supportedReasoningEfforts:r.supportedReasoningEfforts.filter(({reasoningEffort:e})=>e!==`ultra`);'
                b'return e.filter(({reasoningEffort:e})=>' + binding + b'(e)&&i.has(e))}' + self.dropdown())

    def dropdown(self):
        # 26.917 npa: the composer's model/effort rows, built from the filtered model list.
        return (b'function rows(e){return e.flatMap(({model:n,supportedReasoningEfforts:r})=>{'
                b'let a=r.flatMap(({reasoningEffort:e})=>vw(e)&&e!==`persistent`?[e]:[]);'
                b'return(a.length>0?a:[`medium`]).map(e=>n+`:`+e)})}')

    def test_verified_bindings_preserve_effort_validation_and_native_gating(self):
        for binding in (b'gj', b'WXn', b'UXn', b'vw', b'pye'):
            with self.subTest(binding=binding):
                result = patch(self.fixture(binding))
                self.assertIn(b'let e=o||s?', result)
                self.assertIn(b'(' + binding + b'(e)||s&&e===`ultracode`)&&(s||i.has(e))', result)
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
                  b'{models:u,defaultModel:d}}') + self.dropdown()
        result = patch(source)
        self.assertEqual(result.count(b'let e=o||s?r.supportedReasoningEfforts:'), 1)
        self.assertEqual(result.count(b'=>(vw(e)||s&&e===`ultracode`)&&(s||i.has(e))),a={...r,'), 1)

    @unittest.skipUnless(shutil.which('node'), 'Node is needed to execute the desktop picker fixture')
    def test_ultracode_choice_is_executable_only_for_an_advertising_custom_provider(self):
        source = patch(self.fixture(b'vw')).decode()
        script = '''const o=false, i=new Set(['high']);
const r={supportedReasoningEfforts:['high','max','ultracode','unknown'].map(reasoningEffort=>({reasoningEffort}))};
function vw(e){return ['high','max'].includes(e)};
''' + source + '''
console.log(JSON.stringify([true,false].map(s=>picker({isCustomModelProvider:s}).map(e=>e.reasoningEffort))));'''
        result = subprocess.run([shutil.which('node'), '-e', script], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), [['high', 'max', 'ultracode'], ['high']])

    def managed_fixture(self):
        return self.fixture(b'vw').replace(b'let e=o?', b'let e=o||s?').replace(
            b'vw(e)&&i.has(e)', b'vw(e)&&(s||i.has(e))')

    def test_managed_upgrade_matches_current_picker_and_preserves_other_code(self):
        original = self.managed_fixture() + self.composer()
        self.assertEqual(upgrade_managed(original), patch(self.fixture(b'vw')) + self.composer())

    def test_managed_upgrade_rejects_unknown_duplicate_native_and_already_new_shapes(self):
        old = self.managed_fixture()
        for source in (old * 2, old.replace(b'vw(e)', b'UXn(e)'),
                       old.replace(b'let e=o||s?', b'let e=o||x?'),
                       old.replace(b'models:c', b'models:x'), self.fixture(b'vw'),
                       patch(self.fixture(b'vw')), old + patch(self.fixture(b'vw')), self.composer()):
            with self.subTest(source=source[-60:]), self.assertRaisesRegex(ValueError, 'verified 26.917'):
                upgrade_managed(source)

    @unittest.skipUnless(shutil.which('node'), 'Node is needed to execute the managed picker fixture')
    def test_managed_upgrade_retains_native_gating_and_rejects_unadvertised_efforts(self):
        script = '''const o=false, i=new Set(['high']);
let r={supportedReasoningEfforts:['high','max','ultracode','unknown'].map(reasoningEffort=>({reasoningEffort}))};
function vw(e){return ['high','max'].includes(e)};
''' + upgrade_managed(self.managed_fixture()).decode() + '''
const advertised=[true,false].map(s=>picker({isCustomModelProvider:s}).map(e=>e.reasoningEffort));
r.supportedReasoningEfforts=[{reasoningEffort:'high'}];
console.log(JSON.stringify([advertised,picker({isCustomModelProvider:true}).map(e=>e.reasoningEffort)]));'''
        result = subprocess.run([shutil.which('node'), '-e', script], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), [[['high', 'max', 'ultracode'], ['high']], ['high']])

    def composer(self):
        return (b'function pj(e,t){let n=e?.find(e=>e.model===t);return n==null?[]:n.supportedReasoningEfforts.filter(e=>fne(e.reasoningEffort)&&e.reasoningEffort!==`persistent`)}'
                b'function mj(e,t){return fne(e)&&t.some(t=>t.reasoningEffort===e)?e:`high`}'
                b'const labels={persistent:{id:`composer.mode.local.reasoning.persistent.label`,defaultMessage:`Persistent`}};'
                b'const h3=`dot`,Hi=`low`,Wi=`medium`,Bi=`high`,u3=`bolt`;'
                b'const icons={none:h3,minimal:h3,low:Hi,medium:Wi,high:Bi,xhigh:u3,max:u3,ultra:u3,persistent:u3};')

    @unittest.skipUnless(shutil.which('node'), 'Node is needed to execute the desktop composer fixture')
    def test_composer_retains_only_advertised_ultracode_and_has_a_visible_label(self):
        script = b'function fne(e){return [`high`,`max`,`persistent`].includes(e)};' + patch_composer(self.composer()) + b'''
const model={model:'cc-opus',supportedReasoningEfforts:['high','max','ultracode','unknown','persistent'].map(reasoningEffort=>({reasoningEffort}))};
const choices=pj([model],'cc-opus');
console.log(JSON.stringify([choices.map(e=>e.reasoningEffort),mj('ultracode',choices),mj('ultracode',[{reasoningEffort:'high'}]),labels.ultracode.defaultMessage,icons.ultracode]));'''
        result = subprocess.run([shutil.which('node'), '-e', script.decode()], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), [['high', 'max', 'ultracode'], 'ultracode', 'high', 'Ultracode', 'bolt'])

    @unittest.skipUnless(shutil.which('node'), 'Node is needed to execute the desktop dropdown fixture')
    def test_dropdown_rows_include_only_an_advertised_ultracode(self):
        script = '''function vw(e){return ['low','high','xhigh','max','persistent'].includes(e)};''' + patch(self.fixture(b'vw')).decode() + '''
const claude={model:'cc-opus',supportedReasoningEfforts:['high','max','ultracode','unknown','persistent'].map(reasoningEffort=>({reasoningEffort}))};
const gpt={model:'gpt-5.5',supportedReasoningEfforts:['low','xhigh','persistent'].map(reasoningEffort=>({reasoningEffort}))};
console.log(JSON.stringify([rows([claude]),rows([gpt])]));'''
        result = subprocess.run([shutil.which('node'), '-e', script], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout),
                         [['cc-opus:high', 'cc-opus:max', 'cc-opus:ultracode'], ['gpt-5.5:low', 'gpt-5.5:xhigh']])

    def test_missing_dropdown_or_icon_shape_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'dropdown'):
            patch(self.fixture(b'vw').replace(self.dropdown(), b''))
        with self.assertRaisesRegex(ValueError, 'dropdown'):
            upgrade_managed(self.managed_fixture() + self.dropdown())
        with self.assertRaisesRegex(ValueError, 'icons'):
            patch_composer(self.composer().replace(b'ultra:u3,', b''))

    def test_composer_rejects_incomplete_or_duplicate_shapes(self):
        for source in (self.composer() * 2, self.composer().replace(b'return fne(e)', b'return other(e)'),
                       self.composer().replace(b'composer.mode.local.reasoning.persistent.label', b'changed.label')):
            with self.subTest(source=source[-60:]), self.assertRaises(ValueError):
                patch_composer(source)

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
