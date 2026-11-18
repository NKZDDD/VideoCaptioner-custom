"""Constrain a proposed script turn with unique literal audio-text evidence."""
import re,unicodedata

def normalized_with_offsets(text):
 chars=[];offsets=[]
 for i,c in enumerate(text):
  for n in unicodedata.normalize('NFKC',c).casefold():
   if n.isalnum():chars.append(n);offsets.append(i)
 return ''.join(chars),offsets

def unique_source_span(source,target,names=()):
 raw,_=normalized_with_offsets(source);canonical,positions=normalized_with_offsets(target)
 if len(raw)<4 or not re.search(r'[\u3400-\u9fffA-Za-z]',raw):return None
 # Only unambiguous three-character names with unchanged first/last letters.
 # This does not authorize arbitrary near-text, negation or number changes.
 for name in names:
  name,_=normalized_with_offsets(name)
  if len(name)!=3:continue
  for i in range(len(raw)-2):
   part=raw[i:i+3]
   if part[0]==name[0] and part[-1]==name[-1] and part!=name and sum(a!=b for a,b in zip(part,name))==1:
    candidate=raw[:i]+name+raw[i+3:]
    if candidate in canonical:raw=candidate
 first=canonical.find(raw)
 if first<0 or canonical.find(raw,first+1)>=0:return None
 left,right=positions[first],positions[first+len(raw)-1]+1
 # Latin evidence must cover whole words, not an accidental interior substring.
 for token in re.finditer(r"[A-Za-z0-9]+(?:['’\-][A-Za-z0-9]+)*",target):
  if token.start()<left<token.end() or token.start()<right<token.end():return None
 return left,right


def _number_view(text):
 """A comparison view only. Every expanded numeral maps to its whole source token."""
 from .numbers import normalize_arabic_numbers
 chars=[];starts=[];ends=[];cursor=0
 for match in re.finditer(r'[0-9０-９]+(?:\.[0-9０-９]+)?',text):
  for i in range(cursor,match.start()):
   key,_=normalized_with_offsets(text[i])
   chars.extend(key);starts.extend([i]*len(key));ends.extend([i+1]*len(key))
  converted,_=normalized_with_offsets(normalize_arabic_numbers(match.group()))
  chars.extend(converted);starts.extend([match.start()]*len(converted));ends.extend([match.end()]*len(converted))
  cursor=match.end()
 for i in range(cursor,len(text)):
  key,_=normalized_with_offsets(text[i]);chars.extend(key);starts.extend([i]*len(key));ends.extend([i+1]*len(key))
 return ''.join(chars),starts,ends


def _unique_position(needle,text):
 first=text.find(needle)
 return first if first>=0 and text.find(needle,first+1)<0 else None


def supported_source_span(source,target,left,right,names=()):
 """Repair near-edge clipping only with literal evidence in this same script turn.

 Numeric equivalence is a comparison operation; output always uses original script.
 Filler removal is considered only when the complete source has no unique match.
 Ambiguous anchors never choose a different occurrence or guess missing speech.
 """
 exact=unique_source_span(source,target,names)
 if exact is not None:return exact
 if not re.search(r'[\u3400-\u9fff]',source) or not re.search(r'[\u3400-\u9fff]',target):return None
 raw,_,_=_number_view(source);canonical,starts,ends=_number_view(target)
 if len(raw)<2:return None
 candidates=[raw]
 # A short filler may be absent from the script. Do not trim lexical prefixes.
 filler=re.match(r'^[啊嗯呃哎唉]{1,2}',raw)
 if filler and len(raw)-filler.end()>=4:candidates.append(raw[filler.end():])
 for candidate in candidates:
  pos=_unique_position(candidate,canonical)
  if pos is None:continue
  finish=pos+len(candidate)
  # Never map a substring inside an expanded numeric token back to its full token.
  if pos and starts[pos]==starts[pos-1] or finish<len(canonical) and ends[finish-1]==ends[finish]:continue
  a,b=starts[pos],ends[finish-1]
  if len(candidate)>=4 or (all('\u3400'<=c<='\u9fff' for c in candidate) and a<=left<right<=b and (left-a<=2 and b-right<=2)):
   return a,b
 # Internal ASR mistakes must not disable a strong, unique literal edge anchor.
 # Extend only a small clipped edge; this never adds unsupported script context.
 a,b=left,right
 for candidate in candidates:
  for size in range(min(12,len(candidate)),3,-1):
   prefix=candidate[:size];pos=_unique_position(prefix,canonical)
   if pos is None:continue
   begin,finish=starts[pos],ends[pos+size-1]
   omitted,_=normalized_with_offsets(target[begin:left]) if begin<left else ('',[])
   if begin<left<finish<=right and 1<=len(omitted)<=3:a=begin;break
  for size in range(min(12,len(candidate)),3,-1):
   suffix=candidate[-size:];pos=_unique_position(suffix,canonical)
   if pos is None:continue
   begin,finish=starts[pos],ends[pos+size-1]
   omitted,_=normalized_with_offsets(target[right:finish]) if right<finish else ('',[])
   if left<=begin<right<finish and 1<=len(omitted)<=3:b=finish;break
  if (a,b)!=(left,right):return a,b
 return None

def unexplained_edge_loss(source,excerpt):
 """Detect small lexical edge deletions; uncertain corrections retain source text."""
 from difflib import SequenceMatcher
 raw,_,_=_number_view(source);target,_,_=_number_view(excerpt)
 if not re.search(r'[\u3400-\u9fff]',raw):return False
 filler=re.match(r'^[啊嗯呃哎唉哦]{1,2}',raw)
 if filler and not target.startswith(filler.group()):raw=raw[filler.end():]
 if 1<=len(target)<len(raw)<=3 and re.fullmatch(r'[\u3400-\u9fff]+',raw+target):
  if not re.fullmatch(r'[零〇一二两三四五六七八九十百千万亿点]+',raw+target):return True
 if min(len(raw),len(target))<4:return False
 matcher=SequenceMatcher(None,raw,target,autojunk=False)
 if matcher.ratio()<.75:return False
 ops=matcher.get_opcodes()
 for tag,a,b,c,d in (ops[0],ops[-1]):
  edge=(a==0 and c==0) or (b==len(raw) and d==len(target))
  lost=raw[a:b]
  if edge and tag in ('delete','replace') and 0<len(lost)<=3 and b-a>d-c and all('\u3400'<=x<='\u9fff' for x in lost):
   if not re.fullmatch(r'[零〇一二两三四五六七八九十百千万亿点]+',lost):return True
 return False


def later_source_in_excerpt(excerpt,source,rows,end):
 target,_=normalized_with_offsets(excerpt);spoken,_=normalized_with_offsets(source)
 following=rows[end+1:end+33]
 if not following:return False
 group=following[0].get('asr_segment_id');parts=[]
 for row in following:
  if parts and (group is None or row.get('asr_segment_id')!=group):break
  parts.append(row.get('text',''))
 future,_=normalized_with_offsets(''.join(parts))
 return len(future)>=4 and future in target and future not in spoken
