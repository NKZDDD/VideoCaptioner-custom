"""UI-only editor integration; processing continues through existing task factories."""
import copy
import datetime
import json
import tempfile
import uuid
from pathlib import Path
from app.view.background_jobs import BackgroundJobs
from app.view.caption_timeline import CaptionTimeline,apply_interval
from app.view.history_browser import choose_history,history_label,recent_projects
from PyQt5.QtCore import Qt, QTimer, QUrl, QRectF, QEvent, QSize, QTime, QSignalBlocker, QThreadPool
from PyQt5.QtGui import QPainter, QColor, QPen, QKeySequence
from PyQt5.QtWidgets import QWidget, QHBoxLayout, QVBoxLayout, QSplitter, QMessageBox, QSlider, QDialog, QApplication, QStackedWidget, QLabel, QShortcut, QTimeEdit
from qfluentwidgets import ComboBox, PushButton, BodyLabel, isDarkTheme, ToolButton, FluentIcon as FIF, Theme
from app.core.batch_project_store import list_batch_projects, load_batch_project
from app.core.bk_asr.asr_data import ASRData
from app.common.config import cfg
from app.core.utils.get_subtitle_style import get_subtitle_style


_PREVIEW_POOL=QThreadPool()
_PREVIEW_POOL.setMaxThreadCount(1)


