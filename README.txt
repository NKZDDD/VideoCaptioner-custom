VideoCaptioner v1.3.3 - Video Quality Patch

This patch adds a Video Quality selector to the old VideoCaptioner video synthesis page.

Choices:
  Ultra quality: CRF 18
  High quality:  CRF 20
  Standard:      CRF 23
  Save space:    CRF 28

Lower CRF = higher image quality and usually larger output files.

How to install:
1. Put this patch folder inside the VideoCaptioner folder (the folder that contains app and runtime),
   or put it beside the VideoCaptioner folder.
2. Run install_video_quality_patch.bat.
3. Restart VideoCaptioner.exe.
4. Open video synthesis. A "Video Quality" selector should appear under the video file row.

The patch creates timestamped .bak_video_quality_... backups before editing.
It changes the hard-subtitle FFmpeg command to include -crf.
Soft-subtitle mode copies the original video stream and therefore does not re-encode video.

If the app is currently running, close it before applying the patch.
