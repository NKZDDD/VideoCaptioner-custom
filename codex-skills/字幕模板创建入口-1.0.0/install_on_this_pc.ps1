$ErrorActionPreference='Stop'
$codexRoot=if($env:CODEX_HOME){$env:CODEX_HOME}else{Join-Path $env:USERPROFILE '.codex'}
$skillsRoot=Join-Path $codexRoot 'skills'
$main=Join-Path $skillsRoot 'kaka-template-assistant/SKILL.md'
if(-not (Test-Path -LiteralPath $main)){throw '请先安装字母模板创建主技能1.1.1，再安装中文入口。'}
$dest=Join-Path $skillsRoot '字幕模板创建'
foreach($rel in @('SKILL.md','agents/openai.yaml')){
 $source=Join-Path $PSScriptRoot ('字幕模板创建/'+$rel)
 $target=Join-Path $dest $rel
 if((Test-Path -LiteralPath $target) -and (Get-FileHash -LiteralPath $target).Hash -ne (Get-FileHash -LiteralPath $source).Hash){throw '同名中文入口已存在且内容不同，未覆盖。'}
}
foreach($rel in @('SKILL.md','agents/openai.yaml')){
 $source=Join-Path $PSScriptRoot ('字幕模板创建/'+$rel)
 $target=Join-Path $dest $rel
 New-Item -ItemType Directory -Force -Path (Split-Path $target) | Out-Null
 Copy-Item -LiteralPath $source -Destination $target -Force
 if((Get-FileHash -LiteralPath $target).Hash -ne (Get-FileHash -LiteralPath $source).Hash){throw '安装后校验失败'}
}
Write-Output '中文入口已安装。重启Codex后，在输入框输入 /字幕模板创建 并选择技能。'