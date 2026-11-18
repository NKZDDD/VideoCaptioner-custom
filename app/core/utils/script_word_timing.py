"""Use verified native ASR word timestamps only in script matching mode."""
import json
import math
import re
import unicodedata
from pathlib import Path


def normalized(text):
    return ''.join(c for c in unicodedata.normalize('NFKC', text).casefold() if c.isalnum())


def load_native_words(data, subtitle_path):
    from app.core.bk_asr.asr_data import ASRDataSeg
    path = Path(subtitle_path).with_suffix('.asr.json')
    if not path.exists():
        return False, '没有原生词时间，保留原字幕时间；不按字数推算'
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
        segments = raw['segments']
        if len(segments) != len(data.segments):
            raise ValueError('原始结果与当前字幕段数不同')
        output = []
        previous = -1
        for source, cue in zip(segments, data.segments):
            start, end = float(source['start']), float(source['end'])
            if not math.isfinite(start) or not math.isfinite(end) or end <= start:
                raise ValueError('句子时间无效')
            if (abs(round(start*1000)-cue.start_time) > 20 or
                    abs(round(end*1000)-cue.end_time) > 20 or
                    normalized(source['text']) != normalized(cue.text)):
                raise ValueError('原始结果与当前字幕不一致')
            words = source.get('words')
            if not words or normalized(''.join(w['word'] for w in words)) != normalized(source['text']):
                raise ValueError('词时间未完整覆盖句子')
            for word in words:
                text = word['word'].strip()
                a, b = float(word['start']), float(word['end'])
                if (not text or not all(map(math.isfinite,(a,b))) or a < 0 or b <= a or
                        a < start-.02 or b > end+.02 or round(a*1000) < previous):
                    raise ValueError('词时间无效、倒序或超出句子')
                first, last = round(a*1000), round(b*1000)
                if last <= first:
                    raise ValueError('词持续时间不足1毫秒')
                output.append(ASRDataSeg(text, first, last))
                previous = last
        if not output:
            raise ValueError('词时间为空')
        duration = float(raw.get('duration', 0))
        if math.isfinite(duration) and round(duration*1000)>=output[-1].end_time:
            data.source_duration_ms = round(duration*1000)
        data.segments = output
        return True, f'已读取 {len(output)} 个原生词时间；没有估算或平移'
    except (ValueError, TypeError, KeyError, OSError) as error:
        return False, '词时间未采用，保留原字幕时间：' + str(error)


def script_excerpt(row, match):
    """Word indices are zero-based, end-exclusive; keep the original spelling."""
    first, last = match.get('script_word_start'), match.get('script_word_end')
    if first is None and last is None:
        return row, False
    tokens = list(re.finditer(r'\S+', row))
    if (type(first) is not int or type(last) is not int or
            not 0 <= first < last <= len(tokens)):
        raise ValueError('剧本词范围无效（结束编号不包含在内）')
    return row[tokens[first].start():tokens[last-1].end()], first != 0 or last != len(tokens)
