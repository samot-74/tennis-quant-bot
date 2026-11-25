# Called by the Windows tasks: run_pipeline.ps1 scheduler | run_pipeline.ps1 pipeline T2 --t=1,2
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
New-Item -ItemType Directory -Force "$root\data\logs" | Out-Null
$log = "$root\data\logs\$(Get-Date -Format yyyy-MM-dd).log"
$module = $args[0]
$rest = @()
if ($args.Count -gt 1) { $rest = $args[1..($args.Count - 1)] }
"=== $(Get-Date -Format s) $module $rest" | Out-File -Append -Encoding utf8 $log
& python -X utf8 -m "src.$module" @rest *>> $log
