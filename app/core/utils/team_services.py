"""Shared service credentials, separate from workstation and task preferences."""
import json
import logging
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from app.config import SHARED_APPDATA_PATH
from app.core.concurrency import shared_lock, atomic_text

STORE=SHARED_APPDATA_PATH/'team_services.json'
SERVICE_FIELDS=(
 'llm_service','openai_model','openai_api_key','openai_api_base',
 'silicon_cloud_model','silicon_cloud_api_key','silicon_cloud_api_base',
 'deepseek_model','deepseek_api_key','deepseek_api_base',
 'ollama_model','ollama_api_key','ollama_api_base',
 'lm_studio_model','lm_studio_api_key','lm_studio_api_base',
 'gemini_model','gemini_api_key','gemini_api_base',
 'chatglm_model','chatglm_api_key','chatglm_api_base',
 'public_model','public_api_key','public_api_base',
 'multilingual_asr_base','multilingual_asr_key','multilingual_asr_model',
 'whisper_api_base','whisper_api_key','whisper_api_model','deeplx_endpoint',
)
DEFAULT_FIELDS=('transcribe_model',)
FIELDS=SERVICE_FIELDS+DEFAULT_FIELDS
logger=logging.getLogger(__name__)


def snapshot(config):
    return {name:getattr(config,name).serialize() for name in FIELDS if hasattr(config,name)}


def is_dirty(config):
    baseline=getattr(config,'_team_baseline',None)
    return baseline is not None and any(getattr(config,n).serialize()!=baseline.get(n) for n in SERVICE_FIELDS if hasattr(config,n))


def status_text(config):
    return getattr(config,'_team_status','尚未发布团队公共配置') + (' · 本机修改尚未共享' if is_dirty(config) else '')


def read_shared():
    data=json.loads(STORE.read_text(encoding='utf-8-sig'))
    if not isinstance(data,dict) or data.get('schema')!=1 or not isinstance(data.get('services'),dict):
        raise ValueError('团队服务配置格式无效，未修改本机配置')
    if type(data.get('revision')) is not int or data['revision']<1:
        raise ValueError('团队服务配置版本无效')
    return data


def validate(config, values):
    decoded={}
    for name,value in values.items():
        if name not in FIELDS or not hasattr(config,name):continue
        item=getattr(config,name)
        value=item.serializer.deserialize(value)
        if not item.validator.validate(value):raise ValueError('团队配置参数无效：'+name)
        decoded[name]=value
    return decoded


def reload_shared(config, force=False, defaults=False):
    """Called on startup and before new tasks; existing task objects stay frozen."""
    if not STORE.exists():
        config._team_status='尚未发布团队公共配置'
        return False
    data=read_shared()
    previous=getattr(config,'_team_revision',0)
    if not force and data['revision']==previous:return False
    baseline=getattr(config,'_team_baseline',None)
    if not force and baseline is not None and any(getattr(config,n).serialize()!=baseline.get(n) for n in SERVICE_FIELDS if hasattr(config,n)):
        raise ValueError('团队服务配置已有更新，本机也有未发布的修改；请在服务与设置中重新读取团队配置后再开始任务')
    decoded=validate(config,data['services'])
    # Validate the whole document before applying a single item.
    for name,value in decoded.items():
        if defaults or name not in DEFAULT_FIELDS:
            config.set(getattr(config,name),value,save=False)
    config.save()
    config._team_revision=data['revision']
    config._team_baseline=snapshot(config)
    config._team_status=f"团队公共配置 v{data['revision']} · {data.get('updated_at','')}"
    return True


def publish(config):
    values=snapshot(config)
    validate(config,values)
    if values.get('multilingual_asr_base'):
        parsed=urlsplit(values['multilingual_asr_base'])
        if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.query or parsed.fragment:
            raise ValueError('请先填写正确的云端 ASR 地址')
        if not values.get('multilingual_asr_model') or not values.get('multilingual_asr_key'):
            raise ValueError('云端 ASR 地址、模型和密钥需填写完整后再更新团队配置')
    with shared_lock(STORE,wait_seconds=5):
        current=read_shared() if STORE.exists() else {'revision':0}
        expected=getattr(config,'_team_revision',0)
        if current['revision']!=expected:
            raise ValueError('团队配置已被更新，请先重新读取，避免覆盖其他人的新配置')
        data={'schema':1,'revision':expected+1,'updated_at':datetime.now().isoformat(timespec='seconds'),'services':values}
        atomic_text(STORE,json.dumps(data,ensure_ascii=False,indent=2))
    config._team_revision=data['revision']
    config._team_baseline=snapshot(config)
    config._team_status=f"团队公共配置 v{data['revision']} · {data['updated_at']}"
    return data['revision']


def initialize(config):
    try:
        reload_shared(config,force=True,defaults=True)
    except (OSError,ValueError,TypeError) as exc:
        # Retain the last local, validated copy; never switch backend on failure.
        config._team_status='公共配置暂未读取，保留上次本机配置'
        logger.warning('团队配置暂未读取；保留本机缓存（%s）',type(exc).__name__)


def refresh_for_new_task(config):
    try:
        return reload_shared(config)
    except OSError:
        # SMB may disappear briefly; already loaded credentials remain usable.
        config._team_status='共享盘暂不可用，当前任务使用上次服务配置'
        return False
