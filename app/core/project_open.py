"""Prepare an editor project off the GUI thread; opening does not rewrite history."""
import hashlib,json,time
from pathlib import Path

def read_subtitle(path):
    from app.core.bk_asr.asr_data import ASRData
    path=Path(path);raw=path.read_bytes()
    try:text=raw.decode('utf-8-sig')
    except UnicodeDecodeError:text=raw.decode('gbk')
    # Match text-mode universal newlines; CRLF must not change VTT block parsing.
    text=text.replace('\r\n','\n').replace('\r','\n')
    parsers={'.srt':ASRData.from_srt,'.ass':ASRData.from_ass,'.vtt':ASRData.from_youtube_vtt if '<c>' in text else ASRData.from_vtt,'.json':lambda content:ASRData.from_json(json.loads(content))}
    if path.suffix.lower() not in parsers:raise ValueError('不支持的字幕格式')
    data=parsers[path.suffix.lower()](text)
    if not data.segments:raise ValueError('字幕文件中没有可编辑内容')
    return data,hashlib.sha256(raw).hexdigest()

def read_anchors(subtitle_path,project):
    directories=[Path(subtitle_path).parent]
    if project.get('work_manifest'):directories.insert(0,Path(project['work_manifest']).parent/'subtitle')
    for directory in directories:
        try:
            candidates=list(directory.glob('【原始字幕】*.srt'))
            if len(candidates)!=1:continue
            data,_=read_subtitle(candidates[0])
            if data.is_word_timestamp():return data.segments
        except (OSError,ValueError):continue
    return []

def prepare_project(manifest):
    from app.core.batch_project_store import load_batch_project
    from app.core.task_factory import TaskFactory
    started=time.perf_counter();project=load_batch_project(manifest)
    video=project.get('source_video','')
    candidates=(project.get('editable_subtitle',''),project.get('styled_subtitle',''),str(Path(video).with_suffix('.ass')) if video else '')
    subtitle=next((path for path in candidates if path and Path(path).is_file()),'')
    if not subtitle:raise FileNotFoundError('可编辑字幕已被移动、删除或共享盘暂不可读取')
    data,digest=read_subtitle(subtitle)
    anchors=read_anchors(subtitle,project)
    task=TaskFactory.create_subtitle_task(subtitle,video,customer_profile=project.get('customer_template_snapshot') or None)
    return {'manifest':str(manifest),'project':project,'subtitle':subtitle,'video':video,'data':data.to_json(),'hash':digest,
            'anchors':anchors,'task':task,'video_available':bool(video and Path(video).is_file()),'load_seconds':time.perf_counter()-started}
