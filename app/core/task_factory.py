import datetime
import hashlib
import uuid
from pathlib import Path
from typing import Optional

from app.common.config import cfg
from app.core.utils.team_services import refresh_for_new_task
from app.core.customer_templates import task_profile, effective_profile, profile_value, style_with_line_limit, require_shared_profile
from app.config import MODEL_PATH, SUBTITLE_STYLE_PATH, WORK_PATH
from app.core.task_contract import freeze_profile, runtime_config
from app.core.entities import (
    LANGUAGES,
    FullProcessTask,
    LLMServiceEnum,
    SplitTypeEnum,
    SubtitleConfig,
    SubtitleTask,
    SynthesisConfig,
    SynthesisTask,
    TranscribeConfig,
    TranscribeTask,
    TranscriptAndSubtitleTask,
)


class TaskFactory:
    """任务工厂类，用于创建各种类型的任务"""

    @staticmethod
    def get_subtitle_style(style_name: str) -> str:
        """获取字幕样式内容

        Args:
            style_name: 样式名称

        Returns:
            str: 样式内容字符串，如果样式文件不存在则返回None
        """
        style_path = SUBTITLE_STYLE_PATH / f"{style_name}.txt"
        if style_path.exists():
            return style_path.read_text(encoding="utf-8")
        return None

    @staticmethod
    def create_transcribe_task(
        file_path: str, need_next_task: bool = False, customer_profile: Optional[dict] = None
    ) -> TranscribeTask:
        """创建转录任务"""
        profile = customer_profile if customer_profile is not None else (task_profile(file_path) if need_next_task else effective_profile())
        profile = freeze_profile(profile)
        require_shared_profile(profile)
        runtime = runtime_config(profile)

        # 根据是否需要分段来决定是否需要词级时间戳

        # 获取文件名
        file_name = Path(file_path).stem

        # 构建输出路径
        if need_next_task:
            need_word_time_stamp = profile_value(profile, 'need_split') or any('; VideoCaptionerOriginalLanguage: ' + lang in profile.get('style_template', '') for lang in ('hi', 'ar'))
            output_path = str(
                WORK_PATH
                / (file_name + '_' + hashlib.sha256(str(Path(file_path).resolve()).lower().encode('utf-8')).hexdigest()[:8] + '_' + uuid.uuid4().hex[:8])
                / "subtitle"
                / f"【原始字幕】{file_name}-{profile_value(profile, 'transcribe_model').value}-{profile_value(profile, 'transcribe_language').value}.srt"
            )
        else:
            need_word_time_stamp = False
            output_path = str(Path(file_path).parent / f"{file_name}.srt")

        if profile.get('text_source_mode') in ('strict', 'hybrid', 'script_correct'):
            need_word_time_stamp = True
        config = TranscribeConfig(
            transcribe_model=profile_value(profile, 'transcribe_model'),
            transcribe_language=LANGUAGES[profile_value(profile, 'transcribe_language').value],
            use_asr_cache=False if profile.get('text_source_mode') in ('strict', 'hybrid', 'script_correct') or any('; VideoCaptionerOriginalLanguage: ' + lang in profile.get('style_template', '') for lang in ('hi', 'ar')) else runtime.use_asr_cache.value,
            need_word_time_stamp=need_word_time_stamp,
            multilingual_asr_base=runtime.multilingual_asr_base.value,
            multilingual_asr_key=runtime.multilingual_asr_key.value,
            multilingual_asr_model=runtime.multilingual_asr_model.value,
            multilingual_asr_dialect=runtime.multilingual_asr_dialect.value,
            # Whisper Cpp 配置
            whisper_model=runtime.whisper_model.value.value,
            # Whisper API 配置
            whisper_api_key=runtime.whisper_api_key.value,
            whisper_api_base=runtime.whisper_api_base.value,
            whisper_api_model=runtime.whisper_api_model.value,
            whisper_api_prompt=runtime.whisper_api_prompt.value,
            # Faster Whisper 配置
            faster_whisper_program=runtime.faster_whisper_program.value,
            faster_whisper_model=profile_value(profile, 'faster_whisper_model').value,
            faster_whisper_model_dir=str(MODEL_PATH),
            faster_whisper_device=profile_value(profile, 'faster_whisper_device'),
            faster_whisper_vad_filter=runtime.faster_whisper_vad_filter.value,
            faster_whisper_vad_threshold=runtime.faster_whisper_vad_threshold.value,
            faster_whisper_vad_method=runtime.faster_whisper_vad_method.value.value,
            faster_whisper_ff_mdx_kim2=cfg.faster_whisper_ff_mdx_kim2.value,
            faster_whisper_one_word=runtime.faster_whisper_one_word.value,
            faster_whisper_prompt=profile_value(profile, 'faster_whisper_prompt'),
        )

        return TranscribeTask(
            queued_at=datetime.datetime.now(),
            file_path=file_path,
            output_path=output_path,
            transcribe_config=config,
            need_next_task=need_next_task,
        )

    @staticmethod
    def create_subtitle_task(
        file_path: str, video_path: Optional[str] = None, need_next_task: bool = False, customer_profile: Optional[dict] = None
    ) -> SubtitleTask:
        """创建字幕任务"""
        profile = customer_profile if customer_profile is not None else (task_profile(video_path or file_path) if need_next_task else effective_profile())
        profile = freeze_profile(profile)
        require_shared_profile(profile)
        runtime = runtime_config(profile)
        output_name = (
            Path(file_path)
            .stem.replace("【原始字幕】", "")
            .replace(f"【下载字幕】", "")
        )
        # 只在需要翻译时添加翻译服务后缀
        suffix = (
            f"-{profile_value(profile, 'translator_service').value}" if profile_value(profile, 'need_translate') else ""
        )

        if need_next_task:
            output_path = str(
                Path(file_path).parent / f"【样式字幕】{output_name}{suffix}.ass"
            )
        else:
            output_path = str(
                Path(file_path).parent / f"【字幕】{output_name}{suffix}.srt"
            )

        if profile_value(profile, 'split_type') in (SplitTypeEnum.SENTENCE, SplitTypeEnum.SENTENCE.value):
            split_type = "sentence"
        else:
            split_type = "semantic"

        # 根据当前选择的LLM服务获取对应的配置
        current_service = runtime.llm_service.value
        if current_service == LLMServiceEnum.OPENAI:
            base_url = runtime.openai_api_base.value
            api_key = runtime.openai_api_key.value
            llm_model = runtime.openai_model.value
        elif current_service == LLMServiceEnum.SILICON_CLOUD:
            base_url = runtime.silicon_cloud_api_base.value
            api_key = runtime.silicon_cloud_api_key.value
            llm_model = runtime.silicon_cloud_model.value
        elif current_service == LLMServiceEnum.DEEPSEEK:
            base_url = runtime.deepseek_api_base.value
            api_key = runtime.deepseek_api_key.value
            llm_model = runtime.deepseek_model.value
        elif current_service == LLMServiceEnum.OLLAMA:
            base_url = runtime.ollama_api_base.value
            api_key = runtime.ollama_api_key.value
            llm_model = runtime.ollama_model.value
        elif current_service == LLMServiceEnum.LM_STUDIO:
            base_url = runtime.lm_studio_api_base.value
            api_key = runtime.lm_studio_api_key.value
            llm_model = runtime.lm_studio_model.value
        elif current_service == LLMServiceEnum.GEMINI:
            base_url = runtime.gemini_api_base.value
            api_key = runtime.gemini_api_key.value
            llm_model = runtime.gemini_model.value
        elif current_service == LLMServiceEnum.CHATGLM:
            base_url = runtime.chatglm_api_base.value
            api_key = runtime.chatglm_api_key.value
            llm_model = runtime.chatglm_model.value
        elif current_service == LLMServiceEnum.PUBLIC:
            base_url = runtime.public_api_base.value
            api_key = runtime.public_api_key.value
            llm_model = runtime.public_model.value
        else:
            base_url = ""
            api_key = ""
            llm_model = ""

        config = SubtitleConfig(
            # 翻译配置
            base_url=base_url,
            api_key=api_key,
            llm_model=llm_model,
            deeplx_endpoint=runtime.deeplx_endpoint.value,
            # 翻译服务
            translator_service=profile_value(profile, 'translator_service'),
            # 字幕处理
            split_type=split_type,
            need_reflect=runtime.need_reflect_translate.value,
            need_translate=profile_value(profile, 'need_translate'),
            need_optimize=profile_value(profile, 'need_optimize'),
            thread_num=runtime.thread_num.value,
            batch_size=runtime.batch_size.value,
            # 字幕布局、样式
            subtitle_layout=profile_value(profile, 'subtitle_layout'),
            subtitle_style=style_with_line_limit(profile) or TaskFactory.get_subtitle_style(profile_value(profile, 'subtitle_style_name')),
            # 字幕分割
            max_word_count_cjk=profile_value(profile, 'max_word_count_cjk'),
            max_word_count_english=profile_value(profile, 'max_word_count_english'),
            need_split=profile_value(profile, 'need_split'),
            # 字幕翻译
            target_language=profile_value(profile, 'target_language').value,
            # 字幕优化
            need_remove_punctuation=profile_value(profile, 'needs_remove_punctuation'),
            # 字幕提示
            custom_prompt_text=profile_value(profile, 'custom_prompt_text'),
        )

        config.text_source_mode = profile.get('text_source_mode')
        config._source_language = profile_value(profile, 'transcribe_language').value
        if profile.get('program_text_rules'):
            import copy
            config.program_text_rules=copy.deepcopy(profile['program_text_rules'])
        from app.core.customer_execution import bind
        bind(config,profile['_customer_execution'])
        if '_script_reference' in profile:config.frozen_script_reference=profile['_script_reference']
        elif video_path:
            from app.core.script_reference_store import load_exact_video_script_reference
            config.frozen_script_reference=load_exact_video_script_reference(video_path)
        import copy
        from app.core.script_reference_store import load_exact_bilingual_reference
        config.frozen_bilingual_reference=copy.deepcopy(profile['_bilingual_reference'] if '_bilingual_reference' in profile else load_exact_bilingual_reference(video_path)) if video_path else None
        return SubtitleTask(
            queued_at=datetime.datetime.now(),
            subtitle_path=file_path,
            video_path=video_path,
            output_path=output_path,
            subtitle_config=config,
            need_next_task=need_next_task,
        )

    @staticmethod
    def create_synthesis_task(
        video_path: str, subtitle_path: str, need_next_task: bool = False, customer_profile: Optional[dict] = None
    ) -> SynthesisTask:
        """创建视频合成任务"""
        profile = customer_profile if customer_profile is not None else (task_profile(video_path) if need_next_task else effective_profile())
        profile = freeze_profile(profile)
        require_shared_profile(profile)
        runtime = runtime_config(profile)
        if need_next_task:
            output_path = str(
                Path(video_path).parent / f"【卡卡】{Path(video_path).stem}.mp4"
            )
        else:
            output_path = str(
                Path(video_path).parent / f"【卡卡】{Path(video_path).stem}.mp4"
            )

        config = SynthesisConfig(
            need_video=profile_value(profile, 'need_video'),
            soft_subtitle=profile_value(profile, 'soft_subtitle'),
            video_quality=profile_value(profile, 'video_quality'),
        )

        task = SynthesisTask(
            queued_at=datetime.datetime.now(),
            video_path=video_path,
            subtitle_path=subtitle_path,
            output_path=output_path,
            synthesis_config=config,
            need_next_task=need_next_task,
        )
        import copy
        task.customer_template_snapshot = copy.deepcopy(profile)
        task.apply_customer_style = bool(profile.get('id') or profile.get('apply_customer_style'))
        return task

    @staticmethod
    def create_transcript_and_subtitle_task(
        file_path: str,
        output_path: Optional[str] = None,
        transcribe_config: Optional[TranscribeConfig] = None,
        subtitle_config: Optional[SubtitleConfig] = None,
    ) -> TranscriptAndSubtitleTask:
        """创建转录和字幕任务"""
        if output_path is None:
            output_path = str(
                Path(file_path).parent / f"{Path(file_path).stem}_processed.srt"
            )

        return TranscriptAndSubtitleTask(
            queued_at=datetime.datetime.now(),
            file_path=file_path,
            output_path=output_path,
        )

    @staticmethod
    def create_full_process_task(
        file_path: str,
        output_path: Optional[str] = None,
        transcribe_config: Optional[TranscribeConfig] = None,
        subtitle_config: Optional[SubtitleConfig] = None,
        synthesis_config: Optional[SynthesisConfig] = None,
    ) -> FullProcessTask:
        """创建完整处理任务（转录+字幕+合成）"""
        if output_path is None:
            output_path = str(
                Path(file_path).parent
                / f"{Path(file_path).stem}_final{Path(file_path).suffix}"
            )

        return FullProcessTask(
            queued_at=datetime.datetime.now(),
            file_path=file_path,
            output_path=output_path,
        )
