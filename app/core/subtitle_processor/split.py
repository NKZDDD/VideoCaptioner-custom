from app.core.llm_permissions import TEXT_BOUNDARY,SPLIT_BOUNDARY,request_scope,sanitize_corrections,safe_text
import logging
import os
import re
import difflib
import sys
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from string import Template
from typing import List, Optional, Union
import json
from concurrent.futures import as_completed

from openai import OpenAI
from app.core.utils.llm_transport import request_chat

from app.config import CACHE_PATH
from app.core.bk_asr.asr_data import ASRData, ASRDataSeg
from app.core.storage.cache_manager import CacheManager
from app.core.subtitle_processor.prompt import (
    SPLIT_PROMPT_SEMANTIC,
    SPLIT_PROMPT_SENTENCE,
)
from app.core.subtitle_processor.script_alignment import (
    apply_script_boundary_hints,
    compact_script_guidance,
    script_chars_and_boundaries,
)
from app.core.utils.logger import setup_logger

logger = setup_logger("subtitle_splitter")

# V26 ships its own tokenizer so a copied VideoCaptioner folder behaves the
# same on every computer.  The first path is used by the installed app and the
# second by the staged/offline regression suite.
for _vendor_path in (
    Path(__file__).resolve().parents[2] / "vendor",
    Path(__file__).resolve().parent / "vendor",
):
    if (_vendor_path / "jieba").is_dir() and str(_vendor_path) not in sys.path:
        sys.path.insert(0, str(_vendor_path))
try:
    import jieba as _jieba

    _jieba.setLogLevel(logging.ERROR)
except Exception as _jieba_error:  # pragma: no cover - legacy fallback only
    _jieba = None
    logger.warning("V26中文分词组件不可用，将使用内置规则：%s", _jieba_error)

# 字幕分段的配置常量
MAX_WORD_COUNT_CJK = 25  # 中日韩文本最大字数


MAX_WORD_COUNT_ENGLISH = 18  # 英文文本最大单词数
SEGMENT_THRESHOLD = 300  # 每个分段的最大字数
MAX_GAP = 1500  # 允许每个词语之间的最大时间间隔（毫秒）
# A complete caption must not bridge a real speaking pause. Otherwise words
# spoken later are displayed from the first word's timestamp and look early.
CAPTION_MAX_INTERNAL_GAP_MS = 600
CAPTION_UNCONDITIONAL_GAP_MS = 1500
SEMANTIC_SPLIT_CACHE_VERSION = "semantic-dialogue-v27-entry-anchor-lock"

# These are clause-boundary cues, not a protected-word dictionary.  They only
# rank possible cuts when an LLM returns a segment above the configured hard
# limit; the LLM's semantic split remains the primary decision maker.
SEMANTIC_BOUNDARY_BEFORE = (
    "但是",
    "可是",
    "不过",
    "所以",
    "因为",
    "如果",
    "虽然",
    "然后",
    "而且",
    "否则",
    "于是",
    "给",
    "去",
    "来",
    "再",
    "才",
    "就",
    "都",
    "也",
    "还会",
    "不会",
    "不能",
    "可以",
    "必须",
    "对",
    "只要",
    "要是",
    "不如",
    "与其",
    "把",
    "被",
    "亲自",
    "主动",
    "接受",
    "这",
    "那",
    "哪",
    "每",
    "各",
    "沦落到",
)
SEMANTIC_BOUNDARY_AFTER = (
    "到底",
    "的话",
    "以后",
    "之前",
    "下来",
    "起来",
    "还是",
    "现在",
    "其实",
    "当然",
    "放心",
    "结果",
    "后来",
    "就是",
    "只是",
    "不过",
)
BAD_BOUNDARY_END = set("的地得把被让给向对从在和与或将到这那哪每各")
BAD_BOUNDARY_START = set("的地得了着过吗呢吧啊呀嘛啦")

# A small language-level safety net for the deterministic fallback.  These are
# ordinary, high-frequency lexical units that should not be broken even when a
# token-level timestamp contains an artificial pause in the middle.  The LLM
# remains the primary semantic splitter; this list is used only when its result
# still contains a caption above the configured hard limit.
SEMANTIC_JOIN_TERMS = (
    "打电话",
    "电话",
    "搞定",
    "道歉",
    "事情",
    "派出所",
    "分公司",
    "受伤",
    "联系",
    "接受",
    "和解",
    "支持",
    "医院",
    "绝对",
    "不会",
    "不能",
    "不但",
    "还是",
    "亲自",
    "坐视不管",
    "如你所愿",
    # Productive direction/result complements.  Acoustic timestamps can carry
    # a tiny artificial gap between their characters, but a caption must not
    # split ``下/来`` or ``起/来`` because of that gap.
    "下来",
    "起来",
    "出来",
    "进去",
    "回去",
    "过来",
    "沦落到",
)

CLAUSE_SUBJECT_PREFIXES = (
    "为什么",
    "怎么",
    "哪儿",
    "谁",
    "哪",
    "我",
    "你",
    "他",
    "她",
    "它",
    "咱",
)
CLAUSE_FINAL_PARTICLES = set("吗呢吧啊呀嘛")
TIME_TOPIC_WORDS = (
    "今天",
    "明天",
    "后天",
    "今晚",
    "以后",
    "回头",
    "一会",
    "一会儿",
    "等会",
    "等会儿",
    "待会",
    "待会儿",
    "到时候",
)


def is_pure_punctuation(text: str) -> bool:
    """
    检查字符串是否仅由标点符号组成

    Args:
        text: 待检查的文本

    Returns:
        bool: 是否仅包含标点符号
    """
    return not re.search(r"\w", text, flags=re.UNICODE)


def is_mainly_cjk(text: str) -> bool:
    """
    判断文本是否主要由中日韩文字组成

    Args:
        text: 输入文本

    Returns:
        bool: 如果CJK字符占比超过50%则返回True
    """
    # 定义以单字形式出现的语言字符范围
    cjk_patterns = [
        # 东亚语言
        r"[\u4e00-\u9fff]",  # 中日韩统一表意文字
        r"[\u3040-\u309f]",  # 日文平假名
        r"[\u30a0-\u30ff]",  # 日文片假名
        r"[\uac00-\ud7af]",  # 韩文音节
        r"[\u3130-\u318f]",  # 韩文兼容字母
        # 东南亚语言
        r"[\u0e00-\u0e7f]",  # 泰文
        r"[\u0e80-\u0eff]",  # 老挝文
        r"[\u1000-\u109f]",  # 缅甸文
        r"[\u1780-\u17ff]",  # 高棉文(柬埔寨)
        # 南亚语言
        r"[\u0900-\u097f]",  # 天城文(印地语等)
        r"[\u0980-\u09ff]",  # 孟加拉语
        r"[\u0a00-\u0a7f]",  # 古木基文(旁遮普语)
        r"[\u0b00-\u0b7f]",  # 奥里亚文
        r"[\u0c00-\u0c7f]",  # 泰卢固文
        r"[\u0c80-\u0cff]",  # 卡纳达文
        r"[\u0d00-\u0d7f]",  # 马拉雅拉姆文
        r"[\u0d80-\u0dff]",  # 僧伽罗文(斯里兰卡)
        # 其他
        r"[\u1100-\u11ff]",  # 谚文字母
        r"[\u3100-\u312f]",  # 注音符号
    ]

    # 计算CJK字符数
    cjk_count = 0
    for pattern in cjk_patterns:
        cjk_count += len(re.findall(pattern, text))

    # 计算总字符数（不包括空白字符）
    total_chars = len("".join(text.split()))

    # 如果CJK字符占比超过50%，则认为主要是CJK文本
    return cjk_count / total_chars > 0.5 if total_chars > 0 else False


