"""Final Chinese spacing, independent of semantic grouping and punctuation mode."""
import copy,re
CJK=r'\u3400-\u9fff'
def clean(text):
 if not re.search('['+CJK+']',text):return text
 text=re.sub('[\u200b\u200c\u200d\ufeff]','',text)
 # Preserve actual Latin word boundaries in mixed content (New York), not CJK gaps.
 def space(m):
  a=text[m.start()-1] if m.start() else '';b=text[m.end()] if m.end()<len(text) else ''
  return ' ' if a.isascii() and a.isalpha() and b.isascii() and b.isalpha() else ''
 return re.sub(r'\s+',space,text)

def finish(data):
 output=copy.deepcopy(data);changes=[]
 for cue in output.segments:
  for field in ('text','translated_text'):
   before=getattr(cue,field,'');after=clean(before)
   if after!=before:
    setattr(cue,field,after);changes.append(dict(field=field,start_ms=cue.start_time,end_ms=cue.end_time,before=before,after=after))
 return output,changes
