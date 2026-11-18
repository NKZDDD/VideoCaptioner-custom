"""One-line Arabic captions, cut only at native ASR word boundaries."""
import copy
import difflib
import re
import unicodedata
from app.core.utils.ass_auto_wrap import parse_ass_info, english_safe_width, estimate_text_width


def plain_text(text):
    return re.sub(r'[\u202a-\u202e\u2066-\u2069\ufeff]', '', text).replace(r'\N', ' ').replace('\n', ' ').strip()


def normalized(word):
    word = unicodedata.normalize('NFKC', word)
    word = word.translate(str.maketrans('أإآى', 'اااي'))
    return ''.join(c for c in word if unicodedata.category(c)[0] in ('L', 'N'))


def word_alignment(script, heard):
    """Monotonic edit alignment; unsupported words receive no time anchor."""
    a, b = list(map(normalized, script)), list(map(normalized, heard))
    n, m = len(a), len(b)
    costs = [[0.] * (m+1) for _ in range(n+1)]
    steps = {}
    for i in range(1,n+1):costs[i][0] = i
    for j in range(1,m+1):costs[0][j] = j
    similarities = {}
    for i in range(1,n+1):
        for j in range(1,m+1):
            similarity = 1. if a[i-1] and a[i-1] == b[j-1] else difflib.SequenceMatcher(None,a[i-1],b[j-1]).ratio() if a[i-1] and b[j-1] else 0.
            similarities[i,j] = similarity
            choices = [(costs[i-1][j-1] + (0 if similarity == 1 else 1.4*(1-similarity)), 'pair'),
                       (costs[i-1][j]+1., 'skip_script'), (costs[i][j-1]+1., 'skip_audio')]
            costs[i][j], steps[i,j] = min(choices,key=lambda item:item[0])
    anchors = {}
    i,j = n,m
    while i and j:
        step = steps[i,j]
        if step == 'pair':
            if similarities[i,j] >= .60:anchors[i-1] = j-1
            i-=1;j-=1
        elif step == 'skip_script':i-=1
        else:j-=1
    return anchors


