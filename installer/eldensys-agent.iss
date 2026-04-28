; EldenSys Agent — Inno Setup installer
; Compile with Inno Setup 6+ (https://jrsoftware.org/isinfo.php)

#define MyAppName "EldenSys Agent"
#define MyAppVersion "0.1.0"
#define MyAppPublisher "EldenSys"
#define MyAppURL "https://eldensys.com.br"
#define MyAppExeName "EldenSysAgent.exe"

[Setup]
AppId={{B0E2F3A2-4C7B-4A3E-9F4D-7B6A9F2D3E11}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
DefaultDirName={localappdata}\Programs\EldenSysAgent
DefaultGroupName=EldenSys
DisableProgramGroupPage=yes
OutputDir=..\dist\installer
OutputBaseFilename=EldenSysAgent-Setup-{#MyAppVersion}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
UninstallDisplayIcon={app}\{#MyAppExeName}
ArchitecturesInstallIn64BitMode=x64
SetupIconFile=..\assets\icon.ico

[Languages]
Name: "brazilianportuguese"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"

[Tasks]
Name: "desktopicon"; Description: "Criar atalho na Área de Trabalho"; GroupDescription: "Atalhos:"; Flags: unchecked
Name: "autostart"; Description: "Iniciar automaticamente com o Windows (recomendado)"; GroupDescription: "Inicialização:"

[Files]
Source: "..\dist\EldenSysAgent\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{userprograms}\Desinstalar {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{userdesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

; Auto-start: registra em HKCU\Run para o usuário que instalou.
; Usamos {userappdata} via Run key — funciona sem precisar de admin no boot.
[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
    ValueType: string; ValueName: "EldenSysAgent"; \
    ValueData: """{app}\{#MyAppExeName}"""; \
    Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Iniciar {#MyAppName} agora"; \
    Flags: nowait postinstall skipifsilent

[UninstallRun]
; Mata o processo antes de remover, se estiver rodando
Filename: "{cmd}"; Parameters: "/C taskkill /F /IM {#MyAppExeName}"; Flags: runhidden
