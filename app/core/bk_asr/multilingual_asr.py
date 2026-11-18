"""Model-independent, OpenAI-compatible audio transcription client.

The server must return verbose_json with global second-based segments.
No script rewriting or estimated word timestamps are performed here.
"""
import math
import io
import hashlib
import json
from urllib.parse import urlsplit

import requests

from .base import BaseASR
from .asr_data import ASRDataSeg


def parse_segments(data, need_words=False):
    if not isinstance(data, dict):
        raise ValueError('转录服务返回格式错误：需要 JSON 对象')
    items = data.get('segments')
    if not isinstance(items, list) or not items:
        raise ValueError('服务未返回带时间点的 segments；纯文字结果不能用于字幕')
    # Keep sentence boundaries until multilingual word handling is updated.
    # Never silently replace sentences with fabricated per-word timestamps.
    result = []
    previous = -1
    for item in items:
        if not isinstance(item, dict):
            raise ValueError('字幕段格式错误')
        text = str(item.get('text') or '').strip()
        if not text:
            continue
        try:
            start, end = float(item['start']), float(item['end'])
        except (KeyError, TypeError, ValueError):
            raise ValueError('字幕缺少有效起止时间（单位必须为秒）') from None
        if not all(map(math.isfinite, (start, end))) or start < 0 or end <= start:
            raise ValueError('字幕时间无效：结束时间必须晚于开始时间')
        if start < previous:
            raise ValueError('服务返回时间倒序；请检查长音频分段是否恢复整片时间')
        result.append(ASRDataSeg(text, round(start * 1000), round(end * 1000)))
        previous = start
    if not result:
        raise ValueError('转录结果为空，请检查语种和音频')
    return result


class MultilingualASR(BaseASR):
    def __init__(self, audio_path, base_url='', api_key='', model='',
                 language='', dialect='', need_word_time_stamp=False, use_cache=False):
        self.base_url = str(base_url or '').strip().rstrip('/')
        parts = urlsplit(self.base_url)
        if parts.scheme not in ('http', 'https') or not parts.netloc or parts.query or parts.fragment or parts.username:
            raise ValueError('请在多语言 ASR 设置中填写有效服务地址，例如 https://服务器/v1')
        self.model = str(model or '').strip()
        if not self.model:
            raise ValueError('请填写云端服务提供的转录模型名称')
        self.api_key = str(api_key or '').strip()
        self.language = str(language or '').strip()
        self.dialect = str(dialect or '').strip()
        self.need_word_time_stamp = need_word_time_stamp
        self.raw_response = None
        super().__init__(audio_path, use_cache=False)

    def _get_key(self):
        return hashlib.sha256(json.dumps([self.crc32_hex, self.base_url, self.model,
            self.language, self.dialect], ensure_ascii=False).encode()).hexdigest()

    def _run(self, callback=None):
        callback = callback or (lambda *_: None)
        callback(5, '上传音频并等待云端转录；长音频可能需要数分钟')
        args = {'model': self.model, 'response_format': 'verbose_json'}
        if self.language and self.language.lower() not in ('auto', 'none'):
            args['language'] = self.language
        if self.dialect:
            args['prompt'] = self.dialect
        headers = {'Authorization': 'Bearer ' + self.api_key} if self.api_key else {}
        try:
            # Do not retry POST automatically: a timeout may already have created
            # a paid GPU task. User can explicitly retry after checking the server.
            with requests.Session() as session:
                session.trust_env = False
                # Preserve fields and audio bytes, but stream small socket writes.
                prepared = session.prepare_request(requests.Request(
                    'POST', self.base_url + '/audio/transcriptions',
                    headers=headers, data=args,
                    files={'file': ('audio.wav', self.file_binary, 'audio/wav')}))
                with io.BytesIO(prepared.body) as body:
                    prepared.body = body
                    response = session.send(prepared,
                        timeout=(10, 1800), allow_redirects=False)
                if response.status_code != 200:
                    raise RuntimeError(f'云端转录失败（HTTP {response.status_code}）；请检查服务日志、模型和密钥')
                try:
                    data = response.json()
                except ValueError:
                    raise ValueError('服务返回的不是 JSON，请确认地址指向转录服务') from None
        except requests.Timeout:
            raise RuntimeError('云端转录超时；任务可能仍在服务端执行，请先检查再重试') from None
        except requests.RequestException:
            raise RuntimeError('无法连接云端 ASR，请检查服务地址、网络和证书') from None
        parse_segments(data)
        self.raw_response = data
        callback(95, '云端转录完成，正在保存时间轴')
        return data

    def _make_segments(self, data):
        return parse_segments(data, self.need_word_time_stamp)
