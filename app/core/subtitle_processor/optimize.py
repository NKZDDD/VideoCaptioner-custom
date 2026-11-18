from app.core.llm_permissions import TEXT_BOUNDARY,SPLIT_BOUNDARY,request_scope,sanitize_corrections,safe_text
import json
import difflib
import logging
import os
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple, Union

import retry
from app.core.utils.llm_transport import request_chat
from openai import OpenAI

from app.config import CACHE_PATH
from app.core.bk_asr.asr_data import ASRData, ASRDataSeg
from app.core.storage.cache_manager import CacheManager
from app.core.utils import json_repair
from app.core.subtitle_processor.alignment import SubtitleAligner
from app.core.subtitle_processor.prompt import OPTIMIZER_PROMPT
from app.core.utils.logger import setup_logger

logger = setup_logger("subtitle_optimizer")

ENTITY_CONTEXT_VERSION = "context-v36-english-correction"
CONTEXT_MARGIN = 4
DELETABLE_FILLER_CHARS = set("嗯呃啊呀哟哦诶唉哎嘛啦哈呵嘿嘻")
PERSPECTIVE_PRONOUNS = set("我你您他她它咱")
THIRD_PERSON_PRONOUNS = set("他她它")
CONTENT_SOFTENING_REPLACEMENTS = {
    "要死要活": "闹个不停",
    "我要杀了你": "我要收拾你",
    "闹自杀": "闹着想不开",
    "杀掉": "解决掉",
    "杀了": "收拾了",
    "杀人": "害人",
    "自杀": "想不开",
    "闭嘴": "别说了",
    "滚滚滚": "走开走开",
    "滚蛋": "走开",
    "滚开": "走开",
    "不要脸": "不讲理",
    "下贱": "过分",
    "恶心": "讨厌",
    "愚蠢": "糊涂",
    "没脑子的蠢货": "不太机灵的可爱",
    "死鸭子嘴硬": "嘴硬",
    "害死": "害了",
    "死得": "走得",
    "饿死": "饿着",
    "气死": "气坏",
    "吓死": "吓坏",
    "笑死": "笑坏",
    "累死": "累坏",
    "烦死": "烦透",
    "急死": "急坏",
    "痛死": "痛坏",
    "找死": "自找麻烦",
    "想死": "想不开",
    "死死": "紧紧",
    "生死": "安危",
    "玩死": "玩赢",
    "打死": "打服",
    "弄死": "收拾",
    "杀死": "击败",
    "去死": "走开",
    "该死": "讨厌",
    "死定了": "完了",
    "死了": "倒了",
    "没头脑": "小迷糊",
    "没脑子": "不机灵",
    "畜生": "人",
    "禽兽": "人",
    "王八蛋": "可爱",
    "狗东西": "可爱",
    "乡巴佬": "可爱",
    "神经病": "可爱",
    "丑八怪": "可爱",
    "下等人": "可爱",
    "拜金女": "可爱",
    "臭女人": "可爱",
    "臭男人": "可爱",
    "讨厌鬼": "可爱",
    "土鳖": "可爱",
    "村姑": "可爱",
    "穷鬼": "可爱",
    "废物": "可爱",
    "蠢货": "可爱",
    "傻子": "可爱",
    "脑残": "可爱",
    "混蛋": "可爱",
    "贱人": "可爱",
    "婊子": "可爱",
    "狐狸精": "可爱",
    "绿茶": "可爱",
    "泼妇": "可爱",
    "野种": "可爱",
    "残废": "可爱",
    "肥猪": "可爱",
    "小三": "可爱",
    "坏种": "可爱",
    "白痴": "可爱",
    "智障": "可爱",
    "弱智": "可爱",
    "垃圾": "可爱",
    "疯婆子": "可爱",
    "疯子": "可爱",
    "废柴": "可爱",
    "蠢": "糊涂",
}
PLATFORM_FRIENDLY_GUIDANCE = """
【平台友好用词规则】
- 保持人物、动作对象和句子其余内容不变，只柔化暴力或辱骂部分。
- 死鸭子嘴硬→嘴硬；饿死→饿着；气死/吓死/笑死/累死→气坏/吓坏/笑坏/累坏；找死→自找麻烦；想死→想不开。
- 玩死→玩赢；打死→打服；弄死→收拾；杀死→击败；去死→走开；该死→讨厌。
- 要死要活→闹个不停；自杀→想不开；我要杀了你→我要收拾你；闭嘴→别说了；愚蠢/蠢→糊涂；滚/滚开/滚蛋→走开。
- 畜生、禽兽→人；没头脑→小迷糊；没脑子→不机灵。
- 土鳖、村姑、乡巴佬、穷鬼、废物、蠢货、傻子、脑残、神经病、混蛋、王八蛋、贱人、婊子、狐狸精、绿茶、泼妇、野种、残废、肥猪、丑八怪、小三、狗东西、坏种、下等人、拜金女、白痴、智障、弱智、垃圾、疯子、疯婆子、臭女人、臭男人、废柴及同类针对人物的恶意称呼→可爱。
- 禁止为了柔化词语而改变我/你/他/她等指代，也不得删除句中其他信息。
""".strip()
COMMON_SURNAME_HOMOPHONES = {
    "傅": ("付", "富"),
    "周": ("舟", "洲"),
    "程": ("成", "城"),
    "魏": ("卫", "位"),
    "吴": ("无", "吾"),
    "江": ("姜",),
}