def count_words(text: str) -> int:
    """
    统计多语言文本中的字符/单词数
    支持:
    - 英文（按空格分词）
    - CJK文字（中日韩统一表意文字）
    - 韩文/谚文
    - 泰文
    - 阿拉伯文
    - 俄文西里尔字母
    - 希伯来文
    - 越南文
    每个字符都计为1个单位，英文按照空格分词计数

    Args:
        text: 输入文本

    Returns:
        int: 字符/单词总数
    """
    # 定义各种语言的Unicode范围
    patterns = [
        r"[\u4e00-\u9fff]",  # 中日韩统一表意文字
        r"[\u3040-\u309f]",  # 平假名
        r"[\u30a0-\u30ff]",  # 片假名
        r"[\uac00-\ud7af]",  # 韩文音节
        r"[\u0e00-\u0e7f]",  # 泰文
        r"[\u0600-\u06ff]",  # 阿拉伯文
        r"[\u0400-\u04ff]",  # 西里尔字母（俄文等）
        r"[\u0590-\u05ff]",  # 希伯来文
        r"[\u1e00-\u1eff]",  # 越南文
        r"[\u3130-\u318f]",  # 韩文兼容字母
    ]

    # 统计所有非英文字符
    non_english_chars = 0
    remaining_text = text

    for pattern in patterns:
        # 计算当前语言的字符数
        chars = len(re.findall(pattern, remaining_text))
        non_english_chars += chars
        # 从文本中移除已计数的字符
        remaining_text = re.sub(pattern, " ", remaining_text)

    # 计算英文单词数（处理剩余的文本）
    english_words = len(remaining_text.strip().split())

    return non_english_chars + english_words


def join_caption_fragments(left: str, right: str) -> str:
    """Join captions without inventing spaces in Chinese numbers or amounts."""
    if not left:
        return right
    if not right:
        return left
    from app.core.utils.latin_spacing import join
    return join(left, right)


def _alignment_text(text: str) -> str:
    """Normalize harmless formatting while retaining every spoken character."""
    return re.sub(
        r"[\s\u3000,.，。！？!?;:；：“”‘’、…—-]+",
        "",
        text,
    ).lower()


@lru_cache(maxsize=4096)
def _lexical_token_spans(text: str):
    """Return stable multi-character Chinese word spans for boundary checks."""
    if _jieba is None or not text or not re.search(r"[\u3400-\u9fff]", text):
        return ()
    try:
        return tuple(
            (start, end, word)
            for word, start, end in _jieba.tokenize(text, mode="default")
            if end - start >= 2 and re.search(r"[\u3400-\u9fff]", word)
        )
    except Exception as error:  # pragma: no cover - corrupt dictionary fallback
        logger.debug("V26中文词边界分析失败：%s", error)
        return ()


def _cuts_lexical_token(
    text: str, index: int, max_token_units: int = MAX_WORD_COUNT_CJK
) -> bool:
    """Return True only for cuts inside a word that fits one caption."""
    for start, end, word in _lexical_token_spans(text):
        if start < index < end and count_words(word) <= max_token_units:
            return True
    return False


def _boundary_score(text: str, index: int, target: float) -> float:
    """Score a candidate cut by syntax cues and readable phrase length."""
    left = text[:index].strip()
    right = text[index:].strip()
    if not left or not right:
        return float("-inf")

    left_count = count_words(left)
    score = -abs(left_count - target) * 0.8
    previous = left[-1]
    following = right[0]

    if previous in "。！？!?":
        score += 18.0
    elif previous in "，、；：,;:":
        score += 12.0

    if any(right.startswith(marker) for marker in SEMANTIC_BOUNDARY_BEFORE):
        score += 7.0
    if any(left.endswith(marker) for marker in SEMANTIC_BOUNDARY_AFTER):
        score += 6.0
    # Prefer the start of productive Chinese adverb patterns such as
    # 一遍遍地 / 一次次地 / 一天天地 without maintaining an episode word list.
    if re.match(r"^一([\u4e00-\u9fff])\1地", right):
        score += 12.0
    # A cut before the final character of a verb/adjective is especially
    # likely when that character is immediately followed by an aspect or
    # structural particle: 威逼利|诱了、糊|涂的、哥|哥了.
    if len(right) >= 2 and right[1] in "了着过的地得":
        score -= 14.0
    # Do not detach a pronoun from the predicate that follows it merely because
    # the predicate itself is a normal clause-start cue: 拍下来他|给我磕头.
    if previous in "我你他她它您咱" and any(
        right.startswith(marker)
        for marker in SEMANTIC_BOUNDARY_BEFORE
        if marker not in {"这", "那", "哪", "每", "各"}
    ):
        score -= 12.0
    if previous in BAD_BOUNDARY_END:
        score -= 10.0
    if following in BAD_BOUNDARY_START:
        score -= 9.0
    if left_count < 3:
        score -= 8.0
    return score


def _cuts_protected_term(
    text: str,
    index: int,
    protected_terms: List[str],
    max_token_units: int = MAX_WORD_COUNT_CJK,
) -> bool:
    """Return True when a boundary cuts a name or a lexical word."""
    for term in list(SEMANTIC_JOIN_TERMS) + list(protected_terms):
        if len(term) < 2:
            continue
        for match in re.finditer(re.escape(term), text):
            if match.start() < index < match.end():
                return True
    return _cuts_lexical_token(text, index, max_token_units)


def _boundary_splits_protected_term(
    left: str, right: str, protected_terms: List[str]
) -> bool:
    """Detect a canonical entity that was created across an old boundary.

    Text correction runs after semantic splitting.  A homophone may therefore
    become a canonical name only after the boundary has already been chosen,
    for example ``傅泽 / 琰``.  Repair just those boundaries and leave all
    unrelated timing and segmentation untouched.
    """
    left = left.strip()
    right = right.strip()
    if not left or not right:
        return False
    for term in protected_terms:
        if len(term) < 2:
            continue
        for index in range(1, len(term)):
            if left.endswith(term[:index]) and right.startswith(term[index:]):
                return True
    return False


def _boundary_splits_lexical_token(
    left: str, right: str, max_token_units: int
) -> bool:
    """Detect a Chinese word split across two already-created captions."""
    left = left.strip()
    right = right.strip()
    if not left or not right:
        return False
    joined = join_caption_fragments(left, right)
    cut = len(left)
    return _cuts_lexical_token(joined, cut, max_token_units)


