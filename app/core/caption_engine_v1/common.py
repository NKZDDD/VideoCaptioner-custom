from pathlib import Path
import os,uuid
def atomic_text(path,text):
 path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
 temp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
 try:
  with temp.open('x',encoding='utf-8') as f:
   os.chmod(temp,0o600);f.write(text)
  os.replace(temp,path)
 finally:temp.unlink(missing_ok=True)