class StudioVideoWidget(QWidget):
    """Single VLC player; fit the entire frame and keep one playback state."""
    def __init__(self,parent=None):
        super().__init__(parent)
        from app.vendor import vlc
        self.vlc=vlc
        self.instance=vlc.Instance('--quiet','--no-video-title-show','--no-sub-autodetect-file')
        if not self.instance:raise RuntimeError('便携播放器初始化失败')
        self.backend=self.instance.media_player_new()
        self._dialog=None;self.expand_handler=None;self._subtitle_file=None
        self._pending_move=None;self._move_generation=0
        self._subtitle_generation=0
        layout=QVBoxLayout(self);layout.setContentsMargins(0,0,0,0);layout.setSpacing(7)
        self.surface=QWidget(self);self.surface.setMinimumHeight(260)
        self.surface.setStyleSheet('background:#11191f;')
        self.surface.setAutoFillBackground(True)
        palette=self.surface.palette();palette.setColor(self.surface.backgroundRole(),QColor('#11191f'));self.surface.setPalette(palette)
        self.surface.setToolTip('双击放大预览；完整画面等比例显示')
        self.surface.installEventFilter(self)
        self.frame_stack=QStackedWidget(self)
        self.frame_stack.addWidget(self.surface)
        self.waiting_label=QLabel('正在生成与成片一致的字幕预览…',self)
        self.waiting_label.setAlignment(Qt.AlignCenter);self.waiting_label.setWordWrap(True)
        self.waiting_label.setStyleSheet('color:#c1d1da;background:#11191f;padding:20px;font-size:13px;')
        self.frame_stack.addWidget(self.waiting_label);self.frame_stack.setMinimumHeight(260)
        layout.addWidget(self.frame_stack,1)
        self.backend.set_hwnd(int(self.surface.winId()))
        self.backend.video_set_mouse_input(False);self.backend.video_set_key_input(False)
        controls=QHBoxLayout();controls.setSpacing(8);controls.setContentsMargins(7,0,7,0)
        self.play_button=ToolButton(self);self.play_button.setFixedSize(36,34)
        self.play_button.setIcon(FIF.PLAY.icon(Theme.DARK));self.play_button.setIconSize(QSize(18,18))
        self.play_button.setToolTip('播放');self.play_button.setAccessibleName('播放');controls.addWidget(self.play_button)
        self.slider=QSlider(Qt.Horizontal,self);self.slider.setRange(0,0);controls.addWidget(self.slider,1)
        self.time_label=BodyLabel('00:00 / 00:00',self);self.time_label.setMinimumWidth(106);controls.addWidget(self.time_label)
        self.expand_button=ToolButton(self);self.expand_button.setFixedSize(36,34)
        self.expand_button.setIcon(FIF.FULL_SCREEN.icon(Theme.DARK));self.expand_button.setIconSize(QSize(18,18))
        self.expand_button.setToolTip('放大预览');self.expand_button.setAccessibleName('放大预览');controls.addWidget(self.expand_button)
        for button in (self.play_button,self.expand_button):
            button.setStyleSheet('QToolButton {background:#24343f;border:1px solid #3a4d5a;border-radius:6px;padding:5px;} QToolButton:hover {background:#304754;}')
        layout.addLayout(controls)
        self.play_button.clicked.connect(self.toggle_play)
        self.expand_button.clicked.connect(self.toggle_expanded)
        self.slider.sliderMoved.connect(self.setPosition)
        self.timer=QTimer(self);self.timer.setInterval(100);self.timer.timeout.connect(self.tick);self.timer.start()
        self.on_position=None;self.media=None;self.vlc_player=self
        self.fit_video()

    def fit_video(self):
        # Let VLC letterbox the complete source frame. Never force a zoom/crop
        # or rewrite ASS font/position settings to match the small editor pane.
        self.backend.video_set_aspect_ratio(None)
        self.backend.video_set_crop_geometry(None)
        self.backend.video_set_scale(0)

    def toggle_play(self):
        if self.backend.is_playing():self.backend.set_pause(1)
        elif self.media:
            if self.backend.get_state()==self.vlc.State.Ended:self.backend.stop()
            self.backend.play()
        self.update_play_icon()

    def update_play_icon(self):
        playing=bool(self.backend.is_playing())
        self.play_button.setIcon((FIF.PAUSE if playing else FIF.PLAY).icon(Theme.DARK))
        text='暂停' if playing else '播放'
        self.play_button.setToolTip(text);self.play_button.setAccessibleName(text)
        self.play_button.setProperty('playing',playing)

    def tick(self):
        if self._pending_move:
            position,playing,generation=self._pending_move
            if self.backend.get_length()>0 and self.backend.is_playing():
                self.backend.set_time(position)
                self._pending_move=None
                if not playing:
                    # Decode the target frame before pausing, rather than
                    # leaving the first frame onscreen with a later timestamp.
                    QTimer.singleShot(200,lambda:self.backend.set_pause(1) if generation==self._move_generation else None)
            else:
                self.update_play_icon();return
        length=max(0,self.backend.get_length());pos=max(0,self.backend.get_time())
        self.slider.setMaximum(length)
        if not self.slider.isSliderDown():self.slider.setValue(pos)
        def stamp(value):return f'{value//60000:02d}:{value//1000%60:02d}'
        self.time_label.setText(stamp(pos)+' / '+stamp(length))
        self.update_play_icon()
        if self.on_position:self.on_position(pos)

    def eventFilter(self,obj,event):
        if obj is self.surface and event.type()==QEvent.MouseButtonDblClick:
            self.toggle_expanded();return True
        return super().eventFilter(obj,event)

    def toggle_expanded(self):
        if self.expand_handler:self.expand_handler()

    def setVideo(self,url):
        self._pending_move=None;self._subtitle_generation+=1;self.backend.stop();self._subtitle_file=None
        previous=self.media;self.media=self.instance.media_new(url.toLocalFile());self.backend.set_media(self.media)
        self.fit_video()
        if previous:previous.release()

    def play(self):self.backend.play();self.update_play_icon()
    def stop(self):self._subtitle_generation+=1;self._move_generation+=1;self._pending_move=None;self.backend.stop();self.update_play_icon()
    def duration(self):return self.backend.get_length()
    def setPosition(self,ms):
        if self._pending_move:self._pending_move=(int(ms),self._pending_move[1],self._pending_move[2])
        else:self.backend.set_time(int(ms))
    def seek_paused(self,ms):
        self._move_generation+=1
        self._pending_move=(int(ms),False,self._move_generation)
        self.backend.play()
    def addSubtitle(self,path):
        self._subtitle_file=str(path);self._subtitle_generation+=1
        generation=self._subtitle_generation
        self._attach_subtitle(generation,0)
        return True

    def _attach_subtitle(self,generation,attempt):
        if generation!=self._subtitle_generation or not self.media:return
        if self.backend.get_length()<=0:
            if attempt<20:QTimer.singleShot(100,lambda:self._attach_subtitle(generation,attempt+1))
            return
        uri=QUrl.fromLocalFile(self._subtitle_file).toString(QUrl.FullyEncoded)
        self.backend.video_set_spu(-1)
        result=self.backend.add_slave(self.vlc.MediaSlaveType.subtitle,uri,True)
        if result!=0:
            if attempt<20:QTimer.singleShot(100,lambda:self._attach_subtitle(generation,attempt+1))
            return
        if not self.backend.is_playing():self.seek_paused(max(0,self.backend.get_time()))

    def set_preparing(self,preparing,message=None):
        if message:self.waiting_label.setText(message)
        self.frame_stack.setCurrentWidget(self.waiting_label if preparing else self.surface)
        self.play_button.setEnabled(not preparing);self.slider.setEnabled(not preparing)
    def use_rendered_video(self,path,position,playing):
        self.setVideo(QUrl.fromLocalFile(str(path)))
        self.set_preparing(False);self._move_generation+=1
        self._pending_move=(int(position),bool(playing),self._move_generation)
        self.backend.play()
    def hideEvent(self,event):
        self.backend.set_pause(1);super().hideEvent(event)


