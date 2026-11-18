param([string]$InstallRoot)
$ErrorActionPreference='Stop'
$base=$PSScriptRoot
$m=Get-Content -LiteralPath (Join-Path $base 'release-manifest.json') -Raw -Encoding utf8 | ConvertFrom-Json
if((Get-FileHash -LiteralPath $PSCommandPath).Hash.ToLowerInvariant() -ne $m.installer_sha256){throw '安装器校验失败'}
$codexRoot=if($InstallRoot){[IO.Path]::GetFullPath($InstallRoot)}elseif($env:CODEX_HOME){$env:CODEX_HOME}else{Join-Path $env:USERPROFILE '.codex'}
$root=[IO.Path]::GetFullPath((Join-Path $codexRoot 'skills'))
$stamp=Join-Path $root 'dialogue-extractor/.release.json'
$prior=if(Test-Path -LiteralPath $stamp){Get-Content -LiteralPath $stamp -Raw -Encoding utf8 | ConvertFrom-Json}else{$null}
$items=@()
foreach($f in $m.files.PSObject.Properties){
 $src=[IO.Path]::GetFullPath((Join-Path $base $f.Name));$dst=[IO.Path]::GetFullPath((Join-Path $root $f.Name))
 if(-not $src.StartsWith([IO.Path]::GetFullPath($base)+'\',[StringComparison]::OrdinalIgnoreCase) -or -not $dst.StartsWith($root+'\',[StringComparison]::OrdinalIgnoreCase)){throw '安装路径越界'}
 if((Get-FileHash -LiteralPath $src).Hash.ToLowerInvariant() -ne $f.Value){throw '发布包文件校验失败'}
 if(Test-Path -LiteralPath $dst){
  $h=(Get-FileHash -LiteralPath $dst).Hash.ToLowerInvariant()
  if($h -ne $f.Value -and ($null -eq $prior -or $prior.files.$($f.Name) -ne $h)){throw ('本机同名技能有未知修改，未覆盖：'+$f.Name)}
 }
 $items+= [pscustomobject]@{Source=$src;Target=$dst;Rel=$f.Name;Hash=$f.Value}
}
$backup=Join-Path $codexRoot ('skill-backups/dialogue-extractor-'+(Get-Date -Format 'yyyyMMdd_HHmmss_ffff'))
$changed=@()
try{
 foreach($i in $items){
  $exists=Test-Path -LiteralPath $i.Target;$bak=Join-Path $backup $i.Rel
  if($exists){New-Item -ItemType Directory -Force -Path (Split-Path $bak) | Out-Null;Copy-Item -LiteralPath $i.Target -Destination $bak}
  New-Item -ItemType Directory -Force -Path (Split-Path $i.Target) | Out-Null
  $changed+=[pscustomobject]@{Target=$i.Target;Backup=$bak;Existed=$exists}
  Copy-Item -LiteralPath $i.Source -Destination $i.Target -Force
  if((Get-FileHash -LiteralPath $i.Target).Hash.ToLowerInvariant() -ne $i.Hash){throw '安装后哈希不一致'}
 }
 $m | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $stamp -Encoding utf8
}catch{
 [array]::Reverse($changed)
 foreach($i in $changed){if($i.Existed){Copy-Item -LiteralPath $i.Backup -Destination $i.Target -Force}elseif(Test-Path -LiteralPath $i.Target){Remove-Item -LiteralPath $i.Target}}
 throw
}
Write-Output '台词提炼1.0.0已安装。新开聊天输入 /台词提炼；如未出现，请重启Codex。'