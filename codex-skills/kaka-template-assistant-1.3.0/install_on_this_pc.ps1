# Release 1.2.2 explicit per-style solid shadows; preserve unknown-edit protection.
param([string]$InstallRoot)
$ErrorActionPreference = 'Stop'
$packageRoot = $PSScriptRoot
$sourceRoot = [IO.Path]::GetFullPath((Join-Path $packageRoot 'kaka-template-assistant'))
$manifest = Get-Content -LiteralPath (Join-Path $packageRoot 'release-manifest.json') -Raw -Encoding utf8 | ConvertFrom-Json
if ((Get-FileHash -LiteralPath $PSCommandPath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $manifest.installer_sha256) { throw '安装器校验失败，未安装' }
$codexRoot = if ($InstallRoot) { [IO.Path]::GetFullPath($InstallRoot) } elseif ($env:CODEX_HOME) { [IO.Path]::GetFullPath($env:CODEX_HOME) } else { Join-Path $env:USERPROFILE '.codex' }
$destinationRoot = [IO.Path]::GetFullPath((Join-Path $codexRoot 'skills\kaka-template-assistant'))
$stampPath = Join-Path $destinationRoot '.release.json'
$prior = $null
if (Test-Path -LiteralPath $destinationRoot) {
    if (-not (Test-Path -LiteralPath $stampPath)) { throw '本机同名Skill不是此发布包管理，未覆盖，请先核对' }
    $prior = Get-Content -LiteralPath $stampPath -Raw -Encoding utf8 | ConvertFrom-Json
    foreach ($property in $prior.files.PSObject.Properties) {
        $target = [IO.Path]::GetFullPath((Join-Path $destinationRoot $property.Name))
        if (-not $target.StartsWith($destinationRoot + '\', [StringComparison]::OrdinalIgnoreCase)) { throw '旧清单路径越界' }
        if ((Test-Path -LiteralPath $target) -and (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLowerInvariant() -ne $property.Value) { if ($manifest.approved_predecessor_hashes.$($property.Name) -ne (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLowerInvariant()) { throw ('本机Skill有未知修改，未覆盖：' + $property.Name) } }
    }
}
foreach ($property in $manifest.auxiliary_files.PSObject.Properties) {
    $auxPath = [IO.Path]::GetFullPath((Join-Path $packageRoot $property.Name))
    if (-not $auxPath.StartsWith([IO.Path]::GetFullPath($packageRoot)+'\',[StringComparison]::OrdinalIgnoreCase)) {throw '入口路径越界'}
    if ((Get-FileHash -LiteralPath $auxPath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $property.Value) {throw '中文入口包校验失败'}
}
$items = @()
foreach ($property in $manifest.files.PSObject.Properties) {
    $source = [IO.Path]::GetFullPath((Join-Path $sourceRoot $property.Name))
    $target = [IO.Path]::GetFullPath((Join-Path $destinationRoot $property.Name))
    if (-not $source.StartsWith($sourceRoot + '\', [StringComparison]::OrdinalIgnoreCase) -or -not $target.StartsWith($destinationRoot + '\', [StringComparison]::OrdinalIgnoreCase)) { throw '安装清单路径越界' }
    if ((Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash.ToLowerInvariant() -ne $property.Value) { throw ('发布包校验失败：' + $property.Name) }
    $items += [pscustomobject]@{ Source=$source; Target=$target; Relative=$property.Name; Hash=$property.Value }
}
$backupRoot = Join-Path $codexRoot ('skill-backups\kaka-template-assistant-' + (Get-Date -Format 'yyyyMMdd_HHmmss_ffff'))
$changed = @()
try {
    foreach ($item in $items) {
        $existed = Test-Path -LiteralPath $item.Target
        $backup = Join-Path $backupRoot $item.Relative
        if ($existed) { New-Item -ItemType Directory -Force -Path (Split-Path $backup) | Out-Null; Copy-Item -LiteralPath $item.Target -Destination $backup }
        New-Item -ItemType Directory -Force -Path (Split-Path $item.Target) | Out-Null
        $changed += [pscustomobject]@{ Target=$item.Target; Backup=$backup; Existed=$existed }
        Copy-Item -LiteralPath $item.Source -Destination $item.Target -Force
        if ((Get-FileHash -LiteralPath $item.Target -Algorithm SHA256).Hash.ToLowerInvariant() -ne $item.Hash) { throw '安装后校验失败' }
    }
    $manifest | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $stampPath -Encoding utf8
} catch {
    [array]::Reverse($changed)
    foreach ($item in $changed) {
        if ($item.Existed) { Copy-Item -LiteralPath $item.Backup -Destination $item.Target -Force }
        elseif (Test-Path -LiteralPath $item.Target) { Remove-Item -LiteralPath $item.Target }
    }
    throw
}
& (Join-Path $packageRoot 'install_chinese_entry.ps1') -InstallRoot $codexRoot
Write-Output ('字母模板创建 ' + $manifest.version + ' 已安装：' + $destinationRoot)
Write-Output '请在Codex新聊天中输入：用字母模板创建帮我新建/修改共享字幕模板。若未显示，请重启Codex。'
