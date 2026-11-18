import re

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