def _normalize_script_match_text(text: str) -> str:
    """Normalize dialogue for retrieval without changing the displayed script."""
    text = re.sub(r"（[^）]*）|\([^)]*\)|【[^】]*】|\[[^]]*\]", "", text or "")
    # Speaker labels help readers but should not dominate dialogue matching.
    text = re.sub(r"^[\u3400-\u9fffA-Za-z0-9·]{1,12}\s*[:：]", "", text)
    return re.sub(r"[^\w\u3400-\u9fff]", "", text).lower()


def select_script_excerpt(
    script_reference: str,
    subtitle_chunk: Dict[str, str],
    context_lines: int = 2,
) -> Tuple[str, float]:
    """Retrieve one contiguous local script window for the current subtitle block.

    The complete script excerpt is never sent to the correction model.  This
    keeps similarly worded scenes elsewhere in the drama from contaminating a
    local correction request.
    """
    lines = [line.strip() for line in (script_reference or "").splitlines() if line.strip()]
    target = _normalize_script_match_text("".join(subtitle_chunk.values()))
    if not lines or not target:
        return "", 0.0
    if len(lines) <= 6:
        return "\n".join(lines), 1.0

    normalized_lines = [_normalize_script_match_text(line) for line in lines]
    target_length = max(len(target), 1)
    minimum_length = max(12, int(target_length * 0.55))
    maximum_length = max(minimum_length, int(target_length * 1.55))
    target_chars = set(target)
    best_score = 0.0
    best_range = (0, 0)

    for start in range(len(lines)):
        candidate = ""
        for end in range(start, len(lines)):
            candidate += normalized_lines[end]
            if len(candidate) < minimum_length:
                continue
            if len(candidate) > maximum_length:
                break
            ratio = difflib.SequenceMatcher(
                None, target, candidate, autojunk=False
            ).ratio()
            overlap = len(target_chars & set(candidate)) / max(len(target_chars), 1)
            score = ratio * 0.82 + overlap * 0.18
            if score > best_score:
                best_score = score
                best_range = (start, end + 1)

    # A weak match is more dangerous than no script reference at all.
    if best_score < 0.24:
        return "", best_score
    start = max(0, best_range[0] - context_lines)
    end = min(len(lines), best_range[1] + context_lines)
    return "\n".join(lines[start:end]), best_score


def _content_only(text: str) -> str:
    """Ignore formatting while retaining every spoken letter or CJK unit."""
    return re.sub(r"[^\w\u3400-\u9fff]", "", text or "")


def _is_allowed_deletion(source: str, start: int, end: int) -> bool:
    """Allow only configured filler sounds or an immediate spoken stutter."""
    deleted = source[start:end]
    if not deleted:
        return True
    if all(char in DELETABLE_FILLER_CHARS for char in deleted):
        return True

    # A duplicated stutter such as “你你怎么管” may lose one “你”.
    left = source[:start]
    right = source[end:]
    return left.endswith(deleted) or right.startswith(deleted)


def preserves_spoken_content(original: str, optimized: str) -> tuple[bool, str]:
    """Reject correction results that silently drop meaningful source units.

    Homophone replacements are intentionally allowed. Only pure deletions are
    guarded here because names and ordinary words often need character-level
    replacement while their spoken content remains present.
    """
    if is_english_caption(original):
        return english_entry_guard(original, optimized)
    source = _content_only(original)
    target = _content_only(optimized)
    matcher = difflib.SequenceMatcher(None, source, target, autojunk=False)
    deleted_parts = []
    for tag, source_start, source_end, _, _ in matcher.get_opcodes():
        if tag == "delete" and not _is_allowed_deletion(
            source, source_start, source_end
        ):
            deleted_parts.append(source[source_start:source_end])
    return not deleted_parts, "".join(deleted_parts)


