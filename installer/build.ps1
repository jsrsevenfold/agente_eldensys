# EldenSys Agent — build script (PowerShell)
# Run from the repo root: .\installer\build.ps1
param(
    [switch]$Clean,
    [switch]$SkipSumatra,
    # Gera o agente SEM o leitor biometrico (sem a DLL do iDBio). As rotas
    # /biometria respondem sdk_ausente; impressao e canal remoto seguem iguais.
    [switch]$SkipBiometria
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

# 1b) SDK do iDBio (leitor biometrico, v0.6)
# A DLL nao vai para o git (licenca da Control iD a confirmar): fica em
# vendor\idbio\ (ver vendor\idbio\README.txt). Conferimos o hash e a
# arquitetura para nao empacotar outro build (o kit traz uma "win32" que
# tambem e x64, de outro build) nem uma DLL de 32 bits.
$idbioDll = Join-Path $root "vendor\idbio\libcidbio.dll"
$idbioSha256 = "e9ddfff1f7476fe4039f08ec501bc22f8d0b0d8fbb4357a5a23dbef67a336702"
if ($SkipBiometria) {
    Write-Host "==> -SkipBiometria: agente SEM o leitor biometrico (DLL fora do pacote)" -ForegroundColor Yellow
    $env:ELDENSYS_SEM_BIOMETRIA = "1"
} else {
    Write-Host "==> SDK do iDBio" -ForegroundColor Cyan
    Remove-Item Env:\ELDENSYS_SEM_BIOMETRIA -ErrorAction SilentlyContinue
    if (-not (Test-Path $idbioDll)) {
        Write-Host "    libcidbio.dll nao encontrada em vendor\idbio\" -ForegroundColor Yellow
        Write-Host "    Copie do kit IDBIO_SDK 1.7.0: lib\C_C++\win64\libcidbio.dll"
        Write-Host "    Use -SkipBiometria para gerar o agente sem o leitor biometrico."
        throw "libcidbio.dll ausente."
    }
    $hash = (Get-FileHash -Algorithm SHA256 -Path $idbioDll).Hash.ToLower()
    if ($hash -ne $idbioSha256) {
        throw "libcidbio.dll com SHA-256 inesperado ($hash). Esperado: $idbioSha256 (SDK 1.7.0, win64)."
    }
    $bytes = [System.IO.File]::ReadAllBytes($idbioDll)
    $peOffset = [BitConverter]::ToInt32($bytes, 0x3C)
    $assinatura = [BitConverter]::ToUInt32($bytes, $peOffset)
    $maquina = [BitConverter]::ToUInt16($bytes, $peOffset + 4)
    if ($assinatura -ne 0x00004550 -or $maquina -ne 0x8664) {
        throw ("libcidbio.dll nao e uma DLL x64 (maquina 0x{0:X4})." -f $maquina)
    }
    Write-Host "    OK: x64, SHA-256 conferido" -ForegroundColor Green
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

    # O EldenSys baixa o agente por uma URL FIXA:
    #   releases/latest/download/EldenSysAgent-Setup.exe   (PrintAgentSettings.tsx)
    # O Inno gera o nome COM versao, entao deixamos tambem uma copia sem versao
    # para subir como asset do release com o nome exato que o frontend espera.
    $releaseAsset = Join-Path $installerOut.Directory "EldenSysAgent-Setup.exe"
    Copy-Item $installerOut.FullName $releaseAsset -Force
    Write-Host "==> Asset do release: $releaseAsset" -ForegroundColor Green
} else {
    Write-Host "==> Aviso: nao localizei o .exe do instalador em dist\installer" -ForegroundColor Yellow
}
