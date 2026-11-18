"""Cancellable local preview render; never touches a customer's output video."""
import hashlib,json,sys,uuid
from pathlib import Path
from PyQt5.QtCore import QObject,QProcess,QProcessEnvironment,QTimer,pyqtSignal
from app.config import CACHE_PATH,ROOT_PATH

class ExactPreviewRenderer(QObject):
    ready=pyqtSignal(str)
    progress=pyqtSignal(str)
    failed=pyqtSignal(str)
    def __init__(self,parent=None):
        super().__init__(parent)
        self.process=None;self.folder=CACHE_PATH/('video-preview-'+uuid.uuid4().hex)
        self.folder.mkdir(parents=True,exist_ok=True)
        self.token=0;self.closed=False;self.cached={}
        self.watchdog=QTimer(self);self.watchdog.setSingleShot(True);self.watchdog.setInterval(120000)
        self.watchdog.timeout.connect(self.stalled)

    def start(self,video,ass):
        self.cancel();self.closed=False
        source=Path(video);stat=source.stat()
        key=hashlib.sha256((str(source.resolve())+str(stat.st_size)+str(stat.st_mtime_ns)).encode()+Path(ass).read_bytes()).hexdigest()
        if key in self.cached and Path(self.cached[key]).is_file():
            self.ready.emit(self.cached[key]);return
        self.token+=1;token=self.token
        output=self.folder/(key+'.mp4');job=self.folder/(key+'.json')
        job.write_text(json.dumps(dict(video=str(source),ass=str(ass),output=str(output),application_root=str(ROOT_PATH.parent)),ensure_ascii=False),encoding='utf-8')
        process=QProcess(self);self.process=process;process.setProcessChannelMode(QProcess.MergedChannels)
        self._output_buffer='';self._last_error=''
        def drain_output():
            data=bytes(process.readAllStandardOutput()).decode('utf-8',errors='replace')
            if self.process is not process:return
            self.watchdog.start();self._output_buffer+=data
            while '\n' in self._output_buffer:
                line,self._output_buffer=self._output_buffer.split('\n',1)
                if line.startswith('PREVIEW_PROGRESS|'):
                    parts=line.split('|',2)
                    self.progress.emit('原视频可操作 · 字幕预览后台生成 '+parts[1]+'%')
                elif line.strip():self._last_error=line.strip()[-300:]
        process.readyReadStandardOutput.connect(drain_output)
        def completed(code,status):
            if self.process is not process or self.closed or token!=self.token:return
            self.watchdog.stop();self.process=None
            if code==0 and status==QProcess.NormalExit and output.is_file() and output.stat().st_size>0:
                self.cached[key]=str(output);self.ready.emit(str(output))
                # Keep at most two completed snapshots in this private session.
                for obsolete in list(self.cached)[:-2]:
                    previous=Path(self.cached[obsolete])
                    try:previous.unlink(missing_ok=True);self.cached.pop(obsolete,None)
                    except OSError:pass  # Decoder may release its previous file later.
            else:self.failed.emit('字幕预览生成失败；原视频仍可播放。'+self._last_error)
            process.deleteLater()
        process.finished.connect(completed)
        def error(kind):
            if kind==QProcess.FailedToStart and self.process is process:
                self.watchdog.stop();self.process=None;self.failed.emit('本地预览进程未能启动，原视频仍可播放。');process.deleteLater()
        process.errorOccurred.connect(error)
        environment=QProcessEnvironment.systemEnvironment()
        environment.insert('LOCALAPPDATA',str(self.folder/'worker-state'))
        environment.insert('PYTHONIOENCODING','utf-8')
        process.setProcessEnvironment(environment)
        # PYStand launchers are not necessarily Python command-line hosts.
        candidates=[Path(sys.prefix)/'python.exe',Path(getattr(sys,'_base_executable',sys.executable)),Path(sys.executable)]
        interpreter=next((p for p in candidates if p.is_file() and p.stem.lower() in ('python','pythonw','python3')),Path(sys.executable))
        process.setProgram(str(interpreter))
        process.setArguments([str(Path(__file__).with_name('studio_preview_render.py')),str(job)])
        self.watchdog.start();process.start()

    def cancel(self):
        self.watchdog.stop();self.token+=1;process=self.process;self.process=None
        if process is None:return
        if process.processId():
            try:
                import psutil
                for child in psutil.Process(int(process.processId())).children(recursive=True):
                    try:child.kill()
                    except psutil.Error:pass
            except psutil.Error:pass
        process.finished.connect(process.deleteLater)
        process.kill()  # Never block the GUI waiting for a cancelled encoder.

    def stalled(self):
        if self.process is None:return
        self.cancel();self.failed.emit('字幕预览长时间没有进度，已停止后台生成；原视频仍可播放，可重新尝试。')

    def close(self):
        self.closed=True;self.cancel()
        import shutil
        shutil.rmtree(self.folder,ignore_errors=True)
