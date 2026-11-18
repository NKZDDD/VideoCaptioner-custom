from app.core.llm_permissions import TEXT_BOUNDARY,request_scope,safe_text
import hashlib
from string import Template
from typing import Callable, Dict, Optional, List, Any, Union
import logging
from pathlib import Path
import os
import retry
from app.core.utils.llm_transport import request_chat
from concurrent.futures import ThreadPoolExecutor, as_completed
from abc import ABC, abstractmethod
from enum import Enum
from openai import OpenAI
import json
from dataclasses import dataclass
from functools import lru_cache
import signal
import requests
import re
import html
from urllib.parse import quote

from app.core.bk_asr.asr_data import ASRData, ASRDataSeg
from app.core.utils import json_repair
from app.core.subtitle_processor.prompt import (
    TRANSLATE_PROMPT,
    REFLECT_TRANSLATE_PROMPT,
    SINGLE_TRANSLATE_PROMPT,
)
from app.core.storage.cache_manager import CacheManager
from app.config import CACHE_PATH
from app.core.utils.logger import setup_logger


logger = setup_logger("subtitle_translator")


class TranslatorType(Enum):
    """翻译器类型"""

    OPENAI = "openai"
    GOOGLE = "google"
    BING = "bing"
    DEEPLX = "deeplx"


class BaseTranslator(ABC):
    """翻译器基类"""

    def __init__(
        self,
        thread_num: int = 10,
        batch_num: int = 20,
        target_language: str = "Chinese",
        retry_times: int = 1,
        timeout: int = 60,
        update_callback: Optional[Callable] = None,
        custom_prompt: Optional[str] = None,
    ):
        self.thread_num = thread_num
        self.batch_num = batch_num
        self.target_language = target_language
        self.retry_times = retry_times
        self.timeout = timeout
        self.is_running = True
        self.update_callback = update_callback
        self.custom_prompt = custom_prompt
        self._init_thread_pool()
        self.cache_manager = CacheManager(CACHE_PATH)

    def _init_thread_pool(self):
        """初始化线程池"""
        self.executor = ThreadPoolExecutor(max_workers=self.thread_num)
        import atexit

        atexit.register(self.stop)

    def translate_subtitle(self, subtitle_data: Union[str, ASRData]) -> ASRData:
        """翻译字幕文件"""
        try:
            # 读取字幕文件
            if isinstance(subtitle_data, str):
                asr_data = ASRData.from_subtitle_file(subtitle_data)
            else:
                asr_data = subtitle_data

            # 将ASRData转换为字典格式
            subtitle_dict = {
                str(i): seg.text for i, seg in enumerate(asr_data.segments, 1)
            }

            # 分批处理字幕
            chunks = self._split_chunks(subtitle_dict)

            # 多线程翻译
            translated_dict = self._parallel_translate(chunks)

            # 创建新的ASRDataSeg列表
            new_segments = self._create_segments(asr_data.segments, translated_dict)

            return ASRData(new_segments)
        except Exception as e:
            logger.error(f"翻译失败：{str(e)}")
            raise RuntimeError(f"翻译失败：{str(e)}")

    def _split_chunks(self, subtitle_dict: Dict[str, str]) -> List[Dict[str, str]]:
        """将字幕分割成块"""
        items = list(subtitle_dict.items())
        return [
            dict(items[i : i + self.batch_num])
            for i in range(0, len(items), self.batch_num)
        ]

    def _parallel_translate(self,chunks):
        futures={self.executor.submit(self._safe_translate_chunk,chunk):chunk for chunk in chunks}
        translated_dict={};failed=[];failure_details=[]
        for future in as_completed(futures):
            if not self.is_running:raise InterruptedError('翻译已取消')
            chunk=futures[future]
            try:
                result=future.result()
                if not isinstance(result,dict) or set(result)!=set(chunk) or any(not safe_text(result[k],chunk[k]) or result[k].strip()=='ERROR' or result[k].endswith('||ERROR') for k in result):
                    raise ValueError('翻译结果编号或文字格式无效')
                translated_dict.update(result)
            except Exception as error:
                failed.append(list(chunk));current=error;seen=set();status=None
                while current is not None and id(current) not in seen:
                    seen.add(id(current));status=getattr(current,'status_code',None)
                    if status is not None:break
                    current=current.__cause__ or current.__context__
                failure_details.append(type(error).__name__+(' HTTP '+str(status) if status is not None else ''))
                logger.warning('翻译批次未完成：%s',failure_details[-1])
        if failed:raise RuntimeError(f'翻译有{len(failed)}个批次未完成；首个原因：'+failure_details[0]+'；不会把原文或ERROR当作译文完成')
        return translated_dict

    def _safe_translate_chunk(self, chunk: Dict[str, str]) -> Dict[str, str]:
        """安全的翻译块，包含重试逻辑"""
        for i in range(self.retry_times):
            try:
                result = self._translate_chunk(chunk)
                if self.update_callback:
                    self.update_callback(result)
                return result
            except Exception as e:
                if i == self.retry_times - 1:
                    raise
                logger.warning(f"翻译重试 {i+1}/{self.retry_times}: {str(e)}")

    @staticmethod
    def _create_segments(
        original_segments: List[ASRDataSeg], translated_dict: Dict[str, str]
    ) -> List[ASRDataSeg]:
        """创建新的字幕段"""
        for i, seg in enumerate(original_segments, 1):
            try:
                seg.translated_text = translated_dict[str(i)]  # 设置翻译文本
            except Exception as e:
                logger.error(f"创建新的字幕段失败：{str(e)}")
                seg.translated_text = seg.text
        return original_segments

    @abstractmethod
    def _translate_chunk(self, subtitle_chunk: Dict[str, str]) -> Dict[str, str]:
        """翻译字幕块"""
        pass

    def stop(self):
        """停止翻译器"""
        if not self.is_running:
            return

        logger.info("正在停止翻译器...")
        self.is_running = False
        if hasattr(self, "executor") and self.executor is not None:
            try:
                self.executor.shutdown(wait=False, cancel_futures=True)
            except Exception as e:
                logger.error(f"关闭线程池时出错：{str(e)}")
            finally:
                self.executor = None


