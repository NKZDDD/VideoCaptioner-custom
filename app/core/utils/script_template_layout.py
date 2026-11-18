"""Apply CJK template limits after script correction without guessed timing."""
import copy
import re
from difflib import SequenceMatcher
from app.core.subtitle_processor.split import semantic_split_text
from app.core.utils.script_word_timing import normalized


def enforce_script_template_layout(data, original, config, protected_terms=None):
    report = {'timed_splits': 0, 'same_time_wraps': 0, 'review': []}
    if not config.need_split:
        return data, report
    limit = max(1, int(config.max_word_count_cjk))
    output = []
    for cue in data.segments:
        if not re.search(r'[\u3400-\u9fff]', cue.text):
            output.append(cue)
            continue
        chunks = semantic_split_text(cue.text, limit, protected_terms=protected_terms)
        if len(chunks) <= 1:
            output.append(cue)
            continue
        spans = [s for s in original if s.start_time >= cue.start_time and s.end_time <= cue.end_time
                 and s.end_time > s.start_time and normalized(s.text)]
        source = ''.join(normalized(s.text) for s in spans)
        target = ''.join(normalized(chunk) for chunk in chunks)
        anchors, position = {}, 0
        for span in spans:
            length = len(normalized(span.text))
            anchors.setdefault(position, {})['start'] = span.start_time
            anchors.setdefault(position + length, {})['end'] = span.end_time
            position += length
        # Only real source segment edges are eligible. An ASR sentence edge is
        # not evidence for a word boundary somewhere inside that sentence.
        blocks = SequenceMatcher(None, source, target, autojunk=False).get_matching_blocks()
        mapped = {}
        for index, edge in anchors.items():
            for block in blocks:
                if block.size and block.a <= index <= block.a + block.size:
                    dest = block.b + index - block.a
                    mapped.setdefault(dest, {}).update(edge)
        pieces, offset, valid = [], 0, True
        for index, chunk in enumerate(chunks):
            finish = offset + len(normalized(chunk))
            start = cue.start_time if index == 0 else mapped.get(offset, {}).get('start')
            end = cue.end_time if index == len(chunks)-1 else mapped.get(finish, {}).get('end')
            if start is None or end is None or end <= start or (pieces and start < pieces[-1].end_time):
                valid = False
                break
            part = copy.deepcopy(cue)
            part.text, part.start_time, part.end_time = chunk, start, end
            pieces.append(part)
            offset = finish
        if valid:
            output.extend(pieces)
            report['timed_splits'] += len(pieces)-1
        else:
            # Missing native timing cannot justify proportional time slicing.
            # Apply the visual length limit on the original cue instead.
            part = copy.deepcopy(cue)
            part.text = '\n'.join(chunks)
            output.append(part)
            report['same_time_wraps'] += 1
            report['review'].append(f'{cue.start_time}ms—{cue.end_time}ms：缺少可靠词边界，保留原时间换行：{cue.text}')
    data.segments = output
    return data, report
