; Songarr's Windows installer (Inno Setup 6). Build it with installer\build.py, which stages the files
; and passes AppVersion and Stage.
;
; Unattended installs: Songarr-Setup.exe /VERYSILENT /DIR="..." /TASKS="desktopicon,autostart"
;   /NAME="Family Tunes" /LOGO="C:\logo.png" /MUSICDIR="D:\Music" /DATADIR="D:\Songarr data"
;   /PORT=8484 /APPPORT=8486 /LAN=1

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef Stage
  #define Stage "..\build\installer"
#endif
#define Art Stage + "\art"

[Setup]
AppId={{6F1D2C7A-3B58-4E0A-9D6C-2A7E5B91C4D3}
AppName=Songarr
AppVersion={#AppVersion}
AppVerName=Songarr {#AppVersion}
AppPublisher=Songarr
AppComments=Your family's own music server: the songs you like on Spotify, downloaded once and streamed to your phones.
DefaultDirName={autopf}\Songarr
DisableProgramGroupPage=yes
UsePreviousAppDir=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputBaseFilename=Songarr-Setup-{#AppVersion}
SetupIconFile=..\songarr\assets\songarr.ico
UninstallDisplayIcon={app}\songarr\assets\songarr.ico
UninstallDisplayName={code:UninstallName}
; light or dark, as Windows is set
WizardStyle=modern dynamic
WizardSizePercent=120
DisableWelcomePage=no
WizardImageFile={#Art}\wizard-164.png,{#Art}\wizard-205.png,{#Art}\wizard-246.png,{#Art}\wizard-328.png
WizardSmallImageFile={#Art}\small-55.png,{#Art}\small-69.png,{#Art}\small-83.png,{#Art}\small-110.png
Compression=lzma2/ultra64
SolidCompression=yes
LZMAUseSeparateProcess=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
CloseApplications=no
SetupLogging=yes
VersionInfoVersion={#AppVersion}
VersionInfoDescription=Songarr setup

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Messages]
WelcomeLabel2=This installs [name/ver] on this PC.%n%nSongarr is your family's own music server. It downloads the songs you like on Spotify into a music folder and streams them to the Songarr app on your phones.%n%nPython and everything else Songarr needs comes with it.
FinishedLabel=Songarr is installed. Open it to connect Spotify and pair your phones; the setup guide (README) explains each step.

[Tasks]
Name: "desktopicon"; Description: "Put a shortcut on the &desktop"; GroupDescription: "Shortcuts:"
Name: "startmenu"; Description: "Add it to the &Start menu"; GroupDescription: "Shortcuts:"
Name: "autostart"; Description: "Start it &whenever I sign in to Windows (phones can play whenever this PC is on)"; GroupDescription: "Starting:"
Name: "ffmpeg"; Description: "Install &FFmpeg with winget (Songarr needs it to save music)"; GroupDescription: "Also needed:"; Check: FfmpegMissing
Name: "deno"; Description: "Install D&eno with winget (YouTube needs a JavaScript runtime)"; GroupDescription: "Also needed:"; Check: JsRuntimeMissing

[Files]
Source: "{#Stage}\app\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Dirs]
; Songarr keeps its Python packages current itself, and runs as the signed-in user
Name: "{app}\runtime"; Permissions: users-modify
Name: "{code:DataDir}"; Permissions: users-modify; Flags: uninsneveruninstall

[Registry]
; what the uninstaller needs to stop Songarr and offer to remove its data
Root: HKA; Subkey: "Software\Songarr"; ValueType: string; ValueName: "DataDir"; ValueData: "{code:DataDir}"; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Songarr"; ValueType: string; ValueName: "Port"; ValueData: "{code:AdminPort}"

[Run]
Filename: "{app}\runtime\pythonw.exe"; Parameters: "-m songarr --open {code:ServerArgs}"; WorkingDir: "{app}"; Description: "{code:LaunchLabel}"; Flags: postinstall nowait skipifsilent
Filename: "{app}\README.md"; Description: "Read the setup guide"; Flags: postinstall shellexec skipifsilent unchecked

[UninstallDelete]
; compiled files and package updates Songarr made after installing
Type: filesandordirs; Name: "{app}\runtime"
Type: filesandordirs; Name: "{app}\songarr"

[Code]
var
  NamePage: TInputQueryWizardPage;
  LogoEdit: TNewEdit;
  LogoPreview: TBitmapImage;
  LogoNote: TNewStaticText;
  FoldersPage: TInputDirWizardPage;
  NetworkPage: TInputQueryWizardPage;
  LanCheck: TNewCheckBox;
  InitialName, InitialMusic: String;
  UninstallData: String;

function Param(const Name, Default: String): String;
begin
  Result := ExpandConstant('{param:' + Name + '|}');
  if Result = '' then
    Result := Default;
end;

function Quote(const S: String): String;
begin
  Result := '"' + RemoveBackslashUnlessRoot(Trim(S)) + '"';
end;

function IsUpgrade: Boolean;
begin
  Result := GetPreviousData('DataDir', '') <> '';
end;

function BrandName(Param: String): String;
begin
  Result := Trim(NamePage.Values[0]);
end;

function UninstallName(Param: String): String;
begin
  if BrandName('') = 'Songarr' then
    Result := 'Songarr'
  else
    Result := BrandName('') + ' (Songarr)';
end;

function LaunchLabel(Param: String): String;
begin
  Result := 'Open ' + BrandName('') + ' now';
end;

function DataDir(Param: String): String;
begin
  Result := RemoveBackslashUnlessRoot(Trim(FoldersPage.Values[1]));
end;

function AdminPort(Param: String): String;
begin
  Result := Trim(NetworkPage.Values[0]);
end;

{ What Songarr is started with: where its data is, and any ports or network setting that isn't the default. }
function ServerArgs(Param: String): String;
begin
  Result := '--data ' + Quote(DataDir(''));
  if AdminPort('') <> '8484' then
    Result := Result + ' --port ' + AdminPort('');
  if Trim(NetworkPage.Values[1]) <> '8486' then
    Result := Result + ' --app-port ' + Trim(NetworkPage.Values[1]);
  if LanCheck.Checked then
    Result := Result + ' --app-host 0.0.0.0';
end;

function Found(const Command: String): Boolean;
var
  Code: Integer;
begin
  Result := Exec(ExpandConstant('{cmd}'), '/c where ' + Command + ' >nul 2>nul', '', SW_HIDE, ewWaitUntilTerminated, Code)
    and (Code = 0);
end;

function FfmpegMissing: Boolean;
begin
  Result := Found('winget') and not Found('ffmpeg')
    and not DirExists(ExpandConstant('{localappdata}\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe'));
end;

function JsRuntimeMissing: Boolean;
begin
  Result := Found('winget') and not Found('deno') and not Found('node') and not Found('bun');
end;

function PortInUse(Port: Integer): Boolean;
var
  Code: Integer;
begin
  Result := Exec('powershell.exe', '-NoProfile -NonInteractive -Command "if (Get-NetTCPConnection -State Listen -LocalPort '
    + IntToStr(Port) + ' -ErrorAction SilentlyContinue) { exit 1 } else { exit 0 }"', '', SW_HIDE, ewWaitUntilTerminated, Code)
    and (Code = 1);
end;

{ A look at the chosen logo (PNG and BMP pictures; the others still work, there's just no preview). }
procedure ShowLogo(Sender: TObject);
var
  FileName, Ext: String;
begin
  FileName := Trim(LogoEdit.Text);
  Ext := LowerCase(ExtractFileExt(FileName));
  LogoPreview.Visible := False;
  LogoNote.Caption := '';
  if (FileName = '') or not FileExists(FileName) then
    Exit;
  try
    if Ext = '.png' then
      LogoPreview.PngImage.LoadFromFile(FileName)
    else if Ext = '.bmp' then
      LogoPreview.Bitmap.LoadFromFile(FileName)
    else
    begin
      LogoNote.Caption := '(no preview for this kind of picture, but it will work)';
      Exit;
    end;
    LogoPreview.Visible := True;
  except
    LogoNote.Caption := '(this picture couldn''t be shown)';
  end;
end;

procedure LogoBrowse(Sender: TObject);
var
  FileName: String;
begin
  FileName := LogoEdit.Text;
  if GetOpenFileName('Choose a logo', FileName, '',
    'Pictures (*.png;*.jpg;*.jpeg;*.webp;*.gif;*.bmp;*.ico)|*.png;*.jpg;*.jpeg;*.webp;*.gif;*.bmp;*.ico|All files (*.*)|*.*', 'png') then
    LogoEdit.Text := FileName;  { OnChange shows it }
end;

procedure InitializeWizard;
var
  LogoLabel, LogoHint, LanHint: TNewStaticText;
  LogoButton: TNewButton;
begin
  { Name and logo }
  NamePage := CreateInputQueryPage(wpSelectDir, 'Name and logo', 'What should your family''s music server be called?',
    'The name and logo appear in the admin website, the phone app, notifications, the shortcuts and the icon by the clock. ' +
    'You can change them any time in the admin website: Settings, Name and icon.');
  NamePage.Add('&Name:', False);
  InitialName := GetPreviousData('BrandName', 'Songarr');
  NamePage.Values[0] := Param('NAME', InitialName);

  LogoLabel := TNewStaticText.Create(NamePage);
  LogoLabel.Parent := NamePage.Surface;
  LogoLabel.Top := NamePage.Edits[0].Top + NamePage.Edits[0].Height + ScaleY(18);
  LogoLabel.Caption := '&Logo (optional):';
  LogoEdit := TNewEdit.Create(NamePage);
  LogoEdit.Parent := NamePage.Surface;
  LogoEdit.Top := LogoLabel.Top + LogoLabel.Height + ScaleY(6);
  LogoEdit.Width := NamePage.SurfaceWidth - ScaleX(96);
  LogoEdit.Text := Param('LOGO', '');
  LogoLabel.FocusControl := LogoEdit;
  LogoButton := TNewButton.Create(NamePage);
  LogoButton.Parent := NamePage.Surface;
  LogoButton.Caption := 'B&rowse...';
  LogoButton.Left := NamePage.SurfaceWidth - ScaleX(86);
  LogoButton.Top := LogoEdit.Top - ScaleY(1);
  LogoButton.Width := ScaleX(86);
  LogoButton.Height := WizardForm.NextButton.Height;
  LogoButton.OnClick := @LogoBrowse;
  LogoHint := TNewStaticText.Create(NamePage);
  LogoHint.Parent := NamePage.Surface;
  LogoHint.Top := LogoEdit.Top + LogoEdit.Height + ScaleY(8);
  LogoHint.Width := NamePage.SurfaceWidth;
  LogoHint.AutoSize := False;
  LogoHint.WordWrap := True;
  LogoHint.Height := ScaleY(48);
  LogoHint.Caption := 'A square picture, 512 x 512 or bigger, looks best; a logo on a see-through background works well. ' +
    'PNG, JPEG, WebP, GIF, BMP or ICO. Leave it empty for Songarr''s own icon.';
  LogoPreview := TBitmapImage.Create(NamePage);
  LogoPreview.Parent := NamePage.Surface;
  LogoPreview.Top := LogoHint.Top + LogoHint.Height + ScaleY(4);
  LogoPreview.Width := ScaleX(64);
  LogoPreview.Height := ScaleY(64);
  LogoPreview.Stretch := True;
  LogoPreview.BackColor := clNone;
  LogoPreview.Visible := False;
  LogoNote := TNewStaticText.Create(NamePage);
  LogoNote.Parent := NamePage.Surface;
  LogoNote.Top := LogoPreview.Top;
  LogoNote.Caption := '';
  LogoEdit.OnChange := @ShowLogo;
  ShowLogo(nil);

  { Folders }
  FoldersPage := CreateInputDirPage(NamePage.ID, 'Folders', 'Where should Songarr keep music and its own files?',
    'Music already in the music folder (from anywhere) is found and used instead of being downloaded again. ' +
    'A network share works too.', False, '');
  FoldersPage.Add('&Music folder (songs are saved as Artist\Album\Song):');
  FoldersPage.Add('&Data folder (Songarr''s database, settings and logs):');
  InitialMusic := GetPreviousData('MusicDir', ExpandConstant('{%USERPROFILE}\Music\Songarr'));
  FoldersPage.Values[0] := Param('MUSICDIR', InitialMusic);
  FoldersPage.Values[1] := Param('DATADIR', GetPreviousData('DataDir', ExpandConstant('{commonappdata}\Songarr')));

  { Network (advanced) }
  NetworkPage := CreateInputQueryPage(FoldersPage.ID, 'Network (advanced)', 'Which ports should Songarr use?',
    'The defaults suit almost everyone. The admin website is only ever reachable from this PC; phones reach the app port ' +
    'through your own web address (the setup guide shows two free ways: Cloudflare Tunnel or Tailscale).');
  NetworkPage.Add('&Admin website port (this PC only):', False);
  NetworkPage.Add('A&pp port (what phones connect to):', False);
  NetworkPage.Values[0] := Param('PORT', GetPreviousData('Port', '8484'));
  NetworkPage.Values[1] := Param('APPPORT', GetPreviousData('AppPort', '8486'));
  LanCheck := TNewCheckBox.Create(NetworkPage);
  LanCheck.Parent := NetworkPage.Surface;
  LanCheck.Top := NetworkPage.Edits[1].Top + NetworkPage.Edits[1].Height + ScaleY(18);
  LanCheck.Width := NetworkPage.SurfaceWidth;
  LanCheck.Height := ScaleY(20);
  LanCheck.Caption := 'Also let phones on this &home network connect straight to this PC';
  LanCheck.Checked := Param('LAN', GetPreviousData('Lan', '0')) = '1';
  LanHint := TNewStaticText.Create(NetworkPage);
  LanHint.Parent := NetworkPage.Surface;
  LanHint.Top := LanCheck.Top + LanCheck.Height + ScaleY(4);
  LanHint.Left := ScaleX(18);
  LanHint.Width := NetworkPage.SurfaceWidth - ScaleX(18);
  LanHint.AutoSize := False;
  LanHint.WordWrap := True;
  LanHint.Height := ScaleY(48);
  LanHint.Caption := 'For use at home without a web address: phones connect to this PC''s name or address and the app port. ' +
    'Windows asks once whether to let it through the firewall.';
end;

procedure RegisterPreviousData(PreviousDataKey: Integer);
begin
  SetPreviousData(PreviousDataKey, 'BrandName', BrandName(''));
  SetPreviousData(PreviousDataKey, 'MusicDir', Trim(FoldersPage.Values[0]));
  SetPreviousData(PreviousDataKey, 'DataDir', DataDir(''));
  SetPreviousData(PreviousDataKey, 'Port', AdminPort(''));
  SetPreviousData(PreviousDataKey, 'AppPort', Trim(NetworkPage.Values[1]));
  if LanCheck.Checked then
    SetPreviousData(PreviousDataKey, 'Lan', '1')
  else
    SetPreviousData(PreviousDataKey, 'Lan', '0');
end;

function PortOk(const Value, Previous, What: String): Boolean;
var
  Port: Integer;
begin
  Port := StrToIntDef(Trim(Value), 0);
  Result := True;
  if (Trim(Value) <> Previous) and PortInUse(Port) then
    Result := SuppressibleMsgBox(Format('Port %d (the %s) is already in use on this PC. Is Songarr already running? ' +
      'It won''t start on a port that''s taken.%n%nUse port %d anyway?', [Port, What, Port]),
      mbConfirmation, MB_YESNO or MB_DEFBUTTON2, IDYES) = IDYES;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  Name: String;
  I, P1, P2: Integer;
begin
  Result := True;
  if (CurPageID = wpSelectDir) and (Length(WizardDirValue) > 130) then
  begin
    SuppressibleMsgBox('Choose a shorter folder (up to 130 characters): Windows can''t reach files in folders nested that deep.',
      mbError, MB_OK, IDOK);
    Result := False;
  end
  else if CurPageID = NamePage.ID then
  begin
    Name := BrandName('');
    if (Name = '') or (Length(Name) > 30) then
    begin
      SuppressibleMsgBox('Give it a name of up to 30 characters.', mbError, MB_OK, IDOK);
      Result := False;
      Exit;
    end;
    for I := 1 to Length(Name) do
      if Pos(Name[I], '<>"\/|?*:') > 0 then
      begin
        SuppressibleMsgBox('The name can''t contain < > " \ / | ? * or :', mbError, MB_OK, IDOK);
        Result := False;
        Exit;
      end;
    if (Trim(LogoEdit.Text) <> '') and not FileExists(Trim(LogoEdit.Text)) then
    begin
      SuppressibleMsgBox('That logo file wasn''t found.', mbError, MB_OK, IDOK);
      Result := False;
    end;
  end
  else if CurPageID = FoldersPage.ID then
  begin
    if (Trim(FoldersPage.Values[0]) = '') or (Trim(FoldersPage.Values[1]) = '') then
    begin
      SuppressibleMsgBox('Choose both folders.', mbError, MB_OK, IDOK);
      Result := False;
    end;
  end
  else if CurPageID = NetworkPage.ID then
  begin
    P1 := StrToIntDef(Trim(NetworkPage.Values[0]), 0);
    P2 := StrToIntDef(Trim(NetworkPage.Values[1]), 0);
    if (P1 < 1024) or (P1 > 65535) or (P2 < 1024) or (P2 > 65535) or (P1 = P2) then
    begin
      SuppressibleMsgBox('Ports are numbers from 1024 to 65535, and the two must be different.', mbError, MB_OK, IDOK);
      Result := False;
      Exit;
    end;
    Result := PortOk(NetworkPage.Values[0], GetPreviousData('Port', ''), 'admin website')
      and PortOk(NetworkPage.Values[1], GetPreviousData('AppPort', ''), 'app port');
  end;
end;

function UpdateReadyMemo(Space, NewLine, MemoUserInfoInfo, MemoDirInfo, MemoTypeInfo, MemoComponentsInfo,
  MemoGroupInfo, MemoTasksInfo: String): String;
var
  Logo: String;
begin
  Logo := Trim(LogoEdit.Text);
  if Logo = '' then
    Logo := 'Songarr''s own icon';
  Result := MemoDirInfo + NewLine + NewLine +
    'Name and logo:' + NewLine + Space + BrandName('') + NewLine + Space + Logo + NewLine + NewLine +
    'Folders:' + NewLine + Space + 'Music: ' + Trim(FoldersPage.Values[0]) + NewLine + Space + 'Data: ' + DataDir('') + NewLine + NewLine +
    'Network:' + NewLine + Space + 'Admin website: http://127.0.0.1:' + AdminPort('') + NewLine + Space + 'App port: ' +
    Trim(NetworkPage.Values[1]);
  if LanCheck.Checked then
    Result := Result + ' (also on the home network)';
  if MemoTasksInfo <> '' then
    Result := Result + NewLine + NewLine + MemoTasksInfo;
end;

{ Before files are replaced: stop the Songarr this installs over. }
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Python: String;
  Code: Integer;
begin
  Result := '';
  Python := ExpandConstant('{app}\runtime\python.exe');
  if FileExists(Python) then
    Exec(Python, '-m songarr --stop --port ' + GetPreviousData('Port', '8484'), ExpandConstant('{app}'), SW_HIDE,
      ewWaitUntilTerminated, Code);
end;

procedure Step(const Status, Params, What: String);
var
  Code: Integer;
begin
  WizardForm.StatusLabel.Caption := Status;
  if not Exec(ExpandConstant('{app}\runtime\python.exe'), Params, ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, Code)
    or (Code <> 0) then
    SuppressibleMsgBox('Songarr couldn''t ' + What + ' (error ' + IntToStr(Code) + '). You can do it later in the admin website.',
      mbInformation, MB_OK, IDOK);
end;

procedure Winget(const Status, Package: String);
var
  Code: Integer;
begin
  WizardForm.StatusLabel.Caption := Status;
  if not Exec('winget.exe', 'install --id ' + Package + ' --exact --silent --accept-package-agreements --accept-source-agreements ' +
    '--disable-interactivity', '', SW_HIDE, ewWaitUntilTerminated, Code) or ((Code <> 0) and (Code <> -1978335189)) then
    SuppressibleMsgBox('Couldn''t install ' + Package + ' with winget (error ' + IntToStr(Code) + '). The setup guide shows how ' +
      'to install it by hand.', mbInformation, MB_OK, IDOK);
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Base, Shortcuts: String;
begin
  if CurStep <> ssPostInstall then
    Exit;
  if WizardIsTaskSelected('ffmpeg') then
    Winget('Installing FFmpeg (this can take a minute)...', 'Gyan.FFmpeg');
  if WizardIsTaskSelected('deno') then
    Winget('Installing Deno...', 'DenoLand.Deno');
  Base := '-m songarr --data ' + Quote(DataDir(''));
  if not IsUpgrade or (Trim(FoldersPage.Values[0]) <> InitialMusic) then
    Step('Setting up the music folder...', Base + ' --music-folder ' + Quote(FoldersPage.Values[0]), 'set the music folder');
  if (BrandName('') <> InitialName) or (Trim(LogoEdit.Text) <> '') then
  begin
    if Trim(LogoEdit.Text) <> '' then
      Step('Applying the name and logo...', Base + ' --brand-name ' + Quote(BrandName('')) + ' --brand-icon ' + Quote(LogoEdit.Text),
        'use that name and logo')
    else
      Step('Applying the name...', Base + ' --brand-name ' + Quote(BrandName('')), 'use that name');
  end;
  Shortcuts := '';
  if WizardIsTaskSelected('desktopicon') then
    Shortcuts := Shortcuts + ' --shortcut';
  if WizardIsTaskSelected('startmenu') then
    Shortcuts := Shortcuts + ' --start-menu';
  if WizardIsTaskSelected('autostart') then
    Shortcuts := Shortcuts + ' --autostart';
  if Shortcuts <> '' then
    Step('Making the shortcuts...', '-m songarr ' + ServerArgs('') + Shortcuts, 'make the shortcuts');
end;

{ Uninstalling: stop Songarr, remove its shortcuts, and offer to remove its data (never the music). }
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Python, Port: String;
  Code: Integer;
begin
  if CurUninstallStep = usUninstall then
  begin
    if not RegQueryStringValue(HKA, 'Software\Songarr', 'Port', Port) then
      Port := '8484';
    if not RegQueryStringValue(HKA, 'Software\Songarr', 'DataDir', UninstallData) then
      UninstallData := '';
    Python := ExpandConstant('{app}\runtime\python.exe');
    if FileExists(Python) then
    begin
      Exec(Python, '-m songarr --stop --port ' + Port, ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, Code);
      Exec(Python, '-m songarr --remove-shortcuts', ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, Code);
    end;
  end
  else if (CurUninstallStep = usPostUninstall) and (UninstallData <> '') and DirExists(UninstallData) then
  begin
    if SuppressibleMsgBox('Also delete Songarr''s data (its database, settings, sign-ins and logs in ' + UninstallData + ')?' + #13#10#13#10 +
      'Your music folder is kept either way.', mbConfirmation, MB_YESNO or MB_DEFBUTTON2, IDNO) = IDYES then
      DelTree(UninstallData, True, True, True);
  end;
end;
