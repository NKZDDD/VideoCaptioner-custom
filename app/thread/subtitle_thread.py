from app.core.llm_permissions import config_permissions,prompt_conflicts,guard_text_result,task_text_boundary

import datetime

import hashlib

from app.core.concurrency import guarded_task, atomic_text

from app.config import SHARED_APPDATA_PATH

import os

import re

from pathlib import Path

from typing import Dict



from PyQt5.QtCore import QSettings, QThread, pyqtSignal



from app.common.config import cfg

from app.core.bk_asr.asr_data import ASRData

from app.core.entities import SubtitleConfig, SubtitleTask, TranslatorServiceEnum

from app.core.subtitle_processor.split import SubtitleSplitter, enforce_semantic_limit

from app.core.subtitle_processor.summarization import SubtitleSummarizer

from app.core.subtitle_processor.optimize import (

    SubtitleOptimizer,

    apply_final_text_policy,

    extract_story_terms,

)

from app.core.subtitle_processor.script_alignment import (

    reconcile_segments_with_script,

    repair_script_confirmed_numeric_overlap,

)

from app.core.subtitle_processor.translate import TranslatorFactory, TranslatorType

from app.core.utils.logger import setup_logger

from app.core.utils.test_opanai import normalize_openai_base_url, test_openai

from app.core.storage.cache_manager import ServiceUsageManager

from app.core.storage.database import DatabaseManager

from app.core.script_reference_store import load_video_script_reference, load_exact_video_script_reference

from app.core.utils.text_source_mode import process_script_mode

from app.config import CACHE_PATH



# 配置日志

logger = setup_logger("subtitle_optimization_thread")





def normalize_caption_limit_prompt(custom_prompt: str, max_cjk: int) -> str:

    """Keep the user's correction rules aligned with the UI length setting."""

    if not custom_prompt:

        return custom_prompt

    normalized = re.sub(

        r"(最多|不超过)\s*\d+\s*(个)?汉字",

        lambda match: f"{match.group(1)}{max_cjk}个汉字",

        custom_prompt,

    )

    normalized = re.sub(

        r"(最多|不超过)\s*\d+\s*字",

        lambda match: f"{match.group(1)}{max_cjk}字",

        normalized,

    )

    normalized = re.sub(

        r"超过\s*\d+\s*字时",

        f"超过{max_cjk}字时",

        normalized,

    )

    return normalized + (

        f"\n\n【V12程序约束】{max_cjk}字仅为单条字幕硬上限，"

        "不得按固定字数机械切割。当前优化阶段只校正文字，"

        "必须保持字幕编号一一对应；语义断句和最终上限由程序统一处理。"

    )





