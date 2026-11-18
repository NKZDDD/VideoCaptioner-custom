"""Validate all selected ZIP members before publishing a recoverable result set."""
import os,uuid
from pathlib import Path

def publish_bundle(bundle,entries,stopped=lambda:False):
 records=[]
 names=[name for name,target in entries]
 if len(set(bundle.namelist()))!=len(bundle.namelist()):raise ValueError('云端结果包含重复文件名，未覆盖成品')
 targets=[str(Path(target).resolve()).casefold() for name,target in entries]
 if len(set(targets))!=len(targets):raise ValueError('云端结果输出位置重复，未覆盖成品')
 try:
  # CRC/decompression failures happen here, before any existing artifact is moved.
  for name,target in entries:
   if name in ('result.mp4','subtitle.ass','subtitle.srt') and bundle.getinfo(name).file_size<=0:raise ValueError('云端返回空成品，旧文件保留')
   if stopped():raise InterruptedError('下载结果已取消，旧成品保留')
   target=Path(target);target.parent.mkdir(parents=True,exist_ok=True)
   token=uuid.uuid4().hex;staged=target.with_name(target.name+'.'+token+'.tmp')
   backup=target.with_name(target.name+'.'+token+'.bak')
   row=dict(target=target,staged=staged,backup=backup,old_moved=False,new_moved=False);records.append(row)
   with bundle.open(name) as src,staged.open('wb') as dst:
    while True:
     if stopped():raise InterruptedError('下载结果已取消，旧成品保留')
     block=src.read(1024*1024)
     if not block:break
     dst.write(block)
    dst.flush();os.fsync(dst.fileno())
   if staged.stat().st_size!=bundle.getinfo(name).file_size:raise ValueError('云端结果长度校验失败')
  if stopped():raise InterruptedError('下载结果已取消，旧成品保留')
  try:
   for row in records:
    if row['target'].exists():os.replace(row['target'],row['backup']);row['old_moved']=True
    os.replace(row['staged'],row['target']);row['new_moved']=True
  except Exception:
   errors=[]
   for row in reversed(records):
    try:
     if row['old_moved']:os.replace(row['backup'],row['target'])
     elif row['new_moved']:row['target'].unlink(missing_ok=True)
    except OSError:errors.append(str(row['backup']))
   if errors:raise RuntimeError('云端结果回滚未完成，原文件备份保留：'+'；'.join(errors))
   raise
  for row in records:
   try:row['backup'].unlink(missing_ok=True)
   except OSError:pass # A leftover backup must not mark successful output as failed.
 finally:
  for row in records:
   try:row['staged'].unlink(missing_ok=True)
   except OSError:pass
