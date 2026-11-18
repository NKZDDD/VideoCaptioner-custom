param([string]$InstallRoot)
$ErrorActionPreference='Stop'
$codexRoot=if($InstallRoot){[IO.Path]::GetFullPath($InstallRoot)}elseif($env:CODEX_HOME){$env:CODEX_HOME}else{Join-Path $env:USERPROFILE '.codex'}
$skillsRoot=Join-Path $codexRoot 'skills'
$main=Join-Path $skillsRoot 'kaka-template-assistant/SKILL.md'
if(-not (Test-Path -LiteralPath $main)){throw '请先安装字母模板创建主技能1.3.0，再安装中文入口。'}
$dest=Join-Path $skillsRoot '字幕模板创建'
$manifest=Get-Content -LiteralPath (Join-Path $PSScriptRoot 'release-manifest.json') -Raw -Encoding utf8 | ConvertFrom-Json
foreach($rel in @('SKILL.md','agents/openai.yaml')){
 $source=Join-Path $PSScriptRoot ('字幕模板创建/'+$rel)
 $target=Join-Path $dest $rel
 if((Get-FileHash -LiteralPath $source).Hash.ToLowerInvariant() -ne $manifest.auxiliary_files.$('字幕模板创建/'+$rel)){throw '中文入口发布包哈希不匹配'}
 if((Test-Path -LiteralPath $target) -and (Get-FileHash -LiteralPath $target).Hash -ne (Get-FileHash -LiteralPath $source).Hash){
  $actual=(Get-FileHash -LiteralPath $target).Hash.ToLowerInvariant()
  if($actual -ne $manifest.approved_auxiliary_predecessor_hashes.$('字幕模板创建/'+$rel)){throw '同名中文入口存在未知修改，未覆盖。'}
  $backup=Join-Path $codexRoot ('skill-backups/字幕模板创建-'+(Get-Date -Format 'yyyyMMdd_HHmmss_ffff')+'/'+$rel)
  New-Item -ItemType Directory -Force -Path (Split-Path $backup) | Out-Null
  Copy-Item -LiteralPath $target -Destination $backup
 }
}
foreach($rel in @('SKILL.md','agents/openai.yaml')){
 $source=Join-Path $PSScriptRoot ('字幕模板创建/'+$rel)
 $target=Join-Path $dest $rel
 New-Item -ItemType Directory -Force -Path (Split-Path $target) | Out-Null
 Copy-Item -LiteralPath $source -Destination $target -Force
 if((Get-FileHash -LiteralPath $target).Hash -ne (Get-FileHash -LiteralPath $source).Hash){throw '安装后校验失败'}
}
Write-Output '中文入口已安装。重启Codex后，在输入框输入 /字幕模板创建 并选择技能。'