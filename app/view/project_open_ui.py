"""Latest-request-wins project opening with immutable old content until commit."""
import copy,time
from PyQt5.QtCore import QThreadPool
from qfluentwidgets import PushButton,InfoBar
from app.view.background_jobs import BackgroundJobs
from app.core.project_open import prepare_project

_POOL=QThreadPool();_POOL.setMaxThreadCount(2)

class ProjectOpenController:
    def __init__(self,editor):
        self.editor=editor;self.io=BackgroundJobs(editor);self.io.pool=_POOL
        self.generation=0;self.busy=False;self.closed=False;self.saved_controls=[]
        self.cancel_button=PushButton('取消加载',editor);self.cancel_button.hide();self.cancel_button.clicked.connect(self.cancel)
        editor.bottom_layout.addWidget(self.cancel_button)
        self.last_metrics={}
    def _controls(self):
        editor=self.editor;studio=editor.studio
        return [editor.subtitle_table,editor.command_bar,editor.start_button,editor.rerender_action,studio.timeline,studio.start_time_box,studio.end_time_box,studio.apply_time_button]
    def open(self,manifest,callback=None):
        if self.closed:return False
        if getattr(getattr(self.editor,'_save_flow',None),'busy',False):
            InfoBar.info('正在保存','请等保存完成后再打开其他工程',parent=self.editor);return False
        worker=getattr(self.editor,'subtitle_optimization_thread',None)
        if worker is not None and worker.isRunning():
            InfoBar.warning('正在处理字幕','请等当前字幕处理结束后再切换工程',parent=self.editor);return False
        if not self.busy and not self.editor.studio.confirm_switch():return False
        self.generation+=1;generation=self.generation
        if not self.busy:
            self.saved_controls=[(control,control.isEnabled()) for control in self._controls()]
            for control,_ in self.saved_controls:control.setEnabled(False)
        self.busy=True;self.cancel_button.show()
        snapshot=copy.deepcopy(self.editor.model._data)
        self.editor.status_label.setText('正在后台加载工程…可切换页面或取消加载')
        self.io.submit('project',lambda:prepare_project(manifest),lambda payload:self.apply(generation,snapshot,payload,callback),lambda error:self.fail(generation,error))
        return True
    def restore_controls(self):
        for control,enabled in self.saved_controls:control.setEnabled(enabled)
        self.saved_controls=[];self.busy=False;self.cancel_button.hide()
    def apply(self,generation,snapshot,payload,callback):
        if self.closed or generation!=self.generation:return
        if self.editor.model._data!=snapshot:
            self.fail(generation,'加载期间当前字幕发生变化，已保留当前内容；请重新打开');return
        started=time.perf_counter();editor=self.editor
        self.restore_controls()
        editor.studio.loading=True
        editor.subtitle_path=payload['subtitle'];editor._loaded_subtitle_hash=payload['hash']
        editor.batch_project_manifest=payload['manifest'];editor.batch_project_video=payload['video'];editor.task=payload['task']
        editor.model._data=payload['data'];editor.model.word_anchors=payload['anchors'];editor.model.split_history.clear()
        editor.model.layoutChanged.emit()
        editor.rerender_action.setEnabled(payload['video_available'])
        editor.status_label.setText('已载入工程：'+str(payload['project'].get('title','')))
        editor.studio.loaded()
        self.last_metrics={'background_seconds':payload['load_seconds'],'gui_apply_ms':round((time.perf_counter()-started)*1000,2)}
        if callback:callback()
    def fail(self,generation,error):
        if self.closed or generation!=self.generation:return
        self.restore_controls()
        self.editor.status_label.setText('加载失败，当前字幕已保留：'+str(error))
        self.editor.studio.refresh()
        InfoBar.warning('工程暂未打开',str(error),duration=5000,parent=self.editor)
    def cancel(self):
        self.generation+=1;self.io.pending.pop('project',None)
        self.restore_controls();self.editor.status_label.setText('已取消加载，当前字幕保留')
        self.editor.studio.refresh()
    def close(self):
        self.closed=True;self.generation+=1;self.io.close()
