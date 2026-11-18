"""Atomic subtitle commits and immutable, separately named render revisions."""
import datetime,hashlib,os,tempfile,uuid
from pathlib import Path
from app.core.bk_asr.asr_data import ASRData
from app.core.concurrency import shared_lock
from app.core.batch_project_store import load_batch_project,update_batch_project
from app.core.utils.get_subtitle_style import get_subtitle_style

def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def atomic_subtitle(path,data,style,layout,expected=None):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    before=path.read_bytes() if path.exists() else None
    if expected is not None and (before is None or hashlib.sha256(before).hexdigest()!=expected):
        raise RuntimeError('字幕已被其他人或任务更新，已停止覆盖；请重新加载后核对')
    handle=tempfile.NamedTemporaryFile(prefix='.'+path.stem+'-',suffix=path.suffix,dir=str(path.parent),delete=False);temp=Path(handle.name);handle.close()
    try:
        asr=ASRData.from_json(data)
        if path.suffix.lower()=='.ass':asr.to_ass(style,layout,str(temp))
        else:asr.save(str(temp),layout=layout)
        if not temp.stat().st_size:raise ValueError('没有生成有效的字幕文件')
        with temp.open('rb+') as stream:os.fsync(stream.fileno())
        if (path.read_bytes() if path.exists() else None)!=before:raise RuntimeError('保存期间字幕被其他任务更新，已停止覆盖')
        if before is not None:
            archive=path.parent/'.kaka-history'/path.stem;archive.mkdir(parents=True,exist_ok=True)
            (archive/(datetime.datetime.now().strftime('%Y%m%d_%H%M%S_%f')+'_'+uuid.uuid4().hex[:6]+path.suffix)).write_bytes(before)
        sha=digest(temp)
        os.replace(temp,path)
        return sha
    finally:temp.unlink(missing_ok=True)

def save_snapshot(request):
    path=Path(request['path']);manifest=request.get('manifest');project=load_batch_project(manifest) if manifest else {}
    source=project.get('source_video') or request.get('video') or str(path)
    style=project.get('style_template') or (project.get('customer_template_snapshot') or {}).get('style_template')
    if not style and not manifest:style=get_subtitle_style(request['style_name'])
    layout=project.get('subtitle_layout') or request['layout']
    if path.suffix.lower()=='.ass' and not style:raise ValueError('本集字幕外观缺失，未使用其他默认模板保存')
    with shared_lock(source),shared_lock(path):
        sha=atomic_subtitle(path,request['data'],style,layout,request.get('expected_hash'))
        result={'path':str(path),'hash':sha,'saved':True,'warning':'','manifest':manifest,'render':None}
        if manifest:
            values={'editable_subtitle':str(path.resolve()),'status':'edited','subtitle_revision':sha}
            if path.suffix.lower()=='.ass':values['styled_subtitle']=str(path.resolve())
            try:update_batch_project(manifest,**values)
            except (OSError,ValueError,RuntimeError):result['warning']='字幕已保存，但工程索引更新失败；请检查共享盘后重试';return result
        try:
            if request.get('render'):
                video=Path(project.get('source_video',''))
                if not video.is_file():result['warning']='字幕已保存，但原视频不可读取，尚未开始合成';return result
                revision=datetime.datetime.now().strftime('%Y%m%d_%H%M%S_%f')+'_'+uuid.uuid4().hex[:6]
                folder=path.parent/'.kaka-history'/'renders'/revision;folder.mkdir(parents=True,exist_ok=True)
                snapshot=folder/'subtitle.ass'
                if not style:result['warning']='字幕已保存，但缺少本集外观，尚未开始合成';return result
                if path.suffix.lower()=='.ass':snapshot.write_bytes(path.read_bytes())
                else:atomic_subtitle(snapshot,request['data'],style,layout)
                output=video.with_name('【卡卡】'+video.stem+'.修订'+revision+'.mp4')
                result['render']={'id':revision,'manifest':manifest,'video':str(video),'subtitle':str(snapshot),'subtitle_hash':digest(snapshot),'output':str(output),'profile':project.get('customer_template_snapshot') or None}
                update_batch_project(manifest,status='ready_to_render',pending_render_revision=revision,pending_render_subtitle_hash=result['render']['subtitle_hash'],pending_render_edit_hash=sha)
                from app.core.task_factory import TaskFactory
                result['render']['task']=TaskFactory.create_synthesis_task(str(video),str(snapshot),need_next_task=True,customer_profile=project.get('customer_template_snapshot') or None)
                result['render']['task'].synthesis_config.need_video=True
        except Exception as error:
            result['render']=None;result['warning']='字幕已保存，但合成准备失败：'+str(error)
        return result

def record_render(manifest,revision,output=None,error=None):
    from app.config import SHARED_APPDATA_PATH
    with shared_lock(SHARED_APPDATA_PATH / 'transaction-app.core.batch_project_store',wait_seconds=5):
        return _record_render(manifest,revision,output,error)

def _record_render(manifest,revision,output=None,error=None):
    project=load_batch_project(manifest)
    if project.get('pending_render_revision')!=revision:return {'stale':True}
    if error:
        update_batch_project(manifest,status='render_failed',last_render_error=str(error))
        return {'stale':False,'failed':True}
    if not Path(output).is_file() or not Path(output).stat().st_size:raise ValueError('未找到有效的新成片，旧成片索引保留')
    previous=project.get('subtitle_revision')!=project.get('pending_render_edit_hash')
    update_batch_project(manifest,status='edited' if previous else 'completed',output_video=str(output),last_render_revision=revision,
                         last_render_at=datetime.datetime.now().isoformat(timespec='seconds'),last_render_error='')
    return {'stale':False,'output':str(output),'previous':previous}
