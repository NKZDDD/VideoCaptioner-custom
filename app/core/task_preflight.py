"""Readiness checks only: no upload, inference, automatic binding or rule changes."""
import copy,hashlib,json,tempfile
from pathlib import Path
from urllib.parse import urlparse

BLOCKED='检查未通过'
CHECKING='开始前检查'

def stamp(path):
    stat=Path(path).stat();return {'size':stat.st_size,'mtime_ns':stat.st_mtime_ns}

def validate_stamp(task):
    previous=getattr(task,'preflight_stamp',None)
    if previous and stamp(task.file_path)!=previous:
        raise ValueError('素材在检查后发生变更，请重新检查；不会使用旧恢复点继续')

def writable_directory(path):
    # A uniquely named probe never opens or truncates an existing customer file.
    with tempfile.TemporaryFile(prefix='.kaka-check-',dir=str(path)) as handle:
        handle.write(b'kaka');handle.flush()

def valid_url(value):
    try:
        parsed=urlparse(str(value));return parsed.scheme in ('http','https') and bool(parsed.hostname) and not parsed.username and not parsed.password
    except ValueError:return False

class PreflightContext:
    def __init__(self):self.services={}
    def capabilities(self,base,key):
        import requests
        identity=(base,hashlib.sha256(str(key).encode()).hexdigest())
        if identity not in self.services:
            try:
                with requests.Session() as session:
                    session.trust_env=False
                    response=session.get(base.rstrip('/')+'/cloud/capabilities',headers={'Authorization':'Bearer '+str(key)},timeout=(3,5),allow_redirects=False)
                if response.status_code in (401,403):value=(None,'云端认证失败，请检查服务密钥')
                elif response.status_code!=200:value=(None,'当前地址无法提供云端全流程能力，请检查开机状态和服务地址')
                else:
                    data=response.json();value=(data,'') if isinstance(data,dict) else (None,'云端能力返回格式无效')
            except (requests.RequestException,ValueError):value=(None,'云端暂不可连接，请确认机器已开机、地址可用后重新检查')
            self.services[identity]=value
        return self.services[identity]

def script_binding(path):
    from app.core import script_reference_store as store
    text=store.load_exact_video_script_reference(path)
    info={}
    try:
        index=json.loads(store.INDEX_PATH.read_text(encoding='utf-8'))
        info=store.exact_video_entry(path)
    except (OSError,ValueError,AttributeError,TypeError):pass
    return text,info