def _repair_protected_boundaries(
    segments: List[ASRDataSeg],
    max_word_count: int,
    protected_terms: List[str],
    script_reference: str = "",
) -> List[ASRDataSeg]:
    """Merge and reflow boundaries that split names or Chinese words."""
    if not segments:
        return segments

    repaired: List[ASRDataSeg] = []
    repaired_count = 0
    index = 0
    while index < len(segments):
        current = segments[index]
        if index + 1 >= len(segments):
            repaired.append(current)
            index += 1
            continue

        following = segments[index + 1]
        time_gap = max(0, int(following.start_time) - int(current.end_time))
        if _script_marks_text_boundary(
            current.text, following.text, script_reference
        ):
            repaired.append(current)
            index += 1
            continue
        splits_name = _boundary_splits_protected_term(
            current.text, following.text, protected_terms
        )
        splits_word = _boundary_splits_lexical_token(
            current.text, following.text, max_word_count
        )
        if time_gap > CAPTION_MAX_INTERNAL_GAP_MS or not (
            splits_name or splits_word
        ):
            repaired.append(current)
            index += 1
            continue

        merged_text = (current.text + following.text).replace("\\N", "").replace(
            "\n", ""
        ).strip()
        chunks = semantic_split_text(merged_text, max_word_count, protected_terms)
        if len(chunks) == 1:
            repaired.append(
                ASRDataSeg(chunks[0], current.start_time, following.end_time)
            )
            repaired_count += 1
            index += 2
            continue
        if len(chunks) == 2:
            # Keep the original acoustic ranges instead of creating a new
            # proportional timestamp. The text boundary changed, but these
            # word-level time anchors remain more accurate than an estimate.
            repaired.append(
                ASRDataSeg(chunks[0], current.start_time, current.end_time)
            )
            repaired.append(
                ASRDataSeg(chunks[1], following.start_time, following.end_time)
            )
            repaired_count += 1
            index += 2
            continue
        total_units = max(1, sum(max(1, count_words(chunk)) for chunk in chunks))
        duration = max(0, following.end_time - current.start_time)
        elapsed_units = 0
        for chunk_index, chunk in enumerate(chunks):
            start_time = current.start_time + round(
                duration * elapsed_units / total_units
            )
            elapsed_units += max(1, count_words(chunk))
            end_time = (
                following.end_time
                if chunk_index == len(chunks) - 1
                else current.start_time
                + round(duration * elapsed_units / total_units)
            )
            repaired.append(ASRDataSeg(chunk, start_time, end_time))
        repaired_count += 1
        index += 2

    if repaired_count:
        logger.info("V26统一词边界修复：重新合并并断句 %s 处", repaired_count)
    return repaired


def _script_marks_text_boundary(
    left: str, right: str, script_reference: str
) -> bool:
    """Check a boundary against the episode script, tolerating one filler."""
    if not left or not right or not script_reference:
        return False
    script_text, boundary_list = script_chars_and_boundaries(script_reference)
    boundaries = set(boundary_list)
    left_clean = _alignment_text(left)
    right_clean = _alignment_text(right)
    variants = [left_clean]
    trimmed = left_clean.rstrip("嗯呃啊呀哟哦诶唉哎嘛啦哈呵嘿嘻")
    if trimmed and trimmed != left_clean:
        variants.append(trimmed)
    for left_variant in variants:
        joined = left_variant + right_clean
        cursor = 0
        while joined and (position := script_text.find(joined, cursor)) >= 0:
            if position + len(left_variant) in boundaries:
                return True
            cursor = position + 1
    return False


def _reflow_weak_boundaries(
    segments: List[ASRDataSeg],
    max_word_count: int,
    protected_terms: List[str],
    script_reference: str,
) -> List[ASRDataSeg]:
    """Move a weak cut to a much stronger semantic boundary with new timing.

    This runs only for adjacent, already-valid CJK captions whose combined text
    exceeds the hard limit. Script/speaker boundaries are immutable. When a
    cut moves, its timestamp is interpolated inside the side that donated the
    text, preventing newly moved words from appearing at the old timestamp.
    """
    if not script_reference or len(segments) < 2:
        return segments
    changed = 0
    for index in range(len(segments) - 1):
        left_segment = segments[index]
        right_segment = segments[index + 1]
        left = left_segment.text.replace("\\N", "").replace("\n", "").strip()
        right = right_segment.text.replace("\\N", "").replace("\n", "").strip()
        if not left or not right or not (is_mainly_cjk(left) and is_mainly_cjk(right)):
            continue
        gap = max(0, int(right_segment.start_time) - int(left_segment.end_time))
        if gap > CAPTION_MAX_INTERNAL_GAP_MS:
            continue
        if _script_marks_text_boundary(left, right, script_reference):
            continue
        left_units = count_words(left)
        right_units = count_words(right)
        total_units = left_units + right_units
        if (
            left_units > max_word_count
            or right_units > max_word_count
            or total_units <= max_word_count
            or total_units > max_word_count * 2
        ):
            continue
        merged = join_caption_fragments(left, right)
        chunks = semantic_split_text(merged, max_word_count, protected_terms)
        if len(chunks) != 2 or "".join(chunks) != merged:
            continue
        current_cut = len(left)
        proposed_cut = len(chunks[0])
        if current_cut == proposed_cut:
            continue
        target = max(4.0, max_word_count * 0.72)
        current_score = _boundary_score(merged, current_cut, target)
        proposed_score = _boundary_score(merged, proposed_cut, target)
        if not _cuts_protected_term(
            merged, current_cut, protected_terms, max_word_count
        ) and proposed_score < current_score + 4.0:
            continue

        old_boundary = int(left_segment.end_time)
        if proposed_cut < current_cut:
            ratio = max(0.0, min(1.0, proposed_cut / max(1, current_cut)))
            new_boundary = int(
                round(
                    int(left_segment.start_time)
                    + (int(left_segment.end_time) - int(left_segment.start_time))
                    * ratio
                )
            )
        else:
            moved_units = proposed_cut - current_cut
            ratio = max(0.0, min(1.0, moved_units / max(1, len(right))))
            new_boundary = int(
                round(
                    int(right_segment.start_time)
                    + (int(right_segment.end_time) - int(right_segment.start_time))
                    * ratio
                )
            )
        new_boundary = max(
            int(left_segment.start_time),
            min(int(right_segment.end_time), new_boundary),
        )
        left_segment.text = chunks[0]
        right_segment.text = chunks[1]
        left_segment.end_time = new_boundary
        right_segment.start_time = new_boundary
        changed += 1
        logger.info(
            "V27语义边界重排：%s|%s -> %s|%s，时间 %sms -> %sms",
            left,
            right,
            chunks[0],
            chunks[1],
            old_boundary,
            new_boundary,
        )
    if changed:
        logger.info("V27语义边界重排完成：修复 %s 处", changed)
    return segments


def semantic_split_text(
    text: str, max_word_count: int, protected_terms: Optional[List[str]] = None, *, visible_chars: bool = False
) -> List[str]:
    """Apply a deterministic semantic fallback without fixed-width chopping.

    The primary splitter is the LLM.  This function is only the hard safety net
    used when the model or the later correction pass still exceeds the limit.
    It prefers punctuation and clause boundaries and treats the maximum as a
    ceiling rather than a desired line length.
    """
    # Delivery capacity counts punctuation and Latin characters, unlike semantic words.
    measure = len if visible_chars else count_words
    clean_text = text.replace("\\N", "").replace("\n", "").strip()
    if not clean_text or measure(clean_text) <= max_word_count:
        return [clean_text] if clean_text else []

    result: List[str] = []
    remaining = clean_text
    protected_terms = protected_terms or []
    while measure(remaining) > max_word_count:
        candidates = []
        for index in range(1, len(remaining)):
            left = remaining[:index]
            left_count = measure(left)
            if left_count > max_word_count:
                break
            if visible_chars and re.match(r"[A-Za-z0-9]", remaining[index-1]) and re.match(r"[A-Za-z0-9]", remaining[index]):
                continue
            if _cuts_protected_term(
                remaining, index, protected_terms, max_word_count
            ):
                continue
            candidates.append(
                (
                    _boundary_score(
                        remaining, index, target=max(4.0, max_word_count * 0.72)
                    ),
                    index,
                )
            )

        if not candidates:
            # This is reachable mainly for an unusually long Latin token.  It
            # keeps the loop safe without changing normal CJK behaviour.
            if visible_chars:
                raise ValueError("完整词语或受保护术语超过每行字符上限")
            split_index = min(max_word_count, len(remaining) - 1)
        else:
            _, split_index = max(candidates, key=lambda item: (item[0], item[1]))

        chunk = remaining[:split_index].strip()
        if not chunk:
            split_index = min(max_word_count, len(remaining) - 1)
            chunk = remaining[:split_index].strip()
        result.append(chunk)
        remaining = remaining[split_index:].strip()

    if remaining:
        result.append(remaining)
    return result


