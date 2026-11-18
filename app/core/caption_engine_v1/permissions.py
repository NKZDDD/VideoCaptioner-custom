import re

def safe_text(value,source=''):
    if not isinstance(value,str) or (source.strip() and not value.strip()):return False
    patterns=(r'\{\\[^}]*\}',r'\\[Nnh]',r'</?(?:font|span|style|script|div|br|p)\b[^>]*>')
    if any(re.search(pattern,value,re.I) for pattern in patterns):return False
    if ('\n' in value or '\r' in value) and not ('\n' in source or '\r' in source):return False
    return True
