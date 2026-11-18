"""Shared customer profiles; credentials and episode scripts are never exported."""
import copy
import datetime
import json
import os
from pathlib import Path
import uuid
import logging
import re

from app.common.config import cfg
from app.config import SHARED_APPDATA_PATH, SESSION_PATH, SUBTITLE_STYLE_PATH
import app.config as app_config
from app.core.concurrency import atomic_text, locked_store
from app.core.utils.text_source_mode import selected_mode

STORE = SHARED_APPDATA_PATH / 'customer_templates'
ACTIVE = SESSION_PATH / 'active_customer.json'
FIELDS = (
    'transcribe_model', 'transcribe_language', 'need_split', 'split_type',
    'need_optimize', 'need_translate', 'target_language', 'translator_service',
    'max_word_count_cjk', 'max_word_count_english', 'needs_remove_punctuation',
    'custom_prompt_text', 'subtitle_style_name', 'subtitle_layout',
    'need_video', 'soft_subtitle', 'video_quality',
    'faster_whisper_model', 'faster_whisper_device', 'faster_whisper_prompt',
)
_task_profiles = {}


def capture_settings():
    return {name: getattr(cfg, name).serialize() for name in FIELDS}


def preset_defaults():
    """Stable program defaults, never another workstation's current settings."""
    return {name: copy.deepcopy(getattr(cfg, name).serializer.serialize(getattr(cfg, name).defaultValue)) for name in FIELDS}


def normalize_profile(profile):
    result = copy.deepcopy(profile)
    values = preset_defaults()
    values.update({key: value for key, value in result.get('settings', {}).items() if key in FIELDS})
    result['settings'] = values
    return result


def list_profiles(include_disabled=False):
    if not STORE.parent.is_dir():
        raise OSError('共享模板库暂不可读取，已保留当前预设，请恢复共享盘连接后重试')
    profiles = []
    for path in STORE.glob('*.json'):
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            if include_disabled or data.get('enabled', True):
                profiles.append(normalize_profile(data))
        except (OSError, ValueError, TypeError) as exc:
            raise OSError('客户模板无法读取，已保留原列表：' + path.name) from exc
    return sorted(profiles, key=lambda item: item.get('name', '').casefold())


@locked_store
def save_profile(data, expected_revision=0):
    data = normalize_profile(data)
    mode = data.get('text_source_mode')
    if mode not in (None, 'voice', 'strict', 'hybrid', 'script_correct'):
        raise ValueError('字幕文字处理方式无效')
    identity = data.get('id') or uuid.uuid4().hex
    if not identity.isalnum():
        raise ValueError('客户模板编号无效')
    path = STORE / (identity + '.json')
    current = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    from app.core.template_permissions import assert_template_write
    assert_template_write(current or data)
    if int(current.get('revision', 0)) != expected_revision:
        raise RuntimeError('此客户模板已被其他人更新，请刷新后再编辑或复制为新模板。')
    if not str(data.get('name', '')).strip():
        raise ValueError('请填写客户模板名称')
    settings = {key: value for key, value in data.get('settings', {}).items() if key in FIELDS}
    for key, value in settings.items():
        item = getattr(cfg, key)
        decoded = item.serializer.deserialize(value)
        if not item.validator.validate(decoded):
            raise ValueError(f'模板参数无效：{key}')
    # Only explicitly supported fields are written; no API keys or local paths.
    result = dict(id=identity, name=str(data['name']).strip(),
        enabled=bool(data.get('enabled', True)), notes=str(data.get('notes', '')),
          glossary=str(data.get('glossary', '')), settings=settings,
          text_source_mode=mode,
        english_line_words=max(3, min(12, int(data.get('english_line_words', 6)))),
        style_template=str(data.get('style_template', '')),
        revision=expected_revision + 1,
        updated_at=datetime.datetime.now().isoformat(timespec='seconds'),
        updated_by=os.environ.get('USERNAME', ''))
    if data.get('program_text_rules'):
        from app.core.program_text_rules import validate
        result['program_text_rules']=validate(data['program_text_rules'])
    imported=data.get('requirements_import')
    if imported:
        if not isinstance(imported,dict) or set(imported)-{'name','rules','pending','source','acknowledged_pending','operator_edits','original_pending','base_template'}:
            raise ValueError('文档导入记录格式无效')
        if not isinstance(imported.get('pending'),list) or len(json.dumps(imported,ensure_ascii=False))>150000:
            raise ValueError('文档导入记录无效或过大')
        if imported['pending'] and result['enabled']:
            raise ValueError('文档仍有待确认要求；请点“核对导入要求”处理后再启用，或保持停用保存草稿')
        result['requirements_import']=copy.deepcopy(imported)
    if current:
        archive = STORE / '.history' / identity
        archive.mkdir(parents=True, exist_ok=True)
        archive_path = archive / ('v' + str(current.get('revision', 0)) + '_' + datetime.datetime.now().strftime('%Y%m%d_%H%M%S_%f') + '.json')
        atomic_text(archive_path, json.dumps(current, ensure_ascii=False, indent=2))
    atomic_text(path, json.dumps(result, ensure_ascii=False, indent=2))
    return result


