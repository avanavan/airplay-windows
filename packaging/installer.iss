; Inno Setup script for the airplay-windows installer.
; Built by scripts/build.ps1:  ISCC /DAppVersion=1.2.3 packaging\installer.iss
; Expects the PyInstaller output in dist\airplay-windows and the icon in build\.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#define AppName "AirPlay for Windows"
#define AppExe "airplay-windows.exe"
#define FirewallRule "AirPlay for Windows"

[Setup]
AppId={{8F3C2A51-6E0B-4C7D-9A1E-2B5D7C4F9E31}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=airplay-windows
DefaultDirName={autopf}\airplay-windows
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=airplay-windows-{#AppVersion}-setup
SetupIconFile=..\build\airplay-windows.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
; Admin rights: installs to Program Files and adds the firewall rule.
PrivilegesRequired=admin
UsedUserAreasWarning=no
; The app holds this mutex while running, so setup and uninstall ask to close it first.
AppMutex=airplay-windows
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "startup"; Description: "Start {#AppName} when I sign in (in the notification area)"; GroupDescription: "Startup:"; Flags: unchecked

[Files]
Source: "..\dist\airplay-windows\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; Files from an older version that the new one no longer ships.
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon
Name: "{userstartup}\{#AppName}"; Filename: "{app}\{#AppExe}"; Parameters: "--minimized"; Tasks: startup

[Run]
; Speakers send clock-sync and retransmission requests over UDP; let them in from the local network only.
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""{#FirewallRule}"""; Flags: runhidden
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""{#FirewallRule}"" dir=in action=allow program=""{app}\{#AppExe}"" protocol=UDP remoteip=localsubnet profile=any enable=yes"; Flags: runhidden; StatusMsg: "Allowing AirPlay speakers through Windows Firewall..."
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""{#FirewallRule}"""; Flags: runhidden; RunOnceId: "RemoveFirewallRule"
