"""Lifetime identity checks, including legacy records and uncertain OS replies."""
import os
import unittest
from unittest.mock import MagicMock, patch

from manager_core.instances import process_identity
from manager_core.process_state import process_liveness


@unittest.skipUnless(os.name == 'nt', 'Windows process lifetime API')
class ProcessStateTests(unittest.TestCase):
    def test_current_process_needs_the_recorded_birth_to_prove_identity(self):
        current = process_identity(os.getpid())
        self.assertIsNotNone(current)
        self.assertEqual(process_liveness({'pid': os.getpid(), 'created': current['process_created']}), 'alive')
        self.assertEqual(process_liveness({'pid': os.getpid(), 'created': current['process_created'] - 1}), 'reused')
        for value in (None, 0, True, '1'):
            self.assertEqual(process_liveness({'pid': os.getpid(), 'created': value}), 'unknown')

    def test_os_errors_and_legacy_records_do_not_guess_termination(self):
        kernel = MagicMock()
        with patch('manager_core.process_state.ctypes.WinDLL', return_value=kernel), \
                patch('manager_core.process_state.ctypes.get_last_error', return_value=5) as error:
            kernel.OpenProcess.return_value = None
            self.assertEqual(process_liveness({'pid': 321}), 'unknown')
            error.return_value = 87
            self.assertEqual(process_liveness({'pid': 321}), 'exited')
            kernel.OpenProcess.return_value = 12
            kernel.WaitForSingleObject.return_value = 0
            self.assertEqual(process_liveness({'pid': 321}), 'exited')
            kernel.WaitForSingleObject.return_value = 258
            self.assertEqual(process_liveness({'pid': 321}), 'unknown')
            kernel.WaitForSingleObject.return_value = 0xffffffff
            self.assertEqual(process_liveness({'pid': 321, 'created': 123}), 'unknown')
            kernel.WaitForSingleObject.return_value = 258
            kernel.GetProcessTimes.return_value = False
            self.assertEqual(process_liveness({'pid': 321, 'created': 123}), 'unknown')
            self.assertEqual(kernel.CloseHandle.call_count, 4)

    def test_invalid_pid_does_not_wrap_into_a_valid_windows_pid(self):
        with patch('manager_core.process_state.ctypes.WinDLL') as api:
            for value in (0, -1, True, '1', 2 ** 32 + os.getpid()):
                self.assertEqual(process_liveness({'pid': value, 'created': 1}), 'unknown')
            api.assert_not_called()
