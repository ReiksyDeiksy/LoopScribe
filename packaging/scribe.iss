#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif

[Setup]
AppId={{70B42536-498D-441C-859E-038F777CCED0}
AppName=scribe
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\scribe
DefaultGroupName=scribe
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=..\dist\installer
OutputBaseFilename=scribe-Setup-{#AppVersion}-windows-x64
SetupIconFile=..\src\recorder_app\assets\scribe.ico
UninstallDisplayIcon={app}\scribe.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
DisableProgramGroupPage=yes

[Languages]
Name: "german"; MessagesFile: "compiler:Languages\German.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
; Only the dedicated PyInstaller output is distributed, never the project folder.
Source: "..\build\installer-app\scribe\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autodesktop}\scribe"; Filename: "{app}\scribe.exe"; WorkingDir: "{app}"
Name: "{autoprograms}\scribe"; Filename: "{app}\scribe.exe"; WorkingDir: "{app}"

; No automatic app launch and no deletion of personal data during uninstall.