def enforce_semantic_limit(
    asr_data: ASRData,
    max_word_count_cjk: int,
    protected_terms: Optional[List[str]] = None,
    script_reference: str = "",
) -> ASRData:
    """Guarantee one CJK subtitle has at most the configured visible units.

    This final guard runs after text correction.  It is a no-op for compliant
    subtitles and leaves translated/bilingual segments untouched.  When a
    correction expands one segment, its original time span is divided in
    proportion to the resulting semantic chunks.
    """
    if max_word_count_cjk <= 0:
        return asr_data

    source_segments = asr_data.segments
    # One repaired pair can expose a second broken boundary on its right.  A
    # few bounded passes handle chains such as ``髋 | 关节 | 的力量`` without
    # allowing an unbounded merge loop.
    for _ in range(4):
        before = [
            (segment.text, segment.start_time, segment.end_time)
            for segment in source_segments
        ]
        source_segments = _repair_protected_boundaries(
            source_segments,
            max_word_count_cjk,
            protected_terms or [],
            script_reference,
        )
        after = [
            (segment.text, segment.start_time, segment.end_time)
            for segment in source_segments
        ]
        if after == before:
            break
    source_segments = _reflow_weak_boundaries(
        source_segments,
        max_word_count_cjk,
        protected_terms or [],
        script_reference,
    )
    guarded: List[ASRDataSeg] = []
    changed = 0
    for segment in source_segments:
        text = segment.text.replace("\\N", "").replace("\n", "").strip()
        if (
            segment.translated_text
            or not is_mainly_cjk(text)
            or count_words(text) <= max_word_count_cjk
        ):
            segment.text = text
            guarded.append(segment)
            continue

        chunks = semantic_split_text(text, max_word_count_cjk, protected_terms)
        if len(chunks) <= 1:
            segment.text = text
            guarded.append(segment)
            continue

        changed += 1
        total_units = max(1, sum(max(1, count_words(chunk)) for chunk in chunks))
        duration = max(0, segment.end_time - segment.start_time)
        elapsed_units = 0
        for chunk_index, chunk in enumerate(chunks):
            start_time = segment.start_time + round(
                duration * elapsed_units / total_units
            )
            elapsed_units += max(1, count_words(chunk))
            end_time = (
                segment.end_time
                if chunk_index == len(chunks) - 1
                else segment.start_time
                + round(duration * elapsed_units / total_units)
            )
            guarded.append(ASRDataSeg(chunk, start_time, end_time))

    if changed:
        logger.info(
            "V16最终单行校验：重新按语义边界分割 %s 条超长字幕",
            changed,
        )
    return ASRData(guarded)


def preprocess_segments(
    segments: List[ASRDataSeg], need_lower: bool = True
) -> List[ASRDataSeg]:
    """
    预处理ASR数据分段:
    1. 移除纯标点符号的分段
    2. 对仅包含字母、数字和撇号的文本进行小写处理并添加空格

    Args:
        segments: ASR数据分段列表
        need_lower: 是否需要转换为小写

    Returns:
        List[ASRDataSeg]: 处理后的分段列表
    """
    new_segments = []
    for seg in segments:
        if not is_pure_punctuation(seg.text):
            # 如果文本只包含字母、数字和撇号，则将其转换为小写并添加一个空格
            if re.match(r"^[a-zA-Z0-9\']+$", seg.text.strip()):
                if need_lower:
                    seg.text = seg.text.lower() + " "
                else:
                    seg.text = seg.text + " "
            new_segments.append(seg)
    return new_segments


