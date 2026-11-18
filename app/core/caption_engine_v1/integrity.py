from pathlib import Path
import hashlib,json
def ready():
 root=Path(__file__).resolve().parent.parent
 try:
  data=json.loads((root/'script-v7-lock.json').read_text(encoding='utf-8'))
  return bool(data) and all(hashlib.sha256((root/name).read_bytes()).hexdigest()==digest for name,digest in data.items())
 except (OSError,ValueError,TypeError):return False