def inspect_task(task,context=None):
    from app.core.task_contract import freeze_profile
    from app.core.task_factory import TaskFactory
    from app.core.llm_permissions import compile_permissions
    from app.core.entities import BatchTaskType,TranscribeModelEnum,TranslatorServiceEnum
    context=context or PreflightContext()
    result={'path':task.file_path,'ok':False,'errors':[],'warnings':[],'checks':[],'script_source':'','script_preview':'','stamp':None}
    def error(section,reason,fix):result['errors'].append({'section':section,'reason':reason,'fix':fix})
    source=Path(task.file_path)
    try:
        if not source.is_file():raise FileNotFoundError()
        if source.stat().st_size<=0:raise ValueError()
        with source.open('rb') as handle:
            handle.read(65536);handle.seek(max(0,source.stat().st_size-65536));handle.read(65536)
        result['stamp']=stamp(source)
        if getattr(task,'preflight_stamp',None) and result['stamp']!=task.preflight_stamp:
            error('素材','素材与上次执行时不同，旧恢复点不能继续使用','移出旧任务后按新素材创建任务')
        else:result['checks'].append('素材非空且可读取（不代表已完成音视频解码检查）')
    except (OSError,ValueError):error('素材','文件不存在、为空或无法读取','检查共享盘连接、文件路径和读取权限')
    try:writable_directory(source.parent);result['checks'].append('素材目录可写，可用于保存字幕和成片')
    except OSError:error('输出位置','无法在素材目录保存文件','恢复共享盘连接或调整目录写入权限')
    try:
        profile=freeze_profile(copy.deepcopy(task.customer_profile))
        if not profile.get('enabled',True):raise ValueError('客户模板已停用')
        result['profile']=profile
        kind=task.task_type
        checkpoint=getattr(task,'checkpoint',None)
        checkpoint_kind=checkpoint[0] if checkpoint else None
        if checkpoint:
            try:
                saved=Path(checkpoint[1][0].output_path if checkpoint_kind=='transcribed' else checkpoint[1][2])
                if not saved.is_file() or saved.stat().st_size<=0:raise ValueError()
                result['checks'].append('已保存的恢复材料可用；继续执行尚未完成的步骤')
            except (OSError,ValueError,TypeError,IndexError,AttributeError):error('恢复材料','之前保存的原始识别或字幕不可用','恢复对应文件，或移出旧任务后重新创建')
        if kind==BatchTaskType.SUBTITLE and not result['errors']:
            from app.core.bk_asr.asr_data import ASRData
            try:
                data=ASRData.from_subtitle_file(str(source))
                if not data.segments or any(cue.end_time<=cue.start_time or cue.start_time<0 for cue in data.segments):raise ValueError()
                result['checks'].append('已有字幕可解析，时间区间有效')
            except (OSError,ValueError,TypeError):error('字幕素材','字幕无法解析、没有有效内容或时间区间无效','修复字幕文件后重新检查')
        needs_text=kind in (BatchTaskType.SUBTITLE,BatchTaskType.TRANS_SUB,BatchTaskType.FULL_PROCESS) and checkpoint_kind!='subtitled'
        synthesis_only=kind==BatchTaskType.RECOMPOSE or bool(profile.get('cloud_synthesis_only'))
        mode=profile.get('text_source_mode')
        if needs_text and not synthesis_only:
            reference,info=(profile['_script_reference'],profile.get('_preflight_binding',{})) if '_script_reference' in profile else script_binding(task.file_path)
        else:reference,info='',{}
        if needs_text:
            from app.core.script_reference_store import load_exact_bilingual_reference
            if '_bilingual_reference' not in profile:profile['_bilingual_reference']=load_exact_bilingual_reference(task.file_path)
            profile['_script_reference']=reference
            profile['_preflight_binding']=copy.deepcopy(info)
        result['script_source']=str(info.get('source_script') or ('已绑定本素材剧本' if reference else '未绑定（当前模式可选）'))
        result['script_preview']=reference[:100]
        if needs_text and mode in ('strict','hybrid','script_correct') and not reference:
            error('剧本','此模板需要本集剧本，但没有读取到有效绑定','点击批量绑定剧本，将本集素材与对应剧本绑定后重新检查')
        if reference and info.get('episode_number') is not None and info.get('source_episode_number') is not None and info['episode_number']!=info['source_episode_number']:
            error('剧本','素材与绑定剧本的集数标记不一致','核对绑定关系，不能只依靠相似文件名继续处理')
        if kind!=BatchTaskType.TRANSCRIBE:
            style=str(profile.get('style_template') or '')
            if profile.get('id') and ('[V4+ Styles]' not in style or 'Format:' not in style or 'Style:' not in style):
                error('模板外观','客户模板缺少完整字幕外观','到客户模板页检查并保存字幕外观')
        sub=None
        if needs_text and not synthesis_only:
            sub=TaskFactory.create_subtitle_task(str(source.with_suffix('.srt')),str(source),need_next_task=True,customer_profile=profile).subtitle_config
            if sub.need_translate and sub.frozen_bilingual_reference and sub.translator_service!=TranslatorServiceEnum.OPENAI:
                error('双语参考','此绑定包含中文校译参考，需要LLM大模型翻译服务','在模板选择LLM大模型翻译；不会静默丢弃中文参考')
            permissions=compile_permissions(profile['settings'],mode)
            uses_llm=any(permissions[k] for k in ('script_matching','voice_script_assist','general_correction','llm_semantic_split')) or (sub.need_translate and sub.translator_service==TranslatorServiceEnum.OPENAI)
            if uses_llm and (not valid_url(sub.base_url) or not str(sub.llm_model).strip()):
                error('文字服务','此模板需要LLM，但地址或模型没有配置完整','到服务设置中填写当前使用的LLM地址和模型')
            elif uses_llm and not str(sub.api_key).strip() and urlparse(sub.base_url).hostname not in ('localhost','127.0.0.1','::1'):
                error('文字服务','此模板需要LLM，但没有配置服务密钥','到服务设置中填写当前使用的LLM密钥')
            elif uses_llm:result['warnings'].append('LLM仅核对配置完整性，本次检查不发送客户台词，也不发起计费请求')
        if synthesis_only:
            ass=source.with_suffix('.ass')
            try:
                if ass.stat().st_size>1000000:raise ValueError()
                text=ass.read_text(encoding='utf-8-sig')
                if '[Events]' not in text or '[Script Info]' not in text:raise ValueError()
                result['checks'].append('仅重新合成使用的同名ASS存在且具有必要格式字段')
            except (OSError,ValueError):error('合成字幕','缺少有效的同名ASS字幕','在字幕编辑器保存ASS后重新检查；不会重新转录')
        if kind in (BatchTaskType.TRANSCRIBE,BatchTaskType.TRANS_SUB,BatchTaskType.FULL_PROCESS) and not checkpoint and not profile.get('cloud_full_process'):
            tc=TaskFactory.create_transcribe_task(task.file_path,need_next_task=True,customer_profile=profile).transcribe_config
            if tc.transcribe_model==TranscribeModelEnum.MULTILINGUAL_ASR:
                if not valid_url(tc.multilingual_asr_base) or not str(getattr(tc,'multilingual_asr_model','')).strip():
                    error('转录服务','云端转录地址或模型配置不完整','在服务设置中核对当前转录接口配置')
                else:result['warnings'].append('仅转录接口已核对地址与模型配置；未上传音频进行试转录')
        if profile.get('cloud_full_process'):
            trans=TaskFactory.create_transcribe_task(task.file_path,need_next_task=True,customer_profile=profile).transcribe_config
            if trans.transcribe_model!=TranscribeModelEnum.MULTILINGUAL_ASR:error('转录服务','云端全流程需要选择多语言ASR云端服务','在模板中核对转录服务，不会自动切换到本地')
            elif not valid_url(trans.multilingual_asr_base):error('云端服务','云端地址无效','在服务设置中填写有效的HTTP或HTTPS地址')
            elif not result['errors']:
                caps,message=context.capabilities(trans.multilingual_asr_base,trans.multilingual_asr_key)
                if message:error('云端服务',message,'修正服务配置或开机后重新检查')
                else:
                    required=['full_process','cancellation']+(['synthesis_only'] if synthesis_only else ['script_correction_v1'] if mode=='script_correct' else [])
                    missing=[key for key in required if not caps.get(key)]
                    labels={'full_process':'云端全流程','cancellation':'取消任务','synthesis_only':'仅重新合成','script_correction_v1':'原声定位＋剧本校正'}
                    if missing:error('服务能力','当前云端不支持所选功能：'+'、'.join(labels[key] for key in missing),'更新相应服务后重新检查；不会回退到其他模式')
                    try:maximum=int(caps['max_upload_bytes'])
                    except (KeyError,TypeError,ValueError):maximum=0
                    if maximum<=0:error('服务能力','云端未声明有效的上传限制','检查服务版本与能力接口')
                    elif result['stamp'] and result['stamp']['size']>maximum:error('素材','文件超过当前云端单文件上传限制','使用满足限制的素材或调整服务限制后重新检查')
                    if not missing and maximum>0:result['checks'].append('云端当前能力支持所选流程；正式上传时仍会复核')
        modes={'voice':'原声为主','script_correct':'原声定位＋剧本校正','strict':'剧本完全替换','hybrid':'剧本混合匹配（兼容模式）'}
        result['checks'].append('模板已固定：'+str(profile.get('name','自定义'))+'；模式：'+modes.get(mode,'常规流程'))
    except (OSError,ValueError,RuntimeError,KeyError,TypeError,AttributeError) as exc:
        error('模板配置','模板或处理配置无法编译：'+str(exc),'检查客户模板与服务配置后重新检查')
    result['ok']=not result['errors']
    return result

def check_batch(tasks):
    from app.core.task_contract import freeze_profile
    context=PreflightContext()
    results=[];profiles={}
    for task in tasks:
        # One service/config snapshot for identical batch templates; scripts remain per-file.
        try:
            identity=json.dumps(task.customer_profile,ensure_ascii=False,sort_keys=True,default=str)
            if identity not in profiles:profiles[identity]=freeze_profile(task.customer_profile)
            task.customer_profile=copy.deepcopy(profiles[identity])
        except (OSError,ValueError,RuntimeError,TypeError,AttributeError):pass
        results.append((task,inspect_task(task,context)))
    return results

def detail(result):
    lines=['检查通过' if result['ok'] else '检查未通过']
    lines.extend('【'+e['section']+'】'+e['reason']+'\n处理办法：'+e['fix'] for e in result['errors'])
    if result['script_source']:lines.append('本集剧本：'+result['script_source'])
    if result['script_preview']:lines.append('台词摘要：'+result['script_preview'])
    lines.extend(result['checks']);lines.extend(result['warnings'])
    return '\n\n'.join(lines)
