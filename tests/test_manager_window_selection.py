"""Real Win32 fixtures for embedded-window discovery. No Codex windows are changed."""
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import sys
import tempfile
import unittest
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core.instances import Instances, main_window, process_identity
from manager_core.store import Store


@unittest.skipUnless(os.name == 'nt', 'Requires dedicated off-screen Windows fixtures')
class WindowSelectionTests(unittest.TestCase):
    def setUp(self):
        self.user = ctypes.WinDLL('user32', use_last_error=True)
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel.GetModuleHandleW.restype = wintypes.HMODULE
        self.module = kernel.GetModuleHandleW(None)
        self.user.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        self.user.DefWindowProcW.restype = ctypes.c_ssize_t
        callback = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        self.proc = callback(self.user.DefWindowProcW)
        class WindowClass(ctypes.Structure):
            _fields_ = [('style', wintypes.UINT), ('proc', callback), ('class_extra', ctypes.c_int),
                        ('window_extra', ctypes.c_int), ('instance', wintypes.HINSTANCE),
                        ('icon', wintypes.HICON), ('cursor', wintypes.HANDLE), ('background', wintypes.HBRUSH),
                        ('menu', wintypes.LPCWSTR), ('name', wintypes.LPCWSTR)]
        self.name = 'Chrome_WidgetWin_CodexFixture_' + uuid4().hex
        window_class = WindowClass(proc=self.proc, instance=self.module, name=self.name)
        self.user.RegisterClassW.argtypes = [ctypes.POINTER(WindowClass)]
        self.user.RegisterClassW.restype = wintypes.ATOM
        self.assertTrue(self.user.RegisterClassW(ctypes.byref(window_class)))
        self.user.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
        self.user.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU,
            wintypes.HINSTANCE, wintypes.LPVOID]
        self.user.CreateWindowExW.restype = wintypes.HWND
        self.user.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        self.user.DestroyWindow.argtypes = [wintypes.HWND]
        self.user.SetParent.argtypes = [wintypes.HWND, wintypes.HWND]
        self.user.SetParent.restype = wintypes.HWND
        self.user.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.LONG]
        self.user.SetWindowLongW.restype = wintypes.LONG
        self.windows = []
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.close_windows)
        self.store = Store(Path(self.temp.name))
        self.profile = self.store.add_profile('fixture')
        self.instances = Instances(self.temp.name, self.store, None)

    def close_windows(self):
        for hwnd in reversed(self.windows):
            self.user.DestroyWindow(hwnd)
        self.user.UnregisterClassW(self.name, self.module)

    def create(self, title='Fixture', visible=False, *, helper=False, parent=False):
        hwnd = self.user.CreateWindowExW(0, 'STATIC' if parent else self.name, title,
            0x80000000 if helper else 0x00CF0000, -30000, -30000, 960, 640, None, None, self.module, None)
        self.assertTrue(hwnd)
        self.windows.append(hwnd)
        if visible: self.user.ShowWindow(hwnd, 4)  # Off-screen, never activate.
        return hwnd

    def remember(self, hwnd):
        identity = process_identity(os.getpid())
        self.store.mutate(lambda data: self.store.profile(self.profile['id'], data).update(
            **identity, generation=str(uuid4()), window_handle=hwnd))
        self.profile = self.store.profile(self.profile['id'])

    def test_repeated_observation_keeps_embedded_window_when_helper_is_top_level(self):
        primary = self.create('Main application fixture', visible=True)
        helper = self.create('Secondary fixture')
        parent = self.create(parent=True)
        self.remember(primary)
        self.assertEqual(self.instances.observe(self.profile)['window_handle'], primary)
        self.user.SetWindowLongW(primary, -16, 0x50000000)  # WS_CHILD | WS_VISIBLE, as the host does.
        self.user.SetParent(primary, parent)
        # Once embedded, only the helper remains in EnumWindows. A fresh
        # discovery finds it, but observing this profile must retain its child.
        self.assertEqual(main_window(os.getpid()), helper)
        for _ in range(20):
            self.assertEqual(self.instances.observe(self.profile)['window_handle'], primary)
        fresh_backend = Instances(self.temp.name, self.store, None)
        self.assertEqual(fresh_backend.observe(self.profile)['window_handle'], primary)

    def test_closing_main_window_allows_actual_replacement_to_be_discovered(self):
        primary = self.create('Old fixture', visible=True)
        self.remember(primary)
        self.instances.observe(self.profile)
        self.user.DestroyWindow(primary)
        self.windows.remove(primary)
        replacement = self.create('Replacement fixture', visible=True)
        self.assertEqual(self.instances.observe(self.profile)['window_handle'], replacement)

    def test_untitled_hidden_compositor_is_not_selected_on_startup(self):
        self.create('', helper=True)
        self.assertIsNone(main_window(os.getpid()))
        primary = self.create('Hidden application fixture')
        self.assertEqual(main_window(os.getpid()), primary)

    def test_remembered_handle_needs_matching_process_and_window_class(self):
        foreign_class = self.create(parent=True)
        self.assertIsNone(main_window(os.getpid(), remembered=foreign_class))
        primary = self.create('Application fixture')
        self.assertIsNone(main_window(0x7FFFFFFF, remembered=primary))
        self.assertEqual(main_window(os.getpid(), remembered=foreign_class), primary)


if __name__ == '__main__':
    unittest.main()
