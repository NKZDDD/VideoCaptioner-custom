"""Review only unresolved Chinese ASR; never delete merely because script differs."""
import copy,json,subprocess,tempfile,re
from pathlib import Path

def review(data,config,media,app_root=None):
 from .native_timing_review import chinese_source
 if not chinese_source(config) or getattr(config,'text_source_mode',None)!='script_correct':return data,None
 groups=[]
 for index,cue in enumerate(data.segments):
  if getattr(cue,'script_corrected',False) or not re.search(r'[\u3400-\u9fff]',cue.text):continue
  sid=getattr(cue,'asr_segment_id',None)
  word=dict(text=cue.text,start_ms=cue.start_time,end_ms=cue.end_time)
  if groups and sid is not None and groups[-1]['sid']==sid and index==groups[-1]['indices'][-1]+1:
   groups[-1]['words'].append(word);groups[-1]['indices'].append(index)
  else:groups.append(dict(index=index,indices=[index],sid=sid,words=[word]))
 if not groups:return data,None
 report=dict(schema='unmatched-audio-review/v1',removed=[],retained=[],local_only=True)
 root=Path(app_root) if app_root else Path(__file__).resolve().parents[3];asset=root/'resource/zh_alignment'
 try:
  with tempfile.TemporaryDirectory(prefix='kaka-unmatched-') as folder:
   request=Path(folder)/'request.json';answer=Path(folder)/'answer.json'
   request.write_text(json.dumps(dict(mode='unmatched',media=str(media),ffmpeg=str(root/'resource/bin/ffmpeg.exe'),groups=groups[:6]),ensure_ascii=False),encoding='utf-8')
   subprocess.run([str(root/'runtime/python.exe'),'-I','-X','utf8',str(asset/'runner.py'),str(request),str(answer)],capture_output=True,check=True,timeout=60,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
   result=json.loads(answer.read_text(encoding='utf-8'))
  expected={g['index']:g for g in groups[:6]};seen=set();removed=set()
  for item in result['results']:
   i=item['index']
   if i not in expected or i in seen:raise ValueError('Unmatched audio response identity invalid')
   seen.add(i);g=expected[i]
   record=dict(text=''.join(w['text'] for w in g['words']),start_ms=g['words'][0]['start_ms'],end_ms=g['words'][-1]['end_ms'],reason=item.get('reason'),evidence=item.get('evidence',[]))
   if item.get('remove') is True:
    evidence=item.get('evidence',[])
    key=lambda text:''.join(re.findall(r'[\u3400-\u9fff]',text))
    if len(evidence)!=2 or any(key(e.get('text',''))!=key(record['text']) or not e.get('audio_interval_ms') or e['audio_interval_ms'][0]>record['start_ms'] or e['audio_interval_ms'][1]<record['end_ms'] for e in evidence):raise ValueError('Evidence does not match the reviewed source span')
    # Pure-Python decision is repeated on the desktop, never trust a worker flag.
    import importlib.util
    spec=importlib.util.spec_from_file_location('kaka_speech_decision',asset/'speech_presence.py')
    # The decision module must remain importable without numpy/onnx on desktop.
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    if not (module.no_speech(evidence) or module.unsupported_hypothesis(evidence)):raise ValueError('Insufficient no-speech/unstable-hypothesis evidence')
    removed.update(g['indices']);report['removed'].append(record)
   else:report['retained'].append(record)
  if seen!=set(expected):raise ValueError('Incomplete unmatched audio response')
  for g in groups[6:]:report['retained'].append(dict(text=''.join(w['text'] for w in g['words']),reason='Review count budget reached'))
  if removed and len(removed)<len(data.segments):
   output=copy.deepcopy(data);output.segments=[c for i,c in enumerate(output.segments) if i not in removed];return output,report
  if len(removed)==len(data.segments):report['removed']=[];report['retained'].append(dict(reason='All captions uncertain; no automatic empty export'))
 except Exception as error:
  report['removed']=[];report['retained'].append(dict(reason=type(error).__name__+': '+str(error)[:200]))
 return data,report
