; Atlas for Windows: the installer. Built by .github/workflows/build.yml after PyInstaller has made dist\Atlas:
;   iscc /DVersion=1.2.0 windows\atlas.iss
; Installs for you alone (no administrator needed), into %LOCALAPPDATA%\Programs\Atlas.

#ifndef Version
  #define Version "0.0.0"
#endif

[Setup]
AppId={{9466FEAF-34C9-4D55-94DA-49DC3BF616FB}
AppName=Atlas
AppVersion={#Version}
AppPublisher=kanibusrex
AppPublisherURL=https://github.com/kanibusrex/atlas-linux
DefaultDirName={localappdata}\Programs\Atlas
DefaultGroupName=Atlas
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=Atlas-{#Version}-windows-x64-setup
SetupIconFile=..\atlas.ico
UninstallDisplayIcon={app}\Atlas.exe
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; Flags: unchecked

[Files]
Source: "..\dist\Atlas\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{autoprograms}\Atlas"; Filename: "{app}\Atlas.exe"
Name: "{autodesktop}\Atlas"; Filename: "{app}\Atlas.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Atlas.exe"; Description: "{cm:LaunchProgram,Atlas}"; Flags: nowait postinstall skipifsilent
