"""Keep every accepted matching span's clock while composing display pages."""
import copy

def monotone_map(source,target):
 """Global edit path, preferring local substitutions over repeat relocation.

 Only uniquely corresponding boundaries are returned. Insertions/deletions
 cannot authorize invented internal timestamps.
 """
 n,m=len(source),len(target)
 if n*m>250000:return {}
 distance=[list(range(m+1))]+[[i]+[0]*m for i in range(1,n+1)]
 for i in range(1,n+1):
  for j in range(1,m+1):
   distance[i][j]=min(distance[i-1][j-1]+(source[i-1]!=target[j-1]),distance[i-1][j]+1,distance[i][j-1]+1)
 positions={};inverse={};i,j=n,m
 while True:
  positions.setdefault(i,set()).add(j);inverse.setdefault(j,set()).add(i)
  if i==0 and j==0:break
  if i and j and distance[i][j]==distance[i-1][j-1]+(source[i-1]!=target[j-1]):i-=1;j-=1
  elif i and distance[i][j]==distance[i-1][j]+1:i-=1
  else:j-=1
 return {a:next(iter(values)) for a,values in positions.items() if len(values)==1 and len(inverse[next(iter(values))])==1}

def scoped_boundary_map(cue,original,base_map):
 units=getattr(cue,'alignment_units',[])
 if not units:return base_map(cue,original)
 result={};cursor=0
 for unit in units:
  text=unit['text'];offset=cue.text.find(text,cursor)
  if offset<0:return base_map(cue,original)
  span=copy.deepcopy(cue);span.text=text;span.start_time=unit['start_ms'];span.end_time=unit['end_ms']
  if span.start_time<cue.start_time or span.end_time>cue.end_time:return base_map(cue,original)
  edges=base_map(span,original)
  # An accepted span's first/last boundaries cannot be overwritten by a
  # repeated phrase found elsewhere in a concatenated caption.
  edges.setdefault(0,{})['start']=span.start_time
  edges.setdefault(len(text),{})['end']=span.end_time
  for index,edge in edges.items():result.setdefault(offset+index,{}).update(edge)
  cursor=offset+len(text)
 if cursor!=len(cue.text):return base_map(cue,original)
 result.setdefault(0,{})['start']=cue.start_time
 result.setdefault(len(cue.text),{})['end']=cue.end_time
 return result
