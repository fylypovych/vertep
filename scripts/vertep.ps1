param([ValidateSet("status", "update", "test")][string]$Command = "status")
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$coreUrl = if ($env:VERTEP_CORE_URL) { $env:VERTEP_CORE_URL } else { "http://127.0.0.1:8080" }
$headers = @{}
if ($env:ADMIN_PASSWORD) {
  $pair = "{0}:{1}" -f $(if ($env:ADMIN_USER) { $env:ADMIN_USER } else { "admin" }), $env:ADMIN_PASSWORD
  $headers.Authorization = "Basic " + [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($pair))
}
switch ($Command) {
  "status" { Invoke-RestMethod "$coreUrl/api/status" -Headers $headers | ConvertTo-Json -Depth 8 }
  "update" { git -C $root pull --ff-only; docker compose -f "$root/docker-compose.yml" up -d --build }
  "test" { Push-Location $root; python -m pytest -q; Pop-Location }
}