class SubtitleSplitter:
    """字幕分割器，支持缓存功能"""

    def __init__(
        self,
        thread_num: int = 5,
        model: str = "gpt-4o-mini",
        temperature: float = 0.4,
        timeout: int = 60,
        retry_times: int = 1,
        split_type: str = "semantic",
        max_word_count_cjk: int = MAX_WORD_COUNT_CJK,
        max_word_count_english: int = MAX_WORD_COUNT_ENGLISH,
        use_cache: bool = True,
        protected_terms: Optional[List[str]] = None,
        script_reference: str = "",
        base_url=None, api_key=None,
    ):
        """
        初始化字幕分割器

        Args:
            thread_num: 并行处理的线程数
            model: 使用的LLM模型名称
            temperature: LLM温度参数
            timeout: API超时时间（秒）
            retry_times: 重试次数
            split_type: 分段类型，可选值："semantic"（语义分段）或"sentence"（句子分段）
            max_word_count_cjk: 中日韩文本最大字数
            max_word_count_english: 英文文本最大单词数
            use_cache: 是否使用缓存
        """
        self._task_base_url=base_url;self._task_api_key=api_key
        self._init_client()
        self.thread_num = thread_num
        self.model = model
        self.temperature = temperature
        self.timeout = timeout
        self.retry_times = retry_times
        self.split_type = split_type
        self.max_word_count_cjk = max_word_count_cjk
        self.max_word_count_english = max_word_count_english
        self.use_cache = use_cache
        self.protected_terms = list(dict.fromkeys(protected_terms or []))
        self.script_reference = script_reference or ""
        self.is_running = True
        self._init_thread_pool()
        self.cache_manager = CacheManager(str(CACHE_PATH))

        # 验证分段类型
        if split_type not in ["semantic", "sentence"]:
            raise ValueError(
                f"无效的分段类型: {split_type}，必须是 'semantic' 或 'sentence'"
            )

    def _init_client(self):
        """初始化OpenAI客户端"""
        base_url = self._task_base_url if self._task_base_url is not None else os.getenv("OPENAI_BASE_URL")
        api_key = self._task_api_key if self._task_api_key is not None else os.getenv("OPENAI_API_KEY")
        if not (base_url and api_key):
            raise ValueError("环境变量 OPENAI_BASE_URL 和 OPENAI_API_KEY 必须设置")

        self.client = OpenAI(base_url=base_url, api_key=api_key)
        self.request_scope=request_scope(base_url)

    def _init_thread_pool(self):
        """初始化线程池"""
        self.executor = ThreadPoolExecutor(max_workers=self.thread_num)
        import atexit

        atexit.register(self.stop)

    def split_subtitle(self, subtitle_data: Union[str, ASRData]) -> ASRData:
        """
        分割字幕文件

        Args:
            subtitle_file: 字幕文件路径

        Returns:
            处理后的ASR数据对象
        """
        try:
            # 读取字幕文件
            if isinstance(subtitle_data, str):
                asr_data = ASRData.from_subtitle_file(subtitle_data)
            else:
                asr_data = subtitle_data

            if not asr_data.is_word_timestamp():
                asr_data = asr_data.split_to_word_segments()

            # 预处理ASR数据
            asr_data.segments = preprocess_segments(asr_data.segments, need_lower=False)
            txt = asr_data.to_txt().replace("\n", "")

            # 确定分段数
            total_word_count = count_words(txt)
            num_segments = self._determine_num_segments(total_word_count)
            logger.info(f"根据字数 {total_word_count}，确定分段数: {num_segments}")

            # 分割ASR数据
            asr_data_list = self._split_asr_data(asr_data, num_segments)

            # 多线程处理每个asr_data
            processed_segments = self._process_segments(asr_data_list)

            # 合并所有处理后的分段
            final_segments = self._merge_processed_segments(processed_segments)

            # 对短句进行合并优化
            self.merge_short_segment(final_segments)

            return ASRData(final_segments)

        except Exception as e:
            logger.error(f"分割失败：{str(e)}")
            raise RuntimeError(f"分割失败：{str(e)}")

    def _determine_num_segments(self, word_count: int, threshold: int = 500) -> int:
        """
        根据字数确定分段数

        Args:
            word_count: 总字数
            threshold: 每段的目标字数

        Returns:
            分段数
        """
        num_segments = word_count // threshold
        if word_count % threshold > 0:
            num_segments += 1
        return max(1, num_segments)

    def _split_asr_data(self, asr_data: ASRData, num_segments: int) -> List[ASRData]:
        """
        长文本发送LLM前进行进行分割，根据ASR分段中的时间间隔，将ASRData拆分成多个部分。

        处理步骤：
        1. 计算总字数，并确定每个分段的字数范围。
        2. 确定平均分割点。
        3. 在分割点前后一定范围内，寻找时间间隔最大的点作为实际的分割点。

        Args:
            asr_data: ASR数据对象
            num_segments: 目标分段数

        Returns:
            ASR数据对象列表
        """
        SPLIT_RANGE = 30  # 在分割点前后寻找最大时间间隔的范围

        total_segs = len(asr_data.segments)
        total_word_count = count_words(asr_data.to_txt())
        words_per_segment = total_word_count // num_segments

        if num_segments <= 1 or total_segs <= num_segments:
            return [asr_data]

        # 计算每个分段的大致字数 根据每段字数计算分割点
        split_indices = [i * words_per_segment for i in range(1, num_segments)]

        # 调整分割点：在每个平均分割点附近寻找时间间隔最大的点
        adjusted_split_indices = []
        for split_point in split_indices:
            # 定义搜索范围
            start = max(0, split_point - SPLIT_RANGE)
            end = min(total_segs - 1, split_point + SPLIT_RANGE)

            # 在范围内找到时间间隔最大的点
            max_gap = -1
            best_index = split_point

            for j in range(start, end):
                gap = (
                    asr_data.segments[j + 1].start_time - asr_data.segments[j].end_time
                )
                if gap > max_gap:
                    max_gap = gap
                    best_index = j

            adjusted_split_indices.append(best_index)

        # 移除重复的分割点
        adjusted_split_indices = sorted(list(set(adjusted_split_indices)))

        # 根据调整后的分割点拆分ASRData
        segments = []
        prev_index = 0
        for index in adjusted_split_indices:
            part = ASRData(asr_data.segments[prev_index : index + 1])
            segments.append(part)
            prev_index = index + 1

        # 添加最后一部分
        if prev_index < total_segs:
            part = ASRData(asr_data.segments[prev_index:])
            segments.append(part)

        return segments

    def _process_segments(self, asr_data_list: List[ASRData]) -> List[List[ASRDataSeg]]:
        """并行处理所有分段"""
        futures = []
        for asr_data in asr_data_list:
            if not self.executor:
                raise ValueError("线程池未初始化")
            future = self.executor.submit(self._process_single_segment, asr_data)
            futures.append(future)

        processed_segments = []
        for future in as_completed(futures):
            if not self.is_running:
                logger.info("处理被中断，退出处理")
                break
            try:
                result = future.result()
                processed_segments.append(result)
            except Exception as e:
                logger.error(f"处理分段失败：{str(e)}")

        return processed_segments

    def _process_single_segment(self, asr_data_part: ASRData) -> List[ASRDataSeg]:
        """
        处理单个分段
        """
        if not asr_data_part.segments:
            return []
        for i in range(self.retry_times):
            try:
                return self._process_by_llm(asr_data_part.segments)
            except Exception as e:
                if i == self.retry_times - 1:
                    logger.warning(f"LLM处理失败，使用规则based方法进行分割: {str(e)}")
                    return self._process_by_rules(asr_data_part.segments)
                logger.warning(f"分割重试 {i+1}/{self.retry_times}: {str(e)}")
        return self._process_by_rules(asr_data_part.segments)  # 确保总是有返回值

    def _sentences_are_valid(self, source: str, sentences: List[str]) -> bool:
        if not self._sentences_preserve_source(source, sentences):
            return False
        for sentence in sentences:
            limit = (
                self.max_word_count_cjk
                if is_mainly_cjk(sentence)
                else self.max_word_count_english
            )
            if count_words(sentence) > limit:
                return False
        return True

    @staticmethod
    def _sentences_preserve_source(source: str, sentences: List[str]) -> bool:
        """Validate content independently from the display-length ceiling."""
        return bool(sentences) and _alignment_text("".join(sentences)) == _alignment_text(
            source
        )

    def _repair_semantic_sentences(self, source: str) -> List[str]:
        """Ask once more for semantic breaks when the first result breaks rules."""
        system_prompt = f"""
你是影视字幕语义断句校验器。只能在原文中插入<br>，不得修改、删除、增加或调换任何原文字符。
中文每段最多{self.max_word_count_cjk}个字，这只是硬上限，绝对不能按固定字数机械切割。
每段必须是自然、易读、可理解的完整语义片段。优先在分句、语法结构和说话气口处断开。
即使总字数没有超过上限，两个独立的问答、命令、转折、新主语或新时间话题也必须分开。
人名、称谓、金额、数量和固定词组不得从中间拆开。
请直接返回插入<br>后的原文，不要解释。
""".strip()
        if self.protected_terms:
            system_prompt += "\n以下标准人物姓名不得从中间拆开：" + "、".join(
                self.protected_terms
            )
        script_guidance = compact_script_guidance(self.script_reference)
        user_content = source
        if script_guidance:
            system_prompt += (
                "\n参考剧本只能用于识别说话轮次、标点和语义停顿；"
                "必须保留剧本中的句末、说话人换行及可独立分句边界；"
                "待断句原文仍是唯一内容来源，严禁从剧本增删或替换文字。"
            )
            user_content = (
                f"待断句原文：\n{source}\n\n"
                f"本集参考台词：\n{script_guidance}"
            )
        response = request_chat(self.client, request_stage='语义断句',
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt+"\n"+SPLIT_BOUNDARY},
                {"role": "user", "content": user_content},
            ],
            temperature=0.1,
            timeout=self.timeout,
        )
        result = response.choices[0].message.content or ""
        result = result.replace("\n", "")
        return [part.strip() for part in result.split("<br>") if part.strip()]

    def _process_by_llm(self, segments: List[ASRDataSeg]) -> List[ASRDataSeg]:
        """
        使用LLM进行分段处理

        处理步骤：
        1. 合并文本并构建提示词
        2. 检查缓存
        3. 调用API进行分段
        4. 处理响应结果
        5. 缓存结果
        6. 合并分段

        Args:
            segments: ASR数据分段列表

        Returns:
            处理后的分段列表

        Raises:
            ValueError: API返回结果为空时抛出
        """
        # 合并文本
        txt = "".join([seg.text for seg in segments])
        logger.debug(f"处理文本长度: {len(txt)}")

        # 构建提示词
        if self.split_type == "semantic":
            template = Template(SPLIT_PROMPT_SEMANTIC)
        elif self.split_type == "sentence":
            template = Template(SPLIT_PROMPT_SENTENCE)
        else:
            raise ValueError(f"无效的分段类型: {self.split_type}")

        system_prompt = template.safe_substitute(
            max_word_count_cjk=self.max_word_count_cjk,
            max_word_count_english=self.max_word_count_english,
        )

        user_prompt = (
            f"Please use multiple <br> tags to separate the following sentence:\n{txt}"
        )

        script_guidance = compact_script_guidance(self.script_reference)
        if script_guidance:
            user_prompt += (
                "\n\n本集参考台词如下。只参考其中的标点、说话轮次和语义停顿；"
                "必须逐字保留上面的待断句原文，不得从剧本增加、删除或替换文字。"
                "遇到连续重复称谓时，要按剧本标点区分，例如‘亲哥！哥，来’"
                "不能断成‘亲哥哥 / 来’。\n"
                f"<episode_script>{script_guidance}</episode_script>"
            )

        # 检查缓存
        cache_key = (
            f"{self.request_scope}_{SEMANTIC_SPLIT_CACHE_VERSION}_{len(system_prompt)}_{user_prompt}"
        )
        if self.protected_terms:
            system_prompt += "\n以下标准人物姓名不得从中间拆开：" + "、".join(
                self.protected_terms
            )
        param = {
            "temperature": self.temperature,
            "split_type": self.split_type,
        }
        if self.use_cache:
            cached_result = self.cache_manager.get_llm_result(
                prompt=cache_key,
                model_name=self.model,
                **param,
            )
            if cached_result:
                try:
                    logger.info(f"使用缓存数据进行分段，文本长度: {count_words(txt)}")
                    sentences = json.loads(cached_result)
                    if self._sentences_are_valid(txt, sentences):
                        sentences = apply_script_boundary_hints(
                            txt,
                            sentences,
                            self.script_reference,
                            replace_existing=True,
                        )
                        return self._merge_segments_based_on_sentences(
                            segments, sentences
                        )
                    logger.info("V16忽略不符合语义/字数规则的旧断句缓存")
                except json.JSONDecodeError as e:
                    logger.warning(f"缓存数据解析失败: {str(e)}")

        # 调用API
        logger.info(f"开始调用API进行分段，文本长度: {count_words(txt)}")
        response = request_chat(self.client, request_stage='分句细化',
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt+"\n"+SPLIT_BOUNDARY},
                {"role": "user", "content": user_prompt},
            ],
            temperature=self.temperature,
            timeout=self.timeout,
        )

        # 处理响应结果
        result = response.choices[0].message.content
        if result is None:
            raise ValueError("API返回的内容为空")

        result = result.replace("\n", "")  # 清理多余换行符
        sentences = [
            segment.strip() for segment in result.split("<br>") if segment.strip()
        ]

        # 验证结果
        if not sentences:
            raise ValueError("API返回的分段结果为空")

        if not self._sentences_are_valid(txt, sentences):
            logger.info("V16检测到断句超长或语义对齐异常，执行一次严格校正")
            repaired = self._repair_semantic_sentences(txt)
            if self._sentences_are_valid(txt, repaired):
                sentences = repaired
            elif self._sentences_preserve_source(txt, repaired):
                # Preserve any useful retry boundaries even when one clause is
                # still above the display ceiling. The timestamp-aware local
                # splitter will enforce the ceiling after script reconstruction.
                sentences = repaired
            elif not self._sentences_preserve_source(txt, sentences):
                # Never allow a model rewrite to alter the spoken source.
                sentences = [txt]
            logger.info("V16严格校正完成，将交由本地上限校验兜底")

        if self.script_reference and self._sentences_preserve_source(txt, sentences):
            script_aligned = apply_script_boundary_hints(
                txt,
                sentences,
                self.script_reference,
                replace_existing=True,
            )
            if script_aligned != sentences:
                logger.info("V25剧本边界重建：已清除模型机械切口并恢复说话轮次")
            sentences = script_aligned

        logger.info(f"API返回结果，句子数量: {len(sentences)}")

        # 缓存结果
        if self.use_cache and self._sentences_are_valid(txt, sentences):
            try:
                self.cache_manager.set_llm_result(
                    prompt=cache_key,
                    result=json.dumps(sentences, ensure_ascii=False),
                    model_name=self.model,
                    **param,
                )
            except Exception as e:
                logger.error(f"写入缓存失败: {str(e)}")

        # 合并分段
        return self._merge_segments_based_on_sentences(segments, sentences)

    def _enforce_sentence_limits(self, sentences: List[str]) -> List[str]:
        """Apply the existing deterministic ceiling after script boundary shifts."""
        guarded: List[str] = []
        for sentence in sentences:
            for clause in self._refine_short_semantic_clauses(sentence):
                limit = (
                    self.max_word_count_cjk
                    if is_mainly_cjk(clause)
                    else self.max_word_count_english
                )
                if count_words(clause) <= limit:
                    guarded.append(clause)
                else:
                    guarded.extend(
                        semantic_split_text(clause, limit, self.protected_terms)
                    )
        return guarded

    @staticmethod
    def _refine_short_semantic_clauses(sentence: str) -> List[str]:
        """Split grammatical clause changes even below the display ceiling."""
        cuts = set()
        for start in range(3, len(sentence) - 1):
            suffix = sentence[start:]
            if SubtitleSplitter._is_short_question_clause(suffix):
                cuts.add(start)
        for topic in TIME_TOPIC_WORDS:
            cursor = 0
            while (start := sentence.find(topic, cursor)) >= 0:
                subject_index = start + len(topic)
                has_subject = (
                    subject_index < len(sentence)
                    and sentence[subject_index] in "我你他她它咱您"
                )
                if start >= 3 and has_subject and len(sentence) - start >= 4:
                    cuts.add(start)
                cursor = start + len(topic)

        if not cuts:
            return [sentence]
        result = []
        start = 0
        for cut in sorted(cuts):
            if cut - start >= 2:
                result.append(sentence[start:cut])
                start = cut
        if start < len(sentence):
            result.append(sentence[start:])
        return result or [sentence]

    @staticmethod
    def _is_short_question_clause(text: str) -> bool:
        if not 2 <= len(text) <= 5 or text[-1] not in CLAUSE_FINAL_PARTICLES:
            return False
        return any(text.startswith(prefix) for prefix in CLAUSE_SUBJECT_PREFIXES)

    @staticmethod
    def _starts_with_time_topic_clause(text: str) -> bool:
        for topic in TIME_TOPIC_WORDS:
            if not text.startswith(topic):
                continue
            subject_index = len(topic)
            return (
                subject_index < len(text)
                and text[subject_index] in "我你他她它咱您"
            )
        return False

    def _script_marks_boundary(self, left: str, right: str) -> bool:
        """Return whether the episode script separates these exact phrases."""
        if not self.script_reference or not left or not right:
            return False
        script_text, boundary_list = script_chars_and_boundaries(
            self.script_reference
        )
        boundaries = set(boundary_list)

        left_clean = _alignment_text(left)
        right_clean = _alignment_text(right)
        joined = left_clean + right_clean
        cursor = 0
        while joined and (position := script_text.find(joined, cursor)) >= 0:
            if position + len(left_clean) in boundaries:
                return True
            cursor = position + 1

        # Exact lookup fails when actual speech adds a filler immediately
        # before a script/speaker boundary.  Ignore only that trailing filler
        # for lookup, so ``慢慢来啊 / 小心`` still preserves the boundary
        # written as ``慢慢来！ / 小心！`` in the script.
        trimmed_left = left_clean.rstrip("嗯呃啊呀哟哦诶唉哎嘛啦哈呵嘿嘻")
        if trimmed_left and trimmed_left != left_clean:
            trimmed_joined = trimmed_left + right_clean
            cursor = 0
            while trimmed_joined and (
                position := script_text.find(trimmed_joined, cursor)
            ) >= 0:
                if position + len(trimmed_left) in boundaries:
                    return True
                cursor = position + 1
        return False

    def _preserve_semantic_boundary(self, left: str, right: str) -> bool:
        if self._script_marks_boundary(left, right):
            return True
        if self._is_short_question_clause(right):
            return True
        if self._starts_with_time_topic_clause(right):
            return True
        stem = left.rstrip("吗呢吧啊呀嘛啦")
        return len(stem) >= 2 and right.startswith(stem)

    def _should_split_caption_gap(
        self,
        segments: List[ASRDataSeg],
        index: int,
        current_group: List[ASRDataSeg],
        time_gap: int,
    ) -> bool:
        """Treat 600ms as evidence, not permission to create one-char flashes."""
        if time_gap >= CAPTION_UNCONDITIONAL_GAP_MS:
            return True
        if time_gap <= CAPTION_MAX_INTERNAL_GAP_MS:
            return False
        left_count = count_words("".join(seg.text for seg in current_group))
        right_count = count_words(
            "".join(seg.text for seg in segments[index : index + 3])
        )
        return left_count >= 2 and right_count >= 2

    def _process_by_rules(self, segments: List[ASRDataSeg]) -> List[ASRDataSeg]:
        """
        使用规则进行基础的句子分割

        规则包括:
        1. 考虑时间间隔，超过阈值的进行分割
        2. 在常见连接词前后进行分割（保证分割后两个分段都大于5个单词）
        3. 分割大于 MAX_WORD_COUNT 个单词的分段

        Args:
            segments: ASR数据分段列表
        Returns:
            处理后的分段列表
        """
        logger.info(f"分段: {len(segments)}")
        # 1. 先按时间间隔分组
        segment_groups = self._group_by_time_gaps(
            segments, max_gap=500, check_large_gaps=True
        )
        logger.info(f"按时间间隔分组分组: {len(segment_groups)}")

        # 2. 按常用词分割, 只处理长句
        common_result_groups = []
        for group in segment_groups:
            max_word_count = (
                self.max_word_count_cjk
                if is_mainly_cjk("".join(seg.text for seg in group))
                else self.max_word_count_english
            )
            if count_words("".join(seg.text for seg in group)) > max_word_count:
                split_groups = self._split_by_common_words(group)
                common_result_groups.extend(split_groups)
            else:
                common_result_groups.append(group)

        # 3. 处理过长的分段，并合并group为seg
        result_segments = []
        for group in common_result_groups:
            result_segments.extend(self._split_long_segment(group))

        return result_segments

    def _group_by_time_gaps(
        self,
        segments: List[ASRDataSeg],
        max_gap: int = MAX_GAP,
        check_large_gaps: bool = False,
    ) -> List[List[ASRDataSeg]]:
        """
        检查字幕分段之间的时间间隔，如果超过阈值则分段

        Args:
            segments: 待检查的分段列表
            max_gap: 最大允许的时间间隔（ms）
            check_large_gaps: 是否检查连续的大时间间隔

        Returns:
            分段后的列表的列表
        """
        if not segments:
            return []

        result = []
        current_group = [segments[0]]
        recent_gaps = []  # 存储最近的时间间隔
        WINDOW_SIZE = 5  # 检查最近5个间隔

        for i in range(1, len(segments)):
            time_gap = segments[i].start_time - segments[i - 1].end_time

            if check_large_gaps:
                recent_gaps.append(time_gap)
                if len(recent_gaps) > WINDOW_SIZE:
                    recent_gaps.pop(0)
                if len(recent_gaps) == WINDOW_SIZE:
                    avg_gap = sum(recent_gaps) / len(recent_gaps)
                    # 如果当前间隔大于平均值的3倍
                    if time_gap > avg_gap * 3 and len(current_group) > 5:
                        # logger.debug(
                        #     f"检测到大间隔: {time_gap}ms, 平均间隔: {avg_gap}ms"
                        # )
                        result.append(current_group)
                        current_group = []
                        recent_gaps = []  # 重置间隔记录

            should_split = time_gap > max_gap
            if max_gap == CAPTION_MAX_INTERNAL_GAP_MS:
                should_split = self._should_split_caption_gap(
                    segments, i, current_group, time_gap
                )

            if should_split:
                if max_gap == CAPTION_MAX_INTERNAL_GAP_MS:
                    logger.info(
                        "V23通用边界分段：检测到 %sms 停顿，禁止合并前后台词",
                        time_gap,
                    )
                result.append(current_group)
                current_group = []
                recent_gaps = []  # 重置间隔记录

            current_group.append(segments[i])

        if current_group:
            result.append(current_group)

        return result

    def _split_by_common_words(
        self, segments: List[ASRDataSeg]
    ) -> List[List[ASRDataSeg]]:
        """
        在常见连接词前后进行分割，确保分割后的每个分段都至少有5个单词

        Args:
            segments: ASR数据分段列表
        Returns:
            分组后的分段列表的列表
        """
        # 定义在词语前面分割的常见词
        prefix_split_words = {
            # 英文连接词和介词
            "and",
            "or",
            "but",
            "if",
            "then",
            "because",
            "as",
            "until",
            "while",
            "what",
            "when",
            "where",
            "nor",
            "yet",
            "so",
            "for",
            "however",
            "moreover",
            # 中文连接词
            "和",
            "及",
            "与",
            "但",
            "而",
            "或",
            "因",
            # 中文代词
            "我",
            "你",
            "他",
            "她",
            "它",
            "咱",
            "您",
            "这",
            "那",
            "哪",
        }

        # 定义在词语后面分割的常见词
        suffix_split_words = {
            # 标点符号
            ".",
            ",",
            "!",
            "?",
            "。",
            "，",
            "！",
            "？",
            # 中文语气词和助词
            "的",
            "了",
            "着",
            "过",
            "吗",
            "呢",
            "吧",
            "啊",
            "呀",
            "嘛",
            "啦",
            # 英文所有格代词
            "mine",
            "yours",
            "hers",
            "its",
            "ours",
            "theirs",
            # 英文副词
            "either",
            "neither",
        }

        result = []
        current_group = []

        for i, seg in enumerate(segments):
            max_word_count = (
                self.max_word_count_cjk
                if is_mainly_cjk(seg.text)
                else self.max_word_count_english
            )

            # 如果当前词是前缀词且前面已经累积了足够多的词
            if any(
                seg.text.lower().startswith(word) for word in prefix_split_words
            ) and len(current_group) >= int(max_word_count * 0.6):
                result.append(current_group)
                logger.debug(f"在前缀词 {seg.text} 前分割")
                current_group = []

            # 如果前一个词是后缀词且当前组至少有一定数量的词
            if (
                i > 0
                and any(
                    segments[i - 1].text.lower().endswith(word)
                    for word in suffix_split_words
                )
                and len(current_group) >= int(max_word_count * 0.4)
            ):
                result.append(current_group)
                logger.debug(f"在后缀词 {segments[i-1].text} 后分割")
                current_group = []

            current_group.append(seg)

        # 处理最后剩余的分组
        if current_group:
            result.append(current_group)

        return result

    def _split_long_segment(self, segments: List[ASRDataSeg]) -> List[ASRDataSeg]:
        """
        基于最大时间间隔拆分长分段，根据文本类型使用不同的最大词数限制

        Args:
            segments: 要拆分的分段列表
        Returns:
            拆分后的分段列表
        """
        result_segs = []
        segments_to_process = [segments]  # 使用列表作为处理队列

        while segments_to_process:
            current_segments = segments_to_process.pop(0)

            # 添加空列表检查
            if not current_segments:
                continue

            merged_text = "".join(seg.text for seg in current_segments)
            max_word_count = (
                self.max_word_count_cjk
                if is_mainly_cjk(merged_text)
                else self.max_word_count_english
            )
            n = len(current_segments)

            # 基本情况：如果分段足够短或无法进一步拆分
            if count_words(merged_text) <= max_word_count or n < 4:
                merged_seg = ASRDataSeg(
                    merged_text.strip(),
                    current_segments[0].start_time,
                    current_segments[-1].end_time,
                )
                result_segs.append(merged_seg)
                continue

            # V12: choose a phrase/clause boundary.  The configured count is a
            # ceiling, not a fixed chopping width.  Pauses assist the decision,
            # while syntax cues prevent cuts such as "打电 / 话接受调解".
            candidates = []
            target = max(4.0, max_word_count * 0.72)
            for i in range(n - 1):
                left_text = "".join(seg.text for seg in current_segments[: i + 1])
                if count_words(left_text) > max_word_count:
                    break
                boundary_index = len(left_text)
                time_gap = max(
                    0,
                    current_segments[i + 1].start_time
                    - current_segments[i].end_time,
                )
                score = _boundary_score(merged_text, boundary_index, target)
                score += min(10.0, time_gap / 80.0)
                if _cuts_protected_term(
                    merged_text,
                    boundary_index,
                    self.protected_terms,
                    max_word_count,
                ):
                    continue
                if count_words(left_text) < 3:
                    score -= 6.0
                candidates.append((score, i))

            split_index = (
                max(candidates, key=lambda item: (item[0], item[1]))[1]
                if candidates
                else max(0, n // 2 - 1)
            )

            # 将分割后的两部分添加到处理队列中
            first_segs = current_segments[: split_index + 1]
            second_segs = current_segments[split_index + 1 :]

            segments_to_process.extend([first_segs, second_segs])

        # 按时间排序
        result_segs.sort(key=lambda seg: seg.start_time)
        return result_segs

    def _merge_processed_segments(
        self, processed_segments: List[List[ASRDataSeg]]
    ) -> List[ASRDataSeg]:
        """
        合并处理后的分段

        Args:
            processed_segments: 处理后的分段列表的列表

        Returns:
            最终的分段列表
        """
        final_segments = []
        for segments in processed_segments:
            final_segments.extend(segments)

        # 按时间排序
        final_segments.sort(key=lambda seg: seg.start_time)
        return final_segments

    def merge_short_segment(self, segments: List[ASRDataSeg]) -> None:
        """
        经过LLM断句后，继续优化字幕，合并词数少于等于5且时间相邻的段落。

        Args:
            segments: 字幕段落列表，将直接在此列表上进行修改
        """
        if not segments:  # 添加空列表检查
            return

        i = 0  # 从头开始遍历
        while i < len(segments) - 1:  # 修改遍历方式
            current_seg = segments[i]
            next_seg = segments[i + 1]

            # 判断是否需要合并:
            # 1. 时间间隔小于300ms
            # 2. 当前段落或下一段落词数小于 4
            # 3. 合并后总词数不超过限制
            time_gap = abs(next_seg.start_time - current_seg.end_time)
            current_words = count_words(current_seg.text)
            next_words = count_words(next_seg.text)
            total_words = current_words + next_words
            max_word_count = (
                self.max_word_count_cjk
                if is_mainly_cjk(current_seg.text)
                else self.max_word_count_english
            )
            preserve_boundary = self._preserve_semantic_boundary(
                current_seg.text, next_seg.text
            )

            if not preserve_boundary and ((
                time_gap < 200
                and (current_words < 5 or next_words < 5)
                and total_words <= max_word_count
            ) or (
                time_gap < 500
                and (current_words < 3 or next_words < 3)
                and total_words <= max_word_count
            )):
                # 执行合并操作
                logger.debug(
                    f"优化：合并相邻分段: {current_seg.text} --- {next_seg.text} -> {time_gap}"
                )

                # 更新当前段落的文本和结束时间
                current_seg.text = join_caption_fragments(
                    current_seg.text, next_seg.text
                )
                current_seg.end_time = next_seg.end_time

                # 从列表中移除下一个段落
                segments.pop(i + 1)
                # 不增加i，因为需要继续检查合并后的段落
            else:
                i += 1

    def _merge_segments_based_on_sentences(
        self, segments: List[ASRDataSeg], sentences: List[str], max_unmatched: int = 5
    ) -> List[ASRDataSeg]:
        """
        基于提供的句子列表合并ASR分段

        Args:
            segments: ASR数据分段列表
            sentences: 句子列表
            max_unmatched: 允许的最大未匹配句子数量

        Returns:
            List[ASRDataSeg]: 合并后的分段列表

        Raises:
            ValueError: 当未匹配句子数量超过阈值时抛出
        """

        def preprocess_text(s: str) -> str:
            """通过转换为小写并规范化空格来标准化文本"""
            return " ".join(s.lower().split())

        asr_texts = [seg.text for seg in segments]
        asr_len = len(asr_texts)
        asr_index = 0  # 当前分段索引位置
        threshold = 0.5  # 相似度阈值
        max_shift = 30  # 滑动窗口的最大偏移量
        unmatched_count = 0  # 未匹配句子计数

        new_segments = []

        for sentence in sentences:
            logger.debug(f"==========")
            logger.debug(f"处理句子: {sentence}")
            logger.debug("后续句子:" + "".join(asr_texts[asr_index : asr_index + 10]))

            sentence_proc = preprocess_text(sentence)
            word_count = count_words(sentence_proc)
            best_ratio = 0.0
            best_pos = None
            best_window_size = 0

            # 滑动窗口大小，优先考虑接近句子词数的窗口
            max_window_size = min(word_count * 2, asr_len - asr_index)
            min_window_size = max(1, word_count // 2)
            window_sizes = sorted(
                range(min_window_size, max_window_size + 1),
                key=lambda x: abs(x - word_count),
            )

            # 对每个窗口大小尝试匹配
            for window_size in window_sizes:
                max_start = min(asr_index + max_shift + 1, asr_len - window_size + 1)
                for start in range(asr_index, max_start):
                    substr = "".join(asr_texts[start : start + window_size])
                    substr_proc = preprocess_text(substr)
                    ratio = difflib.SequenceMatcher(
                        None, sentence_proc, substr_proc
                    ).ratio()

                    if ratio > best_ratio:
                        best_ratio = ratio
                        best_pos = start
                        best_window_size = window_size
                    if ratio == 1.0:
                        break
                if best_ratio == 1.0:
                    break

            # 处理匹配结果
            if best_ratio >= threshold and best_pos is not None:
                start_seg_index = best_pos
                end_seg_index = best_pos + best_window_size - 1

                segs_to_merge = segments[start_seg_index : end_seg_index + 1]

                # 按照时间切分避免合并跨度大的
                seg_groups = self._group_by_time_gaps(
                    segs_to_merge, max_gap=CAPTION_MAX_INTERNAL_GAP_MS
                )

                for group in seg_groups:
                    merged_text = "".join(seg.text for seg in group)
                    merged_start_time = group[0].start_time
                    merged_end_time = group[-1].end_time
                    merged_seg = ASRDataSeg(
                        merged_text, merged_start_time, merged_end_time
                    )

                    logger.debug(f"合并分段: {merged_seg.text}")

                    # 考虑最大词数的拆分
                    split_segs = self._split_long_segment(group)
                    new_segments.extend(split_segs)
                max_shift = 30
                asr_index = end_seg_index + 1  # 移动到下一个未处理的分段
            else:
                logger.warning(f"无法匹配句子: {sentence}")
                unmatched_count += 1
                if unmatched_count > max_unmatched:
                    raise ValueError(
                        f"未匹配句子数量超过阈值 {max_unmatched}，处理终止"
                    )
                max_shift = 100
                asr_index = min(asr_index + 1, asr_len - 1)

        return new_segments

    def stop(self):
        """停止分割器"""
        if not self.is_running:
            return

        logger.info("正在停止分割器...")
        self.is_running = False
        if hasattr(self, "executor") and self.executor is not None:
            try:
                self.executor.shutdown(wait=False, cancel_futures=True)
            except Exception as e:
                logger.error(f"关闭线程池时出错：{str(e)}")
            finally:
                self.executor = None
