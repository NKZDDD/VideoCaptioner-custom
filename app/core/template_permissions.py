"""Application-level write protection for the existing Arabic template.

This does not replace access controls for people who can directly edit shared files.
Missing or unreadable policy always fails closed for protected profiles.
"""
import hashlib,json,os,platform
from pathlib import Path
from app.config import SHARED_APPDATA_PATH

POLICY=SHARED_APPDATA_PATH/'template_permissions.json'

def device_identity():
    import winreg
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,r'SOFTWARE\Microsoft\Cryptography',0,winreg.KEY_READ|winreg.KEY_WOW64_64KEY) as key:
        guid=winreg.QueryValueEx(key,'MachineGuid')[0]
    value='|'.join((str(guid).lower(),platform.node().lower(),os.environ.get('USERNAME','').lower()))
    return hashlib.sha256(value.encode('utf-8')).hexdigest()

def read_policy():
    try:
        value=json.loads(POLICY.read_text(encoding='utf-8'))
        if value.get('version')!=1 or not isinstance(value.get('protected_ids'),list) or not isinstance(value.get('owner_devices'),list):return {}
        return value
    except (OSError,ValueError,TypeError):return {}

def is_protected(profile):
    policy=read_policy()
    return bool(profile.get('id') in policy.get('protected_ids',[]) or '阿拉伯' in str(profile.get('name','')) or str(profile.get('settings',{}).get('transcribe_language','')).lower() in ('ar','arabic'))

def can_edit(profile):
    if not is_protected(profile):return True
    try:return device_identity() in read_policy().get('owner_devices',[])
    except OSError:return False

def assert_template_write(profile):
    if not can_edit(profile):raise RuntimeError('阿拉伯模板受保护：当前电脑只有使用权限，不能修改、停用、改名或覆盖；请由模板所有者处理')

def assert_assistant_base(profile):
    if is_protected(profile):raise ValueError('阿拉伯模板不参与需求生成；请选择其他基础模板，原模板保持锁定')
