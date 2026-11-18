import re
from pathlib import Path
from typing import Dict, Optional


def english_safe_width(ass_content, play_res_x):
    """Opt-in customer safe area; unmarked legacy templates stay unchanged."""
    match = re.search(r'^; VideoCaptionerEnglishSafeWidth:\s*(0\.\d+)\s*$', ass_content, re.MULTILINE)
    fraction = float(match.group(1)) if match else 0.90
    return int(play_res_x * max(0.20, min(0.90, fraction)))


def is_mainly_cjk(text: str) -> bool:
    """
    判断文本是否主要由中日韩文字组成
    """
    # 定义CJK字符的Unicode范围
    cjk_patterns = [
        r"[\u4e00-\u9fff]",  # 中日韩统一表意文字
        r"[\u3040-\u309f]",  # 平假名
        r"[\u30a0-\u30ff]",  # 片假名
        r"[\uac00-\ud7af]",  # 韩文音节
    ]
    cjk_count = 0
    for pattern in cjk_patterns:
        cjk_count += len(re.findall(pattern, text))
    total_chars = len("".join(text.split()))
    return cjk_count / total_chars > 0.4 if total_chars > 0 else False


def parse_ass_info(ass_content: str) -> tuple[int, Dict[str, int]]:
    """
    从ASS文件内容中解析视频宽度和各样式的字体大小

    Returns:
        tuple: (视频宽度, {样式名: 字体大小})
    """
    # 获取视频宽度
    play_res_x = 1280  # 默认宽度
    font_sizes = {"Default": 40}  # 默认字体大小

    # 查找视频宽度
    res_x_match = re.search(r"PlayResX:\s*(\d+)", ass_content)
    if res_x_match:
        play_res_x = int(res_x_match.group(1))

    # 查找所有样式的字体大小
    style_section = re.search(r"\[V4\+ Styles\].*?\[", ass_content, re.DOTALL)
    if style_section:
        style_content = style_section.group(0)

        # 获取Format行定义的字段顺序
        format_match = re.search(r"Format:(.*?)$", style_content, re.MULTILINE)
        if format_match:
            # 解析字段名称
            fields = [f.strip() for f in format_match.group(1).split(",")]
            # 找到Fontsize字段的位置
            try:
                fontsize_index = fields.index("Fontsize")
                name_index = fields.index("Name")

                # 使用正确的字段位置来匹配样式行
                for style_line in re.finditer(
                    r"Style:(.*?)$", style_content, re.MULTILINE
                ):
                    style_parts = [p.strip() for p in style_line.group(1).split(",")]
                    if len(style_parts) >= max(fontsize_index + 1, name_index + 1):
                        style_name = style_parts[name_index]
                        font_size = float(style_parts[fontsize_index])
                        font_sizes[style_name] = font_size
            except ValueError:
                pass

    return play_res_x, font_sizes


def estimate_text_width(text: str, font_size: int) -> int:
    """
    估算文本宽度（像素）

    Args:
        text: 文本内容
        font_size: 字体大小

    Returns:
        int: 估算的文本宽度（像素）
    """
    # CJK字符通常是方形，宽度约等于字体大小
    # 英文字符宽度约为字体大小的一半
    width = 0
    for char in text:
        if re.match(r"[\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff\uac00-\ud7af]", char):
            width += font_size
        else:
            width += font_size * 0.5
    return int(width)


def auto_wrap_text(text: str, max_width: int, font_size: int, line_words: int = 6, semantic: bool = False, no_squeeze: bool = False) -> str:
    """
    自动为文本添加换行符

    Args:
        text: 原始文本
        max_width: 最大宽度（像素）
        font_size: 字体大小

    Returns:
        str: 处理后的文本
    """
    if not text:
        return text

    # 如果不是主要由CJK字符组成，则不处理
    if not is_mainly_cjk(re.sub(r"\{[^}]*\}", "", text)):
        return wrap_english_two_lines(text, max_width, font_size, line_words, semantic, no_squeeze)
    if "\\N" in text:
        return text

    # 分割文本为字符列表
    chars = list(text)
    current_line = ""
    result = []

    for char in chars:
        temp_line = current_line + char
        # 计算当前行宽度
        line_width = estimate_text_width(temp_line, font_size)

        if line_width > max_width:
            result.append(current_line)
            current_line = char
        else:
            current_line = temp_line

    if current_line:
        result.append(current_line)

    return "\\N".join(result)


