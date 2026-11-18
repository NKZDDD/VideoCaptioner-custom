import datetime
from app.core.concurrency import guarded_task
import logging
import os
import tempfile
from pathlib import Path

from PyQt5.QtCore import QThread, pyqtSignal

from app.common.config import cfg
from app.core.bk_asr.asr_data import ASRData
from app.core.entities import SynthesisTask
from app.core.task_factory import TaskFactory
from app.core.customer_templates import effective_profile, profile_value, style_with_line_limit
from app.core.utils.logger import setup_logger
from app.core.utils.video_utils import add_subtitles

logger = setup_logger("video_synthesis_thread")


class VideoSynthesisThread(QThread):
    finished = pyqtSignal(SynthesisTask)
    progress = pyqtSignal(int, str)
    error = pyqtSignal(str)

    def __init__(self, task: SynthesisTask):
        super().__init__()
        self.task = task
        logger.debug(f"初始化 VideoSynthesisThread，任务: {self.task}")

    @guarded_task
    def run(self):
        temporary_ass = None
        staged_output = None
        staged_style = None
        try:
            logger.info(f"\n===========视频合成任务开始===========")
            logger.info(f"时间：{datetime.datetime.now()}")
            video_file = self.task.video_path
            subtitle_file = self.task.subtitle_path
            output_path = self.task.output_path
            editor_output_started=False
            expected=getattr(self.task,'editor_subtitle_sha256',None)
            if expected:
                import hashlib
                if hashlib.sha256(Path(subtitle_file).read_bytes()).hexdigest()!=expected:raise ValueError('本次合成的字幕快照已改变，未开始编码')
                if Path(output_path).exists():raise ValueError('新成片版本路径已存在，未覆盖原文件')
            soft_subtitle = self.task.synthesis_config.soft_subtitle
            need_video = self.task.synthesis_config.need_video
            video_quality = str(self.task.synthesis_config.video_quality)
            match_source_bitrate = video_quality == "source"
            fallback_crf = 18 if match_source_bitrate else int(video_quality)

            if not need_video:
                logger.info(f"不需要合成视频，跳过")
                self.progress.emit(100, self.tr("合成完成"))
                self.finished.emit(self.task)
                return

            final_output=Path(output_path)
            final_output.parent.mkdir(parents=True,exist_ok=True)
            staged_handle=tempfile.NamedTemporaryFile(prefix='.kaka-render-',suffix='.mp4',dir=str(final_output.parent),delete=False)
            staged_output=Path(staged_handle.name);staged_handle.close()
            output_path=str(staged_output)
            logger.info(f"开始合成视频: {video_file}")
            logger.info(
                "合成参数 soft_subtitle=%s video_quality=%s fallback_crf=%s",
                soft_subtitle,
                video_quality,
                fallback_crf,
            )
            self.progress.emit(5, self.tr("正在合成"))

            # SRT is intentionally kept as the editable project file.  Before
            # synthesis, convert it to a temporary ASS using the currently
            # selected preset so edits never bypass fonts/outlines/shadows.
            apply_ass_style = Path(subtitle_file).suffix.lower() == '.ass' and getattr(self.task, 'apply_customer_style', False)
            if Path(subtitle_file).suffix.lower() == ".srt" or apply_ass_style:
                profile = getattr(self.task, 'customer_template_snapshot', None) or effective_profile()
                style_name = profile_value(profile, 'subtitle_style_name')
                style_text = style_with_line_limit(profile) or TaskFactory.get_subtitle_style(style_name)
                layout = profile_value(profile, 'subtitle_layout')
                from app.core.utils.temporary_delivery import context_for,style_for
                from types import SimpleNamespace
                temporary_context=context_for(video_file,SimpleNamespace(text_source_mode=profile.get('text_source_mode'),need_translate=profile_value(profile,'need_translate'),subtitle_style=style_text))
                if temporary_context and not profile.get('_customer_execution'):style_text=style_for(style_text)
                if not style_text:
                    raise ValueError('所选字幕样式未找到，已停止合成，不使用其他默认样式')
                asr_data = ASRData.from_subtitle_file(subtitle_file, ass_layout_hint=layout)
                temp_handle = tempfile.NamedTemporaryFile(
                    prefix="videocaptioner_styled_",
                    suffix=".ass",
                    delete=False,
                )
                temporary_ass = temp_handle.name
                temp_handle.close()
                asr_data.to_ass(
                    style_str=style_text,
                    layout=layout,
                    save_path=temporary_ass,
                )
                subtitle_file = temporary_ass
                logger.info(
                    "已按本次所选样式重新生成ASS：模板=%s，布局=%s，临时文件=%s",
                    style_name,
                    layout,
                    temporary_ass,
                )
                from app.core.utils.task_style import appearance_details
                import hashlib, json
                details = appearance_details(style_text)
                details.update(style_name=style_name, style_source=profile.get('style_source', '客户模板'),
                    style_sha256=hashlib.sha256(style_text.encode('utf-8')).hexdigest())
                staged_style=Path(output_path).with_suffix('.style.json')
                staged_style.write_text(json.dumps(details, ensure_ascii=False, indent=2), encoding='utf-8')
                logger.info('实际合成样式：模板=%s，字体=%s，字号=%s，底边距=%s，阴影=%s，效果=%s',
                    style_name, details['font'], details['ass_font_size'], details['margin_bottom'], details['shadow'], details['effects'])
                if not style_text:
                    logger.warning(
                        "未找到模板 %s，临时ASS使用软件默认样式", style_name
                    )

            if temporary_ass and '; VideoCaptionerOriginalLanguage: ar' in style_text:
                import re
                emitted = Path(temporary_ass).read_text(encoding='utf-8-sig')
                if any(line.split(',', 9)[-1].count(r'\N') > 1 for line in emitted.splitlines() if line.startswith('Dialogue:')):
                    raise ValueError('阿拉伯字幕仍有过长台词，已停止合成；请先用原始字幕完成按词时间分段，避免再次出现三四行')
                if re.search(r'[\u202a-\u202e\u2066-\u2069]', emitted):
                    raise ValueError('字幕含旧版方向控制字符，请重启卡卡后重新处理，避免白框')

            editor_output_started=bool(expected)
            add_subtitles(
                video_file,
                subtitle_file,
                output_path,
                soft_subtitle=soft_subtitle,
                crf=fallback_crf,
                match_source_bitrate=match_source_bitrate,
                progress_callback=self.progress_callback,
            )

            if self.isInterruptionRequested():raise InterruptedError('已取消合成，原成片保留')
            if not staged_output.is_file() or not staged_output.stat().st_size:raise ValueError('未生成有效的新成片，旧成片保留')
            with staged_output.open('rb+') as verified:os.fsync(verified.fileno())
            os.replace(staged_output,final_output)
            if staged_style and staged_style.exists():
                try:os.replace(staged_style,final_output.with_suffix('.style.json'))
                except OSError:logger.warning('成片已保存，但样式说明未更新；未将已完成视频标成失败')
            output_path=str(final_output)
            self.progress.emit(100, self.tr("合成完成"))
            logger.info(f"视频合成完成，保存路径: {output_path}")

            self.finished.emit(self.task)
        except Exception as e:
            logger.exception(f"视频合成失败: {e}")
            self.error.emit(str(e))
            self.progress.emit(100, self.tr("视频合成失败"))
        finally:
            for staged in (staged_output,staged_style):
                if staged:
                    try:staged.unlink(missing_ok=True)
                    except OSError:logger.warning('合成临时文件未清理：%s',staged)
            if temporary_ass and os.path.exists(temporary_ass):
                try:
                    os.unlink(temporary_ass)
                except OSError as cleanup_error:
                    logger.warning("清理临时样式字幕失败: %s", cleanup_error)

    def progress_callback(self, value, message):
        progress = int(5 + int(value) / 100 * 95)
        logger.debug(f"合成进度: {progress}% - {message}")
        self.progress.emit(progress, str(progress) + "% " + message)