class OpenAITranslator(BaseTranslator):
    """OpenAI翻译器"""

    def __init__(
        self,
        thread_num: int = 10,
        batch_num: int = 20,
        target_language: str = "Chinese",
        model: str = "gpt-4o-mini",
        custom_prompt: str = "",
        is_reflect: bool = False,
        temperature: float = 0.7,
        timeout: int = 60,
        retry_times: int = 1,
        update_callback: Optional[Callable] = None,
        base_url=None, api_key=None,execution_boundary=None,bilingual_reference=None,
    ):
        super().__init__(
            thread_num=thread_num,
            batch_num=batch_num,
            target_language=target_language,
            retry_times=retry_times,
            timeout=timeout,
            update_callback=update_callback,
        )

        from app.core.utils.bilingual_reference import validate_reference
        self.bilingual_reference=validate_reference(bilingual_reference)
        if self.bilingual_reference and str(target_language).lower() not in ('chinese','简体中文','中文','zh-cn','zh'):
            raise ValueError('本双语参考仅用于简体中文翻译，不能用于其他目标语言')
        self._task_base_url=base_url;self._task_api_key=api_key
        self._init_client()
        self.model = model
        self.custom_prompt = custom_prompt
        self.execution_boundary=execution_boundary or TEXT_BOUNDARY
        self.is_reflect = is_reflect
        self.temperature = temperature

    def _init_client(self):
        """初始化OpenAI客户端"""
        base_url = self._task_base_url if self._task_base_url is not None else os.getenv("OPENAI_BASE_URL")
        api_key = self._task_api_key if self._task_api_key is not None else os.getenv("OPENAI_API_KEY")
        if not (base_url and api_key):
            raise ValueError("环境变量 OPENAI_BASE_URL 和 OPENAI_API_KEY 必须设置")

        self.client = OpenAI(base_url=base_url, api_key=api_key)
        self.request_scope=request_scope(base_url)

    def _translate_chunk(self, subtitle_chunk: Dict[str, str]) -> Dict[str, str]:
        from app.core.subtitle_processor.translation_response import VERSION,PROTOCOL,decode
        from app.core.utils.bilingual_reference import guidance
        logger.info('[+]正在翻译字幕：%s - %s',next(iter(subtitle_chunk)),next(reversed(subtitle_chunk)))
        template=REFLECT_TRANSLATE_PROMPT if self.is_reflect else TRANSLATE_PROMPT
        prompt=Template(template).safe_substitute(target_language=self.target_language,custom_prompt=self.custom_prompt)
        prompt+=guidance(subtitle_chunk,getattr(self,'bilingual_reference',None))+PROTOCOL
        prompt_hash=hashlib.md5((VERSION+self.request_scope+self.execution_boundary+prompt).encode()).hexdigest()
        params=dict(target_language=self.target_language,is_reflect=self.is_reflect,temperature=self.temperature,prompt_hash=prompt_hash)
        cache_key=json.dumps(subtitle_chunk,ensure_ascii=False)
        cached=self.cache_manager.get_llm_result(cache_key,self.model,**params)
        good={};bad={}
        if cached:
            good,bad=decode(cached,subtitle_chunk)
            if not bad:return good
            logger.warning('旧翻译缓存有%s条未通过格式检查；仅恢复缺失条目',len(bad))
        if not cached:
            response=self._call_api(prompt,json.dumps(subtitle_chunk,ensure_ascii=False))
            good,bad=decode(response.choices[0].message.content,subtitle_chunk,self.is_reflect)
        if bad and good:
            try:self.cache_manager.set_llm_result(cache_key,json.dumps(good,ensure_ascii=False),self.model,**params)
            except Exception as error:logger.warning('局部翻译缓存写入失败：%s',type(error).__name__)
        if bad:
            logger.warning('翻译格式恢复：失败编号=%s；原因=%s；保留已完成%s条',','.join(bad),','.join(sorted(set(bad.values()))),len(good))
            missing={k:subtitle_chunk[k] for k in bad}
            repaired=self._translate_chunk_single(missing,context=subtitle_chunk)
            good.update(repaired)
        if set(good)!=set(subtitle_chunk):raise ValueError('翻译恢复后仍有缺失编号')
        try:self.cache_manager.set_llm_result(cache_key,json.dumps(good,ensure_ascii=False),self.model,**params)
        except Exception as error:logger.warning('翻译已完成但缓存写入失败：%s',type(error).__name__)
        return good

    def _translate_chunk_single(self, subtitle_chunk: Dict[str, str], context=None) -> Dict[str, str]:
        # Compatibility method name; one contextual repair request, never per-word fanout.
        from app.core.subtitle_processor.translation_response import PROTOCOL,decode
        from app.core.utils.bilingual_reference import guidance
        prompt=Template(REFLECT_TRANSLATE_PROMPT if self.is_reflect else TRANSLATE_PROMPT).safe_substitute(target_language=self.target_language,custom_prompt=self.custom_prompt)
        prompt+=guidance(context or subtitle_chunk,getattr(self,'bilingual_reference',None))+PROTOCOL
        prompt+='\nFORMAT REPAIR: output only the IDs in requested_subtitles. context_subtitles are source context only, never output their other IDs.'
        payload={'context_subtitles':context or subtitle_chunk,'requested_subtitles':subtitle_chunk}
        response=self._call_api(prompt,json.dumps(payload,ensure_ascii=False))
        good,bad=decode(response.choices[0].message.content,subtitle_chunk,self.is_reflect)
        if bad:raise ValueError('翻译格式恢复未通过；编号='+','.join(bad)+'；原因='+','.join(sorted(set(bad.values()))))
        return good

    def _call_api(self, prompt: str, user_content: Dict[str, str]) -> Any:
        """调用OpenAI API"""
        messages = [
            {"role": "system", "content": prompt+"\n"+self.execution_boundary+"\nAllowed translation target language: "+str(self.target_language)},
            {"role": "user", "content": user_content},
        ]

        return request_chat(self.client, request_stage='字幕翻译',
            model=self.model,
            messages=messages,
            temperature=self.temperature,
            timeout=self.timeout,
        )

    def _parse_response(self, response: Any) -> Dict[str, str]:
        """解析API响应"""
        try:
            result = json_repair.loads(response.choices[0].message.content)
            if self.is_reflect:
                return {k: v["revised_translation"] for k, v in result.items()}
            return result
        except Exception as e:
            raise ValueError(f"解析翻译结果失败：{str(e)}")


