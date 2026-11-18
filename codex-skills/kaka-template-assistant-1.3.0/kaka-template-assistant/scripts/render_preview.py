"""Preview using the actual ASS exporter and synthesis thread, with a frozen receipt."""
import argparse,copy,hashlib,json,os,subprocess,sys,re
from pathlib import Path
# Embedded Python may omit the entry script directory from sys.path.
sys.path.insert(0,str(Path(__file__).resolve().parent))
from template_bridge import load_app,build,execution_sha,DEFAULT_ROOT

def digest(path):
 h=hashlib.sha256()
 with Path(path).open('rb') as f:
  for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
 return h.hexdigest()
def runtime_snapshot(root):
 root=Path(root)
 files=['app/core/program_text_rules.py','app/core/template_readiness.py','app/core/customer_execution.py','app/core/customer_run.py','app/core/template_rules.py','app/thread/subtitle_thread.py','app/core/subtitle_processor/translation_response.py','app/core/bk_asr/asr_data.py','app/core/utils/ass_auto_wrap.py','app/core/utils/ass_effects.py','app/core/utils/video_utils.py','app/thread/video_synthesis_thread.py','app/view/subtitle_style_interface.py','app/core/utils/delivery_policy.py','resource/bin/ffmpeg.exe']
 result={f:digest(root/f) for f in files if (root/f).is_file()}
 result.update({f.relative_to(root).as_posix():digest(f) for f in sorted((root/'resource/fonts').glob('*')) if f.suffix.lower() in ('.ttf','.otf','.ttc')})
 # Local font provider differs by PC: previews need revalidation on a different computer.
 result['computer']=os.environ.get('COMPUTERNAME','unknown')
 inventory=[]
 for label,folder in [('system',Path(os.environ.get('WINDIR','C:/Windows'))/'Fonts'),('user',Path(os.environ.get('LOCALAPPDATA',''))/'Microsoft/Windows/Fonts')]:
  for f in sorted(folder.glob('*')):
   if f.is_file():
    st=f.stat();inventory.append((label,f.name,st.st_size,st.st_mtime_ns))
 result['font-inventory']=hashlib.sha256(json.dumps(inventory,ensure_ascii=False).encode()).hexdigest()
 for name in ('template_bridge.py','style_adapter.py','render_preview.py','preflight.py'):
  result['skill/'+name]=digest(Path(__file__).resolve().parent/name)
 return result

def check_fonts(root,style,layout):
 import re
 from PyQt5.QtGui import QGuiApplication,QFontDatabase
 app=QGuiApplication.instance() or QGuiApplication([])
 for path in (Path(root)/'resource/fonts').glob('*'):
  if path.suffix.lower() in ('.ttf','.otf','.ttc'):QFontDatabase.addApplicationFont(str(path))
 wanted=[]
 for line in style.splitlines():
  match=re.match(r'^Style:\s*(Default|Secondary),',line)
  if match and (match.group(1)=='Default' or layout in ('原文在上','译文在上')):wanted.append(line.split(',')[1])
 families={f.casefold() for f in QFontDatabase().families()}
 missing=[font for font in wanted if font.casefold() not in families]
 if missing:raise ValueError('缺少确认字体，未静默替换；请主动询问选择已安装字体或安装缺少字体：'+','.join(missing))
 return {'requested':wanted,'available':True}

def validate_receipt(path,draft,plan,root):
 receipt=json.loads(Path(path).read_text(encoding='utf-8'))
 if receipt.get('schema')!='kaka-preview/v1' or not receipt.get('all_decoded_frames_equal'):raise ValueError('缺少真实合成比对记录')
 if receipt['execution_sha256']!=execution_sha(draft,plan.get('canvas',{'width':1080,'height':1920})):raise ValueError('预览与当前参数不一致，需重新预览')
 if receipt['runtime']!=runtime_snapshot(root):raise ValueError('渲染程序、字体或电脑已变化，需重新验证预览')
 for file,expected in receipt['files'].items():
  if not Path(file).is_file() or digest(file)!=expected:raise ValueError('预览素材或结果已变化：'+Path(file).name)
 return receipt

