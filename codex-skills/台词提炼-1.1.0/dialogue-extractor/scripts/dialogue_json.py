"""Source inventory, provenance validation and collision-safe per-episode publication.

Semantic extraction belongs to Codex. This helper does not guess what is spoken.
"""
import argparse,hashlib,json,re,os,uuid,zipfile,sys
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0,str(Path(__file__).resolve().parent))
DEFAULT_ROOT=r'\\Desktop-8roqjoa\素材共享盘\Json台词'
MODES={'dialogue','OS','VO','narration','PHONE','recording'}
def digest(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def read(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def write(p,data):
 p=Path(p);p.parent.mkdir(parents=True,exist_ok=True)
 tmp=p.with_name(p.name+'.'+uuid.uuid4().hex+'.tmp')
 tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8');os.replace(tmp,p)
def source_lines(p):
 if p.suffix.lower()=='.docx':
  with zipfile.ZipFile(p) as z:
   root=ET.fromstring(z.read('word/document.xml'))
  ns={'w':'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
  return [
   ''.join(n.text or '' for n in para.findall('.//w:t',ns)) for para in root.findall('.//w:p',ns)]
 raw=p.read_bytes()
 for enc in (('utf-16',) if raw.startswith((b'\xff\xfe',b'\xfe\xff')) else ('utf-8-sig','gb18030')):
  try:return raw.decode(enc).splitlines()
  except UnicodeError:pass
 raise ValueError('无法解码原件：'+str(p))
def inventory(source,out):
 source=Path(source).resolve()
 if not source.exists():raise ValueError('源路径不存在')
 files=[source] if source.is_file() else sorted(p for p in source.rglob('*') if p.is_file() and not any(x.startswith('.') for x in p.relative_to(source).parts))
 supported={'.txt','.md','.docx'};units=[];sources=[];unsupported=[]
 for p in files:
  if p.suffix.lower() not in supported:
   unsupported.append(str(p));continue
  fid='F'+str(len(sources)+1).zfill(4)
  lines=source_lines(p)
  for i,line in enumerate(lines,1):
   if line.strip():units.append({'id':f'{fid}:L{i}','text':line,'source_file':str(p),'line':i})
  sources.append({'id':fid,'path':str(p),'sha256':digest(p),'nonempty_units':sum(bool(x.strip()) for x in lines)})
 if not sources:raise ValueError('没有可读取的TXT/MD/DOCX；其他格式需先用对应工具完整提取')
 result={'schema':'dialogue-inventory/v1','source_root':str(source),'sources':sources,'units':units,'unsupported':unsupported}
 write(out,result);return {'files':len(sources),'units':len(units),'unsupported':unsupported,'inventory':str(Path(out).resolve())}
def validate(bundle,inv,check_source=True):
 if inv.get('schema')!='dialogue-inventory/v1':raise ValueError('清单格式错误')
 if check_source:
  for s in inv['sources']:
   if not Path(s['path']).is_file() or digest(s['path'])!=s['sha256']:raise ValueError('原件已变化，请重新读取：'+s['path'])
 units={u['id']:u for u in inv['units']}
 ledger=bundle.get('coverage',{})
 if set(ledger)!=set(units):raise ValueError('原文覆盖台账有遗漏或未知单元')
 for uid,item in ledger.items():
  if item.get('kind') not in ('dialogue','reference','non_spoken','duplicate','review'):raise ValueError('覆盖分类无效：'+uid)
  if item['kind']=='review':raise ValueError('存在未解决的原文歧义：'+uid)
  if item['kind']!='dialogue' and not str(item.get('reason','')).strip():raise ValueError('排除原文需说明原因：'+uid)
  if item['kind']=='duplicate' and (item.get('duplicate_of') not in units or item['duplicate_of']==uid):raise ValueError('重复项需引用已保留来源：'+uid)
 if bundle.get('review_items'):raise ValueError('存在待确认项，不发布为正式台词')
 title=bundle.get('title','')
 if not isinstance(title,str) or not title.strip():raise ValueError('缺少剧名')
 episodes=bundle.get('episodes',[])
 if not episodes:raise ValueError('没有集数')
 used=set();occupied={};numbers=set();result=[]
 for ep in episodes:
  number=ep.get('current_episode')
  if type(number)is not int or number<1 or number in numbers:raise ValueError('集号无效或重复')
  numbers.add(number);ds=ep.get('dialogues',[])
  if not ds:raise ValueError('空集需先核对是否无对白，不能当提取成功')
  last_segment=0
  for i,d in enumerate(ds,1):
   if d.get('order')!=i or not isinstance(d.get('speaker'),str) or d.get('mode') not in MODES:raise ValueError('台词顺序、人物或发声类型无效')
   if type(d.get('segment'))is not int or d['segment']<last_segment:raise ValueError('段号未按顺序排列')
   last_segment=d['segment'];text=d.get('text');refs=d.get('source_refs',[])
   if not isinstance(text,str) or not text.strip() or not refs:raise ValueError('空台词或缺少来源')
   parts=[]
   for ref in refs:
    uid=ref.get('unit_id');u=units.get(uid);a=ref.get('start');b=ref.get('end')
    if not u or type(a)is not int or type(b)is not int or not 0<=a<b<=len(u['text']):raise ValueError('原文位置无效')
    if ledger[uid]['kind']!='dialogue':raise ValueError('台词引用被排除来源')
    if any(a<y and b>x for x,y in occupied.get(uid,[])):raise ValueError('同一原文台词片段被重复收录：'+uid)
    occupied.setdefault(uid,[]).append((a,b))
    parts.append(u['text'][a:b]);used.add(uid)
   if ''.join(parts)!=text:raise ValueError('台词与引用原文不同，禁止改写或翻译：'+str(i))
  result.append({'schema_version':'1.0','title':title,'current_episode':number,'episode_id':f'EP{number:02}',
    'characters':ep.get('characters',[]),'scenes':ep.get('scenes',[]),'dialogues':ds,'review_items':[]})
 reference=bundle.get('bilingual_reference')
 if reference:
  if len(result)!=1:raise ValueError('双语参考按集保存，请逐集建立带来源的bundle')
  from bilingual_reference import validate_reference
  checked=validate_reference(reference,result[0]['dialogues'])
  referenced=set()
  for group in checked['groups']:
   for textkey,refkey in (('script_english','script_english_refs'),('chinese_reference','chinese_refs')):
    refs=group.get(refkey,[]);parts=[]
    if not refs:raise ValueError('双语参考缺少剧本原文跨度：'+refkey)
    for ref in refs:
     uid=ref.get('unit_id');u=units.get(uid);a=ref.get('start');b=ref.get('end')
     if not u or type(a)is not int or type(b)is not int or not 0<=a<b<=len(u['text']) or ledger[uid]['kind']!='reference':raise ValueError('双语参考原文位置或分类无效')
     parts.append(u['text'][a:b]);referenced.add(uid)
    if ''.join(parts)!=group[textkey]:raise ValueError('双语参考与剧本原文不一致：'+textkey)
  if referenced!={uid for uid,v in ledger.items() if v['kind']=='reference'}:raise ValueError('参考原文有遗漏或无引用内容')
  result[0]['bilingual_reference']=checked
 elif any(v['kind']=='reference' for v in ledger.values()):raise ValueError('存在参考原文但没有双语参考结构')
 if used!={uid for uid,v in ledger.items() if v['kind']=='dialogue'}:raise ValueError('已标为台词的来源没有全部进入输出')
 for uid,v in ledger.items():
  if v['kind']=='duplicate' and v['duplicate_of'] not in used:raise ValueError('重复项未指向实际保留台词来源')
 return sorted(result,key=lambda x:x['current_episode'])
def folder_name(title):
 name=re.sub(r'[<>:"/\\|?*\x00-\x1f]','_',title).strip(' .')
 if not name or len(name)>100 or re.fullmatch(r'(?i)(CON|PRN|AUX|NUL|COM\d|LPT\d)(\..*)?',name):raise ValueError('剧名不能安全用作文件夹名，请明确别名')
 return name+'-台词json'
def _publish_unlocked(bundle,inv,root):
 episodes=validate(bundle,inv);root=Path(root).resolve();target=(root/folder_name(bundle['title'])).resolve()
 if not target.is_relative_to(root):raise ValueError('输出路径越界')
 manifest=target/'_提取清单.json';old=read(manifest) if manifest.exists() else None
 if target.exists() and not old:raise ValueError('同名文件夹不是本工具管理，未覆盖')
 if old and old['source_root']!=inv['source_root']:raise ValueError('同名剧来自不同源路径，需确认名称，未混用')
 files={f"EP{x['current_episode']:02}_台词.json":x for x in episodes}
 # Validate the entire requested set before writing any episode.
 for name,obj in files.items():
  p=target/name
  if p.exists() and read(p)!=obj:raise ValueError('同名集已有不同内容，未覆盖：'+name+'；请核对后另存版本')
 target.mkdir(parents=True,exist_ok=True)
 for name,obj in files.items():
  p=target/name
  if not p.exists():write(p,obj)
  if read(p)!=obj:raise ValueError('发布回读不一致：'+name)
 # Keep provenance per run; partial episode additions do not erase earlier run evidence.
 run=uuid.uuid4().hex;write(target/'_提取记录'/f'{run}.json',{'inventory':inv,'coverage':bundle['coverage'],'episodes':sorted(x['current_episode'] for x in episodes)})
 write(manifest,{'schema':'dialogue-published/v1','title':bundle['title'],'source_root':inv['source_root'],
    'episode_files':sorted(p.name for p in target.glob('EP*_台词.json')),'last_run':run})
 text=[]
 for ep in episodes:
  text.append(f"\n{ep['episode_id']}")
  text.extend(f"{d['order']}. {d['speaker']} [{d['mode']}] {d['text']}" for d in ep['dialogues'])
 (target/'_提取记录'/f'{run}_纯台词.txt').write_text('\n'.join(text),encoding='utf-8')
 return {'folder':str(target),'episodes':[e['current_episode'] for e in episodes],'dialogues':sum(len(e['dialogues']) for e in episodes),'readback_verified':True,'semantic_review':'由提取者结合原文核对，来源守恒检查不等于自动理解全部台词'}
def publish(bundle,inv,root):
 root=Path(root).resolve();root.mkdir(parents=True,exist_ok=True)
 name=folder_name(bundle.get('title',''))
 locks=root/'_提取锁';locks.mkdir(exist_ok=True)
 lock=locks/(hashlib.sha256(name.encode()).hexdigest()+'.lock')
 try:fd=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY)
 except FileExistsError:raise ValueError('同剧正在发布或上次中断留锁；请核对后重试，不强行覆盖')
 try:
  os.write(fd,str(os.getpid()).encode());os.close(fd)
  return _publish_unlocked(bundle,inv,root)
 finally:lock.unlink(missing_ok=True)
def main():
 p=argparse.ArgumentParser();p.add_argument('action',choices=['scan','validate','publish']);p.add_argument('--source');p.add_argument('--inventory',required=True);p.add_argument('--bundle');p.add_argument('--root',default=DEFAULT_ROOT);a=p.parse_args()
 if a.action=='scan':r=inventory(a.source,a.inventory)
 elif a.action=='validate':r={'episodes':len(validate(read(a.bundle),read(a.inventory))),'passed':True}
 else:r=publish(read(a.bundle),read(a.inventory),a.root)
 print(json.dumps(r,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
