"""Group native Hindi word timestamps without extending cues into pauses."""
import copy
import re


def group_hindi_words(asr_data):
    # The legacy detector only recognises ASCII words and <=2 code points;
    # Devanagari words contain combining marks and must be counted by spaces.
    if (not asr_data.segments or sum(len(seg.text.split()) == 1 for seg in asr_data.segments)
            / len(asr_data.segments) < 0.8):
        return asr_data, {'word_timing': False}
    groups, current = [], []
    for word in asr_data.segments:
        if current:
            gap = word.start_time - current[-1].end_time
            finished = re.search(r'[।!?]$', current[-1].text.strip())
            if gap >= 300 or finished:
                groups.append(current)
                current = []
        current.append(word)
    if current:
        groups.append(current)
    result = []
    for group in groups:
        cue = copy.deepcopy(group[0])
        cue.text = ' '.join(word.text.strip() for word in group).strip()
        cue.start_time = group[0].start_time
        cue.end_time = group[-1].end_time
        if cue.text and cue.end_time > cue.start_time:
            result.append(cue)
    asr_data.segments = result
    return asr_data, {'word_timing': True, 'cues': len(result), 'gap_ms': 300}