def preserves_entry_anchor(original: str, optimized: str) -> tuple[bool, str]:
    """Forbid the correction model from moving speech between time anchors.

    Script reconciliation has already handled insertions/deletions before this
    optimizer runs.  At this stage an entry may correct equal-length spelling
    (for example 司令成→傅凛成), but it may not gain or lose spoken units.  This
    prevents a trailing character such as ``好`` or a repeated ``哥`` from
    being transferred to a neighbouring subtitle with a different timestamp.
    Punctuation remains free because it is excluded by ``_content_only``.
    """
    if is_english_caption(original):
        return english_entry_guard(original, optimized)
    source = _content_only(original)
    target = _content_only(optimized)
    if len(source) == len(target):
        return True, ""
    return False, f"{len(source)}→{len(target)}"


def is_english_caption(text):
    return bool(re.search(r"[A-Za-z]", text)) and not re.search(r"[\u3400-\u9fff]", text)


def english_entry_guard(original, optimized):
    """Allow local spelling changes without adding or moving spoken words."""
    def words(text):
        return re.findall(r"[a-z]+(?:['’][a-z]+)*|\d+(?:[.,]\d+)*", text.lower())
    source, target = words(original), words(optimized)
    if not is_english_caption(optimized) or len(source) != len(target):
        return False, "English word count changed"
    pronouns = {'i', 'you', 'he', 'she', 'it', 'we', 'they', 'me', 'him', 'her', 'us', 'them'}
    for a, b in zip(source, target):
        if a == b:
            continue
        if a in pronouns or b in pronouns or re.search(r"\d", a + b):
            return False, "English pronoun or number changed"
        if difflib.SequenceMatcher(None, a, b, autojunk=False).ratio() < 0.5:
            return False, "Uncertain English word replacement"
    return True, ""


def normalize_english_i(text):
    # Sentence starts and names require context; only standalone I is unambiguous.
    return re.sub(r"(?<![A-Za-z0-9_])i(?=(?:['’](?:m|ve|ll|d)\b)|\b)", "I", text)


def repair_perspective_pronouns(
    original: str, optimized: str, lock_gender: bool = False
) -> tuple[str, bool]:
    """Block first/second/third-person swaps introduced by the correction LLM.

    他/她/它 may still be corrected among themselves when explicit gender
    context is available. A switch such as 他→你 or 我→他 changes who did the
    action and must never be guessed by the optimizer.
    """
    if original == optimized:
        return optimized, False
    matcher = difflib.SequenceMatcher(None, original, optimized, autojunk=False)
    output = []
    changed = False
    for tag, source_start, source_end, target_start, target_end in matcher.get_opcodes():
        source_part = original[source_start:source_end]
        target_part = optimized[target_start:target_end]
        if tag == "equal":
            output.append(target_part)
            continue
        if tag == "replace" and len(source_part) == len(target_part):
            repaired = []
            for source_char, target_char in zip(source_part, target_part):
                is_pronoun_change = (
                    source_char != target_char
                    and (
                        source_char in PERSPECTIVE_PRONOUNS
                        or target_char in PERSPECTIVE_PRONOUNS
                    )
                )
                gender_only = (
                    source_char in THIRD_PERSON_PRONOUNS
                    and target_char in THIRD_PERSON_PRONOUNS
                )
                if is_pronoun_change and (lock_gender or not gender_only):
                    repaired.append(source_char)
                    changed = True
                else:
                    repaired.append(target_char)
            output.append("".join(repaired))
            continue
        if any(char in PERSPECTIVE_PRONOUNS for char in source_part + target_part):
            return original, True
        if tag in ("replace", "insert"):
            output.append(target_part)
    return "".join(output), changed


def apply_content_softening(text: str, custom_prompt: str = "") -> str:
    """Apply deterministic platform-friendly wording after LLM correction."""
    from app.core.utils.cn_general_policy import enabled, soften
    if enabled(custom_prompt):
        return soften(text)
    softened = text
    for source, replacement in sorted(
        CONTENT_SOFTENING_REPLACEMENTS.items(), key=lambda item: len(item[0]), reverse=True
    ):
        softened = softened.replace(source, replacement)
    # Do not globally replace a single character (``死→活`` produced phrases
    # such as ``饿活``).  A stand-alone insult is safe to soften, while normal
    # words such as ``翻滚`` and any unlisted context remain linguistically
    # intact for the model rather than being corrupted mechanically.
    if re.fullmatch(r"\s*滚+\s*", softened):
        return "走开" if softened.strip() == "滚" else "走开走开"
    if re.fullmatch(r"\s*死\s*", softened):
        return "没命"
    return softened


