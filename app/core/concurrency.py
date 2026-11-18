"""SMB-safe, crash-released resource locks and atomic shared writes."""
import contextlib
import functools
import hashlib
import json
import os
import tempfile
import threading
import time
from pathlib import Path

from app.config import SHARED_APPDATA_PATH

_mutex = threading.Lock()
_locks = {}


class ResourceBusyError(RuntimeError):
    """A resource is occupied, not a processing failure."""
    pass


def _directory_identity(directory):
    """SMB file IDs identify one parent directory through host/IP aliases."""
    if os.name!='nt':return None
    import ctypes
    from ctypes import wintypes
    class Information(ctypes.Structure):
        _fields_=[('attributes',wintypes.DWORD),('created',wintypes.FILETIME),('accessed',wintypes.FILETIME),('written',wintypes.FILETIME),('volume',wintypes.DWORD),('size_high',wintypes.DWORD),('size_low',wintypes.DWORD),('links',wintypes.DWORD),('index_high',wintypes.DWORD),('index_low',wintypes.DWORD)]
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
    kernel.CreateFileW.restype=wintypes.HANDLE
    kernel.GetFileInformationByHandle.argtypes=[wintypes.HANDLE,ctypes.POINTER(Information)];kernel.GetFileInformationByHandle.restype=wintypes.BOOL
    kernel.CloseHandle.argtypes=[wintypes.HANDLE];kernel.CloseHandle.restype=wintypes.BOOL
    handle=kernel.CreateFileW(str(directory),0x80,7,None,3,0x02000000,None)
    if handle==ctypes.c_void_p(-1).value:return None
    try:
        info=Information()
        if not kernel.GetFileInformationByHandle(handle,ctypes.byref(info)) or not (info.index_high or info.index_low):return None
        return 'directory-id:%08x:%08x%08x'%(info.volume,info.index_high,info.index_low)
    finally:kernel.CloseHandle(handle)


def resource_key(value):
    path=Path(value).expanduser().resolve()
    identity=_directory_identity(path.parent)
    # Always parent+leaf, so creating an output cannot change its lock identity.
    return identity+'/'+path.name.casefold() if identity else str(path).replace('\\','/').casefold()


@contextlib.contextmanager
def shared_lock(resource, wait_seconds=0):
    key = hashlib.sha256(resource_key(resource).encode('utf-8')).hexdigest()
    with _mutex:
        state = _locks.setdefault(key, {'lock': threading.RLock(), 'depth': 0})
    if not state['lock'].acquire(timeout=wait_seconds):
        raise ResourceBusyError('该视频或共享文件正在被另一个任务使用，请稍后再试')
    handle = None
    acquired = False
    try:
        if state['depth'] == 0:
            directory = SHARED_APPDATA_PATH / 'concurrency_locks'
            directory.mkdir(parents=True, exist_ok=True)
            handle = open(directory / (key + '.lock'), 'a+b')
            handle.seek(0, 2)
            if handle.tell() == 0:
                handle.write(b'0')
                handle.flush()
            handle.seek(0)
            deadline = time.monotonic() + wait_seconds
            while True:
                try:
                    if os.name == 'nt':
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        raise ResourceBusyError('该视频或共享文件正在被其他电脑使用，请稍后再试') from exc
                    time.sleep(0.05)
            acquired = True
        state['depth'] += 1
        try:
            yield
        finally:
            state['depth'] -= 1
    finally:
        if handle:
            if acquired:
                handle.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
        state['lock'].release()


def atomic_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                dir=str(path.parent), prefix=path.name + '.', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def locked_store(function):
    """Serialize short shared metadata transactions, including read-modify-write."""
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        with shared_lock(SHARED_APPDATA_PATH / ('transaction-' + function.__module__), wait_seconds=5):
            return function(*args, **kwargs)
    return wrapped


@contextlib.contextmanager
def queued_task_lock(worker, resource):
    notified = False
    while True:
        if getattr(worker, 'isInterruptionRequested', lambda: False)():
            raise RuntimeError('已取消等待')
        stack = contextlib.ExitStack()
        try:
            stack.enter_context(shared_lock(resource))
        except ResourceBusyError:
            stack.close()
            worker._waiting_resource = True
            if not notified:
                worker.progress.emit(0, '等待文件释放，随后自动继续')
                notified = True
            time.sleep(0.2)
            continue
        break
    worker._waiting_resource = False
    try:
        # Cancellation may arrive just as the previous owner releases its lock.
        if getattr(worker, 'isInterruptionRequested', lambda: False)():
            raise RuntimeError('已取消等待')
        yield
    finally:
        stack.close()


def guarded_task(function):
    @functools.wraps(function)
    def wrapped(self, *args, **kwargs):
        task = self.task
        source = (getattr(task, 'video_path', None) or getattr(task, 'file_path', None)
                  or getattr(task, 'subtitle_path', None) or getattr(task, 'output_path', None))
        try:
            with queued_task_lock(self, source):
                output = getattr(task, 'output_path', None)
                with queued_task_lock(self, output or source):
                    return function(self, *args, **kwargs)
        except Exception as exc:
            import logging
            logging.getLogger('batch_process_thread').exception('任务失败：%s', source)
            if hasattr(self, 'has_error'):
                self.has_error = True
            self.error.emit(str(exc))
    return wrapped


def guarded_editor(function):
    @functools.wraps(function)
    def wrapped(self, *args, **kwargs):
        if not self.subtitle_path:
            return function(self, *args, **kwargs)
        task = getattr(self, 'task', None)
        source = (getattr(self, 'batch_project_video', None) or getattr(task, 'video_path', None)
                  or getattr(task, 'file_path', None) or self.subtitle_path)
        try:
            with shared_lock(source):
                with shared_lock(self.subtitle_path):
                    return function(self, *args, **kwargs)
        except (OSError, RuntimeError) as exc:
            from qfluentwidgets import InfoBar
            InfoBar.error('共享字幕未保存', str(exc), duration=6000, parent=self)
            return False
    return wrapped
