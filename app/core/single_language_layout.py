"""Word-preserving pagination measured with the production ASS wrapper."""
import re
from app.core.utils.ass_auto_wrap import parse_ass_info,english_safe_width,auto_wrap_text

def latin_text(text):
 return bool(re.search(r'[A-Za-z]',text)) and not re.search(r'[\u3400-\u9fff\u0600-\u06ff\u0900-\u097f]',text)

def limits(style):
 width,sizes=parse_ass_info(style+'\n[Events]\n')
 canvas=re.search(r'^; VideoCaptionerScriptCanvas:\s*(\d+),',style,re.M)
 if canvas:width=int(canvas.group(1))
 words=re.search(r'^; VideoCaptionerEnglishLineWords:\s*(\d+)',style,re.M)
 return english_safe_width(style,width),sizes.get('Default',40),int(words.group(1)) if words else 6

def wrapped(text,style):
 width,size,words=limits(style)
 return auto_wrap_text(text,width,size,words,'; VideoCaptionerEnglishSemanticWrap: 1' in style,True)

def pages(text,style,max_words):
 words=text.replace(r'\N',' ').split();maximum=max(1,int(max_words));result=[]
 abbreviations={'mr.','mrs.','ms.','dr.','prof.','sr.','jr.','st.','vs.','etc.'}
 edges=[0]
 for i,word in enumerate(words[:-1],1):
  token=word.rstrip('\"\'”’')
  if re.search(r'[.!?]$',token) and token.lower() not in abbreviations and not re.fullmatch(r'(?:[A-Za-z]\.){2,}',token):edges.append(i)
 edges.append(len(words))
 attached={'a','an','the','to','of','for','with','without','my','your','his','her','their','and','or','but',"she's","he's","it's","i'm","we're","they're"}
 for begin,end in zip(edges,edges[1:]):
  start=begin
  while start<end:
   best=start+1
   for stop in range(start+1,min(end,start+maximum)+1):
    if wrapped(' '.join(words[start:stop]),style).count(r'\N')<=1:best=stop
   if best<end:
    while best>start+1 and words[best-1].lower().strip('.,!?;:') in attached:best-=1
   result.append(' '.join(words[start:best]));start=best
 return result

def audit_export(data,style,layout):
 """Inspect actual generated events, including renderer-inserted line breaks."""
 from app.core.utils.line_geometry import restore
 ass=restore(data.to_ass(style_str=style,layout=layout));checked=0
 for line in ass.splitlines():
  if not line.startswith('Dialogue:'):continue
  fields=line.split(',',9)
  if len(fields)!=10 or fields[3]!='Default':continue
  text=re.sub(r'\{[^}]*\}','',fields[9])
  if latin_text(text):
   checked+=1
   if text.count(r'\N')>1:raise ValueError('英文最终导出超过两行，未发布异常字幕')
 return {'stage':'single_language_export','passed':True,'latin_events_checked':checked,'max_latin_lines':2,'font_reduction':False}
