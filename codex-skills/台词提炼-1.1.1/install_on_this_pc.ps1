param([string]$InstallRoot)
$ErrorActionPreference='Stop'
$codexRoot=if($InstallRoot){[IO.Path]::GetFullPath($InstallRoot)}elseif($env:CODEX_HOME){$env:CODEX_HOME}else{Join-Path $env:USERPROFILE '.codex'}
$skills=[IO.Path]::GetFullPath((Join-Path $codexRoot 'skills'))
$manifest=Get-Content -LiteralPath (Join-Path $PSScriptRoot 'release-manifest.json') -Raw -Encoding utf8 | ConvertFrom-Json
foreach($entry in $manifest.files.PSObject.Properties){
 $src=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot $entry.Name))
 if(-not $src.StartsWith([IO.Path]::GetFullPath($PSScriptRoot)+'\',[StringComparison]::OrdinalIgnoreCase)){throw '发布路径越界'}
 if((Get-FileHash -LiteralPath $src).Hash.ToLowerInvariant() -ne $entry.Value){throw '发布文件校验失败'}
}
$backup=Join-Path $codexRoot ('skill-backups/dialogue-single-'+(Get-Date -Format 'yyyyMMdd_HHmmss_ffff'))
New-Item -ItemType Directory -Path $backup -Force | Out-Null
New-Item -ItemType Directory -Path $skills -Force | Out-Null
$moved=@()
try{
 foreach($name in @('dialogue-extractor','台词提炼')){
  $target=[IO.Path]::GetFullPath((Join-Path $skills $name))
  if((Split-Path $target) -ne $skills){throw '技能目标越界'}
  if(Test-Path -LiteralPath $target){Move-Item -LiteralPath $target -Destination (Join-Path $backup $name);$moved+=$name}
 }
 Copy-Item -LiteralPath (Join-Path $PSScriptRoot '台词提炼') -Destination (Join-Path $skills '台词提炼') -Recurse
 foreach($entry in $manifest.files.PSObject.Properties){if((Get-FileHash -LiteralPath (Join-Path $skills $entry.Name)).Hash.ToLowerInvariant() -ne $entry.Value){throw '安装校验失败'}}
}catch{
 # 保留失败的新文件，不删除；恢复旧技能。
 $target=Join-Path $skills '台词提炼'
 if(Test-Path -LiteralPath $target){Move-Item -LiteralPath $target -Destination (Join-Path $backup 'failed-new')}
 foreach($name in $moved){Move-Item -LiteralPath (Join-Path $backup $name) -Destination (Join-Path $skills $name)}
 throw
}
Write-Output ('已合并，只保留 /台词提炼。旧入口备份：'+$backup)
