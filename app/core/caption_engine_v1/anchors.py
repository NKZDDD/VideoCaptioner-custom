import re,unicodedata

def normalized(text):
    return ''.join(c for c in unicodedata.normalize('NFKC', text).casefold() if c.isalnum())

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
