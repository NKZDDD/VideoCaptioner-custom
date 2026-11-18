"""Reattach stranded terminal punctuation; preserve times, words and intentional prefixes."""
import copy,re
END=set('.,!?;:，。！？；：、”’」』）》】)]')
OPEN=set('([{（【《「『“‘')

def head(text):
 s=text.lstrip()
 # Ellipses and leading decimals are meaningful, not stranded sentence endings.
 if s.startswith(('...','…')) or re.match(r'\.\d',s):return '',text
 i=0
 while i<len(s) and s[i] in END:i+=1
 return s[:i],s[i:].lstrip() if i else text

def repair(data):
 result=copy.deepcopy(data);changes=[]
 for attr in ('text','translated_text'):
  previous=None
  for index,cue in enumerate(result.segments):
   value=getattr(cue,attr,'') or ''
   if not re.search(r'[A-Za-z\u3400-\u9fff]',value):
    previous=None
    continue
   # Newline is a visual boundary, not a new sentence.
   lines=value.replace(r'\N','\n').split('\n')
   for i in range(1,len(lines)):
    mark,rest=head(lines[i])
    if mark and rest:
     lines[i-1]=lines[i-1].rstrip()+mark;lines[i]=rest
    tail=lines[i-1].rstrip()
    j=len(tail)
    while j and tail[j-1] in OPEN:j-=1
    if j<len(tail):lines[i-1]=tail[:j].rstrip();lines[i]=tail[j:]+lines[i].lstrip()
   value='\n'.join(lines)
   mark,rest=head(value)
   if mark and rest:
    if previous is not None:
     old=getattr(previous,attr).rstrip()
     # A duplicated sentence delimiter from overlapping script slices is not a second sentence.
     setattr(previous,attr,old+(mark if not old.endswith(mark) else ''))
     action='reattached_to_previous'
    elif all(c in '.,!?;:，。！？；：、' for c in mark):action='removed_orphan_terminal_prefix'
    else:
     setattr(cue,attr,value);previous=cue;continue
    value=rest;changes.append({'cue':index,'field':attr,'punctuation':mark,'action':action})
   if previous is not None:
    old=getattr(previous,attr).rstrip();j=len(old)
    while j and old[j-1] in OPEN:j-=1
    if j<len(old):setattr(previous,attr,old[:j].rstrip());value=old[j:]+value.lstrip()
   from app.core.utils.latin_spacing import normalize
   spaced=normalize(value)
   if spaced!=value:changes.append({'cue':index,'field':attr,'action':'restored_latin_punctuation_spacing'})
   setattr(cue,attr,spaced);previous=cue
 return result,changes
