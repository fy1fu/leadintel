# LeadIntel Friend Edition - export prospects to a new CSV file (read-only database session).
# Default: candidates (provisionally qualified + needs research). -All exports every prospect; -Niche limits to one niche.
param([switch]$All, [string]$Niche = '')
. (Join-Path $PSScriptRoot 'common.ps1')
$exit = 1
try {
    $state = Assert-Installed
    $dir = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'LeadIntel Exports'
    New-Item -ItemType Directory -Path $dir -Force | Out-Null
    $tag = if ($Niche) { "-$Niche" } else { '' }
    $out = Join-Path $dir ("prospects$tag-" + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.csv')
    $cliArgs = @('export', $out)
    if ($Niche) { $cliArgs += @('--niche', $Niche) }
    if (-not $All) { $cliArgs += @('--qualification', 'provisionally_qualified,needs_research') }
    Start-Pg | Out-Null
    Set-AppEnvironment $state -ReadOnly
    $code = Invoke-LeadIntel $cliArgs
    if ($code -ne 0) { Stop-WithError "the export failed (exit code $code, see above)." }
    Write-Host "`nSaved: $out" -ForegroundColor Green
    Start-Process explorer.exe -ArgumentList "/select,`"$out`""
    $exit = 0
} catch {
    if ($_.Exception.Message -notlike 'LeadIntel:*') { Write-Host "`nExport stopped: $($_.Exception.Message)" -ForegroundColor Red }
} finally {
    Clear-AppEnvironment
    try { Stop-PgIfIdle } catch { }
}
Write-Host 'Press Enter to close.'; [void](Read-Host)
exit $exit
