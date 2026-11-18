"""Restore ASR grouping only when every source text/time still agrees."""
import json
from pathlib import Path

def restore_origins(data,path):
 try:
  rows=json.loads(Path(path).read_text(encoding='utf-8'))
  if len(rows)!=len(data.segments):return False
  if any((r['text'],r['start_ms'],r['end_ms'])!=(c.text,c.start_time,c.end_time) for r,c in zip(rows,data.segments)):return False
  for r,c in zip(rows,data.segments):c.asr_segment_id=r['asr_segment_id']
  return True
 except (OSError,ValueError,KeyError,TypeError):return False
