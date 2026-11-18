"""Silero v6.2 CPU adapter. See SILERO_LICENSE.txt; 64-sample context is required."""
from pathlib import Path
class SpeechPresence:
 def __init__(self,path):
  import numpy as np
  import onnxruntime as ort
  options=ort.SessionOptions();options.intra_op_num_threads=1;options.inter_op_num_threads=1
  self.model=ort.InferenceSession(str(path),sess_options=options,providers=['CPUExecutionProvider'])
 def check(self,signal,offset,interval):
  import numpy as np
  state=np.zeros((2,1,128),dtype=np.float32);context=np.zeros((1,64),dtype=np.float32);frames=[]
  for position in range(0,len(signal),512):
   block=np.asarray(signal[position:position+512],dtype=np.float32);block=np.pad(block,(0,512-len(block)))[None,:]
   feed=np.concatenate([context,block],axis=1)
   out,state=self.model.run(None,{'input':feed,'state':state,'sr':np.array(16000,dtype=np.int64)})
   context=feed[:,-64:]
   a=offset*1000+position/16;b=a+32
   if a<interval[1]+100 and b>interval[0]-100:frames.append(float(out.reshape(-1)[0]))
  return dict(vad_frames=len(frames),vad_max=max(frames,default=1),vad_mean=float(np.mean(frames)) if frames else 1)

def no_speech(evidence):
 if len(evidence)!=2:return False
 return all(e.get('vad_frames',0)>=5 and e.get('vad_max',1)<.1 and e.get('vad_mean',1)<.03 and e.get('character_spans') and
  sum(x['acoustic_score'] for x in e['character_spans'])/len(e['character_spans'])<.15 and sum(x['acoustic_score']>=.5 for x in e['character_spans'])/len(e['character_spans'])<.15 for e in evidence)

def unsupported_hypothesis(evidence):
 """Quarantine unstable text, not a claim of physical silence or deleted audio."""
 import re
 from difflib import SequenceMatcher
 key=lambda t:''.join(re.findall(r'[\u3400-\u9fff]',t))
 if len(evidence)!=2:return False
 target=key(evidence[0].get('text',''))
 if len(target)<2:return False
 if any(key(e.get('text',''))!=target or not e.get('character_spans') for e in evidence):return False
 if all(e.get('vad_max',1)>=.85 for e in evidence):return False
 for e in evidence:
  scores=[s['acoustic_score'] for s in e['character_spans']]
  if sum(scores)/len(scores)>=.15 or sum(s>=.5 for s in scores)/len(scores)>=.15:return False
  if target in key(e.get('independent_decode','')):return False
 a,b=[key(e.get('independent_decode','')) for e in evidence]
 agreement=SequenceMatcher(None,a,b).ratio() if a or b else 0
 # Repeated fillers require almost absent support; stable independent extra
 # dialogue is retained even when it differs from the written script.
 if len(set(target))==1 and max(sum(s['acoustic_score'] for s in e['character_spans'])/len(e['character_spans']) for e in evidence)<.02:return True
 return len(target)>=6 and agreement<.45
