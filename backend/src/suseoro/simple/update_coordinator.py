"""One update worker, separate preferences, and an explicit exit handoff."""
from __future__ import annotations

import json
from pathlib import Path
import threading
from typing import Callable

from suseoro.simple import VERSION
from suseoro.simple import updating


class UpdateCoordinator:
    def __init__(self, data_dir: Path, *, auto_supported: bool = False):
        self.data_dir = Path(data_dir)
        self.auto_supported = auto_supported
        self._lock = threading.RLock()
        self._operation = threading.Lock()
        self._worker: threading.Thread | None = None
        self._cancel = threading.Event()
        self._closing = False
        self._check_requested = False
        self._manual_pending = False
        self._manual_started = False
        self._ready: Path | None = None
        self._info = {'current_version': VERSION, 'latest_version': None,
                      'available': False, 'url': updating.RELEASES_URL,
                      'message': '새 버전을 확인할 수 있습니다.'}
        self._phase = 'idle' if auto_supported else 'manual'
        self.auto_enabled = True
        try:
            saved = json.loads(self._preferences.read_text(encoding='utf-8'))
            if isinstance(saved, dict) and type(saved.get('auto_enabled')) is bool:
                self.auto_enabled = saved['auto_enabled']
        except (OSError, ValueError):
            pass

    @property
    def _preferences(self) -> Path:
        return self.data_dir / 'update-preferences.json'

    def status(self) -> dict:
        with self._lock:
            result = {k: v for k, v in self._info.items() if k not in ('asset', 'error')}
            result.update(phase=self._phase, auto_enabled=self.auto_enabled,
                          auto_supported=self.auto_supported)
            if not self.auto_supported:
                result['message'] += ' 자동 업데이트는 기본 경로에 설치한 Windows 수서로에서 지원합니다.'
            return result

    def set_preferences(self, enabled: bool) -> dict:
        if type(enabled) is not bool:
            raise ValueError('자동 업데이트 사용 여부를 확인해 주세요.')
        with self._lock:
            if self._closing:
                raise ValueError('수서로가 종료 중입니다.')
            self.data_dir.mkdir(parents=True, exist_ok=True)
            temporary = self._preferences.with_suffix('.tmp')
            try:
                temporary.write_text(json.dumps({'auto_enabled': enabled}), encoding='utf-8')
                temporary.replace(self._preferences)
            except OSError:
                raise ValueError('업데이트 설정을 저장하지 못했습니다. 다시 시도해 주세요.') from None
            finally:
                temporary.unlink(missing_ok=True)
            self.auto_enabled = enabled
            if not enabled:
                self._check_requested = False
                if not self._manual_pending:
                    self._cancel.set()
                self._phase = 'manual' if self._info.get('available') else 'idle'
                self._info['message'] = '자동 업데이트를 껐습니다. 원할 때 직접 설치할 수 있습니다.'
        if enabled and self.auto_supported:
            return self.request_check()
        return self.status()

    def start(self) -> dict:
        """Only the desktop launcher opts in; app construction never uses network."""
        if self.auto_supported and self.auto_enabled:
            return self.request_check()
        return self.status()

    def request_check(self) -> dict:
        with self._lock:
            if self._closing or self._manual_pending or self._manual_started:
                return self.status()
            self._check_requested = True
            if self._worker is None:
                self._phase = 'checking'
                self._info['message'] = '새 버전을 확인하고 있습니다.'
                self._worker = threading.Thread(target=self._run, daemon=True,
                                                name='suseoro-update-worker')
                self._worker.start()
            return self.status()

    def _run(self) -> None:
        while True:
            with self._lock:
                if self._closing or self._manual_pending or not self._check_requested:
                    self._worker = None
                    return
                self._check_requested = False
                cancel = threading.Event()
                self._cancel = cancel
                self._phase = 'checking'
                self._info['message'] = '새 버전을 확인하고 있습니다.'
            with self._operation:
                try:
                    updating._check_cancelled(cancel)
                    info = updating.check_update(VERSION)
                    updating._check_cancelled(cancel)
                    with self._lock:
                        if self._closing:
                            return
                        self._info = info
                        self._ready = None
                        if info.get('error'):
                            self._phase = 'error'
                            continue
                        automatic = self.auto_supported and self.auto_enabled
                        compatible = updating.supports_automatic_install(info.get('latest_version'))
                        if not info.get('available'):
                            self._phase = 'idle' if self.auto_supported else 'manual'
                            continue
                        if not automatic or not compatible:
                            self._phase = 'manual'
                            if automatic and not compatible:
                                self._info['message'] = '이 버전은 설치 버튼으로 직접 업데이트해 주세요.'
                            continue
                        self._phase = 'downloading'
                        self._info['message'] = '새 버전을 준비하고 있습니다. 수서 업무를 계속하셔도 됩니다.'
                    path = updating.download_update(info, self.data_dir, cancel=cancel)
                    updating.verify_download(path, self.data_dir)
                    with self._lock:
                        if not self._closing and not cancel.is_set() and self.auto_enabled:
                            self._ready = Path(path)
                            self._phase = 'ready'
                            self._info['message'] = '새 버전 준비가 끝났습니다. 수서로를 종료하면 자동으로 설치합니다.'
                except updating.UpdateCancelled:
                    with self._lock:
                        if not self._closing:
                            self._phase = 'manual' if self._info.get('available') else 'idle'
                except Exception:
                    with self._lock:
                        self._ready = None
                        self._phase = 'error'
                        self._info['message'] = '업데이트를 준비하지 못했습니다. 인터넷 연결을 확인한 뒤 다시 시도해 주세요.'

    def install_manual(self, callback: Callable[[Path], None]) -> None:
        """The explicit install action waits for/cancels the background operation."""
        with self._lock:
            if self._closing or self._manual_pending or self._manual_started:
                raise ValueError('이미 업데이트를 준비하고 있거나 수서로가 종료 중입니다.')
            self._manual_pending = True
            self._check_requested = False
            self._cancel.set()
        try:
            with self._operation:
                with self._lock:
                    if self._closing:
                        raise updating.UpdateCancelled('수서로가 종료 중입니다.')
                    cancel = threading.Event()
                    self._cancel = cancel
                    self._phase = 'checking'
                info = updating.check_update(VERSION)
                updating._check_cancelled(cancel)
                if not info.get('available'):
                    raise ValueError(info.get('message') or '현재 적용할 새 버전이 없습니다.')
                with self._lock:
                    self._info = info
                    self._phase = 'downloading'
                path = updating.download_update(info, self.data_dir, cancel=cancel)
                updating.verify_download(path, self.data_dir)
                with self._lock:
                    updating._check_cancelled(cancel)
                    if self._closing:
                        raise updating.UpdateCancelled('수서로가 종료 중입니다.')
                    callback(path)
                    self._manual_started = True
                    self._ready = None
                    self._phase = 'manual'
        except Exception:
            with self._lock:
                self._ready = None
                self._phase = 'error'
                self._info['message'] = '업데이트를 설치하지 못했습니다. 다시 시도해 주세요.'
            raise
        finally:
            with self._lock:
                self._manual_pending = False

    def close(self, *, normal_exit: bool) -> Path | None:
        """Cancel unfinished work; only a completed, still-enabled update can run."""
        with self._lock:
            if self._closing:
                return None
            self._closing = True
            self._check_requested = False
            self._cancel.set()
            worker = self._worker
            ready = self._ready if (normal_exit and self.auto_supported and self.auto_enabled
                                    and not self._manual_started and not self._manual_pending) else None
        # Each metadata/read operation has at most a 15-second network timeout.
        # Waiting lets download_update's finally remove its .part before exit.
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=20)
        if not self._operation.acquire(timeout=20):
            return None
        try:
            if ready is not None:
                try:
                    updating.verify_download(ready, self.data_dir)
                    return ready
                except updating.UpdateError:
                    with self._lock:
                        self._phase = 'error'
                        self._info['message'] = '준비한 설치 파일이 변경되어 자동 설치하지 않았습니다.'
            return None
        finally:
            self._operation.release()
