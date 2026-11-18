"""Cache immutable runtime/resources locally; app code and presets remain shared/live."""
import contextlib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile


class Status:
    def __init__(self, enabled=True):
        self.hwnd = None
        if enabled and os.name == 'nt':
            try:
                import ctypes
                from ctypes import wintypes
                self.ctypes, self.types = ctypes, wintypes
                self.ui = ctypes.windll.user32
                self.ui.CreateWindowExW.restype = wintypes.HWND
                self.ui.CreateWindowExW.argtypes = [wintypes.DWORD,wintypes.LPCWSTR,wintypes.LPCWSTR,wintypes.DWORD,
                    ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_int,wintypes.HWND,wintypes.HMENU,wintypes.HINSTANCE,ctypes.c_void_p]
                self.ui.DestroyWindow.argtypes = [wintypes.HWND]
                self.ui.SetWindowTextW.argtypes = [wintypes.HWND,wintypes.LPCWSTR]
                x=(self.ui.GetSystemMetrics(0)-510)//2;y=(self.ui.GetSystemMetrics(1)-145)//2
                self.hwnd=self.ui.CreateWindowExW(0,'STATIC','岩中花树字幕工具 · 准备本机加速',0x90C00000,x,y,510,145,None,None,None,None)
                self.label=self.ui.CreateWindowExW(0,'STATIC','首次使用正在准备本机运行库，请稍候。\n模板与任务不受影响，以后启动会更快。',0x50000000,22,24,460,75,self.hwnd,None,None,None)
                self.update()
            except Exception:
                self.hwnd=None

    def update(self, text=None):
        if not self.hwnd:return
        if text:self.ui.SetWindowTextW(self.label,text)
        msg=self.types.MSG()
        while self.ui.PeekMessageW(self.ctypes.byref(msg),None,0,0,1):
            self.ui.TranslateMessage(self.ctypes.byref(msg));self.ui.DispatchMessageW(self.ctypes.byref(msg))

    def close(self):
        if self.hwnd:self.ui.DestroyWindow(self.hwnd);self.hwnd=None


def cache_base():
    return Path(os.environ.get('LOCALAPPDATA') or tempfile.gettempdir())/'VideoCaptioner'/'runtime-cache'


def log(event, **values):
    try:
        base=cache_base();base.mkdir(parents=True,exist_ok=True)
        path=base/'startup.jsonl'
        if path.exists() and path.stat().st_size>1024*1024:path.replace(base/'startup.previous.jsonl')
        with path.open('a',encoding='utf-8') as f:
            f.write(json.dumps(dict(event=event,time=time.time(),pid=os.getpid(),**values),ensure_ascii=False)+'\n')
    except OSError:pass


@contextlib.contextmanager
def cache_lock(base, status, timeout=180):
    base.mkdir(parents=True,exist_ok=True)
    with (base/'prepare.lock').open('a+b') as f:
        f.seek(0,2)
        if f.tell()==0:f.write(b'0');f.flush()
        deadline=time.monotonic()+timeout
        acquired=False
        try:
            while not acquired:
                f.seek(0)
                try:
                    if os.name=='nt':
                        import msvcrt
                        msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
                    else:
                        import fcntl
                        fcntl.flock(f.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
                    acquired=True
                except OSError:
                    if time.monotonic()>=deadline:raise TimeoutError('Runtime cache preparation busy')
                    status.update('另一窗口正在准备运行库，完成后将自动打开。');time.sleep(.1)
            yield
        finally:
            if acquired:
                f.seek(0)
                if os.name=='nt':msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1)
                else:fcntl.flock(f.fileno(),fcntl.LOCK_UN)


def ready(target, digest):
    try:
        return (target/'READY').read_text(encoding='ascii')==digest and all((target/p).is_file() for p in
            ['runtime/python.exe','runtime/pythonw.exe','runtime/python311.dll','runtime/Lib/site-packages/PyQt5/QtCore.pyd'])
    except OSError:return False


def ensure_runtime(root, base=None, progress=True):
    root=Path(root);base=Path(base) if base else cache_base()
    manifest=json.loads((root/'startup/runtime.json').read_text(encoding='utf-8'))
    digest=manifest['sha256']
    if len(digest)!=64 or any(c not in '0123456789abcdef' for c in digest):raise ValueError('Invalid runtime digest')
    archive_name=manifest['archive']
    if Path(archive_name).name!=archive_name or not archive_name.endswith('.zip'):raise ValueError('Invalid runtime archive')
    target=base/digest
    if ready(target,digest):return target/'runtime'
    status=Status(progress);temporary=None
    try:
        with cache_lock(base,status):
            if ready(target,digest):return target/'runtime'
            temporary=base/('prepare-'+uuid.uuid4().hex);temporary.mkdir()
            local_zip=temporary/'runtime.zip';h=hashlib.sha256();copied=0
            with (root/'startup'/archive_name).open('rb') as src,local_zip.open('wb') as dest:
                while True:
                    chunk=src.read(1024*1024)
                    if not chunk:break
                    dest.write(chunk);h.update(chunk);copied+=len(chunk)
                    status.update('首次准备：正在复制运行库 %d MB\n模板与任务保留在原位置。'%(copied//1024**2))
            if h.hexdigest()!=digest:raise ValueError('Runtime archive checksum mismatch')
            status.update('正在解压本机运行库，准备完成后自动打开。')
            with zipfile.ZipFile(local_zip) as z:
                for index,info in enumerate(z.infolist()):
                    rel=PurePosixPath(info.filename)
                    if rel.is_absolute() or '..' in rel.parts or not rel.parts or rel.parts[0]!='runtime' or '\\' in info.filename or ':' in info.filename:
                        raise ValueError('Unsafe runtime archive entry')
                    z.extract(info,temporary)
                    if index%60==0:status.update()
            local_zip.unlink()
            subprocess.run([str(temporary/'runtime/python.exe'),'-c','from PyQt5 import QtCore; import qfluentwidgets'],
                check=True,timeout=45,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            (temporary/'READY').write_text(digest,encoding='ascii')
            if not ready(temporary,digest):raise RuntimeError('Runtime incomplete')
            if target.exists():
                # Never overwrite a version that may still be used by another window.
                raise RuntimeError('Existing runtime damaged; using original shared runtime')
            # Windows antivirus may briefly hold a newly extracted DLL/exe.
            for attempt in range(30):
                try:
                    temporary.replace(target)
                    break
                except PermissionError:
                    if attempt==29:raise
                    status.update();time.sleep(.1)
            temporary=None
            log('cache_prepared',runtime=digest)
            return target/'runtime'
    finally:
        status.close()
        if temporary:shutil.rmtree(temporary,ignore_errors=True)


def handoff(root, args):
    root=Path(root)
    # Only this dependency manifest is cached. Shared app code and all writable
    # configuration paths remain untouched, so updates never fork the templates.
    started=time.monotonic()
    try:
        manifest=json.loads((root/'startup/runtime.json').read_text(encoding='utf-8'))
        expected=cache_base()/manifest['sha256']/'runtime'
        if Path(sys.executable).parent.resolve()==expected.resolve():
            from studio_resource_cache import configure_resources
            configure_resources(root)
            log('cached_app_start',runtime=manifest['sha256'])
            return False
        runtime=ensure_runtime(root)
        child=subprocess.Popen([str(runtime/'pythonw.exe'),str(root/'_pystand_static.int'),*args],cwd=str(root),
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        log('handoff',seconds=round(time.monotonic()-started,3),child_pid=child.pid)
        return True
    except Exception as exc:
        log('shared_fallback',error=type(exc).__name__)
        return False
