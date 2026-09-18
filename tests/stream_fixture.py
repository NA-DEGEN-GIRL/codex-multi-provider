"""Deterministic GUI regression: output must be visible while this process is alive."""
import sys
import time

sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')
print('STREAM_OUTPUT_FIRST 한글 (아직 실행 중)', end='', flush=True)
print('STREAM_STDERR_FIRST', file=sys.stderr, flush=True)
time.sleep(5)
print('\nSTREAM_OUTPUT_LAST', flush=True)
