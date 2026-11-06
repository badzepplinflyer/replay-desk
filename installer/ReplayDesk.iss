; Inno Setup script for Replay Desk. Built by tools\build.ps1:
;   ISCC.exe /DAppVersion=1.2.3 installer\ReplayDesk.iss
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
ChangesAssociations=yes
AppId={{6E1B4C2A-7F3D-4B8E-9A51-2C7D0E8F4B19}
AppName=Replay Desk
AppVersion={#AppVersion}
AppVerName=Replay Desk {#AppVersion}
AppPublisher=Alexander Johnson
DefaultDirName={localappdata}\Programs\Replay Desk
DisableProgramGroupPage=yes
DisableDirPage=yes
DisableReadyPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=ReplayDesk-Setup
SetupIconFile=..\build\icon.ico
UninstallDisplayIcon={app}\ReplayDesk.exe
UninstallDisplayName=Replay Desk
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=force
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "startup"; Description: "Start Replay Desk in the tray when Windows starts"; GroupDescription: "Startup:"; Flags: unchecked

[InstallDelete]
; clear files from the previous version so nothing stale is left behind
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\dist\ReplayDesk\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userprograms}\Replay Desk"; Filename: "{app}\ReplayDesk.exe"
Name: "{userdesktop}\Replay Desk"; Filename: "{app}\ReplayDesk.exe"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "ReplayDesk"; \
  ValueData: """{app}\ReplayDesk.exe"" --no-window"; Flags: uninsdeletevalue; Tasks: startup

[Run]
Filename: "{app}\ReplayDesk.exe"; Description: "Open Replay Desk"; Flags: nowait postinstall

[UninstallRun]
Filename: "{cmd}"; Parameters: "/C taskkill /IM ReplayDesk.exe /F"; Flags: runhidden; RunOnceId: "KillApp"
