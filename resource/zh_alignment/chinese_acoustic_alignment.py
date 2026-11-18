"""Local Chinese CTC alignment candidate. Never rewrites text or silently guesses."""
from pathlib import Path
import json, re

class ChineseAligner:
    def __init__(self, model_dir):
        import onnxruntime as ort
        model_dir=Path(model_dir)
        self.vocab=json.loads((model_dir/'vocab.json').read_text(encoding='utf-8'))
        self.inverse={v:k for k,v in self.vocab.items()}
        options=ort.SessionOptions();options.intra_op_num_threads=4
        self.session=ort.InferenceSession(str(model_dir/'model_quantized.onnx'),sess_options=options,providers=['CPUExecutionProvider'])
        self.blank=self.vocab['<pad>']

    def align(self,audio,text,offset=0,sample_rate=16000,review_interval=None):
        import numpy as np
        from acoustic_word_alignment import ctc_path
        if sample_rate!=16000:raise ValueError('Requires mono 16kHz PCM')
        chars=[c for c in text if re.match(r'[\u3400-\u9fff]',c)]
        if not chars or any(c not in self.vocab for c in chars):raise ValueError('Unsupported Chinese characters')
        if any(c.isalnum() and not re.match(r'[\u3400-\u9fff]',c) for c in text):raise ValueError('Mixed numeric/Latin text needs explicit acoustic token mapping')
        if not 0<len(audio)<=16000*20:raise ValueError('Audio window outside 0–20 seconds')
        signal=np.asarray(audio,dtype=np.float32)
        if not np.isfinite(signal).all() or signal.var()<1e-9:raise ValueError('Empty or silent audio')
        signal=(signal-signal.mean())/np.sqrt(signal.var()+1e-7)
        feeds={i.name:signal[None,:] if i.name=='input_values' else np.ones((1,len(signal)),dtype=np.int64) for i in self.session.get_inputs()}
        logits=self.session.run(None,feeds)[0][0]
        shifted=logits-logits.max(axis=-1,keepdims=True)
        logp=shifted-np.log(np.exp(shifted).sum(axis=-1,keepdims=True))
        spans=ctc_path(logp,[self.vocab[c] for c in chars],self.blank)
        words=[dict(text=c,start_ms=round(offset*1000+(a*320+40)/16),
                    end_ms=round(offset*1000+(b*320+40)/16),acoustic_score=round(score,4))
               for c,(a,b,score) in zip(chars,spans)]
        previous=round(offset*1000)
        for word in words:
            if not previous<=word['start_ms']<word['end_ms']<=round(offset*1000+len(audio)/16):raise ValueError('Invalid acoustic span')
            previous=word['end_ms']
        ids=logits.argmax(axis=-1);decoded=[];last=None;emissions=[]
        for frame,token in enumerate(ids):
            if token!=last and token!=self.blank:
                letter=self.inverse.get(int(token),'?');decoded.append(letter)
                emissions.append(dict(text=letter,start_ms=round(offset*1000+(frame*320+40)/16),score=float(np.exp(logp[frame,int(token)]))))
            last=token
        review={}
        if review_interval:
            frame_ms=offset*1000+(np.arange(len(ids))*320+40)/16
            core=(frame_ms>=review_interval[0]) & (frame_ms<=review_interval[1])
            core_ids=ids[core]
            review={'core_frames':len(core_ids),'core_blank_fraction':float(np.mean(core_ids==self.blank)) if len(core_ids) else 0,
                    'core_nonblank_frames':int(np.sum(core_ids!=self.blank))}
        return {'text':text,'character_spans':words,'independent_decode':''.join(decoded).replace('|',' '),'emissions':emissions,**review,
                'audio_interval_ms':[round(offset*1000),round(offset*1000+len(audio)/16)],
                'human_verified':False,'automatic_apply':False}
