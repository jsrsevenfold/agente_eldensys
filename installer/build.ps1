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
pyinstaller --clean --noconfirm agente.spec

$out = Join-Path $root "dist\EldenSysAgent\EldenSysAgent.exe"
if (Test-Path $out) {
    Write-Host "==> OK: $out" -ForegroundColor Green
} else {
    throw "Build falhou: $out não foi gerado."
}