class SubtitleThread(QThread):

    finished = pyqtSignal(str, str)

    progress = pyqtSignal(int, str)

    update = pyqtSignal(dict)

    update_all = pyqtSignal(dict)

    error = pyqtSignal(str)

    MAX_DAILY_LLM_CALLS = 30



    def __init__(self, task: SubtitleTask):

        super().__init__()

        self.task: SubtitleTask = task

        self.subtitle_length = 0

        self.finished_subtitle_length = 0

        self.custom_prompt_text = ""

        # 初始化数据库和服务使用管理器

        self.db_manager = DatabaseManager(CACHE_PATH)

        self.service_manager = ServiceUsageManager(self.db_manager)



    def set_custom_prompt_text(self, text: str):

        self.custom_prompt_text = text



    def _setup_api_config(self) -> SubtitleConfig:

        """设置API配置，返回SubtitleConfig"""

        requested = self.task.subtitle_config.thread_num

        self.task.subtitle_config.thread_num = min(3, max(1, int(requested)))

        if requested != self.task.subtitle_config.thread_num:

            logger.info('LLM任务线程限流：请求线程=%s 实际线程=%s；跨本进程任务共享3个请求名额',

                requested, self.task.subtitle_config.thread_num)

        self.task.subtitle_config.base_url = normalize_openai_base_url(

            self.task.subtitle_config.base_url

        )

        public_base_url = "https://ddg.bkfeng.top/v1"

        if self.task.subtitle_config.base_url == public_base_url:

            # 检查是否可以使用服务



            if not self.service_manager.check_service_available(

                "llm", self.MAX_DAILY_LLM_CALLS

            ):

                raise Exception(

                    self.tr(

                        f"公益LLM服务已达到每日使用限制 {self.MAX_DAILY_LLM_CALLS} 次，建议使用自己的API"

                    )

                )

            self.task.subtitle_config.thread_num = 3

            self.task.subtitle_config.batch_size = 10

            return self.task.subtitle_config



        if self.task.subtitle_config.base_url and self.task.subtitle_config.api_key:

            if getattr(self.task.subtitle_config,'text_source_mode',None) in ('strict','hybrid','voice','script_correct'):

                if not self.task.subtitle_config.llm_model:

                    raise ValueError('请先在LLM配置中填写剧本匹配模型')

                # The real matching request validates this endpoint; no extra

                # preflight completion per video is needed in script mode.

                return self.task.subtitle_config

            is_success, error_message = test_openai(

                self.task.subtitle_config.base_url,

                self.task.subtitle_config.api_key,

                self.task.subtitle_config.llm_model,

                task_check=True,

            )

            if not is_success:

                raise Exception(

                    self.tr(

                        "（字幕断句或字幕修正需要大模型）\n"

                        "OpenAI API 测试失败："

                    )

                    + error_message

                )

            # 增加服务使用次数

            if self.task.subtitle_config.base_url == public_base_url:

                self.service_manager.increment_usage("llm", self.MAX_DAILY_LLM_CALLS)

            return self.task.subtitle_config

        else:

            raise Exception(

                self.tr(

                    "（字幕断句或字幕修正需要大模型）\nOpenAI API 未配置, 请检查LLM配置"

                )

            )



    @guarded_task

    def run(self):

        try:

            logger.info(f"\n===========字幕处理任务开始===========")

            logger.info(f"时间：{datetime.datetime.now()}")



            # 字幕文件路径检查、对断句字幕路径进行定义

            subtitle_path = self.task.subtitle_path

            output_name = (

                Path(subtitle_path)

                .stem.replace("【原始字幕】", "")

                .replace("【下载字幕】", "")

            )

            split_path = str(

                Path(subtitle_path).parent / f"【断句字幕】{output_name}.srt"

            )

            assert subtitle_path is not None, self.tr("字幕文件路径为空")



            subtitle_config = self.task.subtitle_config

            permission_plan=config_permissions(subtitle_config)

            permission_plan['conflicts']=prompt_conflicts(subtitle_config.custom_prompt_text,subtitle_config.need_remove_punctuation)

            logger.info('LLM权限：%s',permission_plan)

            try:

                import json as _policy_json

                atomic_text(Path(self.task.video_path or subtitle_path).with_suffix('.LLM权限检查.json'),_policy_json.dumps(permission_plan,ensure_ascii=False,indent=2))

            except OSError:logger.warning('无法保存LLM权限记录；内存配置继续生效')

            source_mode = getattr(subtitle_config, 'text_source_mode', None)
            rule_contract = None
            if not any('; VideoCaptionerOriginalLanguage: '+lang in (subtitle_config.subtitle_style or '') for lang in ('ar','hi')):
                from app.core.template_rules import compile_rules
                rule_contract = compile_rules(vars(subtitle_config), source_mode)
                rule_path = Path(self.task.video_path or subtitle_path).with_suffix('.规则执行配置.json')
                import json
                atomic_text(rule_path, json.dumps(rule_contract, ensure_ascii=False, indent=2))


            script_locked = source_mode in ('strict', 'hybrid', 'script_correct')

            logger.info('本任务字幕文字来源：%s', source_mode or '客户模板原流程')

            arabic_original = '; VideoCaptionerOriginalLanguage: ar' in (subtitle_config.subtitle_style or '')

            keep_original = arabic_original or '; VideoCaptionerOriginalLanguage: hi' in (subtitle_config.subtitle_style or '')
            from app.core import customer_run as _customer
            customer_context=_customer.context_for(subtitle_config,self.task.video_path)
            from app.core.utils import temporary_delivery as _temporary
            temporary_context=None if customer_context else _temporary.context_for(self.task.video_path,subtitle_config)
            if temporary_context:
                subtitle_config.subtitle_style=_temporary.style_for(subtitle_config.subtitle_style)
                self.progress.emit(2,'本批临时交付：格式问题记入检查单，继续生成')

            custom_prompt = normalize_caption_limit_prompt(

                subtitle_config.custom_prompt_text,

                subtitle_config.max_word_count_cjk,

            )

            script_reference = '' if keep_original else (subtitle_config.frozen_script_reference if hasattr(subtitle_config,'frozen_script_reference') else load_video_script_reference(self.task.video_path))

            if script_locked:

                script_reference = ''

            if script_reference:

                logger.info(

                    "已加载本集剧本参考，共 %s 个字符",

                    len(script_reference),

                )

            protected_terms = extract_story_terms(custom_prompt)

            script_authoritative = False



            asr_data = ASRData.from_subtitle_file(subtitle_path)
            display_audit_changes=[]

            import copy

            # Separate voice-first Latin correction from the script-authoritative

            # Arabic/strict path. Never let legacy optimizers replace it again.

            source_text = ' '.join(c.text for c in asr_data.segments)

            voice_assisted = (source_mode == 'voice' and not keep_original

                              and bool(re.search(r'[A-Za-z]', source_text))

                              and not re.search(r'[\u0600-\u06ff\u3400-\u9fff\u0900-\u097f]', source_text))

            if script_locked:

                from app.core.utils.script_word_timing import load_native_words

                native_ok, native_message = load_native_words(asr_data, subtitle_path)

                logger.info('剧本模式时间来源：%s', native_message)

            if source_mode in ('script_correct','voice'):
                from app.core.caption_engine_v1.origin import restore_origins
                restore_origins(asr_data,Path(subtitle_path).with_suffix('.origin.json'))
            if source_mode in ('voice','script_correct') and self.task.video_path:
                from app.core.caption_engine_v1.voice_guard import exclude_confirmed,confirmed_for_media
                asr_data=exclude_confirmed(asr_data,confirmed_for_media(self.task.video_path))
            # Inspect original Chinese native units before correction/pagination.
            # The bounded local worker never uploads audio and never blocks export.
            if source_mode in ('voice','script_correct') and not keep_original and self.task.video_path:
                from app.core.utils.native_timing_review import review as review_native_timing
                asr_data,native_timing_report=review_native_timing(asr_data,subtitle_config,self.task.video_path)
                if native_timing_report:
                    if customer_context and native_timing_report['retained']:
                        _customer.note(customer_context,'时间待核对',detail={'items':native_timing_report['retained']})
                    try:
                        atomic_text(Path(self.task.output_path).with_suffix('.时间校准.json'),json.dumps(native_timing_report,ensure_ascii=False,indent=2))
                    except OSError:
                        logger.warning('时间校准检查单保存失败，继续字幕处理',exc_info=True)
                    logger.info('中文原生时间核对：疑点=%s，修正=%s，保留=%s',native_timing_report['suspect_groups'],len(native_timing_report['changes']),len(native_timing_report['retained']))
            original_timing_segments = copy.deepcopy(asr_data.segments)

            def _review_source_language(current):
                from app.core.utils.source_language_review import review as review_source_language
                current,source_language_records=review_source_language(current,subtitle_config,
                    getattr(subtitle_config,'frozen_script_reference','') or script_reference or '')
                if source_language_records:
                    if customer_context:
                        for item in source_language_records:_customer.note(customer_context,'原声语言待核对',detail=item)
                        customer_context.setdefault('stages',[]).append({'stage':'source_language_review','records':source_language_records,'speech_times_changed':False})
                    else:
                        review_file=Path(self.task.output_path).with_suffix('.原声语言核对.json')
                        atomic_text(review_file,json.dumps({'records':source_language_records},ensure_ascii=False,indent=2))
                    self.update_all.emit(current.to_json())
                return current

            if not script_locked:asr_data=_review_source_language(asr_data)

            if customer_context and not voice_assisted and not script_locked:
                asr_data=_customer.group_native_input(asr_data)

            if voice_assisted:

                from app.core.utils.script_word_timing import load_native_words

                from app.core.utils.adaptive_script import _fallback_cues

                from app.core.utils.english_layout import paginate_english

                from app.core.utils.voice_script_assist import process_voice_assist, collect_asr_review

                timing_data = copy.deepcopy(asr_data)

                native_ok, native_message = load_native_words(timing_data, subtitle_path)

                subtitle_config.raw_asr_reviews = collect_asr_review(subtitle_path)

                logger.info('语音辅助校正时间来源：%s', native_message)

                anchors = timing_data.segments if native_ok else original_timing_segments

                word_level_input = asr_data.is_word_timestamp()

                for i, cue in enumerate(asr_data.segments):

                    cue.voice_source_id = i

                from app.core.utils.english_text_policy import preserve_speaker_boundaries, correct_english, repair_sentence_units

                english_policy = '; VideoCaptionerEnglishSemanticWrap: 1' in (subtitle_config.subtitle_style or '')

                if english_policy:

                    asr_data.segments, speaker_report = preserve_speaker_boundaries(

                        asr_data, timing_data, native_ok, subtitle_path, _fallback_cues)

                else:

                    asr_data.segments = _fallback_cues(asr_data.segments)

                if (native_ok or word_level_input) and not english_policy and not (customer_context and customer_context['contract']['presentation']):

                    asr_data, voice_layout = paginate_english(asr_data,

                        subtitle_config.subtitle_style or '', anchors, native_only=True)

                    logger.info('语音为主原生时间分页：%s', voice_layout)

                elif not (native_ok or word_level_input):

                    logger.warning('没有原生词时间，保留原句区间；如需估算拆条将在客户规则执行报告中标记' if customer_context else '没有原生词时间，保留原字幕区间，不按字数推算分段时间')

                if hasattr(timing_data, 'source_duration_ms'):

                    asr_data.source_duration_ms = timing_data.source_duration_ms

                reference = subtitle_config.frozen_script_reference if hasattr(subtitle_config,'frozen_script_reference') else load_exact_video_script_reference(self.task.video_path)

                # Typography correction is mandatory even without a script.

                if subtitle_config.need_optimize and (reference or english_policy):

                    subtitle_config = self._setup_api_config()

                report_path = Path(self.task.video_path or subtitle_path).with_name(

                    Path(self.task.video_path or subtitle_path).stem + '.语音辅助校正报告.'

                    + datetime.datetime.now().strftime('%Y%m%d_%H%M%S_%f') + '.json')

                self.progress.emit(3, self.tr('语音为主，JSON仅辅助校正局部识别错误...'))

                if subtitle_config.need_optimize:

                    asr_data = process_voice_assist(asr_data, reference, subtitle_config, report_path)

                else:

                    asr_data.voice_assist_report={'summary':'模板已关闭字幕校正：未调用语音辅助校正模型','warnings':[],'status':'disabled'}

                    atomic_text(report_path,_policy_json.dumps(asr_data.voice_assist_report,ensure_ascii=False,indent=2))

                report = asr_data.voice_assist_report

                logger.info('%s；检查单：%s', report['summary'], report_path)

                for warning in report['warnings']:

                    logger.warning('语音辅助校正：%s', warning)
                    if customer_context:_customer.note(customer_context,'语音局部校正待核对',detail=warning)

                if english_policy and subtitle_config.need_optimize:

                    self.progress.emit(4, self.tr('英语大小写、标点独立校验（无需剧本）...'))

                    policy_path = Path(report_path).with_name(Path(report_path).stem + '.英语文字校验.json')

                    if customer_context:
                        before_english=copy.deepcopy(asr_data)
                        try:asr_data,policy_report=correct_english(asr_data,subtitle_config,policy_path,speaker_report,native_segments=anchors if (native_ok or word_level_input) else [],reference=reference)
                        except Exception as error:
                            asr_data=before_english;policy_report={'changes':[],'speaker_boundaries':speaker_report.get('speaker_boundaries',0),'semantic_splits':[]}
                            _customer.note(customer_context,'英语文字校验未完成，保留输入',error_type=type(error).__name__)
                    else:
                        asr_data, policy_report = correct_english(asr_data, subtitle_config, policy_path, speaker_report,

                        native_segments=anchors if (native_ok or word_level_input) else [], reference=reference)

                    if subtitle_config.need_split:

                        if customer_context:
                            before_sentence=copy.deepcopy(asr_data)
                            try:asr_data,sentence_report=repair_sentence_units(asr_data,anchors if (native_ok or word_level_input) else [],reference,allow_continuations=not bool(speaker_report.get('speaker_boundaries')))
                            except Exception as error:
                                asr_data=before_sentence;sentence_report={'status':'needs_review'}
                                _customer.note(customer_context,'句子修复未完成，保留输入',error_type=type(error).__name__)
                        else:
                            asr_data, sentence_report = repair_sentence_units(asr_data,

                            anchors if (native_ok or word_level_input) else [], reference,

                            allow_continuations=not bool(speaker_report.get('speaker_boundaries')))

                    else:sentence_report={"skipped":"模板关闭语义断句"}

                    policy_report['sentence_units'] = sentence_report

                    import json as _sentence_json

                    atomic_text(policy_path, _sentence_json.dumps(policy_report,ensure_ascii=False,indent=2))

                    if (native_ok or word_level_input) and not (customer_context and customer_context['contract']['presentation']):

                        asr_data, voice_layout = paginate_english(asr_data,

                            subtitle_config.subtitle_style or '', anchors, native_only=True)

                        logger.info('英语先校验断句、后原生时间分页：%s；语义拆分%s条',

                            voice_layout, len(policy_report.get('semantic_splits', [])))

                    logger.info('英语独立校验完成：修改%s条，保留说话人边界%s处；报告：%s',

                        len(policy_report['changes']), policy_report['speaker_boundaries'], policy_path)

                script_reference = ''

                self.update_all.emit(asr_data.to_json())

            if script_locked:

                reference = subtitle_config.frozen_script_reference if hasattr(subtitle_config,'frozen_script_reference') else load_exact_video_script_reference(self.task.video_path)

                if not reference:

                    raise ValueError('请先在剧本台词参考中绑定本视频的对应台词，再运行剧本模式；不会回退到转录文字。')

                subtitle_config = self._setup_api_config()

                report_path = Path(self.task.video_path).with_name(

                    Path(self.task.video_path).stem + '.台词匹配报告.' + datetime.datetime.now().strftime('%Y%m%d_%H%M%S_%f') + '.json')

                self.progress.emit(3, self.tr('原声定位、按本集剧本校正，疑似差异列入检查单...' if source_mode=='script_correct' else '软件内GPT语义匹配剧本，保留语音时间...'))

                if source_mode == 'script_correct':
                    subtitle_config._progress = lambda message: self.progress.emit(3, message)
                before_script=copy.deepcopy(asr_data) if (temporary_context or customer_context) else None
                try:
                    asr_data = process_script_mode(asr_data, reference, subtitle_config, report_path)
                except Exception as error:
                    if customer_context and source_mode=='script_correct':
                        asr_data=_customer.prepare_source_fallback(before_script,customer_context)
                        _customer.note(customer_context,'剧本校正未完成，保留原声',error_type=type(error).__name__)
                    elif temporary_context:asr_data=_temporary.fallback_matching(before_script,error,temporary_context)
                    else:raise

                if customer_context and source_mode=='script_correct':
                    from app.core.utils.unmatched_audio_review import review as review_unmatched_audio
                    asr_data,unmatched_audio_report=review_unmatched_audio(asr_data,subtitle_config,self.task.video_path)
                    if unmatched_audio_report:
                        atomic_text(report_path.with_suffix('.未匹配音频核对.json'),json.dumps(unmatched_audio_report,ensure_ascii=False,indent=2))
                        for item in unmatched_audio_report['retained']:
                            _customer.note(customer_context,'未匹配原声待听检',detail=item)
                        for item in unmatched_audio_report['removed']:
                            _customer.note(customer_context,'低证据识别隔离，原记录保留',detail=item)
                script_review = getattr(asr_data, 'script_review_report', {})
                if customer_context:
                    for warning in script_review.get('warnings',[]):_customer.note(customer_context,'剧本局部检查待核对',detail=warning)
                    for item in script_review.get('review_items',[]):_customer.note(customer_context,'剧本局部内容待核对',detail=item)

                logger.info('剧本局部纠错：%s；检查单：%s', script_review.get('summary'), script_review.get('review_path'))

                for warning in script_review.get('warnings', []):

                    logger.warning('剧本局部纠错：%s', warning)

                # Script matching controls wording, not the customer's display

                # policy. Never switch off punctuation, length or translation

                # options in the task's frozen template.

                self.update_all.emit(asr_data.to_json())



            # 1. 分割成字词级时间戳（对于非断句字幕且开启分割选项）

            if subtitle_config.need_split and not script_locked and not voice_assisted and not customer_context and not asr_data.is_word_timestamp():

                asr_data.split_to_word_segments()



            # Only explicit strict/hybrid modes may perform script-authoritative matching.

            if script_reference:

                logger.info('常规校正：剧本仅作参考；不执行隐式整段强替换')



            # 获取API配置，会先检查可用性（优先使用设置的API，其次使用自带的公益API）

            if (

                (subtitle_config.need_optimize and not script_locked and not voice_assisted)

                or (subtitle_config.need_split and asr_data.is_word_timestamp() and not keep_original and not script_locked and not voice_assisted and not customer_context)

                or (

                    (

                        subtitle_config.need_translate

                        and subtitle_config.translator_service

                        not in [

                            TranslatorServiceEnum.DEEPLX,

                            TranslatorServiceEnum.BING,

                            TranslatorServiceEnum.GOOGLE,

                        ]

                    )

                )

            ):

                self.progress.emit(2, self.tr('准备文字处理服务...') if customer_context else self.tr('正在检查LLM接口（单次25秒，临时失败最多尝试2次）...'))

                subtitle_config = self._setup_api_config()



            # 2. 重新断句（对于字词级字幕）

            if script_locked or voice_assisted or customer_context:

                pass  # Exact script wording and matched timing must not be regrouped.

            elif keep_original:

                if arabic_original:

                    from app.core.utils.arabic_captions import group_arabic_words

                    asr_data, timing_report = group_arabic_words(asr_data)

                else:

                    from app.core.utils.hindi_timing import group_hindi_words

                    asr_data, timing_report = group_hindi_words(asr_data)

                logger.info('原语种词时间停顿分条：%s', timing_report)

                self.update_all.emit(asr_data.to_json())

            elif subtitle_config.need_split and asr_data.is_word_timestamp():

                self.progress.emit(5, self.tr("字幕断句..."))

                logger.info("正在字幕断句...")

                splitter = SubtitleSplitter(

                    base_url=subtitle_config.base_url,api_key=subtitle_config.api_key,

                    thread_num=subtitle_config.thread_num,

                    model=subtitle_config.llm_model,

                    temperature=0.3,

                    timeout=60,

                    retry_times=1,

                    split_type=subtitle_config.split_type,

                    max_word_count_cjk=subtitle_config.max_word_count_cjk,

                    max_word_count_english=subtitle_config.max_word_count_english,

                    protected_terms=protected_terms,

                    script_reference=script_reference,

                )

                asr_data = splitter.split_subtitle(asr_data)

                asr_data.save(save_path=split_path)

                self.update_all.emit(asr_data.to_json())



            # 3. 优化字幕

            self.subtitle_length = len(asr_data.segments)



            if subtitle_config.need_optimize and not script_locked and not voice_assisted:

                self.progress.emit(0, self.tr("优化字幕..."))

                logger.info("正在优化字幕...")

                self.finished_subtitle_length = 0  # 重置计数器

                optimizer = SubtitleOptimizer(

                    base_url=subtitle_config.base_url,api_key=subtitle_config.api_key,execution_boundary=task_text_boundary(subtitle_config),

                    custom_prompt=custom_prompt,

                    script_reference=script_reference,

                    script_authoritative=script_authoritative,

                    model=subtitle_config.llm_model,

                    batch_num=subtitle_config.batch_size,

                    thread_num=subtitle_config.thread_num,

                    temperature=0.2,

                    update_callback=self.callback,

                )

                before_correction=copy.deepcopy(asr_data)

                if customer_context:
                    asr_data=_customer.optional_stage(asr_data,lambda data:guard_text_result(before_correction,optimizer.optimize_subtitle(data),keep_punctuation=not subtitle_config.need_remove_punctuation),customer_context,'general_correction')
                else:
                    asr_data = guard_text_result(before_correction,optimizer.optimize_subtitle(asr_data),keep_punctuation=not subtitle_config.need_remove_punctuation)

                if getattr(asr_data,'permission_review',[]):logger.warning("LLM输出与模板冲突，已保护原文：%s",asr_data.permission_review)

                self.update_all.emit(asr_data.to_json())



            numeric_overlap_repairs = repair_script_confirmed_numeric_overlap(

                asr_data.segments, '' if source_mode == 'voice' or not subtitle_config.need_optimize else script_reference

            )

            if numeric_overlap_repairs:

                logger.info(

                    "V23数字跨字幕去重：依据本集剧本修复 %s 处",

                    numeric_overlap_repairs,

                )



            shared_finalize = (not customer_context and source_mode == 'script_correct' and rule_contract
                and rule_contract['display']['substitution_policy']=='cn-general-v1'
                and not keep_original and not subtitle_config.need_translate)
            if shared_finalize:
                from app.core.caption_engine_v1.desktop import finalize_desktop
                asr_data = finalize_desktop(asr_data, original_timing_segments,
                    vars(subtitle_config), report_path, reference=reference)
                self.update_all.emit(asr_data.to_json())
            else:
                # V21 final text policy is independent of the LLM/optimizer. This

                # also covers local-ASR and correction-disabled workflows.

                if rule_contract:
                    from app.core.template_rules import apply_display_rules
                    from app.core.subtitle_processor.optimize import normalize_arabic_numbers
                    from app.core.utils.cn_general_policy import soften as saved_chinese_soften
                    display_changes = apply_display_rules(asr_data, rule_contract,
                        normalize_arabic_numbers, saved_chinese_soften)
                    text_policy_report = {'number_lines': 0, 'softened_lines': len(display_changes)}
                    display_audit_changes.extend(dict(item,phase='before_grouping') for item in display_changes)
                    display_path = Path(self.task.video_path or subtitle_path).with_suffix('.模板显示执行.json')
                    import json as _display_json
                    atomic_text(display_path, _display_json.dumps({'rule_sha256': rule_contract['sha256'],
                        'automatic_changes': display_changes}, ensure_ascii=False, indent=2))
                elif keep_original or voice_assisted or script_locked or not subtitle_config.need_optimize:
                    text_policy_report = {'number_lines': 0, 'softened_lines': 0}
                else:
                    asr_data, text_policy_report = apply_final_text_policy(asr_data, custom_prompt=custom_prompt)

                if any(text_policy_report.values()):

                    logger.info(

                        "V21最终文字硬校验：数字转中文 %s 条，友好用词 %s 条",

                        text_policy_report.get("number_lines", 0),

                        text_policy_report.get("softened_lines", 0),

                    )



                # V12 final guard: correction models must not be able to re-create

                # an overlong CJK caption.  Compliant subtitles pass through

                # unchanged, so translation and all other workflows keep their

                # existing behaviour.

                if script_locked and not keep_original and not customer_context:

                    from app.core.utils.script_template_layout import enforce_script_template_layout

                    asr_data, template_report = enforce_script_template_layout(

                        asr_data, original_timing_segments, subtitle_config,

                        protected_terms=protected_terms)

                    logger.info('剧本后模板规则：去标点=%s，中文上限=%s，断句=%s，排版结果=%s',

                        subtitle_config.need_remove_punctuation, subtitle_config.max_word_count_cjk,

                        subtitle_config.need_split, template_report)

                    if template_report['same_time_wraps']:

                        review_path = Path(self.task.video_path or subtitle_path).with_suffix('.模板排版检查.txt')

                        atomic_text(review_path, '\n'.join(template_report['review']))

                        logger.warning('部分长句缺少可靠词边界，保留原时间并按模板换行；检查单：%s', review_path)

                if subtitle_config.need_split and not keep_original and not script_locked and not voice_assisted and not customer_context:

                    asr_data = enforce_semantic_limit(

                        asr_data,

                        subtitle_config.max_word_count_cjk,

                        protected_terms=protected_terms,

                        script_reference=script_reference,

                    )

                self.update_all.emit(asr_data.to_json())



            asr_data=_review_source_language(asr_data)

            if customer_context and source_mode=='script_correct':
                asr_data=_customer.group_unmatched_input(asr_data)
            if rule_contract:
                from app.core.template_rules import settle_display_rules
                from app.core.subtitle_processor.optimize import normalize_arabic_numbers
                from app.core.utils.cn_general_policy import soften as saved_chinese_soften
                asr_data,display_finish=settle_display_rules(asr_data,rule_contract,normalize_arabic_numbers,saved_chinese_soften)
                display_audit_changes.extend(dict(item,phase='after_source_grouping') for item in display_finish['automatic_changes'])

            from functools import partial
            from app.core.utils.delivery_policy import read_policy,prepare,finalize,requires_translation
            delivery_policy=read_policy(subtitle_config.subtitle_style or '')
            if customer_context and delivery_policy:
                from functools import partial
                prepare=partial(_customer.prepare,context=customer_context)
                finalize=partial(_customer.finalize,context=customer_context)
            if temporary_context:
                from functools import partial
                prepare=partial(_temporary.prepare_delivery,context=temporary_context)
                finalize=partial(_temporary.finalize_delivery,context=temporary_context)
            if delivery_policy and not customer_context and not temporary_context:
                finalize=partial(finalize,need_translation=subtitle_config.need_translate)
            delivery_native=[]
            delivery_chain=[]
            if delivery_policy:
                if source_mode in ('strict','hybrid'):
                    raise ValueError('交付规则尚未适配完全替换模式；未静默忽略')
                if requires_translation(delivery_policy) and (not subtitle_config.need_translate or subtitle_config.subtitle_layout not in ('原文在上','译文在上')):
                    raise ValueError('双语交付规则需要开启翻译并选择双语排列')
                from app.core.utils.script_word_timing import load_native_words
                delivery_timing=copy.deepcopy(asr_data)
                delivery_timing.segments=copy.deepcopy(original_timing_segments)
                delivery_ok,delivery_note=load_native_words(delivery_timing,subtitle_path)
                delivery_native=delivery_timing.segments if delivery_ok else original_timing_segments
                asr_data,delivery_pages=prepare(asr_data,delivery_native,delivery_policy)
                from app.core.utils.delivery_policy import timing_audit
                delivery_chain.append(timing_audit(asr_data,delivery_native,'first_pagination'))
                logger.info('交付分页时间来源：%s；估算拆条数=%s（估算仅限原句区间，见客户规则执行报告）',delivery_note,sum(bool(getattr(c,'_display_time_estimated',False)) for c in asr_data.segments))
                logger.info('交付规则原生时间分页：%s',delivery_pages)
                atomic_text(Path(self.task.output_path).with_suffix('.交付分页检查.json'),_policy_json.dumps({'schema':'delivery-timing/v2','native_loader':delivery_note,'original_edges_fallback':not delivery_ok,'source_edges':len(delivery_native),'pages':len(asr_data.segments),'changes':delivery_pages,'estimated_display_cues':sum(bool(getattr(c,'_display_time_estimated',False)) for c in asr_data.segments),'estimated_timing_requires_review':any(getattr(c,'_display_time_estimated',False) for c in asr_data.segments)},ensure_ascii=False,indent=2))

            # 4. 翻译字幕

            if not subtitle_config.need_translate and not script_locked and not voice_assisted:

                from app.core.utils.english_layout import paginate_english

                asr_data, layout_report = paginate_english(asr_data,

                    subtitle_config.subtitle_style or '', original_timing_segments)

                if layout_report:

                    logger.info('英语无变形分页：%s', layout_report)

                    self.update_all.emit(asr_data.to_json())

            translator_map = {

                TranslatorServiceEnum.OPENAI: TranslatorType.OPENAI,

                TranslatorServiceEnum.DEEPLX: TranslatorType.DEEPLX,

                TranslatorServiceEnum.BING: TranslatorType.BING,

                TranslatorServiceEnum.GOOGLE: TranslatorType.GOOGLE,

            }

            if subtitle_config.need_translate:

                self.progress.emit(0, self.tr("翻译字幕..."))

                logger.info("正在翻译字幕...")

                self.finished_subtitle_length = 0  # 重置计数器

                from app.core.customer_execution import translation_guidance
                translator = TranslatorFactory.create_translator(

                    base_url=subtitle_config.base_url,api_key=subtitle_config.api_key,deeplx_endpoint=subtitle_config.deeplx_endpoint,execution_boundary=task_text_boundary(subtitle_config),

                    translator_type=translator_map[subtitle_config.translator_service],

                    thread_num=subtitle_config.thread_num,

                    batch_num=subtitle_config.batch_size,

                    target_language=subtitle_config.target_language,

                    model=subtitle_config.llm_model,

                    custom_prompt=custom_prompt+( translation_guidance(customer_context['contract']) if customer_context else ''),
                    bilingual_reference=getattr(subtitle_config,'frozen_bilingual_reference',None),

                    is_reflect=subtitle_config.need_reflect,

                    update_callback=self.callback,

                )

                from app.core.utils.bilingual_reference import select_groups
                bilingual=getattr(subtitle_config,'frozen_bilingual_reference',None)
                if bilingual:
                    import json as _bilingual_json
                    matched,unmatched=select_groups({str(i):c.text for i,c in enumerate(asr_data.segments,1)},bilingual)
                    atomic_text(Path(self.task.output_path).with_suffix('.双语参考检查.json'),_bilingual_json.dumps({'schema':'bilingual-use/v1','groups':len(bilingual['groups']),'matched_groups':[g['id'] for g in matched],'unmatched_subtitle_ids':unmatched,'allow_adapt':True,'status':'参考随请求传入翻译，最终译文需素材验收'},ensure_ascii=False,indent=2))

                before_translation=copy.deepcopy(asr_data)

                asr_data = guard_text_result(before_translation,translator.translate_subtitle(asr_data),translation=True)

                # 移除末尾标点符号

                if subtitle_config.need_remove_punctuation:

                    asr_data.remove_punctuation()

                self.update_all.emit(asr_data.to_json())

                if delivery_policy:
                    from app.core.utils.delivery_policy import repair_translation
                    if customer_context:repair_translation=partial(_customer.repair_translation,context=customer_context)
                    if temporary_context:repair_translation=partial(_temporary.repair_translation,context=temporary_context)
                    def delivery_translate(smaller):
                        return guard_text_result(copy.deepcopy(smaller),translator.translate_subtitle(smaller),translation=True)
                    def delivery_on_report(report):
                        delivery_chain.append(dict(stage='translation_check',passed=report['passed'],cues=report.get('timing_audit',{}).get('cues'),repair_history=report.get('repair_history',[])))
                        atomic_text(Path(self.task.output_path).with_suffix('.交付规则检查.json'),
                            _policy_json.dumps(report,ensure_ascii=False,indent=2))
                    asr_data=repair_translation(asr_data,before_translation,delivery_native,
                        delivery_policy,subtitle_config.subtitle_layout,delivery_translate,delivery_on_report)

                # 保存翻译结果(单语、双语)

                if self.task.need_next_task and self.task.video_path:

                    for subtitle_layout in ["原文在上", "译文在上", "仅原文", "仅译文"]:

                        save_path = str(

                            Path(self.task.subtitle_path).parent

                            / f"{Path(self.task.video_path).stem}-{subtitle_layout}.srt"

                        )

                        asr_data.save(

                            save_path=save_path,

                            ass_style=subtitle_config.subtitle_style,

                            layout=subtitle_layout,

                        )

                        logger.info(f"字幕保存到 {save_path}")



            # V16.1: 去标点是字幕输出设置，不应依赖是否开启翻译。

            # 在最终保存前再次统一执行，确保原文模式和翻译模式一致。

            if subtitle_config.need_remove_punctuation:

                asr_data.remove_punctuation()

                self.update_all.emit(asr_data.to_json())



            # Arabic presentation is a separate pass: exact dialogue stays

            # unchanged, and page cuts use only native ASR word timestamps.

            if arabic_original and not subtitle_config.need_translate:

                import copy, json

                from app.core.utils.arabic_pagination import paginate_arabic

                from app.core.utils.script_word_timing import load_native_words

                timing_data = ASRData.from_subtitle_file(subtitle_path)

                timing_ok, timing_note = load_native_words(timing_data, subtitle_path)

                native_words = timing_data.segments if timing_ok else []

                if hasattr(timing_data, 'source_duration_ms'):

                    asr_data.source_duration_ms = timing_data.source_duration_ms

                asr_data, pagination_report = paginate_arabic(asr_data,

                    subtitle_config.subtitle_style or '', native_words)

                pagination_report['native_timing_validation'] = timing_note

                Path(self.task.output_path).with_suffix('.layout.json').write_text(

                    json.dumps(pagination_report, ensure_ascii=False, indent=2), encoding='utf-8')

                logger.info('阿拉伯分页：原%s条 → %s条；一行%s条，两行%s条；合并%s处，重分配%s处，空隙延时%s处；剩余快闪%s条',

                    pagination_report['input_cues'], pagination_report['output_cues'],

                    pagination_report['single_line_cues'], pagination_report.get('two_line_cues',0),

                    len(pagination_report.get('merges',[])),pagination_report.get('rebalanced_pages',0),

                    len(pagination_report.get('display_holds',[])),len(pagination_report.get('short_cues',[])))

                if pagination_report.get('overflow_cues'):

                    raise ValueError('阿拉伯字幕无法在保留时间依据的前提下控制为最多两行，请检查分段报告')

                self.update_all.emit(asr_data.to_json())



            if customer_context and not delivery_policy and subtitle_config.need_split and not subtitle_config.need_translate:
                asr_data=_customer.finalize_single(asr_data,original_timing_segments,subtitle_config,customer_context)

            # English/Latin script fragments need the same bounded readability

            # protection as Arabic. Keep original word timing sidecars untouched.

            if (script_locked or voice_assisted) and not arabic_original and not subtitle_config.need_translate:

                text = ' '.join(c.text for c in asr_data.segments)

                if re.search(r'[A-Za-z]', text) and not re.search(r'[\u0600-\u06ff\u3400-\u9fff\u0900-\u097f]', text):

                    from app.core.utils.latin_readability import improve_latin_readability

                    asr_data, readability = improve_latin_readability(asr_data, subtitle_config.subtitle_style or '')

                    import json

                    atomic_text(Path(self.task.output_path).with_suffix('.readability.json'),

                                json.dumps(readability, ensure_ascii=False, indent=2))

                    logger.info('拉丁语字幕可读性：合并%s处，有限显示延时%s处，剩余不足0.5秒%s条',

                                len(readability['merges']), len(readability['display_holds']), len(readability['short_cues']))

                    self.update_all.emit(asr_data.to_json())



            if source_mode == 'voice' and rule_contract and rule_contract['display']['substitution_policy']=='cn-general-v1' and not subtitle_config.need_translate:
                from app.core.caption_engine_v1.voice_guard import guard_sidecar
                voice_report={}
                exclusions=[]
                exclusion_path=Path(self.task.video_path or subtitle_path).with_suffix('.asr_exclusions.json')
                if exclusion_path.is_file():
                    proof=json.loads(exclusion_path.read_text(encoding='utf-8'))
                    media=Path(self.task.video_path or '')
                    if media.is_file() and hashlib.sha256(media.read_bytes()).hexdigest()==proof.get('source_sha256'):
                        exclusions=proof.get('exclusions',[])
                try:
                    asr_data=guard_sidecar(asr_data,subtitle_path,vars(subtitle_config),voice_report,exclusions)
                finally:
                    atomic_text(Path(self.task.output_path).with_suffix('.原声输出检查.json'),json.dumps(voice_report,ensure_ascii=False,indent=2))

            if rule_contract:
                self.progress.emit(3,self.tr('执行模板最终数字与词语显示规则...'))
                from app.core.template_rules import settle_display_rules,issue_message
                from app.core.subtitle_processor.optimize import normalize_arabic_numbers
                from app.core.utils.cn_general_policy import soften as saved_chinese_soften
                asr_data,display_finish=settle_display_rules(asr_data,rule_contract,normalize_arabic_numbers,saved_chinese_soften)
                final_changes=display_finish['automatic_changes']
                display_audit_changes.extend(dict(item,phase='after_source_recovery') for item in final_changes)
                if final_changes and customer_context and not delivery_policy and subtitle_config.need_split and not subtitle_config.need_translate:
                    asr_data=_customer.finalize_single(asr_data,original_timing_segments,subtitle_config,customer_context)
                    # Reflow can form adjacent phrases; settle before export.
                    asr_data,reflow_finish=settle_display_rules(asr_data,rule_contract,normalize_arabic_numbers,saved_chinese_soften)
                    display_audit_changes.extend(dict(item,phase='after_display_reflow') for item in reflow_finish['automatic_changes'])
                    display_finish=reflow_finish
                violations=display_finish['issues']
                report_path=Path(self.task.video_path or subtitle_path).with_suffix('.模板规则检查.json')
                atomic_text(report_path,json.dumps(violations,ensure_ascii=False,indent=2))
                display_path=Path(self.task.video_path or subtitle_path).with_suffix('.模板显示执行.json')
                atomic_text(display_path,json.dumps({'rule_sha256':rule_contract['sha256'],'automatic_changes':display_audit_changes,'final_checked':True},ensure_ascii=False,indent=2))
                if violations:raise ValueError(issue_message(violations))

            if customer_context and source_mode not in ('strict','hybrid'):
                from app.core.utils.cjk_output_spacing import finish as finish_cjk_spacing
                asr_data,spacing_finish=finish_cjk_spacing(asr_data)
                if spacing_finish:customer_context['stages'].append({'stage':'final_cjk_spacing','changes':spacing_finish})
            if delivery_policy and not subtitle_config.need_translate:
                # Final substitutions or raw-source recovery may grow a cue.
                # Repage under the same single-language caps before export.
                asr_data,single_delivery_updates=prepare(asr_data,delivery_native,delivery_policy)
                delivery_chain.append({'stage':'single_language_final_pagination','changes':single_delivery_updates})
            if delivery_policy:
                asr_data,delivery_report=finalize(asr_data,delivery_policy,subtitle_config.subtitle_layout)
                atomic_text(Path(self.task.output_path).with_suffix('.交付规则检查.json'),
                    _policy_json.dumps(delivery_report,ensure_ascii=False,indent=2))
                if not delivery_report['passed']:
                    raise ValueError('最终交付检查未通过：'+str(delivery_report['issues'][0]))

            if delivery_policy:
                from app.core.utils.delivery_policy import export_audit
                if customer_context:export_audit=partial(_customer.export_audit,context=customer_context)
                if temporary_context:export_audit=partial(_temporary.export_audit,context=temporary_context)
                if not customer_context and not temporary_context:export_audit=partial(export_audit,need_translation=subtitle_config.need_translate)
                delivery_chain.append(export_audit(asr_data,delivery_native,delivery_policy,subtitle_config.subtitle_style,subtitle_config.subtitle_layout))
                atomic_text(Path(self.task.output_path).with_suffix('.字幕链路检查.json'),_policy_json.dumps({'schema':'subtitle-chain/v1','passed':True,'stages':delivery_chain,'acoustic_alignment':'not_verified'},ensure_ascii=False,indent=2))

            if customer_context and source_mode not in ('strict','hybrid'):
                from app.core.utils.cjk_output_spacing import finish as finish_cjk_spacing
                asr_data,spacing_finish=finish_cjk_spacing(asr_data)
                if spacing_finish:customer_context['stages'].append({'stage':'export_cjk_spacing','changes':spacing_finish})
            # After bounded readability adjustments, audit the same ASS events
            # that are about to be saved, rather than checking only word counts.
            if customer_context and not delivery_policy and subtitle_config.need_split and not subtitle_config.need_translate:
                from app.core.single_language_layout import audit_export
                customer_context['stages'].append(audit_export(asr_data,subtitle_config.subtitle_style,subtitle_config.subtitle_layout))
                _customer.save(customer_context,self.task.output_path)

            # 5. 保存字幕

            asr_data.save(

                save_path=self.task.output_path,

                ass_style=subtitle_config.subtitle_style,

                layout=subtitle_config.subtitle_layout,

            )

            logger.info(f"字幕保存到 {self.task.output_path}")



            # 6. 文件移动与清理

            if self.task.need_next_task and self.task.video_path:

                # 保存srt/ass文件到视频目录（对于全流程任务）

                save_srt_path = (

                    Path(self.task.video_path).parent

                    / f"{Path(self.task.video_path).stem}.srt"

                )

                asr_data.to_srt(

                    save_path=str(save_srt_path), layout=subtitle_config.subtitle_layout

                )

                # 同时在原视频旁保留最终样式 ASS。后续逐条修改和重新合成

                # 都直接使用这份文件，避免从 SRT 重建时丢失字体、描边、阴影和位置。

                save_ass_path = (

                    Path(self.task.video_path).parent

                    / f"{Path(self.task.video_path).stem}.ass"

                )

                asr_data.to_ass(

                    save_path=str(save_ass_path),

                    layout=subtitle_config.subtitle_layout,

                    style_str=subtitle_config.subtitle_style,

                )

                logger.info(f"可编辑样式字幕保存到 {save_ass_path}")

                # Keep the small original timing sidecar shared, so another

                # workstation can still use Enter-to-split word anchors later.

                original_path = Path(self.task.subtitle_path)

                if original_path.name.startswith('【原始字幕】'):

                    project_id = hashlib.sha1(str(Path(self.task.video_path).resolve()).lower().encode('utf-8')).hexdigest()[:16]

                    archive = SHARED_APPDATA_PATH / 'batch_projects' / 'projects' / project_id / 'subtitle' / original_path.name

                    try:

                        atomic_text(archive, original_path.read_text(encoding='utf-8'))

                    except OSError:

                        logger.exception('归档原始字幕时间点失败，最终字幕已保留')

            else:

                # 删除断句文件（对于仅字幕任务）

                split_path = str(

                    Path(self.task.subtitle_path).parent

                    / f"【智能断句】{Path(self.task.subtitle_path).stem}.srt"

                )

                if os.path.exists(split_path):

                    os.remove(split_path)



            if customer_context:
                customer_context['status']='completed_with_review' if customer_context['warnings'] else 'completed'
                _customer.save(customer_context,self.task.output_path)
                if customer_context['warnings']:asr_data.script_review_report=dict(status='completed_with_review',review_path=str(Path(self.task.video_path or self.task.output_path).with_suffix('.客户规则执行.json')))
            if temporary_context:
                _temporary.persist(temporary_context)
                asr_data.script_review_report=dict(status='completed_with_review',review_path=str(Path(self.task.video_path).with_suffix('.临时交付检查.json')))
            review = getattr(asr_data, 'script_review_report', {})

            if review.get('status') == 'completed_with_review':

                self.progress.emit(100, self.tr('字幕已完成（有待检查片段），可继续合成；检查单：') + review.get('review_path', ''))

            else:

                self.progress.emit(100, self.tr("优化完成"))

            logger.info("优化完成")

            self.finished.emit(self.task.video_path, self.task.output_path)

        except Exception as e:

            if locals().get('customer_context'):
                customer_context.update(status='failed',error_type=type(e).__name__,error=str(e))
                try:_customer.save(customer_context,self.task.output_path)
                except OSError:pass
            if locals().get('delivery_policy'):
                try:atomic_text(Path(self.task.output_path).with_suffix('.字幕链路检查.json'),_policy_json.dumps({'schema':'subtitle-chain/v1','passed':False,'stages':locals().get('delivery_chain',[]),'error_type':type(e).__name__,'error':str(e)},ensure_ascii=False,indent=2))
                except OSError:logger.warning('链路检查报告未保存，原错误保留')
            logger.exception(f"优化失败: {str(e)}")

            self.error.emit(str(e))

            self.progress.emit(100, self.tr("优化失败"))



    def callback(self, result: Dict):

        self.finished_subtitle_length += len(result)

        # 简单计算当前进度（0-100%）

        progress = min(

            int((self.finished_subtitle_length / self.subtitle_length) * 100), 100

        )

        self.progress.emit(progress, self.tr("{0}% 处理字幕").format(progress))

        self.update.emit(result)



    def stop(self):

        """停止所有处理"""

        try:

            # 先停止优化器

            if hasattr(self, "optimizer"):

                try:

                    self.optimizer.stop()

                except Exception as e:

                    logger.error(f"停止优化器时出错：{str(e)}")



            # 终止线程

            self.terminate()

            # 等待最多3秒

            if not self.wait(3000):

                logger.warning("线程未能在3秒内正常停止")



            # 发送进度信号

            self.progress.emit(100, self.tr("已终止"))



        except Exception as e:

            logger.error(f"停止线程时出错：{str(e)}")

            self.progress.emit(100, self.tr("终止时发生错误"))