class GoogleTranslator(BaseTranslator):
    """谷歌翻译器"""

    def __init__(
        self,
        thread_num: int = 10,
        batch_num: int = 20,
        target_language: str = "Chinese",
        retry_times: int = 1,
        timeout: int = 20,
        update_callback: Optional[Callable] = None,
    ):
        super().__init__(
            thread_num=thread_num,
            batch_num=batch_num,
            target_language=target_language,
            retry_times=retry_times,
            timeout=timeout,
            update_callback=update_callback,
        )
        self.session = requests.Session()
        self.endpoint = "http://translate.google.com/m"
        self.headers = {
            "User-Agent": "Mozilla/4.0 (compatible;MSIE 6.0;Windows NT 5.1;SV1;.NET CLR 1.1.4322;.NET CLR 2.0.50727;.NET CLR 3.0.04506.30)"
        }
        self.lang_map = {
            "简体中文": "zh-CN",
            "繁体中文": "zh-TW",
            "英语": "en",
            "日本語": "ja",
            "韩语": "ko",
            "粤语": "yue",
            "法语": "fr",
            "德语": "de",
            "西班牙语": "es",
            "俄语": "ru",
            "葡萄牙语": "pt",
            "土耳其语": "tr",
        }

    def _translate_chunk(self, subtitle_chunk: Dict[str, str]) -> Dict[str, str]:
        """翻译字幕块"""
        result = {}
        if self.target_language in self.lang_map.values():
            target_lang = self.target_language
        else:
            target_lang = self.lang_map.get(self.target_language, "zh-CN")

        for idx, text in subtitle_chunk.items():
            try:
                # 检查缓存
                cache_params = {"target_language": target_lang}
                cache_result = self.cache_manager.get_translation(
                    text, TranslatorType.GOOGLE.value, **cache_params
                )

                if cache_result:
                    result[idx] = cache_result
                    logger.info(f"使用缓存的Google翻译结果：{idx}")
                    continue

                text = text[:5000]  # google translate max length
                response = self.session.get(
                    self.endpoint,
                    params={"tl": target_lang, "sl": "auto", "q": text},
                    headers=self.headers,
                    timeout=self.timeout,
                )

                if response.status_code == 400:
                    result[idx] = "TRANSLATION ERROR"
                    continue

                response.raise_for_status()
                re_result = re.findall(
                    r'(?s)class="(?:t0|result-container)">(.*?)<', response.text
                )
                if re_result:
                    translated_text = html.unescape(re_result[0])
                    # 保存到缓存
                    self.cache_manager.set_translation(
                        text,
                        translated_text,
                        TranslatorType.GOOGLE.value,
                        **cache_params,
                    )
                    result[idx] = translated_text
                else:
                    result[idx] = "ERROR"
                    logger.warning(f"无法从Google翻译响应中提取翻译结果: {idx}")
            except Exception as e:
                logger.error(f"Google翻译失败 {idx}: {str(e)}")
                result[idx] = "ERROR"
        return result