def restore_readable_stutter(text: str) -> str:
    """Keep a spoken pronoun stutter readable after punctuation-free ASR."""
    return re.sub(
        r"^([我你他她它您咱])\1(?=[\u3400-\u9fff])",
        lambda match: f"{match.group(1)}…{match.group(1)}",
        text or "",
    )


CHINESE_DIGITS = "零一二三四五六七八九"
CHINESE_NUMERAL_CHARS = set(CHINESE_DIGITS + "〇十百千万亿两")
NATURAL_NUMBER_SUFFIXES = set("十百千万亿元块岁集次个人名号楼层天月日点分秒")


def _four_digit_chinese(number: int, omit_leading_one: bool = True) -> str:
    digits = f"{number:04d}"
    units = ("千", "百", "十", "")
    output = []
    pending_zero = False
    for index, digit_text in enumerate(digits):
        digit = int(digit_text)
        if digit == 0:
            if output and any(value != "0" for value in digits[index + 1 :]):
                pending_zero = True
            continue
        if pending_zero:
            output.append("零")
            pending_zero = False
        output.append(CHINESE_DIGITS[digit] + units[index])
    result = "".join(output)
    if omit_leading_one and result.startswith("一十"):
        result = result[1:]
    return result or "零"


def _integer_to_chinese(number: int) -> str:
    if number == 0:
        return "零"
    sections = []
    while number:
        sections.append(number % 10000)
        number //= 10000
    section_units = ("", "万", "亿", "万亿")
    output = []
    pending_zero = False
    for index in range(len(sections) - 1, -1, -1):
        section = sections[index]
        if section == 0:
            if output:
                pending_zero = True
            continue
        if output and (pending_zero or section < 1000):
            output.append("零")
        output.append(_four_digit_chinese(section, omit_leading_one=not output) + section_units[index])
        pending_zero = False
    return "".join(output)


def _digit_sequence(raw: str, zero_char: str = "零") -> str:
    return "".join(zero_char if char == "0" else CHINESE_DIGITS[int(char)] for char in raw)


def normalize_arabic_numbers(text: str) -> str:
    """Remove Arabic numerals from display text with dialogue-friendly forms."""
    if not text or not re.search(r"\d", text):
        return text
    text = text.translate(str.maketrans('０１２３４５６７８９', '0123456789'))

    # ASR sometimes emits spoken digits as ``3 6 万``. Treat them as 36万.
    text = re.sub(
        r"(?<!\d)\d(?:\s+\d)+(?!\d)",
        lambda match: re.sub(r"\s+", "", match.group(0)),
        text,
    )

    def replace_number(match: re.Match) -> str:
        raw = match.group(0)
        integer_part, dot, decimal_part = raw.replace(',', '').partition(".")
        next_char = match.string[match.end() : match.end() + 1]
        before = match.string[max(0, match.start() - 40):match.start()]
        after = match.string[match.end():match.end() + 16]
        # Quantity is the default, including a cue ending at the number.
        # A digit count or missing measure word is NOT evidence of a code.
        explicit_code = bool(re.search(
            r'(?:电话号码|手机号|电话|手机号码|座机|传真|验证码|校验码|密码|口令|PIN|'
            r'号码|编号|工号|学号|身份证(?:号)?|银行卡(?:号)?|卡号|账号|帐号|'
            r'订单号|流水号|车牌号|代码|代号|快递单号|航班号|房号|房间号|病房号|'
            r'门牌号|门禁码|邮编|邮政编码|股票代码|型号|版本号|拨打|拨通|拨号)'
            r'\s*(?:分别是|就是|是|为|叫|[:：=])?\s*$', before, re.I))
        code_suffix = bool(re.match(r'(?:号房|号病房|号房间|号车厢|号航班|室|房间|病房|航班|次列车)', after))
        education_code = integer_part in ('985', '211') and bool(re.search(r'大学|高校|院校|毕业|学校|工程', before + after))
        emergency_code = integer_part in ('110', '119', '120', '122') and bool(re.search(r'(?:打|拨|呼叫)\s*$', before))
        letter_code = bool(re.search(r'[A-Za-z]$', before))
        calendar_year = len(integer_part) == 4 and next_char == '年' and (1000 <= int(integer_part) <= 2999 or bool(re.search(r'公元\s*$', before))) and not bool(re.search(r'(?:活了|过了|等了|长达|经历了|持续了|已有)\s*$', before))
        if not dot and (explicit_code or (code_suffix and not before.endswith('第')) or education_code or emergency_code or letter_code):
            converted = _digit_sequence(integer_part)
        elif calendar_year and not dot:
            converted = _digit_sequence(integer_part, zero_char="〇")
        elif len(integer_part) > 1 and integer_part.startswith("0"):
            converted = _digit_sequence(integer_part)
        elif len(integer_part) > 16:
            # Preserve long identifiers without overflowing the supported units.
            converted = _digit_sequence(integer_part)
        else:
            converted = _integer_to_chinese(int(integer_part))
        if dot:
            converted += "点" + _digit_sequence(decimal_part)
        return converted

    text = re.sub(r"[0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?", replace_number, text)
    numeral_class = "".join(sorted(CHINESE_NUMERAL_CHARS))
    return re.sub(
        rf"(?<=[{numeral_class}])\s+(?=[{numeral_class}])", "", text
    )


