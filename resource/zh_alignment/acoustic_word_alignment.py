"""CTC acoustic alignment. Text is immutable; no duration-based word timing."""
import copy
import json
import re
from pathlib import Path


def guard_unreliable_segments(result, report, original):
    """Reject a largely unsupported displaced alignment as a whole utterance.

    Scores are screening heuristics, not probabilities. Preserve the original
    ASR interval explicitly in the report; never label this as acoustic alignment.
    """
    import statistics
    report['fallback_segments'] = []
    cursor = 0
    for index, (source, target) in enumerate(zip(original['segments'], result['segments'])):
        count = len(source['words'])
        records = report['words'][cursor:cursor+count]
        cursor += count
        scores = [r['acoustic_score'] for r in records]
        unsupported = (scores and sum(s < .15 for s in scores)/len(scores) >= .75
                       and statistics.median(scores) < .1)
        displaced = (abs(target['words'][0]['start']-source['words'][0]['start']) > .25
                     or target['words'][-1]['end']-target['words'][0]['start'] <
                        .65*(source['words'][-1]['end']-source['words'][0]['start']))
        left = result['segments'][index-1]['words'][-1]['end'] if index else 0
        right = (result['segments'][index+1]['words'][0]['start']
                 if index+1 < len(result['segments']) else float('inf'))
        if unsupported and displaced and left <= source['words'][0]['start'] < source['words'][-1]['end'] <= right:
            report['fallback_segments'].append({'segment':index,'text':source['text'],
                'reason':'多数词缺少声学支持且校准明显偏移；保留原始ASR时间，需复核',
                'source':'original_asr_not_acoustically_verified'})
            for word, old, record in zip(target['words'],source['words'],records):
                record['rejected_candidate'] = record['after']
                record['after'] = [old['start'],old['end']]
                record['used_source'] = 'original_asr_not_acoustically_verified'
                word['start'],word['end'] = old['start'],old['end']
    return result


def ctc_path(log_probs, labels, blank=0):
    import numpy as np
    states = np.full(2 * len(labels) + 1, blank, dtype=np.int64)
    states[1::2] = labels
    frames, count = len(log_probs), len(states)
    if frames < len(labels) or not labels:
        raise ValueError('音频帧不足以覆盖文字')
    scores = np.full(count, -np.inf, dtype=np.float32)
    scores[0] = log_probs[0, blank]
    scores[1] = log_probs[0, labels[0]]
    back = np.zeros((frames, count), dtype=np.uint8)
    skip = np.zeros(count, dtype=bool)
    skip[2:] = (states[2:] != blank) & (states[2:] != states[:-2])
    for t in range(1, frames):
        one = np.concatenate(([-np.inf], scores[:-1]))
        two = np.concatenate(([-np.inf, -np.inf], scores[:-2]))
        two[~skip] = -np.inf
        choices = np.stack((scores, one, two))
        step = choices.argmax(axis=0)
        back[t] = step
        scores = choices[step, np.arange(count)] + log_probs[t, states]
    state = count - 1 if scores[-1] >= scores[-2] else count - 2
    if not np.isfinite(scores[state]):
        raise ValueError('没有完整声学对齐路径')
    path = np.empty(frames, dtype=np.int32)
    for t in range(frames - 1, -1, -1):
        path[t] = state
        state -= int(back[t, state])
    spans = []
    for index, label in enumerate(labels):
        positions = np.flatnonzero(path == 2 * index + 1)
        if not len(positions):
            raise ValueError('对齐结果缺少字符')
        spans.append((int(positions[0]), int(positions[-1]) + 1,
                      float(np.exp(log_probs[positions, label].mean()))))
    return spans


