# -*- coding: utf-8 -*-

from pathlib import Path

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QFileDialog, QHBoxLayout, QSplitter, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CardWidget,
    FluentIcon as FIF,
    InfoBar,
    InfoBarPosition,
    LineEdit,
    PrimaryPushButton,
    PushButton,
    StrongBodyLabel,
    TextEdit,
)

from app.core.entities import BatchTaskStatus, BatchTaskType, SupportedVideoFormats
from app.core.script_reference_store import (
    batch_bind_script_inputs,
    count_video_references,
    extract_episode_number,
    is_script_file,
    load_active_script,
    load_script_file,
    save_active_script,
    save_video_script_reference,
)


class ScriptReferenceInterface(QWidget):
    """Bind per-episode dialogue JSON files to source videos."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ScriptReferenceInterface")
        self.setWindowTitle(self.tr("剧本台词参考"))
        self.setAcceptDrops(True)
        self.script_source_path = ""
        self.script_batch_inputs = []
        self.video_batch_inputs = []
        self._setup_ui()
        self._load_last_script()

    def _setup_ui(self):
        from app.view.script_binding_layout import build_binding_ui
        build_binding_ui(self)

    def _load_last_script(self):
        text, source_path = load_active_script()
        if text:
            self.script_editor.setPlainText(text)
            self.script_source_path = source_path
        from app.view.script_binding_layout import refresh_script_source
        refresh_script_source(self)

    def _update_count(self):
        self.reference_count_label.setText(
            self.tr(f"已绑定 {count_video_references()} 个视频")
        )

    def import_script(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            self.tr("导入本集纯台词 JSON"),
            "",
            self.tr("每集纯台词 JSON (*.json);;兼容剧本文件 (*.txt *.docx *.srt);;所有文件 (*.*)"),
        )
        if not file_path:
            return
        try:
            self._load_script_path(file_path)
            InfoBar.success(
                self.tr("导入成功"),
                self.tr(f"已载入：{Path(file_path).name}"),
                duration=3000,
                position=InfoBarPosition.TOP,
                parent=self,
            )
        except Exception as exc:
            InfoBar.error(
                self.tr("导入失败"), str(exc), duration=5000, parent=self
            )

    def _load_script_path(self, file_path, use_as_excerpt=False):
        text = load_script_file(file_path)
        if not text:
            raise ValueError("剧本中没有可读取的文字")
        self.script_editor.setPlainText(text)
        self.script_source_path = file_path
        save_active_script(text, file_path)
        from app.view.script_binding_layout import refresh_script_source
        refresh_script_source(self)
        if use_as_excerpt or extract_episode_number(file_path) is not None:
            self.excerpt_editor.setPlainText(text)
        return text

    def use_selected_script(self):
        selected = self.script_editor.textCursor().selectedText().replace("\u2029", "\n")
        if not selected.strip():
            selected = self.script_editor.toPlainText()
        if not selected.strip():
            InfoBar.warning(
                self.tr("没有台词"),
                self.tr("请先导入本集 JSON"),
                duration=4000,
                parent=self,
            )
            return
        self.excerpt_editor.setPlainText(selected.strip())

    def choose_video(self):
        formats = " ".join(f"*.{fmt.value}" for fmt in SupportedVideoFormats)
        file_path, _ = QFileDialog.getOpenFileName(
            self, self.tr("选择原视频"), "", f"{self.tr('视频文件')} ({formats})"
        )
        if file_path:
            self.video_input.setText(file_path)

    @staticmethod
    def _append_unique(target, paths):
        existing = {str(Path(path).resolve()).lower() for path in target}
        for path in paths:
            normalized = str(Path(path).resolve()).lower()
            if normalized not in existing:
                target.append(str(Path(path).resolve()))
                existing.add(normalized)

    def _update_batch_input_summary(self):
        script_files = sum(Path(path).is_file() for path in self.script_batch_inputs)
        script_folders = sum(Path(path).is_dir() for path in self.script_batch_inputs)
        video_files = sum(Path(path).is_file() for path in self.video_batch_inputs)
        video_folders = sum(Path(path).is_dir() for path in self.video_batch_inputs)
        self.script_folder_input.setText(
            self.tr(f"已选 {script_files} 个文件，{script_folders} 个文件夹")
            if self.script_batch_inputs
            else ""
        )
        self.video_folder_input.setText(
            self.tr(f"已选 {video_files} 个视频，{video_folders} 个文件夹")
            if self.video_batch_inputs
            else ""
        )
        self.script_folder_input.setToolTip("\n".join(self.script_batch_inputs))
        self.video_folder_input.setToolTip("\n".join(self.video_batch_inputs))
        from app.view.script_binding_layout import refresh_inputs
        refresh_inputs(self)

    def choose_script_files(self):
        files, _ = QFileDialog.getOpenFileNames(
            self,
            self.tr("添加一个或多个分集台词文件"),
            "",
            self.tr("分集台词 (*.json *.txt *.docx *.srt);;所有文件 (*.*)"),
        )
        if files:
            self._append_unique(self.script_batch_inputs, files)
            self._update_batch_input_summary()

    def choose_script_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, self.tr("添加分集剧本文件夹"), ""
        )
        if folder:
            self._append_unique(self.script_batch_inputs, [folder])
            self._update_batch_input_summary()

    def choose_video_files(self):
        formats = " ".join(f"*.{fmt.value}" for fmt in SupportedVideoFormats)
        files, _ = QFileDialog.getOpenFileNames(
            self,
            self.tr("添加一个或多个原视频"),
            "",
            f"{self.tr('视频文件')} ({formats})",
        )
        if files:
            self._append_unique(self.video_batch_inputs, files)
            self._update_batch_input_summary()

    def choose_video_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, self.tr("添加原视频文件夹"), ""
        )
        if folder:
            self._append_unique(self.video_batch_inputs, [folder])
            self._update_batch_input_summary()

    def replace_script_files(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, self.tr("重新选择剧本（替换旧选择）"), "",
            self.tr("分集台词 (*.json *.txt *.docx *.srt);;所有文件 (*.*)"),
        )
        if files:
            self._replace_batch_selection(self.script_batch_inputs, files, "剧本")

    def replace_video_files(self):
        formats = " ".join(f"*.{fmt.value}" for fmt in SupportedVideoFormats)
        files, _ = QFileDialog.getOpenFileNames(
            self, self.tr("重新选择视频（替换旧选择）"), "",
            f"{self.tr('视频文件')} ({formats})",
        )
        if files:
            self._replace_batch_selection(self.video_batch_inputs, files, "视频")

    def _replace_batch_selection(self, target, files, label):
        target.clear()
        self._append_unique(target, files)
        self._update_batch_input_summary()
        self.batch_result_label.setText(
            self.tr(f"已更换{label}选择，旧的{label}已移出本次匹配；请点击匹配并保存。")
        )

    def clear_batch_inputs(self):
        self.script_batch_inputs.clear()
        self.video_batch_inputs.clear()
        self._update_batch_input_summary()
        self.batch_result_label.setText(
            self.tr("已清空批量选择；可以重新添加文件、文件夹或直接拖入。")
        )

    @staticmethod
    def _format_episode_list(values):
        return "、".join(f"{number}集" for number in values) if values else "无"

    def _binding_busy(self,enabled):
        self.binding_stack.setEnabled(not enabled)
        self.binding_tabs.setEnabled(not enabled)
        self.setAcceptDrops(not enabled)

    def _run_binding(self,operation,on_success):
        if getattr(self,'_binding_worker',None) is not None:return
        from app.thread.script_binding_thread import ScriptBindingWorker
        self._binding_success=on_success
        worker=ScriptBindingWorker(operation);self._binding_worker=worker
        worker.progress.connect(self._binding_progress)
        worker.succeeded.connect(self._binding_succeeded)
        worker.failed.connect(self._binding_failed)
        self._binding_busy(True)
        self.reference_count_label.setText('正在匹配并保存…')
        worker.start()

    def _binding_progress(self,message):
        self.reference_count_label.setText(message)

    def _binding_succeeded(self,result):
        callback=self._binding_success;self._binding_worker=None;self._binding_success=None
        self._binding_busy(False)
        try:callback(result)
        except Exception as error:self._binding_failed(str(error))

    def _binding_failed(self,message):
        self._binding_worker=None;self._binding_success=None;self._binding_busy(False)
        self.reference_count_label.setText('绑定保存未完成')
        InfoBar.error(self.tr('绑定保存失败'),message,duration=6000,parent=self)

    def batch_bind_folders(self,checked=False,on_saved=None):
        scripts=list(self.script_batch_inputs);videos=list(self.video_batch_inputs)
        suffixes={fmt.value for fmt in SupportedVideoFormats}
        self._run_binding(lambda progress:batch_bind_script_inputs(scripts,videos,suffixes,progress=progress),lambda result:self._show_batch_binding(result,on_saved))

    def _show_batch_binding(self,result,on_saved=None):
        matched_count = len(result["matched"])
        ambiguous = sorted(
            set(result["ambiguous_script_episodes"])
            | set(result["ambiguous_video_episodes"])
        )
        details = self.tr(
            f"成功绑定 {matched_count} 集；剧本无视频："
            f"{self._format_episode_list(result['unmatched_script_episodes'])}；"
            f"视频无剧本：{self._format_episode_list(result['unmatched_video_episodes'])}；"
            f"重复冲突：{self._format_episode_list(ambiguous)}；"
            f"读取失败：{len(result['failed'])} 集"
        )
        if ambiguous:
            details += self.tr("；同集存在多个候选，请用“更换视频/更换JSON”重新选择正确文件。")
        self.batch_result_label.setText(details)
        from app.view.script_binding_layout import show_binding_result
        show_binding_result(self, result)
        count=result.get('bound_count')
        if count is not None:self.reference_count_label.setText(self.tr(f'已绑定 {count} 个视频'))
        else:self.reference_count_label.setText('本批绑定未保存')
        if matched_count:
            InfoBar.success(
                self.tr("批量匹配完成"),
                self.batch_result_label.text(),
                duration=3500,
                position=InfoBarPosition.TOP,
                parent=self,
            )
        else:
            InfoBar.warning(
                self.tr("没有可保存的匹配"), self.batch_result_label.text(), duration=3500, parent=self
            )
        if on_saved and result['matched']:on_saved([item['video_path'] for item in result['matched']])

    def batch_bind_and_start(self,checked=False,single=False):
        if not single and self.script_batch_inputs and self.video_batch_inputs:
            self.batch_bind_folders(on_saved=self._start_bound_videos)
        else:self.save_reference(on_saved=lambda entry:self._start_bound_videos([entry['video_path']]))

    def _start_bound_videos(self,video_paths):
        try:
            main_window = self.window()
            batch_page = main_window.batchProcessInterface
            full_process_text = str(BatchTaskType.FULL_PROCESS)
            if (
                batch_page.task_type_combo.currentText() != full_process_text
                and batch_page.task_table.rowCount() > 0
            ):
                # Changing task type clears/stops the existing batch list, so
                # never do it implicitly when the user already has work there.
                main_window.switchTo(batch_page)
                InfoBar.warning(
                    self.tr("批量页已有其他任务"),
                    self.tr("剧本参考已保存，但未自动启动。请处理或清空现有任务后再试。"),
                    duration=6000,
                    parent=batch_page,
                )
                return
            batch_page.task_type_combo.setCurrentText(full_process_text)
            batch_page.add_files(video_paths)

            matched_paths = {
                str(Path(path).resolve()).lower() for path in video_paths
            }
            started_paths = []
            for row in range(batch_page.task_table.rowCount()):
                item = batch_page.task_table.item(row, 0)
                status_item = batch_page.task_table.item(row, 2)
                if not item or not status_item:
                    continue
                file_path = item.toolTip()
                normalized = str(Path(file_path).resolve()).lower()
                if (
                    normalized in matched_paths
                    and status_item.text() == str(BatchTaskStatus.WAITING)
                ):
                    started_paths.append(file_path)

            main_window.switchTo(batch_page)
            for file_path in started_paths:
                batch_page.start_task(file_path)

            if not started_paths:
                InfoBar.warning(
                    self.tr("没有新任务"),
                    self.tr("匹配的视频已在批量列表中处理或完成，没有重复启动。"),
                    duration=5000,
                    parent=batch_page,
                )
                return
            InfoBar.success(
                self.tr("已开始全流程"),
                self.tr(f"已把 {len(started_paths)} 集送入批量处理；只启动匹配成功的集数。"),
                duration=5000,
                position=InfoBarPosition.TOP,
                parent=batch_page,
            )
        except Exception as exc:
            InfoBar.error(
                self.tr("启动批量任务失败"), str(exc), duration=6000, parent=self
            )

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        paths = [Path(url.toLocalFile()) for url in event.mimeData().urls()]
        video_suffixes = {f".{fmt.value}" for fmt in SupportedVideoFormats}
        script_files = [path for path in paths if is_script_file(str(path))]
        video_files = [
            path
            for path in paths
            if path.is_file() and path.suffix.lower() in video_suffixes
        ]
        folders = [path for path in paths if path.is_dir()]

        try:
            self._append_unique(
                self.script_batch_inputs, [str(path) for path in script_files]
            )
            self._append_unique(
                self.video_batch_inputs, [str(path) for path in video_files]
            )
            if script_files:
                # A single extracted episode file is already a clean excerpt.
                self._load_script_path(
                    str(script_files[0]),
                    use_as_excerpt=bool(video_files),
                )
            if video_files:
                self.video_input.setText(str(video_files[0]))
            if len(script_files) == 1 and len(video_files) == 1 and not folders:
                script_episode = extract_episode_number(str(script_files[0]))
                video_episode = extract_episode_number(str(video_files[0]))
                if (
                    script_episode is not None
                    and video_episode is not None
                    and script_episode != video_episode
                ):
                    raise ValueError(
                        f"拖入文件集号不一致：剧本第{script_episode}集，视频第{video_episode}集"
                    )

            for folder in folders:
                has_scripts = any(
                    path.is_file() and path.suffix.lower() in {".txt", ".docx", ".srt", ".json"}
                    for path in folder.rglob("*")
                )
                has_videos = any(
                    path.is_file() and path.suffix.lower() in video_suffixes
                    for path in folder.rglob("*")
                )
                if has_scripts and not has_videos:
                    self._append_unique(self.script_batch_inputs, [str(folder)])
                elif has_videos and not has_scripts:
                    self._append_unique(self.video_batch_inputs, [str(folder)])
                elif has_scripts and has_videos:
                    # Mixed folders can serve as both recursive roots.
                    self._append_unique(self.script_batch_inputs, [str(folder)])
                    self._append_unique(self.video_batch_inputs, [str(folder)])

            imported = len(script_files) + len(video_files) + len(folders)
            if not imported:
                raise ValueError("未发现支持的剧本、视频或文件夹")
            self._update_batch_input_summary()
            event.acceptProposedAction()
            InfoBar.success(
                self.tr("拖入成功"),
                self.tr("已加入批量选择；可继续添加其他文件/文件夹，再按集号匹配。"),
                duration=5000,
                position=InfoBarPosition.TOP,
                parent=self,
            )
        except Exception as exc:
            InfoBar.error(self.tr("拖入失败"), str(exc), duration=5000, parent=self)

    def save_reference(self,checked=False,on_saved=None):
        try:
            script_path=self.script_source_path;video_path=self.video_input.text().strip();text=self.excerpt_editor.toPlainText()
            script_episode=extract_episode_number(script_path);video_episode=extract_episode_number(video_path)
            if script_episode is not None and video_episode is not None and script_episode!=video_episode:
                raise ValueError(f'集号不一致：剧本是第{script_episode}集，视频是第{video_episode}集')
            self._run_binding(lambda progress:save_video_script_reference(video_path,text,script_path),lambda entry:self._show_single_binding(entry,on_saved))
        except Exception as exc:InfoBar.error(self.tr('保存失败'),str(exc),duration=5000,parent=self)

    def _show_single_binding(self,entry,on_saved=None):
        count=entry.get('bound_count')
        if count is not None:self.reference_count_label.setText(self.tr(f'已绑定 {count} 个视频'))
        InfoBar.success(self.tr('保存成功'),self.tr(f"已绑定：{entry['video_name']}。之后单集和批量都会自动读取。"),duration=5000,position=InfoBarPosition.TOP,parent=self)
        if on_saved:on_saved(entry)