def apply_final_text_policy(asr_data: ASRData, custom_prompt: str = "") -> tuple[ASRData, Dict[str, int]]:
    """Apply non-optional display rules immediately before length enforcement."""
    report = {"number_lines": 0, "softened_lines": 0}
    from app.core.utils.cn_general_policy import enabled as chinese_general_enabled
    force_chinese_numbers = chinese_general_enabled(custom_prompt)
    for segment in asr_data.segments:
        original = segment.text
        if is_english_caption(original) and not force_chinese_numbers:
            segment.text = normalize_english_i(original)
            continue
        softened = apply_content_softening(original, custom_prompt)
        normalized = normalize_arabic_numbers(softened)
        if softened != original:
            report["softened_lines"] += 1
        if normalized != softened:
            report["number_lines"] += 1
        segment.text = restore_readable_stutter(normalized)

        translated = getattr(segment, "translated_text", "")
        if translated and re.search(r"[\u3400-\u9fff]", translated):
            translated_softened = apply_content_softening(translated, custom_prompt)
            segment.translated_text = restore_readable_stutter(
                normalize_arabic_numbers(translated_softened)
            )
    return asr_data, report


def extract_story_terms(custom_prompt: str) -> List[str]:
    """Extract the user's canonical cast list without changing their prompt."""
    if not custom_prompt:
        return []
    match = re.search(
        r"(?:标准人物姓名|人物姓名|角色姓名)\s*[:：]\s*([^\n。]+)",
        custom_prompt,
    )
    if not match:
        return []
    terms = []
    for raw_term in re.split(r"[、，,;/；]", match.group(1)):
        term = raw_term.strip().strip("“”'\"")
        if 2 <= len(term) <= 8 and re.fullmatch(r"[\u3400-\u9fff]+", term):
            terms.append(term)
    return list(dict.fromkeys(terms))


def extract_character_genders(custom_prompt: str) -> Dict[str, str]:
    """Extract explicit gender groups while leaving unknown roles untouched."""
    if not custom_prompt:
        return {}

    genders: Dict[str, str] = {}
    for label, gender in (("女角色为", "女"), ("男角色为", "男")):
        match = re.search(rf"{label}\s*([^；。\n]+)", custom_prompt)
        if not match:
            continue
        for raw_term in re.split(r"[、，,;/；]", match.group(1)):
            term = re.sub(r"（.*?）|\(.*?\)", "", raw_term).strip()
            if 2 <= len(term) <= 8 and re.fullmatch(r"[\u3400-\u9fff]+", term):
                genders[term] = gender

    # Alias explicitly used by the current project.
    if genders.get("傅子川") == "男" and "川川" in extract_story_terms(custom_prompt):
        genders["川川"] = "男"
    return genders


def derive_family_terms(story_terms: List[str]) -> List[str]:
    """Derive family entities only when the cast provides strong evidence."""
    likely_people = [
        term
        for term in story_terms
        if 2 <= len(term) <= 4
        and not term.endswith(("助理", "经理", "小哥", "母", "父"))
    ]
    surname_counts = Counter(term[0] for term in likely_people)
    explicitly_related = {
        term[0] for term in story_terms if term.endswith(("母", "父"))
    }
    surnames = {
        surname
        for surname, count in surname_counts.items()
        if count >= 2 or surname in explicitly_related
    }
    return [f"{surname}家" for surname in sorted(surnames)]


