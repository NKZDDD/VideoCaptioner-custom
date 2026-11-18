"""SFTP data plane; existing authenticated HTTP reservation/commit/task APIs."""
import base64, hashlib, os, socket, sys, time
from pathlib import Path

def upload_sftp(session,base,headers,video,payload,stopped,progress):
    try:import paramiko
    except ImportError:
        sys.path.insert(0,str(Path(__file__).resolve().parents[3]/'resource'/'sftp_deps'))
        import paramiko
    video=Path(video);stamp=(video.stat().st_size,video.stat().st_mtime_ns)
    def check():
        if stopped():raise InterruptedError('SFTP上传已暂停，重新恢复时校验断点')
    def post(path,**kwargs):
        check();r=session.post(base+path,headers=headers,timeout=(10,60),allow_redirects=False,**kwargs)
        if r.status_code!=200:raise ValueError('SFTP上传控制接口未通过（HTTP '+str(r.status_code)+'）：'+r.text[:160])
        return r.json()
    h=hashlib.sha256()
    with video.open('rb') as f:
        while data:=f.read(1024**2):check();h.update(data)
    jid=payload['submission_id'];route='/cloud/uploads/'+jid
    state=post(route,json=dict(size=stamp[0],sha256=h.hexdigest(),payload=payload))
    if state.get('submitted'):return state['id']
    # Ephemeral private key never leaves memory or enters shared settings.
    key=paramiko.RSAKey.generate(2048)
    info=post(route+'/sftp',json={'public_key':key.get_name()+' '+key.get_base64()})
    if info.get('username')!='kaka-upload' or info.get('path')!='/inbox/'+jid+'.part':raise ValueError('SFTP上传账户或路径异常')
    offset=info['offset']
    if type(offset)is not int or not 0<=offset<=stamp[0]:raise ValueError('SFTP断点无效')
    prefix=hashlib.sha256()
    with video.open('rb') as f:
        remaining=offset
        while remaining:
            check();data=f.read(min(1024**2,remaining))
            if not data:raise ValueError('素材在校验断点时被截断')
            prefix.update(data);remaining-=len(data)
    if prefix.hexdigest()!=info['prefix_sha256']:raise ValueError('SFTP断点与本机素材不一致，未追加')
    started=time.monotonic();start_offset=offset
    def emit(label):
        rate=(offset-start_offset)/max(.01,time.monotonic()-started)
        progress(offset,stamp[0],rate,label)
    emit('SFTP连接与断点校验')
    sock=socket.create_connection((info['host'],int(info['port'])),timeout=10);sock.settimeout(None)
    with paramiko.Transport(sock) as transport:
        transport.auth_timeout=30;transport.get_security_options().key_types=('ssh-ed25519',)
        transport.start_client(timeout=10)
        actual='SHA256:'+base64.b64encode(hashlib.sha256(transport.get_remote_server_key().asbytes()).digest()).decode().rstrip('=')
        if actual!=info['host_key_sha256']:raise ValueError('SFTP主机指纹不匹配，未认证')
        transport.auth_publickey(info['username'],key);transport.set_keepalive(15)
        with paramiko.SFTPClient.from_transport(transport) as sftp,video.open('rb') as source:
            sftp.get_channel().settimeout(30);source.seek(offset)
            while offset<stamp[0]:
                check();end=min(stamp[0],offset+4*1024**2)
                with sftp.open(info['path'],'r+b' if offset else 'wb',bufsize=0) as remote:
                    remote.seek(offset);remote.set_pipelined(True)
                    while offset<end:
                        check();data=source.read(min(64*1024,end-offset))
                        if not data:raise ValueError('素材被截断')
                        remote.write(data);offset+=len(data)
                    # close drains queued write acknowledgements before progress.
                if sftp.stat(info['path']).st_size!=offset:raise ValueError('SFTP远端确认大小异常')
                emit('SFTP上传，已确认')
    if (video.stat().st_size,video.stat().st_mtime_ns)!=stamp:raise ValueError('素材在上传时发生变化，未提交')
    post(route+'/sftp/commit');emit('SFTP整文件校验通过')
    return post(route+'/complete')['id']
