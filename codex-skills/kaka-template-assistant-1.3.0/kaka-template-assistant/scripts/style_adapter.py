"""Reuse calibrated functions from the installed style editor; no alternate conversion."""
import math,re

def apply(style,patch):
 if not patch:return style
 from app.view.subtitle_style_interface import SubtitleStyleInterface as UI,calibrated_margin
 from PyQt5.QtGui import QColor
 if not isinstance(patch,dict) or set(patch)-{'Default','Secondary','capcut_y'}:raise ValueError('外观仅允许Default、Secondary、capcut_y')
 lines=style.splitlines()
 for role in ('Default','Secondary'):
  fields=patch.get(role,{})
  if not isinstance(fields,dict):raise ValueError('外观参数必须是对象')
  allowed={'capcut_y','bold','solid_shadow','font','size','color','outline_width','outline_color','shadow_color','shadow_opacity','shadow_blur','shadow_distance','shadow_angle'}
  if set(fields)-allowed:raise ValueError('未知外观参数；不猜测换算')
  if not fields:continue
  idx=next((i for i,l in enumerate(lines) if re.match(r'^Style:\s*'+role+',',l)),None)
  if idx is None:raise ValueError('基础样式缺少'+role)
  row=lines[idx].split(',')
  if len(row)<23:raise ValueError('样式字段不完整')
  for key,value in fields.items():
   if key=='font':
    if not isinstance(value,str) or not re.fullmatch(r'[\w\u4e00-\u9fff .()\-]{1,80}',value):raise ValueError('字体名格式无效')
    row[1]=value
   elif key in ('bold','solid_shadow'):
    if type(value) is not bool:raise ValueError(key+' must be a boolean')
    if key=='bold':row[7]='-1' if value else '0'
   elif key in ('color','outline_color','shadow_color'):
    color=QColor(value)
    if not color.isValid():raise ValueError('颜色无效')
    old=row[{'color':3,'outline_color':5,'shadow_color':6}[key]]
    opacity=fields.get('shadow_opacity',100) if key=='shadow_color' else round((255-int(old.replace('&H','').zfill(8)[:2],16))*100/255)
    row[{'color':3,'outline_color':5,'shadow_color':6}[key]]=UI._ass_color(color,opacity)
   else:
    if type(value) not in (int,float) or not math.isfinite(value):raise ValueError('外观参数必须是有限数字')
    if key=='size':
     if not 1<=value<=200:raise ValueError('剪映字号范围1~200')
     row[2]=UI._number(UI._capcut_font_to_ass(value))
    elif key=='capcut_y':
     margin=calibrated_margin(value,1920)
     if not 0<=margin<=10000:raise ValueError('换算位置超出软件范围')
     row[21]=str(margin)
    elif key=='outline_width':
     if not 0<=value<=100:raise ValueError('描边范围0~100')
     row[16]=UI._number(UI._capcut_outline_to_ass(value))
    elif key=='shadow_opacity':
     if not 0<=value<=100:raise ValueError('阴影透明度范围0~100')
     old=row[6].replace('&H','').zfill(8);row[6]='&H'+f'{round(255*(100-value)/100):02X}'+old[2:]
    elif key=='shadow_angle' and not -360<=value<=360:raise ValueError('阴影角度超范围')
    elif key in ('shadow_blur','shadow_distance') and not 0<=value<=100:raise ValueError('阴影范围0~100')
  lines[idx]=','.join(row)
  if 'capcut_y' in fields:
   prefix='; VideoCaptionerPosition: '+role+',margin='
   lines=[l for l in lines if not l.startswith(prefix)]+[prefix+row[21]]
  if 'bold' in fields:
   prefix='; VideoCaptionerBold: '+role+','
   lines=[l for l in lines if not l.startswith(prefix)]+[prefix+('1' if fields['bold'] else '0')]
  if 'solid_shadow' in fields:
   prefix='; VideoCaptionerSolidShadow: '+role+','
   lines=[l for l in lines if not l.startswith(prefix)]+[prefix+('1' if fields['solid_shadow'] else '0')]
  if 'outline_width' in fields:
   prefix='; VideoCaptionerOutline: '+role+',';lines=[l for l in lines if not l.startswith(prefix)]+[prefix+'width='+UI._number(fields['outline_width'])]
  if any(k in fields for k in ('shadow_blur','shadow_distance','shadow_angle')):
   prefix='; VideoCaptionerEffect: '+role+',';old=next((l for l in lines if l.startswith(prefix)),prefix+'blur=0,distance=0,angle=0');parts=dict(re.findall(r'(blur|distance|angle)=([-\d.]+)',old))
   for k in ('blur','distance','angle'):
    if 'shadow_'+k in fields:parts[k]=UI._number(fields['shadow_'+k])
   lines=[l for l in lines if not l.startswith(prefix)]+[prefix+','.join(k+'='+parts.get(k,'0') for k in ('blur','distance','angle'))]
 if 'capcut_y' in patch:
  y=patch['capcut_y']
  if type(y) not in (int,float) or not math.isfinite(y):raise ValueError('剪映Y必须为有限数字')
  margin=calibrated_margin(y,1920)
  if not 0<=margin<=10000:raise ValueError('换算位置超出软件范围')
  for i,l in enumerate(lines):
   if re.match(r'^Style:\s*(Default|Secondary),',l):
    row=l.split(',');role=row[0].split(':',1)[1].strip()
    if 'capcut_y' not in patch.get(role,{}):
     row[21]=str(margin);lines[i]=','.join(row)
 return '\n'.join(lines)