def active_profile():
    if not ACTIVE.exists():
        seed = getattr(app_config, 'SETTINGS_SEED_PATH', None)
        previous = Path(seed).parent / 'active_customer.json' if seed else None
        if previous and previous.exists():
            try:
                atomic_text(ACTIVE, previous.read_text(encoding='utf-8'))
            except OSError:
                pass
    try:
        data = json.loads(ACTIVE.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    # The shared file is authoritative even if a stale cache has the same
    # or a higher revision (e.g. a deliberate shared rollback).
    identity = str(data.get('id', ''))
    if identity and identity.isalnum():
        try:
            latest = json.loads((STORE / (identity + '.json')).read_text(encoding='utf-8-sig'))
            if latest.get('id') != identity or not isinstance(latest.get('settings'), dict):
                raise ValueError('共享客户模板格式无效')
            latest = normalize_profile(latest)
            if data != latest:
                try: atomic_text(ACTIVE, json.dumps(latest, ensure_ascii=False, indent=2))
                except OSError: pass
            data = latest
        except (OSError, ValueError, TypeError) as exc:
            data = normalize_profile(data)
            data['_shared_source_error'] = '共享模板暂不可读取，预设已保留；恢复共享盘连接后再开始任务。'
    return data



def apply_profile(profile):
    if profile.get('id'):
        identity = str(profile['id'])
        if not identity.isalnum(): raise ValueError('客户模板编号无效')
        latest = json.loads((STORE / (identity + '.json')).read_text(encoding='utf-8-sig'))
        if latest.get('id') != identity: raise ValueError('共享客户模板编号不匹配')
        profile = normalize_profile(latest)
    if not profile.get('enabled', True):
        raise ValueError('此客户模板已停用')
    for name, value in profile.get('settings', {}).items():
        if name in FIELDS:
            item = getattr(cfg, name)
            cfg.set(item, item.serializer.deserialize(value))
    prompt = str(profile.get('settings', {}).get('custom_prompt_text', ''))
    glossary = str(profile.get('glossary', '')).strip()
    if glossary:
        prompt += '\n\n客户专用词库（仅作为明确拼写参考）：\n' + glossary
    cfg.set(cfg.custom_prompt_text, prompt)
    atomic_text(ACTIVE, json.dumps(profile, ensure_ascii=False, indent=2))


def clear_profile():
    # Keep current controls as a temporary custom configuration.
    atomic_text(ACTIVE, '{}')


def effective_profile():
    data = active_profile()
    current = capture_settings()
    if data.get('id'):
        # Customer mode is authoritative. Never mix frozen appearance with
        # unrelated controls left over from a different customer or page.
        result = normalize_profile(data)
        current = copy.deepcopy(result['settings'])
        if not result.get('enabled', True):
            result['_shared_source_error'] = '所选客户模板已停用，请选择其他模板'
        prompt = str(current.get('custom_prompt_text', ''))
        glossary = str(result.get('glossary', '')).strip()
        if glossary:
            prompt += '\n\n客户专用词库（仅作为明确拼写参考）：\n' + glossary
        current['custom_prompt_text'] = prompt
        result['settings'] = current
        result['text_source_mode'] = selected_mode(result)
        if not result.get('style_template', '').strip():
            raise ValueError('客户模板没有完整外观快照，请在客户模板页重新读取外观并保存')
        return result
    style_name = current.get('subtitle_style_name', 'default')
    if not data or data.get('settings', {}).get('subtitle_style_name') != style_name:
        path = SUBTITLE_STYLE_PATH / (style_name + '.txt')
        style = path.read_text(encoding='utf-8') if path.exists() else ''
    else:
        style = data.get('style_template', '')
    return dict(id=data.get('id', ''), name=data.get('name', '自定义设置'),
        text_source_mode=selected_mode(data),
        revision=data.get('revision', 0), settings=current, style_template=style,
        english_line_words=int(data.get('english_line_words', 6)))


def task_profile(source, begin=False):
    key = str(Path(source).resolve()).lower() if source else ''
    if begin or key not in _task_profiles:
        from app.core.task_contract import freeze_profile
        _task_profiles[key] = freeze_profile(effective_profile())
        profile = _task_profiles[key]
        logging.getLogger('customer_templates').info('任务模板已固定：%s · 版本%s · %s · 视频=%s',
            profile.get('name'), profile.get('revision', 0), appearance_summary(profile), source)
    return copy.deepcopy(_task_profiles[key])


def require_shared_profile(profile):
    if profile.get('_shared_source_error'):
        raise RuntimeError(profile['_shared_source_error'])


def profile_value(profile, name):
    item = getattr(cfg, name)
    fallback = preset_defaults().get(name, item.serialize()) if profile.get('id') else item.serialize()
    value = profile.get('settings', {}).get(name, fallback)
    return item.serializer.deserialize(value)


def style_with_line_limit(profile):
    style = profile.get('style_template', '')
    if style:
        import re
        style = re.sub(r'^; VideoCaptionerEnglishLineWords:.*\n?', '', style, flags=re.MULTILINE)
        style += '\n; VideoCaptionerEnglishLineWords: ' + str(profile.get('english_line_words', 6))
    return style


def summary(profile):
    settings = profile.get('settings', {})
    return (f"{'翻译' if settings.get('need_translate') else '保留原语言'} · "
            f"每条参考≤{settings.get('max_word_count_english', 6)}词 / "
            f"每行≤{profile.get('english_line_words', 6)}词（常规两行） · "
            f"样式：{settings.get('subtitle_style_name', 'default')} · " + appearance_summary(profile) +
            ' · 完整模板模式（临时调参请先选自定义）')


def appearance_summary(profile):
    style = profile.get('style_template', '')
    for line in style.splitlines():
        if line.startswith('Style: Default,'):
            fields = line.split(',')
            try:
                size = float(fields[2]) / (46.0 / 14.0)
                return (f'字体 {fields[1]} · 字号 {size:.2f}（软件字号口径） · '
                        f'描边 {fields[16]} ASS · 底部距离 {fields[21]} ASS')
            except (IndexError, ValueError):
                break
    return '外观快照未就绪'


def bind_task_profile(source,profile):
    """Register the submitted snapshot so later project saves use the same template."""
    key=str(Path(source).resolve()).lower() if source else ''
    _task_profiles[key]=copy.deepcopy(profile)