def wrap_english_two_lines(text, max_width, font_size, line_words=6, semantic=False, no_squeeze=False):
    """Keep existing ASS effect tags and wrap long Latin text at word boundaries."""
    prefix = ""
    line_words = max(3, min(12, int(line_words)))
    while text.startswith("{") and "}" in text:
        end = text.index("}") + 1
        prefix += text[:end]
        text = text[end:]
    if not re.search(r"[A-Za-z\u0900-\u097f\u0600-\u06ff]", text) or re.search(r"\{[^}]*\}", text):
        return prefix + text
    if re.search(r'[\u0600-\u06ff]', text):
        # Keep logical Unicode order. FriBidi/HarfBuzz resolve Arabic directly;
        # injected embedding controls can become visible boxes in other paths.
        plain = re.sub(r'[\u202a-\u202e\u2066-\u2069]', '', text).replace(r'\N', ' ')
        lines, current = [], []
        for word in plain.split():
            candidate = ' '.join(current + [word])
            if current and (len(current) >= line_words or estimate_text_width(candidate, font_size)*1.35 > max_width):
                lines.append(' '.join(current))
                current = []
            current.append(word)
        if current:
            lines.append(' '.join(current))
        prefix = re.sub(r'\\fsc[xy][-+]?\d+(?:\.\d+)?', '', prefix).replace('{}', '')
        return r'{\fe-1}' + re.sub(r'\\fe-?\d+', '', prefix).replace('{}', '') + r'\N'.join(lines)
    existing_lines = text.replace("\r\n", "\\N").replace("\n", "\\N").split(r"\N")
    if no_squeeze:
        prefix = re.sub(r'\\fsc[xy][-+]?\d+(?:\.\d+)?', '', prefix)
        prefix = prefix.replace('{}', '')
        plain = ' '.join(existing_lines)
        balanced = wrap_english_two_lines(plain, max_width, font_size, line_words, semantic, False)
        if r'\fscx' not in balanced:
            return prefix + balanced
        # Old imported cues cannot be time-split by a renderer. Wrap them into
        # more physical lines instead; the processing guard normally pages
        # fresh cues to <=2 lines before they reach this fallback.
        lines, current = [], []
        for word in plain.split():
            candidate = ' '.join(current + [word])
            if current and (len(current) >= line_words or estimate_text_width(candidate, font_size)*1.35 > max_width):
                lines.append(' '.join(current))
                current = []
            current.append(word)
        if current:
            lines.append(' '.join(current))
        return prefix + r'\N'.join(lines)
    if len(existing_lines) == 2 and re.search(r"\\fscx\d", prefix):
        return prefix + r"\N".join(existing_lines)
    # Use a conservative estimate for Latin capitals, wide letters and stroke.
    def latin_width(value):
        return estimate_text_width(value, font_size) * 1.35
    if len(existing_lines) == 2 and all(len(line.split()) <= line_words and latin_width(line) <= max_width for line in existing_lines):
        return prefix + r"\N".join(existing_lines)
    text = " ".join(existing_lines)
    words = text.split()
    width = latin_width(text)
    if width <= max_width and len(words) <= line_words:
        return prefix + text
    if len(words) < 2:
        # A URL or unbroken token cannot be split safely.
        scale = max(1, min(100, int(max_width / max(width, 1) * 95)))
        return prefix + f"{{\\fscx{scale}}}" + text
    candidates = []
    for index in range(1, len(words)):
        if len(words) <= line_words * 2 and (index > line_words or len(words) - index > line_words):
            continue
        left, right = " ".join(words[:index]), " ".join(words[index:])
        a, b = latin_width(left), latin_width(right)
        # Prefer a balanced pair; punctuation wins when lengths are similar.
        score = max(a, b) + abs(a - b) * 0.15
        if left.endswith((",", ";", ":", ".", "?", "!")):
            score -= font_size * 0.4
        if semantic:
            # Opt-in only. Prefer grammatical boundaries over visual balance;
            # this is not a speaker detector or a replacement for LLM splitting.
            last = words[index - 1].lower().strip('.,?!:;"')
            first = words[index].lower().strip('.,?!:;"')
            attached = {'a', 'an', 'the', 'my', 'your', 'his', 'her', 'our', 'their',
                        'for', 'of', 'with', 'without', 'to', 'from', 'into', 'onto',
                        'at', 'by', 'and', 'or', 'but'}
            compounds = {('bone', 'marrow'), ('ice', 'cream'), ('new', 'york'),
                         ('a', 'lot'), ('as', 'well'), ('each', 'other')}
            if last in attached or (last, first) in compounds:
                score += font_size * 12
            if left.endswith(('.', '?', '!')):
                score -= font_size * 6
        candidates.append((score, left, right, max(a, b)))
    _, left, right, widest = min(candidates)
    fit = ""
    if widest > max_width:
        scale = max(1, min(100, int(max_width / widest * 95)))
        fit = f"{{\\fscx{scale}}}"
    return prefix + fit + left + r"\N" + right