def render(plan,subtitle_json,out,root,video=None,image=None,sample_kind='illustrative'):
 out=Path(out).resolve()
 if out.exists() and any(out.iterdir()):raise ValueError('预览输出目录非空，请使用新目录，保留原预览')
 out.mkdir(parents=True,exist_ok=True)
 root=Path(root);os.environ['PATH']=str(root/'resource/bin')+os.pathsep+os.environ['PATH']
 qt_plugins=root/'runtime/Lib/site-packages/PyQt5/Qt5/plugins'
 if os.name=='nt':
  os.environ.setdefault('QT_QPA_PLATFORM','windows')
  os.environ.setdefault('QT_QPA_PLATFORM_PLUGIN_PATH',str(qt_plugins/'platforms'))
  os.environ.setdefault('QT_PLUGIN_PATH',str(qt_plugins))
 store,requirements,assert_base=load_app(root);draft,report=build(plan,store,requirements,assert_base)
 from preflight import inspect_plan
 preflight=inspect_plan(root,draft,plan)
 if not preflight['passed']:raise ValueError('请先解决前置能力缺口：'+str(preflight['errors']))
 if plan.get('pending'):raise ValueError('还有未解决问题，请先主动提问，不生成假完成的预览')
 if draft['settings'].get('need_video') and draft['settings'].get('soft_subtitle'):raise ValueError('软字幕由播放器控制样式，不能承诺预览外观一致；请先明确硬字幕或仅SRT交付')
 from app.core.bk_asr.asr_data import ASRData
 from app.core.customer_templates import style_with_line_limit
 from app.core.entities import SynthesisTask,SynthesisConfig
 from app.thread.video_synthesis_thread import VideoSynthesisThread
 from app.core.utils.video_utils import add_subtitles,get_video_info
 from app.core.utils.delivery_policy import read_policy,finalize
 from PyQt5.QtGui import QGuiApplication
 app=QGuiApplication.instance() or QGuiApplication([])
 data=ASRData.from_json(json.loads(Path(subtitle_json).read_text(encoding='utf-8-sig')))
 if not data.segments:raise ValueError('预览台词为空')
 end=max(c.end_time for c in data.segments);previous=0
 for c in data.segments:
  if c.start_time<previous or c.end_time<=c.start_time:raise ValueError('预览台词时间无效或重叠')
  previous=c.end_time
 if end>10000 or data.segments[0].start_time<0:raise ValueError('代表预览需为0~10秒的片段，不能直接整集渲染')
 style=style_with_line_limit(draft);layout=draft['settings'].get('subtitle_layout','仅原文');policy=read_policy(style)
 from app.core.customer_execution import compile_profile
 from app.core.template_rules import apply_display_rules
 from app.core.subtitle_processor.optimize import normalize_arabic_numbers
 from app.core.utils.cn_general_policy import soften
 contract=compile_profile(draft)
 apply_display_rules(data,contract['text_rules'],normalize_arabic_numbers,soften)
 font_check=check_fonts(root,style,layout)
 if policy:
  data,check=finalize(data,policy,layout,draft['settings'].get('need_translate',False))
  (out/'rule-check.json').write_text(json.dumps(check,ensure_ascii=False,indent=2),encoding='utf-8')
  if not check['passed']:raise ValueError('预览字幕未通过程序规则检查：'+str(check['issues'][0]))
 canvas=plan.get('canvas') or {'width':1080,'height':1920};width,height=canvas['width'],canvas['height']
 ff=str(root/'resource/bin/ffmpeg.exe')
 def call(args):
  p=subprocess.run([ff,'-hide_banner','-loglevel','error','-y',*map(str,args)],capture_output=True)
  if p.returncode:raise ValueError(p.stderr.decode('utf-8',errors='replace')[-1200:])
  return p.stdout
 duration=max(1,end/1000)
 if video and image:raise ValueError('背景视频和图片只选一种')
 if video:
  source=Path(video).resolve();info=get_video_info(str(source)) or {}
  if (info.get('width'),info.get('height'))!=(width,height):raise ValueError('素材与确认画布不一致，请先核对，不自动拉伸')
  if info.get('duration_seconds',100)>12:raise ValueError('请先提取不超过10秒的代表片段，并保留来源')
 else:
  source=out/'source.mp4'
  if image:
   args=['-loop','1','-i',str(Path(image).resolve()),'-vf',f'scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2','-r','24','-t',str(duration)]
  else:args=['-f','lavfi','-i',f'color=c=0x303a44:s={width}x{height}:r=24:d={duration}']
  call(args+['-c:v','libx264','-pix_fmt','yuv420p',source])
 canonical=out/'canonical.json';canonical.write_text(json.dumps(data.to_json(),ensure_ascii=False,indent=2),encoding='utf-8')
 ass=out/'confirmed.ass';data.to_ass(style_str=style,layout=layout,save_path=str(ass))
 data.to_srt(layout=layout,save_path=str(out/'sample.srt'))
 # Report the effective typography, not only the configured base style.
 ass_text=ass.read_text(encoding='utf-8-sig')
 sizes={l.split(',')[0].replace('Style: ',''):float(l.split(',')[2]) for l in ass_text.splitlines() if l.startswith('Style: ')}
 shrinking=[];seen=set()
 for l in ass_text.splitlines():
  if not l.startswith('Dialogue: '):continue
  cols=l.split(',',9)
  if len(cols)!=10:continue
  role=cols[3];base_size=sizes.get(role)
  for v in re.findall(r'\\fs([\d.]+)',cols[9]):
   effective=float(v);key=(cols[1],cols[2],role,effective)
   if base_size and effective<base_size-0.01 and key not in seen:
    seen.add(key);shrinking.append({'start':cols[1],'end':cols[2],'style':role,'base_ass_size':base_size,'effective_ass_size':effective,'ratio':round(effective/base_size,3)})
 layout_audit={'sample_kind':sample_kind,'automatic_font_reduction':shrinking,'requires_visual_review':bool(shrinking),'note':'缩小比例不是可读性验收；必须展示边界图片，不能以逐帧一致替代排版判断。'}
 (out/'layout-audit.json').write_text(json.dumps(layout_audit,ensure_ascii=False,indent=2),encoding='utf-8')
 preview=out/'preview.mp4';formal=out/'verification.mp4'
 add_subtitles(str(source),str(ass),str(preview),crf=18)
 # Independently regenerate render instructions from canonical data and selected template.
 reread=ASRData.from_json(json.loads(canonical.read_text(encoding='utf-8')));check_ass=out/'verification.ass';reread.to_ass(style_str=style,layout=layout,save_path=str(check_ass))
 if digest(ass)!=digest(check_ass):raise ValueError('独立回读的渲染指令不一致')
 task=SynthesisTask(video_path=str(source),subtitle_path=str(check_ass),output_path=str(formal),synthesis_config=SynthesisConfig(need_video=True,soft_subtitle=False,video_quality='18'))
 task.customer_template_snapshot=draft;task.editor_subtitle_sha256=digest(check_ass);task.apply_customer_style=False
 worker=VideoSynthesisThread(task);errors=[];worker.error.connect(errors.append);worker.run()
 if errors or not formal.is_file():raise ValueError('真实合成线程验证失败：'+str(errors))
 def framehash(p):return call(['-i',p,'-map','0:v:0','-pix_fmt','rgba','-f','hash','-hash','sha256','-']).decode().strip()
 same=framehash(preview)==framehash(formal)
 if not same:raise ValueError('预览与正式合成解码帧不一致，不能发布')
 frame=out/'preview.png';call(['-i',preview,'-ss',str((data.segments[0].start_time+min(250,(data.segments[0].end_time-data.segments[0].start_time)//2))/1000),'-frames:v','1',frame])
 files=[source,canonical,ass,check_ass,preview,formal,frame,out/'sample.srt',out/'layout-audit.json',Path(subtitle_json).resolve()]
 if image:files.append(Path(image).resolve())
 receipt={'layout_audit':layout_audit,'schema':'kaka-preview/v1','execution_sha256':report['execution_sha256'],'canvas':{'width':width,'height':height},'runtime':runtime_snapshot(root),'files':{str(f.resolve()):digest(f) for f in files},'all_decoded_frames_equal':True,'sample_kind':sample_kind,'background':'video' if video else 'image' if image else 'standard','style_scope':'hard_subtitles' if draft['settings'].get('need_video') else 'ASS_preview_only_SRT_has_no_style','font_check':font_check,'renderer':'installed ASRData + add_subtitles + VideoSynthesisThread','timing_acceptance':'未验证真实音频对齐' if sample_kind=='illustrative' else '只验证所提供片段；音频对齐另查','asr_translation_acceptance':'本工具不调用ASR/翻译，不把人工示例当成链路验证','production_handoff':'正式新任务应保留原译文身份并冻结ASS；Skill本身不修改软件任务链路'}
 (out/'receipt.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8')
 return {'receipt':str(out/'receipt.json'),'image':str(frame),'video':str(preview),'all_decoded_frames_equal':True,'sample_kind':sample_kind}

def main():
 p=argparse.ArgumentParser();p.add_argument('--app-root',default=os.environ.get('KAKA_APP_ROOT',DEFAULT_ROOT));p.add_argument('--plan',required=True);p.add_argument('--subtitles',required=True);p.add_argument('--output',required=True);p.add_argument('--video');p.add_argument('--image');p.add_argument('--sample-kind',choices=['illustrative','actual'],default='illustrative');a=p.parse_args()
 try:
  import contextlib
  with contextlib.redirect_stdout(sys.stderr):r=render(json.loads(Path(a.plan).read_text(encoding='utf-8-sig')),a.subtitles,a.output,a.app_root,a.video,a.image,a.sample_kind)
  print(json.dumps(r,ensure_ascii=False))
 except Exception as e:raise SystemExit(str(e))
if __name__=='__main__':main()
