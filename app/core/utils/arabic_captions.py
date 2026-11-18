"""Arabic source captions: retain logical Unicode order and native word times."""
import copy
import re


def group_arabic_words(data):
    if not data.segments or sum(len(seg.text.split()) == 1 for seg in data.segments)/len(data.segments) < 0.8:
        return data, {'word_timing': False, 'language': 'ar'}
    groups, current = [], []
    for word in data.segments:
        if current and (word.start_time-current[-1].end_time >= 400
                        or re.search(r'[.!?؟]$', current[-1].text.strip())):
            groups.append(current)
            current = []
        current.append(word)
    if current:
        groups.append(current)
    captions = []
    for group in groups:
        cue = copy.deepcopy(group[0])
        cue.text = ' '.join(word.text.strip() for word in group).strip()
        cue.end_time = group[-1].end_time
        if cue.text and cue.end_time > cue.start_time:
            captions.append(cue)
    data.segments = captions
    return data, {'word_timing': True, 'language': 'ar', 'cues': len(captions)}
