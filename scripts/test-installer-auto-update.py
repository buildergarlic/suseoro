"""Exercise the real NSIS wait, lock, staging and replacement protocol on Windows.

Only fixture files below .test-output are installed; the test build omits user
shortcuts and registry writes. No real Suseoro installation or data is used.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import ctypes
import msvcrt
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


@contextmanager
def held_test_mutex(name: str):
    """Exercise lock handling without touching the real app's named mutexes."""
    assert name.startswith('Local\\Suseoro.Test.')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = ctypes.c_bool
    handle = kernel.CreateMutexW(None, False, name)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        yield
    finally:
        kernel.CloseHandle(handle)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--makensis', type=Path, required=True)
    args = parser.parse_args()
    if os.name != 'nt':
        raise SystemExit('This integration test requires Windows.')
    root = Path(__file__).resolve().parents[1]
    output = root / '.test-output'
    output.mkdir(exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix='installer-auto-', dir=output)).resolve()
    assert run.is_relative_to(root)
    payload = run / 'payload'
    payload.mkdir()
    (payload / 'Suseoro.exe').write_bytes(b'new fixture executable')
    (payload / '_internal').mkdir()
    (payload / '_internal' / 'version.txt').write_text('new')
    installed = run / 'installed'
    installed.mkdir()
    executable = installed / 'Suseoro.exe'
    executable.write_bytes(b'old fixture executable')
    data = run / 'installed.data'
    data.mkdir()
    (data / 'books.json').write_text('school records stay here')
    lockfile = data / 'desktop.lock'
    lockfile.write_bytes(b'0')
    compiler = [str(args.makensis.resolve()), '/V2', '/WX', '/INPUTCHARSET', 'UTF8',
                '/DAPP_VERSION=9.9.9', f'/DSOURCE_DIR={payload}',
                f'/DOUTPUT_DIR={run}', f'/DINSTALLER_TEST_ROOT={installed}',
                f'/DINSTALLER_TEST_ID={run.name}',
                str(root / 'installer' / 'suseoro.nsi')]
    subprocess.run(compiler, check=True)
    installer = run / 'Suseoro-Setup-9.9.9.exe'
    flags = subprocess.CREATE_NO_WINDOW

    def install(pid: str) -> int:
        return subprocess.run([str(installer), '/S', '/AUTOUPDATE', f'/UPDATEPID={pid}'],
                              timeout=45, creationflags=flags).returncode

    for invalid in ('0', '123x', '-1'):
        assert install(invalid) == 22, 'Malformed PID must abort before file changes'
        assert executable.read_bytes() == b'old fixture executable'

    # A definitely exited process is a valid parent; a live data lock still blocks.
    exited = subprocess.Popen([sys.executable, '-c', 'pass'], creationflags=flags)
    exited.wait(timeout=10)
    for suffix, expected_code in (('App', 20), ('Update', 21)):
        with held_test_mutex(f'Local\\Suseoro.Test.{run.name}.{suffix}'):
            result = install(str(exited.pid))
            assert result == expected_code, f'Test {suffix} mutex must abort with {expected_code}, got {result}'
        assert executable.read_bytes() == b'old fixture executable'
    with lockfile.open('r+b') as held:
        msvcrt.locking(held.fileno(), msvcrt.LK_NBLCK, 1)
        try:
            result = install(str(exited.pid))
            assert result == 25, f'An existing app data lock must abort with 25, got {result}'
        finally:
            held.seek(0)
            msvcrt.locking(held.fileno(), msvcrt.LK_UNLCK, 1)
    assert executable.read_bytes() == b'old fixture executable'

    # Wait for a live parent instead of overwriting its files or killing it.
    parent = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(4)'], creationflags=flags)
    started = time.monotonic()
    proc = subprocess.Popen([str(installer), '/S', '/AUTOUPDATE', f'/UPDATEPID={parent.pid}'], creationflags=flags)
    time.sleep(0.8)
    assert proc.poll() is None
    assert executable.read_bytes() == b'old fixture executable'
    assert proc.wait(timeout=45) == 0
    assert time.monotonic() - started >= 3
    assert parent.wait(timeout=5) == 0
    assert executable.read_bytes() == b'new fixture executable'
    assert (installed / '_internal' / 'version.txt').read_text() == 'new'
    assert (data / 'books.json').read_text() == 'school records stay here'

    # Failed extraction leaves the installed tree intact: reserve the staging
    # path with a file, so the installer cannot create its staging directory.
    staging = run / 'installed.update-9.9.9'
    staging.write_text('block staging')
    assert install(str(exited.pid)) != 0
    assert executable.read_bytes() == b'new fixture executable'
    assert (data / 'books.json').read_text() == 'school records stay here'
    print('PASS: invalid PID, isolated app/update mutexes, active data lock, parent wait, replacement, data preservation, staging failure')
    print(f'Test artifacts: {run}')


if __name__ == '__main__':
    main()
