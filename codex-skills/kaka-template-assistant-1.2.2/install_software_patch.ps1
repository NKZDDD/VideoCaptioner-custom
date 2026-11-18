param([string]$AppRoot='\\desktop-8roqjoa\电脑文件储存\工具箱\自动化工具\卡卡字幕工具\VideoCaptioner\VideoCaptioner')
$ErrorActionPreference='Stop'
$root=[IO.Path]::GetFullPath($AppRoot).TrimEnd('\')
$manifest=Get-Content -LiteralPath (Join-Path $PSScriptRoot 'release-manifest.json') -Raw -Encoding utf8 | ConvertFrom-Json
foreach($key in @('install_software_patch.ps1','software-patches/ass_effects.py','software-compatibility.json')){
 $path=Join-Path $PSScriptRoot $key
 if((Get-FileHash -LiteralPath $path).Hash.ToLowerInvariant() -ne $manifest.auxiliary_files.$key){throw ('发布包校验失败：'+$key)}
}
$compat=Get-Content -LiteralPath (Join-Path $PSScriptRoot 'software-compatibility.json') -Raw -Encoding utf8 | ConvertFrom-Json
$target=[IO.Path]::GetFullPath((Join-Path $root $compat.required_app_module))
if(-not $target.StartsWith($root+'\',[StringComparison]::OrdinalIgnoreCase)){throw '软件目标路径越界'}
$actual=(Get-FileHash -LiteralPath $target).Hash.ToLowerInvariant()
if($actual -eq $compat.after_sha256){Write-Output '此软件已安装实投影补丁，未重复写入。';return}
if($actual -ne $compat.before_sha256){throw '软件有未知修改，停止安装；请先核对具体差异。'}
$backupRoot=Join-Path $root ('AppData\patch-backups\solid-shadow-'+(Get-Date -Format 'yyyyMMdd_HHmmss_ffff'))
$backup=Join-Path $backupRoot $compat.required_app_module
New-Item -ItemType Directory -Path (Split-Path $backup) -Force | Out-Null
Copy-Item -LiteralPath $target -Destination $backup
$record=[ordered]@{release=$manifest.version;target=$target;before_sha256=$actual;after_sha256=$compat.after_sha256;backup=$backup;scope='explicit per-style solid shadow only'}
$record|ConvertTo-Json -Depth 6|Set-Content -LiteralPath (Join-Path $backupRoot 'install-manifest.json') -Encoding utf8
try{
 Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'software-patches/ass_effects.py') -Destination $target -Force
 if((Get-FileHash -LiteralPath $target).Hash.ToLowerInvariant() -ne $compat.after_sha256){throw '软件补丁回读校验失败'}
}catch{
 Copy-Item -LiteralPath $backup -Destination $target -Force
 throw
}
Write-Output ('实投影补丁已安装，原件备份：'+$backup)
Write-Output '新模式只对带显式标记的样式生效；已打开的软件进程需重新加载后才能使用新模式。'