def build_contextual_homophone_guidance(custom_prompt: str) -> str:
    """Build a project-specific glossary for context-aware sound corrections."""
    story_terms = extract_story_terms(custom_prompt)
    if not story_terms:
        return ""
    family_terms = derive_family_terms(story_terms)
    character_genders = extract_character_genders(custom_prompt)
    lines = [
        "【剧情实体动态词表】",
        "标准人物/称呼：" + "、".join(story_terms),
    ]
    if family_terms:
        lines.append("由人物关系推导的家族实体：" + "、".join(family_terms))
        alias_notes = []
        for family in family_terms:
            surname = family[0]
            aliases = COMMON_SURNAME_HOMOPHONES.get(surname, ())
            if aliases:
                alias_notes.append(
                    f"若{'/'.join(alias + '家' for alias in aliases)}在上下文中指向{family}，应校正为{family}"
                )
        if alias_notes:
            lines.append("；".join(alias_notes) + "。")
    if character_genders:
        female_roles = [name for name, gender in character_genders.items() if gender == "女"]
        male_roles = [name for name, gender in character_genders.items() if gender == "男"]
        if female_roles:
            lines.append("明确女角色：" + "、".join(female_roles))
        if male_roles:
            lines.append("明确男角色：" + "、".join(male_roles))
    lines.extend(
        [
            "校正原则：结合人物关系、动作对象和前后台词判断姓名错误，标准姓名必须与词表完全一致。",
            "即使错误文本与标准姓名表面差异较大，只要该位置明确是在称呼或指代人物，且剧情上下文只能指向一个标准角色，也应校正为该角色；上下文不唯一时不得猜测。",
            "代词原则：只有指代对象明确时，才按角色性别校正他/她；不得仅凭声音、姓名印象或最近出现的人名推断。",
            "禁止盲目替换：如果词语确实是普通含义（例如“出身富家”表示富裕家庭），必须保留原词。",
        ]
    )
    return "\n".join(lines)


