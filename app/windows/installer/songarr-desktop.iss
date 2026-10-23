; The Songarr desktop app for Windows: built by build.py next to this, which passes the version and
; where the built app is. Installs for this person only (no administrator), to their Programs folder.
; The app updates itself by running a newer one of these quietly: it then opens the app again.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef Source
  #define Source "..\..\build\windows\x64\runner\Release"
#endif

[Setup]
AppId={{9C1F0E0B-5A7B-4D6E-9E1A-3F2B8C7D6A51}
AppName=Songarr
AppVersion={#AppVersion}
AppVerName=Songarr {#AppVersion}
AppPublisher=Songarr contributors
DefaultDirName={localappdata}\Programs\Songarr
DefaultGroupName=Songarr
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=yes
PrivilegesRequired=lowest
OutputBaseFilename=Songarr-Setup
SetupIconFile=..\runner\resources\app_icon.ico
UninstallDisplayIcon={app}\Songarr.exe
UninstallDisplayName=Songarr
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=no

[Tasks]
Name: "desktopicon"; Description: "Put a Songarr shortcut on the desktop"; Flags: unchecked

[InstallDelete]
; the last version's app files go first (a file it no longer has mustn't linger)
Type: filesandordirs; Name: "{app}\data"

[Files]
; (not the linker's leftovers: .lib and .exp files are only for building)
Source: "{#Source}\*"; DestDir: "{app}"; Excludes: "*.lib,*.exp"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{autoprograms}\Songarr"; Filename: "{app}\Songarr.exe"
Name: "{autodesktop}\Songarr"; Filename: "{app}\Songarr.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Songarr.exe"; Description: "Open Songarr"; Flags: nowait postinstall skipifsilent
; an update the app ran quietly: open it again
Filename: "{app}\Songarr.exe"; Flags: nowait; Check: WizardSilent
