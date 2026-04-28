# EldenSys Agent — build script (PowerShell)
# Run from the repo root: .\installer\build.ps1
param(
    [switch]$Clean,
    [switch]$SkipSumatra
)

$ErrorActionPreference = "Stop"
$root = Resolve-Path "$PSScriptRoot\.."
Set-Location $root

if ($Clean) {
    Write-Host "==> Limpando build/ e dist/" -ForegroundColor Cyan
    Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue
}

# 1) Garantir SumatraPDF
$sumatra = Join-Path $root "vendor\SumatraPDF.exe"
if (-not (Test-Path $sumatra) -and -not $SkipSumatra) {
    Write-Host "==> SumatraPDF.exe não encontrado em vendor/" -ForegroundColor Yellow
    Write-Host "    Baixe a versão portable em https://www.sumatrapdfreader.org/download-free-pdf-viewer"
    Write-Host "    e salve como vendor\SumatraPDF.exe (renomeie se vier com versão no nome)."
    Write-Host "    Use -SkipSumatra para ignorar e seguir o build (impressão PDF não funcionará)."
    throw "SumatraPDF.exe ausente."
}

# 2) Garantir venv
if (-not (Test-Path .venv)) {
    Write-Host "==> Criando venv" -ForegroundColor Cyan
    python -m venv .venv
}
& .\.venv\Scripts\Activate.ps1

Write-Host "==> Instalando dependências" -ForegroundColor Cyan
pip install --quiet -r requirements-dev.txt

# 3) Build
Write-Host "==> Rodando PyInstaller" -ForegroundColor Cyan
# PyInstaller escreve INFO no stderr; redireciona pra stdout pra não disparar
# o $ErrorActionPreference = "Stop". Falhas reais retornam exit code != 0.
$prev = $ErrorActionPreference
$ErrorActionPreference = "Continue"
pyinstaller --clean --noconfirm agente.spec 2>&1 | ForEach-Object { "$_" }
$pyiExit = $LASTEXITCODE
$ErrorActionPreference = $prev
if ($pyiExit -ne 0) {
    throw "PyInstaller falhou (exit $pyiExit)"
}

$out = Join-Path $root "dist\EldenSysAgent\EldenSysAgent.exe"
if (Test-Path $out) {
    Write-Host "==> OK: $out" -ForegroundColor Green
} else {
    throw "Build falhou: $out não foi gerado."
}

# 4) Inno Setup (gera o instalador final)
$iscc = $null
foreach ($cand in @(
    "$env:ProgramFiles(x86)\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
)) {
    if ($cand -and (Test-Path $cand)) { $iscc = $cand; break }
}
if (-not $iscc) {
    Write-Host "==> Inno Setup nao encontrado. Pulando geracao do instalador." -ForegroundColor Yellow
    Write-Host "    Instale em https://jrsoftware.org/isdl.php e rode novamente."
    return
}

Write-Host "==> Compilando instalador (Inno Setup)" -ForegroundColor Cyan
$iss = Join-Path $PSScriptRoot "eldensys-agent.iss"
$prev = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& $iscc $iss 2>&1 | ForEach-Object { "$_" }
$isccExit = $LASTEXITCODE
$ErrorActionPreference = $prev
if ($isccExit -ne 0) {
    throw "ISCC falhou (exit $isccExit)"
}

$installerOut = Get-ChildItem -Path (Join-Path $root "dist\installer") -Filter "EldenSysAgent-Setup-*.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
if ($installerOut) {
    Write-Host "==> Instalador gerado: $($installerOut.FullName)" -ForegroundColor Green
} else {
    Write-Host "==> Aviso: nao localizei o .exe do instalador em dist\installer" -ForegroundColor Yellow
}