class BingTranslator(BaseTranslator):
    """必应翻译器"""

    def __init__(
        self,
        thread_num: int = 10,
        batch_num: int = 20,
        target_language: str = "Chinese",
        retry_times: int = 1,
        timeout: int = 20,
        update_callback: Optional[Callable] = None,
    ):
        super().__init__(
            thread_num=thread_num,
            batch_num=batch_num,
            target_language=target_language,
            retry_times=retry_times,
            timeout=timeout,
            update_callback=update_callback,
        )
        self.session = requests.Session()
        self.auth_endpoint = "https://edge.microsoft.com/translate/auth"
        self.translate_endpoint = (
            "https://api-edge.cognitive.microsofttranslator.com/translate"
        )
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
        }
        self.lang_map = {
            "简体中文": "zh-Hans",
            "繁体中文": "zh-Hant",
            "英语": "en",
            "日本語": "ja",
            "韩语": "ko",
            "粤语": "yue",
            "法语": "fr",
            "德语": "de",
            "西班牙语": "es",
            "俄语": "ru",
            "葡萄牙语": "pt",
            "土耳其语": "tr",
            "Chinese": "zh-Hans",
            "English": "en",
            "Japanese": "ja",
            "Korean": "ko",
            "French": "fr",
            "German": "de",
            "Russian": "ru",
            "Spanish": "es",
        }
        self._init_session()

    def _init_session(self):
        """初始化会话，获取必要的token"""
        try:
            response = self.session.get(self.auth_endpoint, timeout=self.timeout)
            response.raise_for_status()
            self.auth_token = response.text
            self.headers["authorization"] = f"Bearer {self.auth_token}"
        except Exception as e:
            logger.error(f"初始化必应翻译会话失败: {str(e)}")
            raise RuntimeError(f"初始化必应翻译会话失败: {str(e)}")

    def _translate_chunk(self, subtitle_chunk: Dict[str, str]) -> Dict[str, str]:
        """翻译字幕块"""
        result = {}
        if self.target_language in self.lang_map.values():
            target_lang = self.target_language
        else:
            target_lang = self.lang_map.get(self.target_language, "zh-Hans")

        # 准备批量翻译的数据
        texts_to_translate = []
        idx_map = []

        for idx, text in subtitle_chunk.items():
            # 检查缓存
            cache_params = {"target_language": target_lang}
            cache_result = self.cache_manager.get_translation(
                text, TranslatorType.BING.value, **cache_params
            )

            if cache_result:
                result[idx] = cache_result
                logger.debug(f"使用缓存的Bing翻译结果：{idx}")
            else:
                texts_to_translate.append({"Text": text[:5000]})  # 限制文本长度
                idx_map.append(idx)

        if texts_to_translate:
            try:
                params = {
                    "to": target_lang,
                    "api-version": "3.0",
                    "includeSentenceLength": "true",
                }

                response = self.session.post(
                    self.translate_endpoint,
                    params=params,
                    headers=self.headers,
                    json=texts_to_translate,
                    timeout=self.timeout,
                )
                response.raise_for_status()
                translations = response.json()

                # 处理翻译结果
                for i, translation in enumerate(translations):
                    idx = idx_map[i]
                    translated_text = translation["translations"][0]["text"]

                    # 保存到缓存
                    original_text = texts_to_translate[i]["Text"]
                    self.cache_manager.set_translation(
                        original_text,
                        translated_text,
                        TranslatorType.BING.value,
                        **{"target_language": target_lang},
                    )

                    result[idx] = translated_text

            except Exception as e:
                logger.error(f"必应翻译失败: {str(e)}")
                # 如果是token过期，尝试重新初始化会话
                if "token" in str(e).lower() or response.status_code in [401, 403]:
                    try:
                        self._init_session()
                    except Exception as e:
                        logger.error(f"重新初始化必应翻译会话失败: {str(e)}")
                # 对于失败的翻译，标记为错误
                for idx in idx_map:
                    if idx not in result:
                        result[idx] = "ERROR"

        return result