class EnglishAcousticAligner:
    def __init__(self, model_dir):
        import onnxruntime as ort
        model_dir = Path(model_dir)
        self.vocab = json.loads((model_dir / 'vocab.json').read_text(encoding='utf-8'))
        options = ort.SessionOptions()
        options.intra_op_num_threads = 4
        self.session = ort.InferenceSession(str(model_dir / 'model_quantized.onnx'),
            sess_options=options, providers=['CPUExecutionProvider'])

    def align(self, audio, response, sample_rate=16000, _repair_windows=False):
        import numpy as np
        if sample_rate != 16000 or response.get('language') != 'en':
            raise ValueError('此对齐模型只用于16kHz英语，不修改其他语种')
        result = copy.deepcopy(response)
        report = {'method':'wav2vec2_ctc_viterbi_v3_windows', 'language':'en',
                  'words':[], 'review':[], 'text_changed':False,
                  'window_repair_applied':_repair_windows}
        groups = []
        for si, segment in enumerate(result['segments']):
            # 400ms context on either side overlaps when the gap is <=800ms.
            # Such neighbours must share one acoustic path, not independently
            # claim the same audio frames. Bound memory at 60s per group.
            if (groups and segment['start']-groups[-1]['end'] <= (.8 if _repair_windows else .3)
                    and (not _repair_windows or segment['end']-groups[-1]['start'] <= 60)):
                groups[-1]['words'].extend(segment.get('words') or [])
                groups[-1]['end'] = segment['end']
            else:
                groups.append({'start':segment['start'],'end':segment['end'],
                               'words':list(segment.get('words') or []),'source_segment':si})
        report['audio_windows'] = []
        for gi, segment in enumerate(groups):
            si = segment['source_segment']
            if segment['end']-segment['start'] > 60:
                raise ValueError('连续语音超过60秒，需要分窗对齐，未在词中间裁音频')
            words = segment.get('words') or []
            if not words:
                raise ValueError('原始转录缺少逐词文字')
            text, ranges = [], []
            for word in words:
                # Preserve the original text. Only the acoustic model's input is normalized.
                letters = re.sub("[^A-Z']", '', word['word'].upper().replace('’', "'"))
                if not letters or any(c not in self.vocab for c in letters):
                    raise ValueError('存在对齐词表不支持的词，需专门处理，未估算时间')
                if text:
                    text.append('|')
                start = len(text)
                text.extend(letters)
                ranges.append((start,len(text)))
            left = max(0, int(round((segment['start'] - .4) * sample_rate)))
            right = min(len(audio), int(round((segment['end'] + .4) * sample_rate)))
            # A duration-bound split still has a disjoint acoustic ownership
            # boundary in the inter-segment gap. Only crop input audio; never
            # clip, shift or interpolate the resulting word timestamps.
            if _repair_windows and gi:
                boundary=round((groups[gi-1]['end']+segment['start'])*sample_rate/2)
                left=max(left,boundary)
            if _repair_windows and gi+1<len(groups):
                boundary=round((segment['end']+groups[gi+1]['start'])*sample_rate/2)
                right=min(right,boundary)
            if right<=left:
                raise ValueError('声学分窗为空，未输出估算时间')
            report['audio_windows'].append([left/sample_rate,right/sample_rate])
            signal = audio[left:right].astype(np.float32)
            signal = (signal-signal.mean()) / np.sqrt(signal.var()+1e-7)
            feeds = {i.name:signal[None,:] if i.name == 'input_values'
                     else np.ones((1,len(signal)), dtype=np.int64)
                     for i in self.session.get_inputs()}
            logits = self.session.run(None,feeds)[0][0]
            maximum = logits.max(axis=-1,keepdims=True)
            shifted = logits-maximum
            log_probs = shifted-np.log(np.exp(shifted).sum(axis=-1,keepdims=True))
            spans = ctc_path(log_probs,[self.vocab[c] for c in text],self.vocab['<pad>'])
            for wi,(word,(a,b)) in enumerate(zip(words,ranges)):
                # Wav2vec2 base: 320-sample stride, 400-sample receptive field.
                # Use frame cells centred on the receptive-field centres (offset 40),
                # rather than overlapping receptive fields as word intervals.
                start = (left+spans[a][0]*320+40)/sample_rate
                end = min(right/sample_rate,(left+spans[b-1][1]*320+40)/sample_rate)
                confidence = float(np.mean([v[2] for v in spans[a:b]]))
                record = {'segment':si,'word_index':wi,'word':word['word'],
                    'before':[word['start'],word['end']], 'after':[round(start,3),round(end,3)],
                    'acoustic_score':round(confidence,4)}
                report['words'].append(record)
                if confidence < .15:
                    report['review'].append(record)
                word['start'],word['end'] = record['after']
        guard_unreliable_segments(result, report, response)
        for segment in result['segments']:
            segment['start'],segment['end'] = segment['words'][0]['start'],segment['words'][-1]['end']
        result['words'] = [w for s in result['segments'] for w in s['words']]
        previous = 0
        for word in result['words']:
            if not previous <= word['start'] < word['end'] <= len(audio)/sample_rate:
                if not _repair_windows:
                    # Preserve successful v2 results. Only retry a conflicting
                    # result using shared paths/disjoint acoustic windows.
                    fixed,retried=self.align(audio,response,sample_rate,_repair_windows=True)
                    retried['retry_reason']='初次校准出现交界冲突，已重新声学对齐；没有裁短或平移词时间'
                    return fixed,retried
                raise ValueError(f'校准结果重叠、倒序或越界，未输出: {word}, previous={previous}')
            previous = word['end']
        if [w['word'] for w in result['words']] != [w['word'] for s in response['segments'] for w in s['words']]:
            raise ValueError('校准前后文字不一致，未输出')
        return result,report