class StudioEditorController:
    def __init__(self, editor):
        self.editor=editor
        self._preview_io=BackgroundJobs(editor)
        self._preview_io.pool=_PREVIEW_POOL
        self._video_load_generation=0
        self.dirty=False
        self.loading=False
        self.player=None
        self.preview_renderer=None
        self._exact_requested=False
        self.preview_resume=(0,False)
        self._focus_saved=None
        self.escape_preview=QShortcut(QKeySequence(Qt.Key_Escape),editor)
        self.escape_preview.activated.connect(self.exit_focus_preview)
        self.preview_path=Path(tempfile.gettempdir())/('kaka-studio-'+uuid.uuid4().hex+'.ass')
        self.preview_base=self.preview_path
        self.preview_files=[]
        self.preview_generation=0
        self.undo_stack=[]; self.redo_stack=[]; self.last_state=copy.deepcopy(editor.model._data)
        layout=editor.main_layout
        header=QHBoxLayout()
        header.addWidget(BodyLabel('历史任务',editor))
        self.history=ComboBox(editor); self.history.setMinimumWidth(240)
        header.addWidget(self.history,1)
        search_history=PushButton('查找历史',editor);search_history.clicked.connect(self.open_history_search);header.addWidget(search_history)
        refresh=PushButton('刷新历史',editor); refresh.clicked.connect(lambda checked=False:self.refresh(force=True))
        header.addWidget(refresh)
        self.history_refresh_button=refresh
        self.dirty_label=BodyLabel('已保存',editor); header.addWidget(self.dirty_label)
        layout.insertLayout(0,header)
        self.splitter=QSplitter(Qt.Horizontal,editor)
        self.preview_box=QWidget(editor); pv=QVBoxLayout(self.preview_box); pv.setContentsMargins(0,0,0,0)
        self.preview_button=PushButton('加载视频预览',editor)
        self.preview_button.clicked.connect(self.start_preview)
        preview_actions=QHBoxLayout();preview_actions.addWidget(self.preview_button,1)
        self.exact_button=PushButton('成片效果核对',editor);self.exact_button.clicked.connect(self.request_exact_preview)
        self.exact_button.setToolTip('按真实导出流程生成核对视频；普通编辑无需等待整段合成')
        preview_actions.addWidget(self.exact_button)
        from app.view.quality_review_ui import QualityReviewController
        self.quality_review=QualityReviewController(self,preview_actions)
        self.latest_result_button=PushButton('打开最新成片',editor);self.latest_result_button.setEnabled(False)
        self.latest_result_button.clicked.connect(self.open_latest_result);preview_actions.addWidget(self.latest_result_button)
        pv.addLayout(preview_actions)
        self.preview_note=BodyLabel('选择历史任务后，可播放原视频核对当前字幕。',editor)
        self.preview_note.setWordWrap(False); self.preview_note.setMaximumHeight(24); pv.addWidget(self.preview_note)
        layout.removeWidget(editor.subtitle_table)
        self.splitter.addWidget(self.preview_box); self.splitter.addWidget(editor.subtitle_table)
        self.splitter.setSizes([400,650]); layout.insertWidget(2,self.splitter,1)
        self.timeline=CaptionTimeline(editor,self); layout.insertWidget(3,self.timeline)
        tools=QHBoxLayout()
        for text,fn in [('撤销',self.undo),('重做',self.redo)]:
            b=PushButton(text,editor); b.clicked.connect(fn); tools.addWidget(b)
        tools.addWidget(BodyLabel('入场',editor))
        self.start_time_box=QTimeEdit(editor);self.start_time_box.setDisplayFormat('HH:mm:ss.zzz');self.start_time_box.setMinimumWidth(145);self.start_time_box.setMinimumHeight(32);tools.addWidget(self.start_time_box)
        tools.addWidget(BodyLabel('结束',editor))
        self.end_time_box=QTimeEdit(editor);self.end_time_box.setDisplayFormat('HH:mm:ss.zzz');self.end_time_box.setMinimumWidth(145);self.end_time_box.setMinimumHeight(32);tools.addWidget(self.end_time_box)
        self.apply_time_button=PushButton('应用时间',editor);self.apply_time_button.clicked.connect(self.apply_timing);tools.addWidget(self.apply_time_button)
        self.duration_note=BodyLabel('选择字幕后拖动两端调整时长',editor);tools.addWidget(self.duration_note,1)
        editor.subtitle_table.selectionModel().currentRowChanged.connect(self.sync_timing_fields)
        layout.insertLayout(3,tools)
        self.timer=QTimer(editor); self.timer.setSingleShot(True); self.timer.setInterval(100)
        self.timer.timeout.connect(self.update_preview)
        editor.model.dataChanged.connect(self.changed)
        editor.model.layoutChanged.connect(self.changed)
        self.history.currentIndexChanged.connect(self.choose)
        self.refresh()

    def refresh(self,force=False):
        if not hasattr(self,'history_loader'):
            from app.view.navigation_history import HistoryLoader
            self.history_loader=HistoryLoader(self.editor,lambda:list_batch_projects())
            self.history_loader.ready.connect(self.populate_history)
            self.history_loader.failed.connect(self.history_failed)
        self.history_loader.refresh(force)
        if self.history_loader.busy:
            self.history_refresh_button.setText('读取历史中…');self.history_refresh_button.setEnabled(False)
            self.history.setToolTip('正在后台刷新历史，原列表暂时保留')

    def history_failed(self,message):
        self.history_refresh_button.setText('读取失败 · 重试');self.history_refresh_button.setEnabled(True)
        self.history.setToolTip('共享盘或历史记录暂不可读，现有字幕保留：'+message)

    def populate_history(self,projects):
        from PyQt5 import sip
        if sip.isdeleted(self.editor) or sip.isdeleted(self.history):return
        self.history_refresh_button.setText('刷新历史');self.history_refresh_button.setEnabled(True)
        self.history.setToolTip('可按日期、项目文件夹或集数查找历史任务')
        self._history_projects=list(projects)
        current=self.editor.batch_project_manifest
        recent=recent_projects(projects,self.editor.batch_project_video)
        active=next((p for p in projects if p.get('manifest_path')==current),None)
        if active and not any(p.get('manifest_path')==current for p in recent):recent.insert(0,active)
        self.history.blockSignals(True);self.history.clear()
        self.history.addItem('最近同项目字幕；更多请点“查找历史”' if self.editor.batch_project_video else '最近7天字幕；更多请点“查找历史”',userData=None)
        for project in recent:self.history.addItem(history_label(project),userData=project['manifest_path'])
        self.history.setCurrentIndex(max(0,self.history.findData(current)))
        self.history.blockSignals(False)
        if getattr(self,'_open_history_pending',False):
            self._open_history_pending=False;QTimer.singleShot(0,self.open_history_search)

    def open_history_search(self,checked=False):
        if not hasattr(self,'_history_projects'):
            self._open_history_pending=True;self.preview_note.setText('正在读取历史任务…');self.refresh(force=True);return
        selected=choose_history(self._history_projects,self.editor,multiple=False,current_source=self.editor.batch_project_video)
        if not selected:return
        worker=getattr(self.editor,'subtitle_optimization_thread',None)
        if worker is not None and worker.isRunning():
            QMessageBox.information(self.editor,'正在处理字幕','请等当前字幕处理完成后再打开其他历史任务。');return
        try:self.editor.open_batch_project_async(selected[0]['manifest_path'])
        except Exception as error:QMessageBox.warning(self.editor,'无法打开',str(error))
        self.refresh()

    def choose(self):
        path=self.history.currentData()
        if not path or path==self.editor.batch_project_manifest: return
        worker=getattr(self.editor,'subtitle_optimization_thread',None)
        if worker is not None and worker.isRunning():
            QMessageBox.information(self.editor,'正在处理字幕','请等当前字幕处理完成后再切换历史任务。')
            self.refresh();return
        try: self.editor.open_batch_project_async(path)
        except Exception as exc:
            QMessageBox.warning(self.editor,'无法打开',str(exc))
        self.refresh()

    def confirm_switch(self):
        if getattr(getattr(self.editor,'_save_flow',None),'busy',False):
            QMessageBox.information(self.editor,'正在保存','请等保存结束后再切换或关闭，避免丢失当前修改');return False
        if not self.dirty: return True
        result=QMessageBox.question(self.editor,'保存修改','当前字幕尚未保存，切换前是否保存？',QMessageBox.Save|QMessageBox.Discard|QMessageBox.Cancel,QMessageBox.Save)
        if result==QMessageBox.Cancel:return False
        return result!=QMessageBox.Save or bool(self.editor.save_current_in_place())

    def loaded(self):
        self._quality_seek_pending=None
        self.quality_review.load()
        self._preview_project_cache=None
        self.dirty=False; self.loading=False
        self.last_state=copy.deepcopy(self.editor.model._data)
        self.undo_stack.clear(); self.redo_stack.clear()
        self.dirty_label.setText('已保存'); self.refresh(force=True); self.timeline.update()
        self.timeline.position=0
        if self.editor.batch_project_video:
            QTimer.singleShot(0,self.start_preview)
        elif self.player:self.load_video()
        self.refresh_latest_result()

    def saved(self):
        self._preview_project_cache=None
        self.dirty=False; self.dirty_label.setText('字幕已保存 · 待合成' if self.editor.batch_project_manifest else '已保存')

    def changed(self,*args):
        if self.loading:return
        state=copy.deepcopy(self.editor.model._data)
        if state!=self.last_state:
            self.undo_stack.append(self.last_state); self.undo_stack=self.undo_stack[-50:]
            self.redo_stack.clear(); self.last_state=state
            self.dirty=True; self.dirty_label.setText('有未保存修改')
            self.latest_result_button.setText('打开上次成片')
        self._exact_requested=False
        self.sync_timing_fields()
        self.timeline.update(); self.timer.start()

    def restore(self,src,dest):
        if not src:return
        dest.append(copy.deepcopy(self.editor.model._data))
        self.loading=True
        self.editor.model._data=copy.deepcopy(src.pop())
        self.editor.model.layoutChanged.emit(); self.loading=False
        self.last_state=copy.deepcopy(self.editor.model._data)
        self.dirty=True; self.dirty_label.setText('有未保存修改')
        self._exact_requested=False
        self.sync_timing_fields()
        self.timeline.update(); self.timer.start()

    def undo(self):self.restore(self.undo_stack,self.redo_stack)
    def redo(self):self.restore(self.redo_stack,self.undo_stack)

    def sync_timing_fields(self,*args):
        row=self.editor.subtitle_table.currentIndex().row()
        items=list(self.editor.model._data.values())
        if not 0<=row<len(items):return
        item=items[row]
        for box,key in ((self.start_time_box,'start_time'),(self.end_time_box,'end_time')):
            blocker=QSignalBlocker(box);box.setTime(QTime.fromMSecsSinceStartOfDay(int(item[key])));del blocker
        self.duration_note.setText(f'时长 {(item["end_time"]-item["start_time"])/1000:.3f}秒；拖两端调整')

    def apply_timing(self,checked=False):
        row=self.editor.subtitle_table.currentIndex().row()
        start=self.start_time_box.time().msecsSinceStartOfDay();end=self.end_time_box.time().msecsSinceStartOfDay()
        ok,message=apply_interval(self.editor.model,row,start,end,self.player.duration() if self.player else None)
        if not ok:self.preview_note.setText(message);self.sync_timing_fields();return
        self.seek(start);self.sync_timing_fields()

    def request_exact_preview(self,checked=False):
        self._exact_requested=True
        if not self.player:self.start_preview()
        else:self.update_preview()

    def toggle_focus_preview(self):
        if self._focus_saved is not None:
            self.exit_focus_preview();return
        editor=self.editor;window=editor.window()
        widgets=[]
        def gather(layout):
            for i in range(layout.count()):
                item=layout.itemAt(i)
                if item.widget() is self.splitter:continue
                if item.widget():widgets.append(item.widget())
                elif item.layout():gather(item.layout())
        gather(editor.main_layout)
        widgets += [editor.subtitle_table,self.preview_button,self.exact_button,self.preview_note]
        if hasattr(window,'studio_sidebar'):widgets.append(window.studio_sidebar)
        if hasattr(window,'titleBar'):widgets.append(window.titleBar)
        home=window.homeInterface
        self._focus_saved=dict(widgets=[(w,w.isHidden()) for w in widgets],state=window.windowState(),geometry=window.saveGeometry(),
            margins=home.vBoxLayout.contentsMargins(),sizes=self.splitter.sizes())
        for widget in widgets:widget.hide()
        home.vBoxLayout.setContentsMargins(0,0,0,0)
        self.player.expand_button.setIcon(FIF.BACK_TO_WINDOW.icon(Theme.DARK))
        self.player.expand_button.setToolTip('返回编辑器（Esc）')
        self.player.expand_button.setAccessibleName('返回编辑器')
        window.showFullScreen()

    def exit_focus_preview(self):
        if self._focus_saved is None:return
        saved=self._focus_saved;self._focus_saved=None
        window=self.editor.window()
        window.setWindowState(saved['state']);window.restoreGeometry(saved['geometry'])
        window.homeInterface.vBoxLayout.setContentsMargins(saved['margins'])
        for widget,hidden in saved['widgets']:widget.setVisible(not hidden)
        self.splitter.setSizes(saved['sizes'])
        if self.player:
            self.player.expand_button.setIcon(FIF.FULL_SCREEN.icon(Theme.DARK))
            self.player.expand_button.setToolTip('放大预览')
            self.player.expand_button.setAccessibleName('放大预览')


    def start_preview(self):
        try:
            if not self.player:
                self.player=StudioVideoWidget(self.preview_box)
                self.preview_box.layout().insertWidget(1,self.player,1)
                self.player.on_position=self.position_changed
                self.player.expand_handler=self.toggle_focus_preview
            self.load_video()
        except Exception as exc:
            self.preview_note.setText('视频预览未能启动：'+str(exc))

    def load_video(self):
        self._video_read_pending=True
        video=self.editor.batch_project_video or getattr(self.editor.task,'video_path',None)
        self._video_load_generation+=1
        generation=self._video_load_generation
        self.preview_generation+=1
        if self.preview_renderer:self.preview_renderer.cancel()
        self.preview_note.setText('正在读取原视频…')
        self._preview_io.submit('video',lambda:bool(video and Path(video).is_file()),
            lambda available:self._video_loaded(generation,video,available),self.preview_failed)

    def _video_loaded(self, generation, video, available):
        if generation!=self._video_load_generation:return
        self._video_read_pending=False
        if not available:
            self.player.stop();self.preview_note.setText('此字幕尚未关联可用原视频。');return
        if self.preview_renderer:self.preview_renderer.cancel()
        self._showing_composite=False
        self.player.setVideo(QUrl.fromLocalFile(str(video)))
        self.player.set_preparing(False)
        self.player.play()
        pending=getattr(self,'_quality_seek_pending',None)
        if pending is not None:
            self._quality_seek_pending=None;self.seek(pending)
        QTimer.singleShot(600,self.update_preview)
        self.preview_note.setText(Path(video).name);self.preview_note.setToolTip(str(video))
        self.preview_button.setText('实时编辑预览 / 重载原视频')

    def update_preview(self):
        if not self.player or not self.editor.model._data:return
        self.preview_generation+=1
        generation=self.preview_generation
        video=self.editor.batch_project_video or getattr(self.editor.task,'video_path',None)
        manifest=self.editor.batch_project_manifest
        data=copy.deepcopy(self.editor.model._data)
        fallback_style_name=cfg.subtitle_style_name.value
        fallback_layout=cfg.subtitle_layout.value
        path=self.preview_base.with_name(self.preview_base.stem+'-'+str(generation)+'.ass')
        self.preview_files.append(path)
        self.preview_note.setText('正在后台准备字幕预览…')
        if self.preview_renderer:self.preview_renderer.cancel()
        def prepare():
            cached=getattr(self,"_preview_project_cache",None)
            project=cached[1] if cached and cached[0]==manifest else (load_batch_project(manifest) if manifest else {})
            self._preview_project_cache=(manifest,project)
            from app.core.customer_templates import task_profile, style_with_line_limit
            profile=task_profile(video) if not project and video else {}
            style=project.get('style_template') or style_with_line_limit(profile) or get_subtitle_style(project.get('style_name') or fallback_style_name)
            ASRData.from_json(data).to_ass(style,project.get('subtitle_layout') or profile.get('settings',{}).get('subtitle_layout') or fallback_layout,str(path))
            return bool(video and Path(video).is_file())
        self._preview_io.submit('subtitle_preview',prepare,
            lambda available:self._preview_prepared(generation,video,path,available),
            lambda error:self.preview_failed(error) if generation==self.preview_generation else None)

    def _preview_prepared(self,generation,video,path,available):
        if generation!=self.preview_generation:return
        if not available:
            self.preview_note.setText('未找到原视频，字幕编辑仍可使用');return
        self.preview_path=path
        position=max(0,self.player.backend.get_time());playing=bool(self.player.backend.is_playing())
        if getattr(self,'_showing_composite',False):self.player.use_rendered_video(str(video),position,playing)
        self._showing_composite=False
        self.player.addSubtitle(str(path));self.player.set_preparing(False)
        self.preview_note.setText('当前编辑字幕预览 · '+('尚未保存' if self.dirty else '字幕已保存')+' · 不是最终成片')
        if not self._exact_requested:return
        self._exact_requested=False
        if self.preview_renderer is None:
            from app.view.studio_preview_renderer import ExactPreviewRenderer
            self.preview_renderer=ExactPreviewRenderer(self.editor)
            self.preview_renderer.ready.connect(self.preview_rendered)
            self.preview_renderer.failed.connect(self.preview_failed)
            self.preview_renderer.progress.connect(self.preview_note.setText)
        self.preview_renderer.start(str(video),str(path))

    def preview_rendered(self,path):
        if not self.player:return
        self.preview_resume=(max(0,self.player.backend.get_time()),bool(self.player.backend.is_playing()))
        self.player.use_rendered_video(path,*self.preview_resume)
        self._showing_composite=True
        self.preview_note.setText('临时成片效果核对 · 当前编辑字幕 · 正式成片需保存并合成')

    def preview_failed(self,message):
        if self.player:self.player.set_preparing(False)
        self.preview_note.setText(message)

    def seek(self,ms):
        if self.preview_renderer and self.preview_renderer.process is not None:
            self.preview_resume=(max(0,int(ms)),False)
        self.timeline.position=max(0,int(ms)); self.timeline.update()
        if getattr(self,'_video_read_pending',False):
            self._quality_seek_pending=max(0,int(ms));return
        if self.player:
            self.player.seek_paused(max(0,int(ms)))

    def position_changed(self,ms):
        if self.timeline.drag:return
        self.timeline.position=max(0,ms); self.timeline.update()
        if self.player and not self.player.backend.is_playing():return
        table=self.editor.subtitle_table
        if table.state()==table.EditingState:return
        for i,item in enumerate(self.editor.model._data.values()):
            if item['start_time']<=ms<item['end_time']:
                if table.currentIndex().row()!=i:table.selectRow(i)
                break

    def archive_before_save(self,path):
        path=Path(path)
        if not path.is_file():return
        import shutil
        directory=path.parent/'.kaka-history'/path.stem
        directory.mkdir(parents=True,exist_ok=True)
        stamp=datetime.datetime.now().strftime('%Y%m%d_%H%M%S_%f')
        shutil.copy2(path,directory/(stamp+path.suffix))

    def refresh_latest_result(self):self.editor.save_controller().refresh_latest()

    def open_latest_result(self):self.editor.save_controller().open_latest()

    def connect_render_result(self,worker,manifest_path):
        if getattr(worker.task,'editor_render_revision',None):
            self.editor.save_controller().connect_result(worker,manifest_path);return
        # Capture the manifest now: changing the selected episode must not
        # redirect completion updates to another project.
        from app.core.batch_project_store import update_batch_project
        def complete(task):
            try:
                update_batch_project(manifest_path,status='completed',output_video=str(task.output_path),
                    last_render_at=datetime.datetime.now().isoformat(timespec='seconds'))
            except (OSError,ValueError) as exc:
                self.editor.status_label.setText('合成已完成，历史索引更新失败：'+str(exc))
        def failed(message):
            try:update_batch_project(manifest_path,status='render_failed',last_render_error=str(message))
            except (OSError,ValueError):pass
        worker.finished.connect(complete)
        worker.error.connect(failed)

    def close(self):
        if hasattr(self.editor,'_save_flow'):self.editor._save_flow.close()
        if hasattr(self.editor,'_project_open'):self.editor._project_open.close()
        self.quality_review.io.close()
        self._preview_io.close()
        self.preview_generation+=1
        if hasattr(self,'history_loader'):self.history_loader.close()
        self.exit_focus_preview()
        if self.player:
            if self.player._dialog:self.player._dialog.close()
            self.player.stop()
            self.player.timer.stop()
        self.timer.stop()
        if self.preview_renderer:self.preview_renderer.close()
        for path in self.preview_files:
            try:path.unlink(missing_ok=True)
            except OSError:pass
