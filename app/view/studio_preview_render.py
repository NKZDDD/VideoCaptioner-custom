"""Private preview process: invoke the same compositor used by final export."""
import sys,json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))

def main():
    job=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    sys.path.insert(0,job.get('application_root',str(Path(__file__).resolve().parents[2])))
    from app.core.utils import video_utils
    import subprocess,os
    original_popen=subprocess.Popen
    def preview_popen(command,*args,**kwargs):
        if isinstance(command,list) and Path(str(command[0])).stem.lower()=='ffmpeg' and '-vcodec' in command:
            command=list(command)
            command[1:1]=['-filter_complex_threads','1','-filter_threads','1']
            command[-1:-1]=['-threads','2']
            if os.name=='nt':kwargs['creationflags']=kwargs.get('creationflags',0)|subprocess.BELOW_NORMAL_PRIORITY_CLASS
        return original_popen(command,*args,**kwargs)
    subprocess.Popen=preview_popen
    add_subtitles=video_utils.add_subtitles
    def progress(value,message):
        print('PREVIEW_PROGRESS|'+str(value)+'|'+str(message),flush=True)
    print('PREVIEW_PROGRESS|0|后台准备字幕预览',flush=True)
    # No language models, uploads, template writes, output-folder exports or
    # settings changes. Encode speed/bitrate differ, geometry does not.
    add_subtitles(job['video'],job['ass'],job['output'],quality='ultrafast',
        crf=20,soft_subtitle=False,match_source_bitrate=False,progress_callback=progress)

if __name__=='__main__':main()