class DeepLXTranslator(BaseTranslator):
    """DeepLX翻译器"""

    def __init__(
        self,
        thread_num: int = 10,
        batch_num: int = 20,
        target_language: str = "Chinese",
        retry_times: int = 1,
        timeout: int = 20,
        update_callback: Optional[Callable] = None,
        endpoint=None,
    ):
        super().__init__(
            thread_num=thread_num,
            batch_num=batch_num,
            target_language=target_language,
            retry_times=retry_times,
            timeout=timeout,
            update_callback=update_callback,
        )
        self.session = requests.Session()
        self.endpoint = endpoint if endpoint is not None else os.getenv("DEEPLX_ENDPOINT", "https://api.deeplx.org/translate")
        self.lang_map = {
            "简体中文": "zh",
            "繁体中文": "zh-TW",
            "英语": "en",
            "日本語": "ja",
            "韩语": "ko",
            "法语": "fr",
            "德语": "de",
            "西班牙语": "es",
            "俄语": "ru",
            "葡萄牙语": "pt",
            "土耳其语": "tr",
            "Chinese": "zh",
            "English": "en",
            "Japanese": "ja",
            "Korean": "ko",
            "French": "fr",
            "German": "de",
            "Spanish": "es",
            "Russian": "ru",
        }

    def _translate_chunk(self, subtitle_chunk: Dict[str, str]) -> Dict[str, str]:
        """翻译字幕块"""
        result = {}
        if self.target_language in self.lang_map.values():
            target_lang = self.target_language
        else:
            target_lang = self.lang_map.get(self.target_language, "zh").lower()

        for idx, text in subtitle_chunk.items():
            try:
                # 检查缓存
                cache_params = {
                    "target_language": target_lang,
                    "endpoint": self.endpoint,
                }
                cache_result = self.cache_manager.get_translation(
                    text, TranslatorType.DEEPLX.value, **cache_params
                )

                if cache_result:
                    result[idx] = cache_result
                    logger.info(f"使用缓存的DeepLX翻译结果：{idx}")
                    continue

                response = self.session.post(
                    self.endpoint,
                    json={
                        "text": text,
                        "source_lang": "auto",
                        "target_lang": target_lang,
                    },
                    timeout=self.timeout,
                )
                response.raise_for_status()
                translated_text = response.json()["data"]

                # 保存到缓存
                self.cache_manager.set_translation(
                    text, translated_text, TranslatorType.DEEPLX.value, **cache_params
                )

                result[idx] = translated_text
            except Exception as e:
                logger.error(f"DeepLX翻译失败 {idx}: {str(e)}")
                result[idx] = "ERROR"
        return result