def paginate_arabic(data, style, native_words):
    width, sizes = parse_ass_info(style+'\n[Events]\n')
    size = sizes.get('Default',38)
    safe_width = english_safe_width(style,width)
    match = re.search(r'VideoCaptionerEnglishLineWords:\s*(\d+)',style)
    limit = int(match.group(1)) if match else 7
    report = {'policy':'arabic_max_two_lines_readable_v2','input_cues':len(data.segments),'pages':[], 'warnings':[], 'estimated_times':0}
    output = []
    def fits(tokens):
        return len(tokens)<=limit and estimate_text_width(' '.join(tokens),size)*1.35<=safe_width
    def physical_lines(tokens):
        lines=1;current=[]
        for word in tokens:
            if current and not fits(current+[word]):
                lines+=1;current=[]
            current.append(word)
        return lines
    for source_id,segment in enumerate(data.segments):
        text = plain_text(segment.text)
        tokens = list(re.finditer(r'\S+',text))
        words = [t.group() for t in tokens]
        native = [w for w in native_words if w.start_time >= segment.start_time and w.end_time <= segment.end_time]
        # Never turn a sentence timestamp into invented per-word times.
        native = native if native and all(len(w.text.split())==1 for w in native) else []
        anchors = word_alignment(words,[w.text for w in native]) if native else {}
        cuts = {0:0,len(words):len(native)}
        for boundary in range(1,len(words)):
            if boundary in anchors and 0 < anchors[boundary] < len(native):
                cuts[boundary] = anchors[boundary]
        chunks = []
        begin = 0
        while begin < len(words):
            possible = [end for end in sorted(cuts) if end>begin and cuts[end]>cuts.get(begin,-1) and fits(words[begin:end])]
            if not possible:
                # Preserve content and timing rather than guessing a split.
                end = next((end for end in sorted(cuts) if end>begin and cuts[end]>cuts.get(begin,-1)),len(words))
                report['warnings'].append({'source_cue':source_id+1,'text':' '.join(words[begin:end]),'reason':'缺少可安全拆分的原生词锚点，未猜测时间'})
            else:
                end = max(possible)
                # A complete sentence or audible pause should advance the cue
                # even when another phrase could fit on the same physical line.
                natural = [p for p in possible if p < len(words) and
                    ((re.search(r'[.!?؟…]$',words[p-1]) and native[cuts[p]-1].end_time-native[cuts[begin]].start_time>=600)
                     or native[cuts[p]].start_time-native[cuts[p]-1].end_time>=400)]
                if natural:end = min(natural)
                elif end < len(words):
                    commas = [p for p in possible if p>=begin+max(2,(end-begin)//2) and re.search(r'[،,;؛]$',words[p-1])]
                    if commas:end = max(commas)
                # Avoid a flash of a single orphan word at the next page.
                if end < len(words) and len(words)-end == 1 and not natural:
                    alternatives=[p for p in possible if begin+1<p<end and fits(words[p:])
                        and native[-1].end_time-native[cuts[p]].start_time>=600]
                    if alternatives:end=max(alternatives)
            chunks.append((begin,end))
            begin=end
        # Rebalance a short page against its neighbor while both still fit on
        # one line. This preserves exact substrings and native word boundaries.
        for j in range(len(chunks)-1):
            a,b=chunks[j];_,c=chunks[j+1]
            def span_duration(lo,hi):
                return (segment.end_time if hi==len(words) else native[cuts[hi]-1].end_time) - (segment.start_time if lo==0 else native[cuts[lo]].start_time)
            if not native or min(span_duration(a,b),span_duration(b,c))>=600:continue
            choices=[v for v in cuts if a<v<c and fits(words[a:v]) and fits(words[v:c])]
            if choices:
                def penalty(v):
                    return max(0,600-span_duration(a,v))+max(0,600-span_duration(v,c))
                cut=min(choices,key=lambda v:(penalty(v),abs(v-b)))
                if penalty(cut)<penalty(b):
                    chunks[j]=(a,cut);chunks[j+1]=(cut,c)
                    report.setdefault('rebalanced_pages',0);report['rebalanced_pages']+=1
        if not chunks:chunks=[(0,0)]
        for begin,end in chunks:
            cue=copy.deepcopy(segment)
            cue.text=text[tokens[begin].start():tokens[end-1].end()] if tokens else text
            cue.start_time = segment.start_time if begin==0 else native[cuts[begin]].start_time
            cue.end_time = segment.end_time if end==len(words) else native[cuts[end]-1].end_time
            if cue.end_time<=cue.start_time:
                raise ValueError('阿拉伯分页时间无效，已停止输出，未移动或猜测时间')
            output.append(cue)
            report['pages'].append({'source_cue':source_id+1,'source_start_ms':segment.start_time,'source_end_ms':segment.end_time,
                'start_ms':cue.start_time,'end_ms':cue.end_time,'word_start':begin,'word_end':end,'text':cue.text,'single_line':fits(words[begin:end])})
        assert normalized(''.join(c.text for c in output[-len(chunks):])) == normalized(text)
    # Merge a flash with an adjacent part of the SAME original utterance only.
    pages = report['pages']
    report['merges'] = []
    changed = True
    while changed:
        changed = False
        for i, cue in enumerate(output):
            if cue.end_time-cue.start_time >= 600:
                continue
            options = []
            for j in (i-1, i+1):
                if not 0 <= j < len(output):continue
                lo, hi = sorted((i,j));a,b=output[lo],output[hi]
                same_source=pages[lo]['source_cue']==pages[hi]['source_cue']
                same_row=(getattr(a,'script_corrected',False) and getattr(b,'script_corrected',False)
                          and getattr(a,'script_row_id',None)==getattr(b,'script_row_id',None))
                gap=b.start_time-a.end_time
                combined=a.text+' '+b.text
                if (same_source or same_row) and 0<=gap<=350 and physical_lines(combined.split())<=2:
                    options.append((physical_lines(combined.split()),gap, len(combined),lo,hi))
            if not options:continue
            _,_,_,lo,hi=min(options);a,b=output[lo],output[hi]
            report['merges'].append({'start_ms':a.start_time,'end_ms':b.end_time,'text':a.text+' '+b.text})
            a.text=a.text+' '+b.text;a.end_time=b.end_time
            pages[lo].update(end_ms=a.end_time,text=a.text,single_line=fits(a.text.split()),word_end=pages[hi]['word_end'])
            del output[hi];del pages[hi]
            changed=True
            break
    # These are deliberate display holds, not invented word timestamps. A hold
    # may use only existing silence and must never overlap the neighboring cue.
    report['display_holds'] = []
    audio_end=getattr(data,'source_duration_ms',max((w.end_time for w in native_words),default=max((c.end_time for c in output),default=0)))
    for i,cue in enumerate(output):
        if cue.end_time-cue.start_time>=600:continue
        a,b=cue.start_time,cue.end_time
        available_right=max(0,(output[i+1].start_time if i+1<len(output) else audio_end)-b)
        right=min(350,available_right,600-(b-a));cue.end_time+=right
        need=600-(cue.end_time-cue.start_time)
        available_left=max(0,a-(output[i-1].end_time if i else 0))
        cue.start_time-=min(150,available_left,max(0,need))
        if (a,b)!=(cue.start_time,cue.end_time):
            report['display_holds'].append({'cue':i+1,'native_start_ms':a,'native_end_ms':b,'start_ms':cue.start_time,'end_ms':cue.end_time,'text':cue.text})
            pages[i].update(start_ms=cue.start_time,end_ms=cue.end_time,native_start_ms=a,native_end_ms=b)
    report['short_cues']=[{'cue':i+1,'start_ms':c.start_time,'end_ms':c.end_time,'text':c.text}
                          for i,c in enumerate(output) if c.end_time-c.start_time<500]
    report['timing_source']='native_words_with_bounded_display_holds' if report['display_holds'] else 'native_words'
    assert all(a.end_time<=b.start_time for a,b in zip(output,output[1:])), '分页显示时间重叠'
    data.segments=output
    report['output_cues']=len(output)
    for page in report['pages']:
        page['line_count']=physical_lines(page['text'].split())
        page['single_line']=page['line_count']==1
    report['max_lines']=2
    report['single_line_cues']=sum(p['line_count']==1 for p in report['pages'])
    report['two_line_cues']=sum(p['line_count']==2 for p in report['pages'])
    report['overflow_cues']=[i+1 for i,p in enumerate(report['pages']) if p['line_count']>2]
    return data,report