class SubtitleOptimizer:
    """字幕优化器,支持缓存功能"""

    def __init__(
        self,
        thread_num: int = 5,
        batch_num: int = 10,
        model: str = "gpt-4o-mini",
        custom_prompt: str = "",
        script_reference: str = "",
        script_authoritative: bool = False,
        temperature: float = 0.7,
        timeout: int = 60,
        retry_times: int = 1,
        update_callback: Optional[Callable] = None,
        base_url=None, api_key=None, execution_boundary=None,
    ):
        self._task_base_url=base_url;self._task_api_key=api_key
        self._init_client()
        self.thread_num = thread_num
        self.batch_num = batch_num
        self.model = model
        self.custom_prompt = custom_prompt
        self.execution_boundary=execution_boundary or TEXT_BOUNDARY
        self.script_reference = script_reference
        self.script_authoritative = script_authoritative
        self.temperature = temperature
        self.timeout = timeout
        self.retry_times = retry_times
        self.is_running = True
        self.update_callback = update_callback
        self._init_thread_pool()
        self.cache_manager = CacheManager(str(CACHE_PATH))

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

    def optimize_subtitle(self, subtitle_data: Union[str, ASRData]) -> ASRData:
        """优化字幕文件"""
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

            # 多线程优化
            optimized_dict = self._parallel_optimize(chunks)

            # 创建新的ASRDataSeg列表
            new_segments = self._create_segments(asr_data.segments, optimized_dict)

            return ASRData(new_segments)
        except Exception as e:
            logger.error(f"优化失败：{str(e)}")
            raise RuntimeError(f"优化失败：{str(e)}")

    def _split_chunks(
        self, subtitle_dict: Dict[str, str]
    ) -> List[Tuple[Dict[str, str], Set[str]]]:
        """Split into unique target blocks with read-only neighbouring context."""
        items = list(subtitle_dict.items())
        chunks: List[Tuple[Dict[str, str], Set[str]]] = []
        for core_start in range(0, len(items), self.batch_num):
            core_end = min(len(items), core_start + self.batch_num)
            context_start = max(0, core_start - CONTEXT_MARGIN)
            context_end = min(len(items), core_end + CONTEXT_MARGIN)
            request_chunk = dict(items[context_start:context_end])
            target_keys = {key for key, _ in items[core_start:core_end]}
            chunks.append((request_chunk, target_keys))
        return chunks

    def _parallel_optimize(
        self, chunks: List[Tuple[Dict[str, str], Set[str]]]
    ) -> Dict[str, str]:
        """并行优化所有块"""
        futures = {}
        optimized_dict = {}
        failed_chunks=[]

        for request_chunk, target_keys in chunks:
            if not self.executor:
                raise ValueError("线程池未初始化")
            future = self.executor.submit(self._safe_optimize_chunk, request_chunk)
            futures[future] = (request_chunk, target_keys)

        for future in as_completed(futures):
            request_chunk, target_keys = futures[future]
            if not self.is_running:
                logger.info("优化器已停止运行，退出优化")
                break
            try:
                result = future.result()
                for key in target_keys:
                    original_text = request_chunk[key]
                    candidate = result.get(key, original_text)
                    anchor_ok, anchor_detail = preserves_entry_anchor(
                        original_text, candidate
                    )
                    if not anchor_ok:
                        logger.warning(
                            "V27时间锚点锁：字幕 %s 的有效字数发生变化（%s），拒绝跨条搬字",
                            key,
                            anchor_detail,
                        )
                        candidate = original_text
                    content_ok, deleted = preserves_spoken_content(
                        original_text, candidate
                    )
                    if not content_ok:
                        logger.warning(
                            "V16内容防丢：字幕 %s 的校正结果删除了有效内容“%s”，保留原文",
                            key,
                            deleted,
                        )
                        candidate = original_text
                    candidate, pronoun_repaired = repair_perspective_pronouns(
                        original_text,
                        candidate,
                        lock_gender=self.script_authoritative,
                    )
                    if pronoun_repaired:
                        logger.warning(
                            "V20代词防错：字幕 %s 检测到已确定的指代被改写，已恢复原指代",
                            key,
                        )
                    softened = apply_content_softening(candidate, self.custom_prompt)
                    if softened != candidate:
                        logger.info("V17友好用词：字幕 %s 已执行本地替换", key)
                    candidate = softened
                    optimized_dict[key] = candidate
                if self.update_callback:
                    self.update_callback({key:optimized_dict[key] for key in target_keys})
            except Exception as e:
                logger.error(f"优化块失败：{str(e)}")
                failed_chunks.append(list(target_keys))
                # Restore only this block's unique target entries.  The former
                # implementation accidentally restored whichever loop chunk
                # happened to be last when a future failed.
                for key in target_keys:
                    optimized_dict[key] = request_chunk[key]

        if failed_chunks:raise RuntimeError(f"字幕校正有{len(failed_chunks)}个批次未完成，保留输入文件；请重试，不会当作校正成功")
        return optimized_dict

    def _safe_optimize_chunk(self, chunk: Dict[str, str]) -> Dict[str, str]:
        """安全的优化块，包含重试逻辑"""
        for i in range(self.retry_times):
            try:
                return self._optimize_chunk(chunk)
            except Exception as e:
                if i == self.retry_times - 1:
                    raise
                logger.warning(f"优化重试 {i+1}/{self.retry_times}: {str(e)}")
        return chunk

    def _optimize_chunk(self, subtitle_chunk: Dict[str, str]) -> Dict[str, str]:
        """优化字幕块"""
        logger.info(
            f"[+]正在优化字幕：{next(iter(subtitle_chunk))} - {next(reversed(subtitle_chunk))}"
        )
        user_prompt = f"Correct the following subtitles. Keep the original language, do not translate:\n<input_subtitle>{str(subtitle_chunk)}</input_subtitle>"
        if self.custom_prompt:
            user_prompt += (
                f"\nReference content:\n<prompt>{self.custom_prompt}</prompt>"
            )
            contextual_guidance = build_contextual_homophone_guidance(
                self.custom_prompt
            )
            if contextual_guidance:
                user_prompt += (
                    "\nContext-aware homophone glossary:\n"
                    f"<context_glossary>{contextual_guidance}</context_glossary>"
                )

        from app.core.utils.cn_general_policy import enabled, GUIDANCE
        softening_guidance = GUIDANCE if enabled(self.custom_prompt) else PLATFORM_FRIENDLY_GUIDANCE
        user_prompt += (
            "\nMandatory platform-friendly wording policy:\n"
            f"<softening_policy>{softening_guidance}</softening_policy>"
        )

        if self.script_reference:
            excerpt, match_score = select_script_excerpt(
                self.script_reference, subtitle_chunk
            )
            if excerpt:
                user_prompt += (
                    "\nMatched script dialogue for this local subtitle block "
                    f"(retrieval score {match_score:.2f}):\n"
                    f"<matched_script>{excerpt}</matched_script>\n"
                    "Script correction rules: when the spoken line and matched "
                    "script clearly correspond, treat the script spelling as "
                    "a reference for names, Chinese pronouns, numbers and "
                    "homophones. The input may already have deterministic script "
                    "corrections: never change a script-confirmed name, number or "
                    "他/她/它 back to a phonetic ASR guess. Keep the actual subtitle "
                    "entry count and timing. Your job is limited to uncertain "
                    "typos, natural phrasing, punctuation and semantic line breaks; "
                    "the program, not the model, owns script-aligned entities. "
                    "When both the ASR subtitle and the matched script use the same "
                    "perspective pronoun (for example 他), never change it to 我 or 你. "
                    "Do not insert an unspoken script line, and do not force a "
                    "replacement when the local match is ambiguous."
                )

        # 检查缓存
        cache_params = {
            "temperature": self.temperature,
            "model": self.model,
        }
        # 构建缓存key
        cache_key = (
            f"{self.request_scope}_{self.execution_boundary}_{ENTITY_CONTEXT_VERSION}_{len(OPTIMIZER_PROMPT)}_{user_prompt}"
        )
        cache_result = self.cache_manager.get_llm_result(
            cache_key, self.model, **cache_params
        )

        if cache_result:
            try:
                cached = json.loads(cache_result)
                if not isinstance(cached, dict) or set(cached) != set(subtitle_chunk) or any(not safe_text(cached[k], subtitle_chunk[k]) for k in cached):
                    raise ValueError("缓存编号或文字无效")
                logger.info("使用缓存的优化结果")
                return self._repair_subtitle(subtitle_chunk, cached)
            except (ValueError, TypeError, KeyError):
                logger.warning("忽略无效的校正缓存，重新请求")

        # 构建提示词
        messages = [
            {"role": "system", "content": OPTIMIZER_PROMPT+"\n"+self.execution_boundary},
            {
                "role": "user",
                "content": user_prompt,
            },
        ]

        # 调用API优化
        response = request_chat(self.client, request_stage='字幕校正',
            model=self.model,
            messages=messages,  # type: ignore
            temperature=self.temperature,
            timeout=self.timeout,
        )

        # 解析结果
        result: Dict[str, str] = json_repair.loads(response.choices[0].message.content)  # type: ignore

        if not isinstance(result,dict) or set(result)!=set(subtitle_chunk) or any(not safe_text(result[k],subtitle_chunk[k]) for k in result):
            raise ValueError("LLM校正返回无效编号、空文字或排版指令")

        # 修复字幕对齐
        aligned_result = self._repair_subtitle(subtitle_chunk, result)

        # 保存到缓存
        try:
            self.cache_manager.set_llm_result(
                cache_key,
                json.dumps(aligned_result, ensure_ascii=False),
                self.model,
                **cache_params,
            )
        except Exception as exc:
            logger.warning("校正结果有效，但缓存写入失败：%s", type(exc).__name__)


        return aligned_result

    @staticmethod
    def _repair_subtitle(
        original: Dict[str, str], optimized: Dict[str, str]
    ) -> Dict[str, str]:
        """Keep every model result attached to its original subtitle ID.

        The former global text aligner could move the last character of entry
        N into entry N+1 even when the model returned the correct number of
        keys.  That silently changed timing.  Missing or invalid keys now fall
        back to the original entry instead of redistributing text globally.
        """
        return sanitize_corrections(original,optimized)

    @staticmethod
    def _create_segments(
        original_segments: List[ASRDataSeg],
        optimized_dict: Dict[str, str],
    ) -> List[ASRDataSeg]:
        """创建新的字幕段"""
        return [
            ASRDataSeg(
                text=optimized_dict.get(str(i), seg.text),
                start_time=seg.start_time,
                end_time=seg.end_time,
            )
            for i, seg in enumerate(original_segments, 1)
        ]

    def stop(self):
        """停止优化器"""
        if not self.is_running:
            return

        logger.info("正在停止优化器...")
        self.is_running = False
        if hasattr(self, "executor") and self.executor is not None:
            try:
                self.executor.shutdown(wait=False, cancel_futures=True)
            except Exception as e:
                logger.error(f"关闭线程池时出错：{str(e)}")
            finally:
                self.executor = None
