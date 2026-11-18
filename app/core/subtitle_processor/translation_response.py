"""Bounded JSON translation protocol. Layout stays program-owned."""
import json,re
from app.core.llm_permissions import safe_text
VERSION='translation-context/v1'
PROTOCOL='''\nTRANSLATION API OUTPUT CONTRACT (applies after customer presentation preferences):
Return exactly one JSON object mapping the requested subtitle IDs to target-language translation strings only.
The program already retains the original text and controls bilingual layout; never include the source language, IDs, labels, explanations or model names inside a translation value.
Read all supplied neighboring source entries together for context, even when an entry is one word. Translate only requested IDs; never move or repeat meaning across IDs.
Return ordinary text inside each value. Do not add tabs, ASS/HTML instructions or invented dialogue.
'''

def normalize(value,source):
 if not isinstance(value,str):raise ValueError('non_string')
 if any(ord(c)<32 and c not in '\r\n' for c in value):raise ValueError('control_character')
 value=' '.join(value.splitlines()).strip()
 if not value or value=='ERROR' or value.endswith('||ERROR'):raise ValueError('empty_or_error')
 if not safe_text(value,source):raise ValueError('markup_or_instruction')
 if re.match(r'^(?:original|translation|译文|原文)\s*[:：]',value,re.I):raise ValueError('output_label')
 return value

def decode(content,sources,reflect=False):
 if not isinstance(content,str):return {},{k:'non_string_response' for k in sources}
 content=content.strip()
 content=re.sub(r'^<think>.*?</think>\s*','',content,flags=re.S)
 fence=re.fullmatch(r'```(?:json)?\s*\n?(.*?)\n?```',content,re.S|re.I)
 if fence:content=fence[1].strip()
 def pairs(rows):
  d={}
  for k,v in rows:
   if k in d:raise ValueError('duplicate_id')
   d[k]=v
  return d
 try:
  values=json.loads(content,object_pairs_hook=pairs)
  if not isinstance(values,dict) or set(values)-set(sources):raise ValueError('unexpected_id_or_shape')
 except (ValueError,TypeError):return {},{k:'json_or_id_shape' for k in sources}
 good={};bad={}
 for k,source in sources.items():
  try:
   if k not in values:raise ValueError('missing_id')
   value=values[k]
   if reflect:
    if not isinstance(value,dict) or not isinstance(value.get('revised_translation'),str):raise ValueError('reflection_shape')
    value=value['revised_translation']
   good[k]=normalize(value,source)
  except ValueError as error:bad[k]=str(error)
 return good,bad
