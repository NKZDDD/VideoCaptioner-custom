"""Read bounded, episode-specific evidence; never infer accurate timing from a score."""
import json
import math
from pathlib import Path

MAX_BYTES=8*1024*1024

def read_json(path):
    path=Path(path)
    if path.stat().st_size>MAX_BYTES:raise ValueError('报告过大，请打开原报告查看')
    return json.loads(path.read_text(encoding='utf-8-sig'))

def interval(item):
    try:
        if 'start_ms' in item:a,b=item['start_ms'],item['end_ms']
        elif 'after' in item:a,b=[float(v)*1000 for v in item['after']]
        else:return None,None
        if isinstance(a,bool) or isinstance(b,bool):return None,None
        a,b=float(a),float(b)
        if not math.isfinite(a+b) or a<0 or b<=a:return None,None
        return round(a),round(b)
    except (TypeError,ValueError,KeyError):return None,None

def normalize_report(report,path):
    if not isinstance(report,dict):raise ValueError('报告结构不支持')
    if not any(key in report for key in ('review_items','review','fallback_segments')):
        raise ValueError('报告未包含质量检查字段')
    if report.get('mode')=='script_correct':
        from app.core.review_policy import classify_report
        from app.core.subtitle_processor.optimize import normalize_arabic_numbers
        report=dict(report)
        report['review_items']=classify_report(report,normalize_arabic_numbers)['manual']
    items=[]
    def add(item,reason,kind):
        if not isinstance(item,dict):return
        a,b=interval(item)
        text=item.get('text') or item.get('word') or item.get('script_text') or (item.get('after') if isinstance(item.get('after'),str) else '')
        items.append({'start_ms':a,'end_ms':b,'text':str(text),
                      'reason':reason,'kind':kind,'report':str(path)})
    if 'review_items' in report:
        for item in report.get('review_items',[]):
            if isinstance(item,dict):add(item,str(item.get('reason') or '文字已校正，请结合原声核对'),str(item.get('kind') or '字幕待核对'))
    elif 'review' in report and isinstance(report['review'],list):
        for item in report['review']:
            if isinstance(item,dict):add(item,'该词声学定位支持不足；分数不代表准确率','时间待核对')
    for item in report.get('fallback_segments',[]):
        if isinstance(item,dict):add(item,'声学校准回退，当前使用原始识别时间','时间回退')
    return items

def collect_review(manifest):
    """Only exact source-stem sidecars or explicit manifest report paths, no recursive scan."""
    project=read_json(manifest) if not isinstance(manifest,dict) else manifest
    files=[];errors=[]
    explicit=project.get('quality_reports',[])
    if isinstance(explicit,list):files.extend(Path(p) for p in explicit if isinstance(p,str))
    # Select newest report per family for this exact source, never another episode.
    source=project.get('source_video','')
    if source:
        source=Path(source)
        for family in ('台词匹配报告','语音辅助校正报告','时间轴校准报告'):
            candidates=[]
            try:
                for p in source.parent.iterdir():
                    if not p.name.endswith('.json'):continue
                    prefix=source.stem+'.'+family
                    cloud_prefix=source.stem+'.云端报告.'
                    if p.name.startswith(prefix):tail=p.name[len(prefix):-5]
                    elif p.name.startswith(cloud_prefix) and ('.'+family) in p.name[len(cloud_prefix):]:
                        report_name=p.name[len(cloud_prefix):]
                        tail=report_name.split('.'+family,1)[1][:-5]
                    else:continue
                    if tail=='' or (tail.startswith('.') and all(ch.isdigit() or ch in '._' for ch in tail)):
                        candidates.append(p)
                if candidates:files.append(max(candidates,key=lambda p:(p.stat().st_mtime_ns,p.name)))
            except OSError:errors.append('报告目录暂不可读')
    # The compiled run report supersedes stale reports from another mode/run.
    if source:
        current=Path(source).with_suffix('.客户规则执行.json')
        if current.is_file():
            try:
                report=read_json(current)
                if report.get('schema')!='customer-run/v1':raise ValueError('客户执行报告格式无效')
                stamp=report.get('source_stamp');stat=Path(source).stat()
                if stamp and stamp!={'size':stat.st_size,'mtime_ns':stat.st_mtime_ns}:raise ValueError('客户执行报告对应旧素材')
                parsed=normalize_report(report,current)
                parsed.sort(key=lambda item:(item['start_ms'] is None,item['start_ms'] or 0,item['kind']))
                errors=[] if report.get('status') in ('completed','completed_with_review') else ['本次字幕检查尚未完成']
                return {'state':'needs_review' if parsed else 'unavailable' if errors else 'no_flags','items':parsed,'count':len(parsed),'report_count':1,'errors':errors,'automatic_count':0}
            except (OSError,ValueError,TypeError):
                return {'state':'unavailable','items':[],'count':0,'report_count':0,'errors':['本次客户规则执行报告无法读取或已失效'],'automatic_count':0}
    items=[];seen=set();valid=0;automatic_count=0
    for path in dict.fromkeys(files):
        try:
            report=read_json(path);parsed=normalize_report(report,path);valid+=1
            if report.get('mode')=='script_correct':
                from app.core.review_policy import classify_report
                from app.core.subtitle_processor.optimize import normalize_arabic_numbers
                automatic_count+=classify_report(report,normalize_arabic_numbers)['automatic_count']
            if report.get('status')=='failed':errors.append('检查未完成：'+path.name)
            for item in parsed:
                key=(item['start_ms'],item['end_ms'],item['text'],item['reason'],item['kind'])
                if key not in seen:items.append(item);seen.add(key)
        except (OSError,ValueError,TypeError):errors.append('无法读取报告：'+path.name)
    items.sort(key=lambda item:(item['start_ms'] is None,item['start_ms'] or 0,item['kind']))
    return {'state':'needs_review' if items else ('unavailable' if errors else 'no_flags' if valid else 'uninspected'),
            'items':items,'report_count':valid,'errors':errors,'count':len(items),'automatic_count':automatic_count}

def summary(result):
    if result['count']:return '已生成 · 待核对 '+str(result['count'])+' 项'
    if result['state']=='no_flags':return '已生成 · 报告无待核对项（未经人工验收）'
    if result['state']=='unavailable':return '已生成 · 报告读取失败'
    return '已生成 · 未检查'

def matching_row(items,review):
    a,b=review.get('start_ms'),review.get('end_ms')
    if a is None or b is None:return None
    overlaps=[max(0,min(b,c['end_time'])-max(a,c['start_time'])) for c in items]
    if not overlaps or max(overlaps)<=0:return None
    return overlaps.index(max(overlaps))
