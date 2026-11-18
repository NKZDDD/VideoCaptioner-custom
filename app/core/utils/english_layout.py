"""Opt-in English cue pagination, using original ASR word anchors when matched."""
import copy
import difflib
import re
from app.core.utils.ass_auto_wrap import parse_ass_info, english_safe_width, auto_wrap_text


def paginate_english(asr_data, style, original_segments, native_only=False):
    if '; VideoCaptionerEnglishNoSqueeze: 1' not in style:
        return asr_data, {}
    width, sizes = parse_ass_info(style + '\n[Events]\n')
    max_width = english_safe_width(style, width)
    size = sizes.get('Default', 40)
    limit_match = re.search(r'VideoCaptionerEnglishLineWords:\s*(\d+)', style)
    limit = int(limit_match.group(1)) if limit_match else 7
    original_words, original_times = [], []
    for segment in original_segments:
        words = segment.text.split()
        for index, word in enumerate(words):
            duration = segment.end_time - segment.start_time
            original_words.append(word)
            original_times.append((segment.start_time + duration*index/len(words),
                                   segment.start_time + duration*(index+1)/len(words)))
    final_words = [word for seg in asr_data.segments for word in seg.text.replace(r'\N', ' ').split()]
    def norm(word):
        return re.sub(r'[^a-z0-9]', '', word.lower())
    matcher = difflib.SequenceMatcher(None, list(map(norm, original_words)), list(map(norm, final_words)), autojunk=False)
    anchors = {}
    for block in matcher.get_matching_blocks():
        for offset in range(block.size):
            anchors[block.b+offset] = original_times[block.a+offset]
    if native_only:
        # Validated voice-assist spelling substitutions keep token count and
        # order. Preserve WHOLE native word intervals for small equal-count
        # substitutions, instead of losing the boundary for e.g. metal/medal.
        for kind,a,b,c,d in matcher.get_opcodes():
            if kind == 'replace' and b-a == d-c and 0 < b-a <= 3:
                for offset in range(d-c):
                    anchors[c+offset] = original_times[a+offset]
        # Join native subwords (e.g. sister / -in / -law) by character identity,
        # but every character retains its WHOLE native word interval. No time
        # is interpolated within a word or a sentence.
        original_chars = ''.join(map(norm, original_words))
        final_chars = ''.join(map(norm, final_words))
        if original_chars == final_chars:
            char_times = [stamp for word,stamp in zip(original_words,original_times) for _ in norm(word)]
            offset = 0
            for i, word in enumerate(final_words):
                length = len(norm(word))
                if length:
                    anchors[i] = (char_times[offset][0],char_times[offset+length-1][1])
                offset += length
    output, cursor, split_count, fallback, span_fallback, rebalanced = [], 0, 0, 0, 0, 0
    abbreviations = {'mr.', 'mrs.', 'ms.', 'dr.', 'prof.', 'sr.', 'jr.', 'st.', 'vs.', 'etc.'}
    for segment in asr_data.segments:
        words = segment.text.replace(r'\N', ' ').split()
        if not words or not re.search('[A-Za-z]', segment.text) or re.search('[\u3400-\u9fff\u0900-\u097f]', segment.text):
            output.append(segment)
            cursor += len(words)
            continue
        # Sentence boundaries must survive layout, irrespective of cue length.
        boundaries = [0]
        for index, word in enumerate(words[:-1], 1):
            token = word.rstrip('\"\'”’')
            if (re.search(r'[.!?]$', token) and token.lower() not in abbreviations
                    and not re.fullmatch(r'(?:[A-Za-z]\.){2,}', token)):
                boundaries.append(index)
        boundaries.append(len(words))
        chunks = []
        for begin, end in zip(boundaries, boundaries[1:]):
            pos = begin
            while pos < end:
                best = pos + 1
                # Find the largest consecutive span that fits two normal-size
                # lines. Never delete words or stretch/compress letter widths.
                for stop in range(pos+1, min(end, pos + limit*2) + 1):
                    wrapped = auto_wrap_text(' '.join(words[pos:stop]), max_width, size, limit, True)
                    if not re.search(r'\\fscx', wrapped) and wrapped.count(r'\N') <= 1:
                        best = stop
                if best < end:
                    # Prefer a clause boundary near the fit limit. Do not end
                    # a cue on a determiner/preposition when another cut fits.
                    attached = {'a','an','the','to','of','for','with','without','my','your','his','her','their','and','or','but'}
                    while best > pos+1 and words[best-1].lower().strip('.,!?') in attached:
                        best -= 1
                chunks.append((pos, best))
                pos = best
        if native_only and any((begin > 0 and cursor+begin not in anchors)
                               or (end < len(words) and cursor+end-1 not in anchors)
                               for begin,end in chunks):
            # Missing internal word boundaries: retain the whole source interval
            # and let the renderer wrap, rather than invent proportional times.
            chunks = [(0,len(words))]
            span_fallback += 1
        if native_only and len(chunks)>1:
            def timing(a,b):
                if cursor+a not in anchors or cursor+b-1 not in anchors:
                    return None
                return anchors[cursor+b-1][1]-anchors[cursor+a][0]
            def fits(a,b):
                wrapped=auto_wrap_text(' '.join(words[a:b]),max_width,size,limit,True,True)
                return wrapped.count(r'\N')<=1 and r'\fscx' not in wrapped
            def score(a,b,c):
                left,right=timing(a,b),timing(b,c)
                if left is None or right is None:return None
                return (max(0,600-left)+max(0,600-right),abs(left-right))
            for i in range(len(chunks)-1):
                a,b=chunks[i];_,c=chunks[i+1]
                if b in boundaries[1:-1]:continue
                old=score(a,b,c)
                if old is None or old[0]==0:continue
                candidates=[]
                for cut in range(a+1,c):
                    value=score(a,cut,c)
                    if value is not None and fits(a,cut) and fits(cut,c):
                        candidates.append((value,cut))
                if candidates:
                    value,cut=min(candidates)
                    if value<old:
                        chunks[i]=(a,cut);chunks[i+1]=(cut,c);rebalanced+=1
        for begin, end in chunks:
            cue = copy.deepcopy(segment)
            cue.text = ' '.join(words[begin:end])
            duration = segment.end_time - segment.start_time
            start_fallback = segment.start_time if native_only else segment.start_time+duration*begin/len(words)
            end_fallback = segment.end_time if native_only else segment.start_time+duration*end/len(words)
            start = anchors.get(cursor+begin, (start_fallback, 0))[0]
            finish = anchors.get(cursor+end-1, (0, end_fallback))[1]
            if cursor+begin not in anchors or cursor+end-1 not in anchors:
                if not native_only:
                    fallback += 1
            cue.start_time = int(max(segment.start_time, min(start, segment.end_time-1)))
            cue.end_time = int(max(cue.start_time+1, min(finish, segment.end_time)))
            if output and cue.start_time < output[-1].end_time:
                cue.start_time = output[-1].end_time
                cue.end_time = max(cue.start_time+1, cue.end_time)
            output.append(cue)
        split_count += max(0, len(chunks)-1)
        cursor += len(words)
    asr_data.segments = output
    return asr_data, {'extra_cues': split_count, 'fallback_timing_cues': fallback,
                      'source_span_fallback_cues':span_fallback,'rebalanced_pages':rebalanced}
