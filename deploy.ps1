param(
    [string]$AppName = "selam-eymen-0540"
)

$ErrorActionPreference = "Stop"
$Fly = Join-Path $env:USERPROFILE ".fly\bin\flyctl.exe"
$EnvFile = Join-Path $PSScriptRoot ".env"

if (-not (Test-Path $Fly)) { throw "flyctl bulunamadı: $Fly" }
if (-not (Test-Path $EnvFile)) { throw ".env dosyası bulunamadı. FLY-DEPLOY.md adım 2'ye bak." }

$Values = @{}
foreach ($line in Get-Content -LiteralPath $EnvFile) {
    $t = $line.Trim()
    if (-not $t -or $t.StartsWith("#")) { continue }
    if ($t -match "^([^=]+)=(.*)$") {
        $Values[$Matches[1].Trim()] = $Matches[2].Trim().Trim('"').Trim("'")
    }
}

foreach ($key in @("DATABASE_URL", "APPSCRIPT_URL", "APPSCRIPT_SECRET")) {
    if (-not $Values.ContainsKey($key) -or [string]::IsNullOrWhiteSpace($Values[$key])) {
        throw ".env içinde $key eksik veya boş."
    }
}

Write-Host "Fly oturumu kontrol ediliyor..." -ForegroundColor Cyan
& $Fly auth whoami

Write-Host "Uygulama hazırlanıyor: $AppName" -ForegroundColor Cyan
$existing = & $Fly apps list --json 2>$null | ConvertFrom-Json
if (-not ($existing | Where-Object { $_.Name -eq $AppName })) {
    & $Fly apps create $AppName
}

Write-Host "Gizli bilgiler Fly'a tanımlanıyor (değerler ekrana basılmaz)..." -ForegroundColor Cyan
& $Fly secrets set `
    "DATABASE_URL=$($Values['DATABASE_URL'])" `
    "APPSCRIPT_URL=$($Values['APPSCRIPT_URL'])" `
    "APPSCRIPT_SECRET=$($Values['APPSCRIPT_SECRET'])" `
    --app $AppName

Write-Host "Dağıtılıyor..." -ForegroundColor Cyan
& $Fly deploy --app $AppName --ha=false

Write-Host "Tamam. Adres: https://$AppName.fly.dev" -ForegroundColor Green
