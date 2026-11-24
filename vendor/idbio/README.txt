SDK do leitor biometrico Control iD iDBio (v0.6 do EldenSys Agent)
=================================================================

Coloque aqui a DLL x64 do SDK como `libcidbio.dll`. Ela NAO vai para o git
(.gitignore: vendor/idbio/*.dll) enquanto a licenca de redistribuicao nao for
confirmada com a Control iD.

Origem
  Kit oficial IDBIO_SDK, versao 1.7.0
  https://www.controlid.com.br/idbio/IDBIO_SDK.zip
  Documentacao: https://www.controlid.com.br/docs/idbio-pt

Qual arquivo
  lib\C_C++\win64\libcidbio.dll  (x64, MinGW; importa so ADVAPI32, KERNEL32,
  msvcrt e SETUPAPI - nao precisa de runtime do Visual C++)
  ATENCAO: lib\C_C++\win32\libcidbio.dll do mesmo kit TAMBEM e x64, de outro
  build. Use a da pasta win64 (o hash abaixo e o dela).

SHA-256
  e9ddfff1f7476fe4039f08ec501bc22f8d0b0d8fbb4357a5a23dbef67a336702

Onde e usada
  agent/biometria/sdk.py carrega <recursos>/vendor/idbio/libcidbio.dll
  (no executavel: _internal\vendor\idbio\libcidbio.dll). O agente.spec copia
  para la; o installer\build.ps1 falha sem a DLL e confere o SHA-256 e o PE
  x64 (use -SkipBiometria para gerar o agente sem o leitor).

Driver
  O Windows 10/11 costuma reconhecer o leitor sozinho. Se nao reconhecer, o
  driver esta no kit (pasta windows_driver).

Licenca
  A CONFIRMAR com a Control iD antes de versionar ou redistribuir a DLL fora
  do instalador do EldenSys Agent.
