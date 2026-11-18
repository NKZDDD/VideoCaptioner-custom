"""Versioned workstation cache for immutable resources; presets stay on SMB."""
import hashlib,json,os,shutil,subprocess,tempfile,uuid,zipfile
from pathlib import Path,PurePosixPath

def source_fingerprint(root):
    files=[]
    for path in sorted((Path(root)/'resource').rglob('*')):
        if not path.is_file() or 'subtitle_style' in path.relative_to(root).parts or '__pycache__' in path.parts:continue
        stat=path.stat();files.append((path.relative_to(root).as_posix(),stat.st_size,stat.st_mtime_ns))
    return hashlib.sha256(json.dumps(files,separators=(',',':')).encode()).hexdigest()

def ready(target,digest,required):
    try:return (target/'READY').read_text(encoding='ascii')==digest and all((target/'resource'/name).is_file() for name in required)
    except OSError:return False

def ensure_resources(root,base=None,progress=True,source_root=None):
    from studio_bootstrap import cache_lock,Status,log
    root=Path(root);base=Path(base) if base else Path(os.environ.get('LOCALAPPDATA') or tempfile.gettempdir())/'VideoCaptioner'/'resource-cache'
    manifest=json.loads((root/'startup/resources.json').read_text(encoding='utf-8'))
    digest=manifest['sha256'];archive=manifest['archive'];required=manifest['required']
    if manifest.get('source_fingerprint') and source_fingerprint(Path(source_root) if source_root else root)!=manifest['source_fingerprint']:
        raise ValueError('Shared resources updated; use current shared files until resource pack is refreshed')
    if len(digest)!=64 or any(c not in '0123456789abcdef' for c in digest):raise ValueError('Invalid resources digest')
    if Path(archive).name!=archive or not archive.endswith('.zip'):raise ValueError('Invalid resource archive')
    for name in required:
        path=PurePosixPath(name)
        if path.is_absolute() or '..' in path.parts or '\\' in name or ':' in name:raise ValueError('Unsafe required path')
    target=base/digest
    if ready(target,digest,required):return target/'resource'
    status=Status(progress);temporary=None
    try:
        with cache_lock(base,status):
            if ready(target,digest,required):return target/'resource'
            if target.exists():raise RuntimeError('Resource cache damaged; using shared resources')
            if shutil.disk_usage(base).free < manifest['unpacked_bytes']+manifest['bytes']+64*1024**2:raise OSError('Not enough local cache space')
            temporary=base/('prepare-'+uuid.uuid4().hex);temporary.mkdir()
            local_zip=temporary/'resources.zip';sha=hashlib.sha256();copied=0
            with (root/'startup'/archive).open('rb') as source,local_zip.open('wb') as destination:
                while True:
                    data=source.read(1024**2)
                    if not data:break
                    destination.write(data);sha.update(data);copied+=len(data)
                    status.update('首次使用：准备本机播放器、字体与工具 '+str(copied//1024**2)+' MB\n完成后同版本直接复用；客户模板保持共享。')
            if sha.hexdigest()!=digest:raise ValueError('Resource archive checksum mismatch')
            status.update('正在准备本机资源，完成后自动打开软件。\n其他电脑及任务不受影响。')
            with zipfile.ZipFile(local_zip) as package:
                for i,info in enumerate(package.infolist()):
                    name=PurePosixPath(info.filename)
                    if name.is_absolute() or '..' in name.parts or not name.parts or name.parts[0]!='resource' or '\\' in info.filename or ':' in info.filename or 'subtitle_style' in name.parts:raise ValueError('Unsafe resource archive entry')
                    package.extract(info,temporary)
                    if i%20==0:status.update()
            local_zip.unlink();(temporary/'READY').write_text(digest,encoding='ascii')
            if not ready(temporary,digest,required):raise ValueError('Resource cache incomplete')
            temporary.replace(target);temporary=None
            log('resources_prepared',version=digest)
            return target/'resource'
    finally:
        status.close()
        if temporary:shutil.rmtree(temporary,ignore_errors=True)

def configure_resources(root):
    from studio_bootstrap import log
    try:
        path=ensure_resources(root);os.environ['VC_LOCAL_RESOURCE_ROOT']=str(path)
        log('resources_local',path=str(path))
        return path
    except Exception as exc:
        os.environ.pop('VC_LOCAL_RESOURCE_ROOT',None)
        log('resources_shared_fallback',error=type(exc).__name__)
        return Path(root)/'resource'
