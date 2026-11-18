import copy
from PyQt5 import sip
from PyQt5.QtCore import QUrl
from PyQt5.QtGui import QDesktopServices
from qfluentwidgets import InfoBar
from app.common.config import cfg
from app.view.background_jobs import BackgroundJobs
from app.core.editor_save import save_snapshot,record_render
from app.core.batch_project_store import load_batch_project
from pathlib import Path

class EditorSaveController:
    def __init__(self,editor):
        self.editor=editor;self.io=BackgroundJobs(editor);self.busy=False;self.controls=[];self.latest_path='';self.latest_generation=0
    def snapshot(self,render=False):
        return {'path':str(self.editor.subtitle_path),'manifest':self.editor.batch_project_manifest,'video':self.editor.batch_project_video,
                'data':copy.deepcopy(self.editor.model._data),'expected_hash':getattr(self.editor,'_loaded_subtitle_hash',None),
                'style_name':cfg.subtitle_style_name.value,'layout':cfg.subtitle_layout.value,'render':render}
    def request(self,render=False):
        editor=self.editor
        if self.busy or getattr(getattr(editor,'_project_open',None),'busy',False):
            InfoBar.info('正在保存或加载','请等当前操作完成，避免重复提交',parent=editor);return False
        if not editor.subtitle_path:
            InfoBar.warning('没有字幕','请先加载字幕',parent=editor);return False
        request=self.snapshot(render);self.busy=True
        self.controls=[(widget,widget.isEnabled()) for widget in (editor.command_bar,editor.start_button,editor.subtitle_table,editor.rerender_action,editor.studio.timeline,editor.studio.start_time_box,editor.studio.end_time_box,editor.studio.apply_time_button)]
        for widget,_ in self.controls:widget.setEnabled(False)
        editor.status_label.setText('正在保存字幕…保存成功后才会开始合成' if render else '正在保存字幕…')
        editor.studio.dirty_label.setText('保存中…')
        self.io.submit('save',lambda:save_snapshot(request),lambda result:self.saved(request,result),self.failed)
        return True
    def unlock(self):
        for widget,enabled in self.controls:widget.setEnabled(enabled)
        self.controls=[];self.busy=False
    def saved(self,request,result):
        self.unlock();editor=self.editor
        editor._loaded_subtitle_hash=result['hash']
        if editor.model._data==request['data']:editor.studio.saved()
        else:editor.studio.dirty_label.setText('仍有未保存修改')
        editor.status_label.setText(result['warning'] or '字幕已保存：'+Path(result['path']).name)
        if result['warning']:InfoBar.warning('字幕已保存，后续未完成',result['warning'],duration=6000,parent=editor);return
        self.refresh_latest()
        if result['render']:
            try:self.launch(result['render'])
            except Exception as error:
                editor.status_label.setText('字幕已保存，但未能启动合成：'+str(error))
                InfoBar.error('字幕已保存，合成未启动',str(error),duration=6000,parent=editor)
        else:InfoBar.success('保存成功','当前字幕已落盘；成片需重新合成才会更新',duration=3500,parent=editor)
    def failed(self,error):
        self.unlock();self.editor.studio.dirty_label.setText('保存失败 · 修改未丢失')
        self.editor.status_label.setText('保存失败，未开始合成：'+str(error))
        InfoBar.error('未能保存',str(error),duration=6000,parent=self.editor)
    def launch(self,revision):
        editor=self.editor;home=editor.window().homeInterface
        active=getattr(home.video_synthesis_interface,'video_synthesis_thread',None)
        if getattr(home,'_workflow',None) is not None or (active is not None and active.isRunning()):
            editor.status_label.setText('字幕已保存，合成通道正忙；请稍后点保存并合成');return
        task=revision['task'];task.output_path=revision['output'];task.synthesis_config.need_video=True;task.apply_customer_style=False
        task.editor_render_revision=revision['id'];task.editor_subtitle_sha256=revision['subtitle_hash'];task.editor_new_output_only=True
        editor.studio.latest_result_button.setText('打开上次成片')
        target=home.video_synthesis_interface
        target.set_task(task);target._studio_result_hook=lambda worker:editor.studio.connect_render_result(worker,revision['manifest'])
        home.stackedWidget.setCurrentWidget(target);home.pivot.setCurrentItem('VideoSynthesisInterface');target.process()
        editor.status_label.setText('字幕已保存，正在合成独立新版本；原成片保留')
    def refresh_latest(self):
        self.latest_generation+=1;generation=self.latest_generation
        manifest=self.editor.batch_project_manifest
        if not manifest:
            self.latest_path='';self.editor.studio.latest_result_button.setEnabled(False);return
        def read():
            project=load_batch_project(manifest);path=project.get('output_video','')
            return path if path and Path(path).is_file() else '',project.get('status')
        def ready(value):
            if self.editor.batch_project_manifest!=manifest or generation!=self.latest_generation:return
            self.latest_path=value[0];button=self.editor.studio.latest_result_button;button.setEnabled(bool(value[0]))
            button.setText('打开上次成片' if self.editor.studio.dirty or value[1] in ('edited','ready_to_render','render_failed') else '打开最新成片')
            button.setToolTip(value[0] or '当前工程尚无可打开的成片')
        self.io.submit('latest',read,ready)
    def open_latest(self):
        if self.latest_path:QDesktopServices.openUrl(QUrl.fromLocalFile(self.latest_path))
    def connect_result(self,worker,manifest):
        task=worker.task;revision=task.editor_render_revision;output=task.output_path
        io=BackgroundJobs(worker);worker._editor_receipt_io=io;editor=self.editor
        def ready(value):
            if sip.isdeleted(editor) or editor.batch_project_manifest!=manifest or value.get('stale'):return
            if value.get('output'):
                self.latest_generation+=1
                self.latest_path=value['output'];button=editor.studio.latest_result_button;button.setEnabled(True);button.setText('打开上次成片' if editor.studio.dirty or value.get('previous') else '打开最新成片');button.setToolTip(value['output'])
                if not editor.studio.dirty:editor.studio.dirty_label.setText('字幕已保存 · 待合成' if value.get('previous') else '字幕已保存 · 已合成')
                editor.status_label.setText('新成片已生成：'+Path(value['output']).name)
            else:editor.status_label.setText('合成失败，原成片保留；可再次保存并合成')
        def metadata_failed(error):
            if not sip.isdeleted(editor) and editor.batch_project_manifest==manifest:editor.status_label.setText('成片处理已结束，但历史索引更新失败：'+str(error))
        worker.finished.connect(lambda ignored:io.submit('receipt',lambda:record_render(manifest,revision,output=output),ready,metadata_failed))
        worker.error.connect(lambda error:io.submit('receipt',lambda:record_render(manifest,revision,error=error),ready,metadata_failed))
    def close(self):self.io.close()
