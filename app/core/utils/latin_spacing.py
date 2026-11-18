"""Restore missing Latin punctuation gaps without changing spoken characters.

URLs, mail, dotted abbreviations and common domain/file tokens are opaque.
No punctuation is invented or removed; non-Latin language runs stay untouched.
"""
import re

_OPAQUE = re.compile(
    r'https?://[^\s<>]+|www\.[^\s<>]+|'
    r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|'
    r'\b(?:[A-Za-z]\.){2,}|'
    r'\b[\w-]+(?:\.[\w-]+)*\.(?:com|org|net|edu|gov|io|cn|co|uk|ai|'
    r'txt|docx|pdf|json|mp4|srt|ass|exe|py)\b|'
    r'\{[^}]*\}', re.I)
_GAP = re.compile(r'''([A-Za-z][,.!?;:]+["”’)]?)(?=[A-Za-z])''')


def normalize(text):
    """Idempotent formatting only; preserve existing whitespace and newlines."""
    if not text or not re.search(r'[A-Za-z]', text):
        return text
    # Never import English typography into Chinese, Arabic or Hindi cues.
    if re.search(r'[\u3400-\u9fff\u0600-\u06ff\u0900-\u097f]', text):
        return text
    result=[];last=0
    for match in _OPAQUE.finditer(text):
        result.append(_GAP.sub(r'\1 ', text[last:match.start()]))
        result.append(match.group());last=match.end()
    result.append(_GAP.sub(r'\1 ', text[last:]))
    return ''.join(result)


def join(left, right, *, digits=False):
    """Keep the established word boundary rule and repair punctuation joins."""
    end=r'[A-Za-z0-9]$' if digits else r'[A-Za-z]$'
    separator=' ' if re.search(end,left) and re.match(r'^[A-Za-z]',right) else ''
    return normalize(left+separator+right)