class TranslatorFactory:
    """翻译器工厂类"""

    @staticmethod
    def create_translator(
        translator_type: TranslatorType,
        thread_num: int = 5,
        batch_num: int = 10,
        target_language: str = "Chinese",
        model: str = "gpt-4o-mini",
        custom_prompt: str = "",
        temperature: float = 0.7,
        is_reflect: bool = False,
        update_callback: Optional[Callable] = None,
        base_url=None,api_key=None,deeplx_endpoint=None,execution_boundary=None,bilingual_reference=None,
    ) -> BaseTranslator:
        """创建翻译器实例"""
        try:
            if bilingual_reference and translator_type != TranslatorType.OPENAI:
                raise ValueError('双语剧本参考校译需要LLM大模型翻译服务；不会静默丢弃中文参考')
            if translator_type == TranslatorType.OPENAI:
                return OpenAITranslator(
                    base_url=base_url,api_key=api_key,execution_boundary=execution_boundary,bilingual_reference=bilingual_reference,
                    thread_num=thread_num,
                    batch_num=batch_num,
                    target_language=target_language,
                    model=model,
                    custom_prompt=custom_prompt,
                    is_reflect=is_reflect,
                    temperature=temperature,
                    update_callback=update_callback,
                )
            elif translator_type == TranslatorType.GOOGLE:
                batch_num = 5
                return GoogleTranslator(
                    thread_num=thread_num,
                    batch_num=batch_num,
                    target_language=target_language,
                    update_callback=update_callback,
                )
            elif translator_type == TranslatorType.BING:
                batch_num = 10
                return BingTranslator(
                    thread_num=thread_num,
                    batch_num=batch_num,
                    target_language=target_language,
                    update_callback=update_callback,
                )
            elif translator_type == TranslatorType.DEEPLX:
                batch_num = 5
                return DeepLXTranslator(
                    endpoint=deeplx_endpoint,
                    thread_num=thread_num,
                    batch_num=batch_num,
                    target_language=target_language,
                    update_callback=update_callback,
                )
            else:
                raise ValueError(f"不支持的翻译器类型：{translator_type}")
        except Exception as e:
            logger.error(f"创建翻译器失败：{str(e)}")
            raise