def auto_wrap_ass_file(
    input_file: str,
    output_file: str = None,
    video_width: Optional[int] = None,
    video_height: Optional[int] = None,
):
    """
    处理ASS文件，为文本添加自动换行

    Args:
        input_file: 输入ASS文件路径
        output_file: 输出ASS文件路径，如果为None则覆盖输入文件
        video_width: 视频宽度，如果提供则覆盖ASS文件中的设置
        video_height: 视频高度，如果提供则覆盖ASS文件中的设置
    """
    if output_file is None:
        output_file = input_file

    # 读取ASS文件
    with open(input_file, "r", encoding="utf-8") as f:
        ass_content = f.read()

    # Delivery-policy ASS has already been paginated and positioned by ASRData.
    # Rewrapping individual fill/outline/shadow events breaks layer alignment.
    from app.core.utils.delivery_policy import read_policy
    if read_policy(ass_content) is not None:
        if output_file != input_file:
            with open(output_file, "w", encoding="utf-8") as f:
                f.write(ass_content)
        return output_file

    # 解析字体大小（在修改分辨率之前）
    play_res_x, font_sizes = parse_ass_info(ass_content)

    # 如果没有提供视频宽度，使用ASS文件中的宽度
    if video_width is None:
        video_width = play_res_x

    # 计算最大文本宽度（考虑边距）
    # Font sizes and text positions live in ASS coordinates, not output pixels.
    max_text_width = english_safe_width(ass_content, play_res_x)
    limit_match = re.search(r'^; VideoCaptionerEnglishLineWords:\s*(\d+)', ass_content, re.MULTILINE)
    line_words = int(limit_match.group(1)) if limit_match else 6

    # 处理对话行
    def process_dialogue_line(match):
        full_line = match.group(0)

        # 提取样式名
        style_pattern = r"Dialogue:[^,]*,[^,]*,[^,]*,([^,]*),"
        style_match = re.search(style_pattern, full_line)
        style_name = style_match.group(1).strip() if style_match else "Default"

        # 获取对应样式的字体大小
        font_size = font_sizes.get(style_name, font_sizes["Default"])

        # 获取文本内容
        text_part = match.group(1)

        # V12 keeps the primary CJK subtitle on one physical line.  Length is
        # already guarded by the semantic splitter; inserting an explicit \N
        # here would undo that guarantee.  Secondary/non-CJK styles retain the
        # original wrapping behaviour for bilingual and other workflows.
        plain_text = re.sub(r"\{[^}]*\}", "", text_part)
        if style_name == "Default" and is_mainly_cjk(plain_text):
            wrapped_text = text_part.replace("\\N", "")
        else:
            wrapped_text = auto_wrap_text(text_part, max_text_width, font_size, line_words,
                '; VideoCaptionerEnglishSemanticWrap: 1' in ass_content,
                '; VideoCaptionerEnglishNoSqueeze: 1' in ass_content or
                '; VideoCaptionerOriginalLanguage: hi' in ass_content or
                '; VideoCaptionerOriginalLanguage: ar' in ass_content)

        # 替换原文本
        return full_line.replace(text_part, wrapped_text)

    # 使用正则表达式匹配并处理对话行
    pattern = r"Dialogue:[^,]*(?:,[^,]*){8},(.*?)$"
    processed_content = re.sub(
        pattern, process_dialogue_line, ass_content, flags=re.MULTILINE
    )

    # 保存处理后的文件
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(processed_content)

    return output_file
