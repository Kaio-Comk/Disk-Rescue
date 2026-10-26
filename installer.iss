; Inno Setup Script para o DiskRescue v0.3.3
; Desenvolvido por Kaio Lima <kaiocomkamil@gmail.com>

#define MyAppName "DiskRescue"
#define MyAppVersion "0.3.3"
#define MyAppPublisher "Kaio Lima"
#define MyAppURL "https://github.com/Kaio-Comk/Disk-Rescue"
#define MyAppExeName "DiskRescue.exe"

[Setup]
AppId={{9B78A52C-5F1E-4B52-870D-9E9A683E1234}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
AllowNoIcons=yes
OutputDir=B:\Projetos\Disk Rescue\dist
OutputBaseFilename=DiskRescue-Setup-v0.3.3
SetupIconFile=B:\Projetos\Disk Rescue\gui\assets\diskrescue.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
WizardResizable=no
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64compatible
DisableProgramGroupPage=yes

[Languages]
Name: "brazilianportuguese"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "B:\Projetos\Disk Rescue\dist\DiskRescue.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "B:\Projetos\Disk Rescue\gui\assets\diskrescue.ico"; DestDir: "{app}\assets"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\assets\diskrescue.ico"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\assets\diskrescue.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent
