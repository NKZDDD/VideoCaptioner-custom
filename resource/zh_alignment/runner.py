"""Isolated CPU worker. Reads only supplied local media; no network requests."""
import sys,json,subprocess,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent))
sys.path.insert(0,str(Path(__file__).parent/'deps'))
import numpy as np
from chinese_acoustic_alignment import ChineseAligner
from chinese_timing_decision import decide

def run(request):
    model=ChineseAligner(Path(request.get('model_dir',Path(request.get('asset_root',Path(__file__).parent))/'model')));answers=[]
    vad=None
    if request.get('mode')=='unmatched':
        from speech_presence import SpeechPresence,no_speech,unsupported_hypothesis
        vad=SpeechPresence(Path(__file__).parent/'silero_vad.onnx')
    deadline=time.monotonic()+45
    for group in request['groups'][:6]:
        if time.monotonic()>deadline:
            answers.append({'index':group['index'],'apply':False,'reason':'Review time budget reached'});continue
        words=group['words'];text=''.join(x['text'] for x in words);evidence=[]
        try:
            if words[-1]['end_ms']-words[0]['start_ms']>16000:raise ValueError('Group too long for local review')
            for context in (.4,1.0):
                left=max(0,words[0]['start_ms']/1000-context)
                right=words[-1]['end_ms']/1000+context
                audio=subprocess.run([request['ffmpeg'],'-v','error','-ss',str(left),'-i',request['media'],
                    '-t',str(right-left),'-map','0:a:0','-ac','1','-ar','16000','-f','s16le','-'],
                    capture_output=True,timeout=15,check=True,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0)).stdout
                signal=np.frombuffer(audio,dtype='<i2').astype(np.float32)/32768
                item=model.align(signal,text,left,review_interval=[words[0]['start_ms'],words[-1]['end_ms']] if request.get('mode')=='unmatched' else None)
                if vad:item.update(vad.check(signal,left,[words[0]['start_ms'],words[-1]['end_ms']]))
                evidence.append(item)
            if request.get('mode')=='unmatched':
                silent=no_speech(evidence);unsupported=unsupported_hypothesis(evidence)
                remove=silent or unsupported
                decision={'apply':False,'remove':remove,'reason':'Two contexts agree: low speech activity and weak lexical evidence' if silent else 'Unmatched hypothesis lacks acoustic support and independent decoding is unstable; quarantined with original records retained' if unsupported else 'Not enough evidence to discard speech; retained for review'}
            else:decision=decide(words,*evidence)
            answers.append(dict(decision,index=group['index'],evidence=evidence))
        except Exception as error:
            answers.append({'index':group['index'],'apply':False,'reason':type(error).__name__+': '+str(error)[:200]})
    return {'schema':'chinese-prefix-alignment/v1','results':answers,'unchanged_text':True,'local_only':True}

if __name__=='__main__':
    request=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    answer=run(request)
    Path(sys.argv[2]).write_text(json.dumps(answer,ensure_ascii=False),encoding='utf-8')
