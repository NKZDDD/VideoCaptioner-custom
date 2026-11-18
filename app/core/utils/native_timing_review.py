"""Bounded Chinese native-prefix review, prior to all script and display processing."""
from pathlib import Path
import copy,json,subprocess,tempfile

def chinese_source(config):
    language=getattr(config,'_source_language',None)
    return str(getattr(language,'value',language)).strip().casefold() in {'zh','zh-cn','zh-tw','chinese','中文','简体中文','繁体中文'}

def groups_for_review(data):
    groups=[]
    for index,cue in enumerate(data.segments):
        sid=getattr(cue,'asr_segment_id',None)
        if sid is None:continue
        if not groups or groups[-1]['sid']!=sid:groups.append({'sid':sid,'index':index,'words':[]})
        groups[-1]['words'].append({'text':cue.text,'start_ms':cue.start_time,'end_ms':cue.end_time})
    selected=[]
    for group in groups:
        words=group['words']
        if len(words)<3:continue
        a,b=words[:2]
        if len(a['text'].strip())==1 and a['end_ms']-a['start_ms']>=100 and b['start_ms']-a['end_ms']>=400:
            selected.append(group)
    return selected

def review(data,config,media,app_root=None,python_path=None,ffmpeg_path=None):
    if not chinese_source(config):return data,None
    groups=groups_for_review(data)
    if not groups:return data,None
    report={'schema':'native-timing-review/v1','suspect_groups':len(groups),'changes':[],
            'retained':[],'text_changed':False,'local_only':True,'scope':'isolated_leading_native_unit'}
    root=Path(app_root) if app_root else Path(__file__).resolve().parents[3]
    asset=root/'resource/zh_alignment';python=Path(python_path) if python_path else root/'runtime/python.exe'
    ffmpeg=Path(ffmpeg_path) if ffmpeg_path else root/'resource/bin/ffmpeg.exe'
    try:
        if not all(p.is_file() for p in (asset/'runner.py',asset/'model/model_quantized.onnx',python,ffmpeg)):
            raise FileNotFoundError('Chinese alignment assets/runtime unavailable; original timing retained')
        with tempfile.TemporaryDirectory(prefix='kaka-timing-') as folder:
            request=Path(folder)/'request.json';answer=Path(folder)/'answer.json'
            request.write_text(json.dumps({'media':str(media),'ffmpeg':str(ffmpeg),'groups':groups[:4]},ensure_ascii=False),encoding='utf-8')
            subprocess.run([str(python),'-I','-X','utf8',str(asset/'runner.py'),str(request),str(answer)],
                capture_output=True,timeout=60,check=True,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            result=json.loads(answer.read_text(encoding='utf-8'))
        output=copy.deepcopy(data)
        expected={group['index']:group for group in groups[:4]};seen=set()
        for item in result['results']:
            index=item['index']
            if index not in expected or index in seen:raise ValueError('Invalid alignment response identity')
            seen.add(index)
            if not item.get('apply'):
                report['retained'].append({'index':index,'reason':item.get('reason','Insufficient acoustic evidence')});continue
            cue=output.segments[index];nxt=output.segments[index+1]
            start,end=item['start_ms'],item['end_ms']
            if not all(isinstance(x,int) for x in (start,end)) or not cue.start_time+250<=start<end==nxt.start_time:
                raise ValueError('Alignment boundaries failed validation')
            if start-cue.start_time>2000:raise ValueError('Alignment shift exceeds maximum')
            report['changes'].append({'index':index,'text':cue.text,'before':[cue.start_time,cue.end_time],
                'after':[start,end],'evidence':item.get('evidence',[]),'human_verified':False})
            cue.start_time=start;cue.end_time=end;cue.timing_origin='two_window_chinese_ctc_prefix'
        if seen!=set(expected):raise ValueError('Incomplete alignment response')
        for group in groups[4:]:report['retained'].append({'index':group['index'],'reason':'Review count budget reached'})
        if [x.text for x in output.segments]!=[x.text for x in data.segments]:raise ValueError('Text changed')
        if any(a.end_time>b.start_time for a,b in zip(output.segments,output.segments[1:])):raise ValueError('Overlapping timing')
        return output,report
    except Exception as error:
        report['changes']=[];report['retained'].append({'reason':type(error).__name__+': '+str(error)[:200]})
        return data,report
