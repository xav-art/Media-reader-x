; Inno Setup script: per-user installer that never asks for admin rights.
; Built by build.ps1, which passes /DAppVersion=x.y.z and generates associations.iss.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{C4F1A9E7-3B62-4D8A-A5E0-7F2C9B1D6E58}
AppName=Media Reader x
AppVersion={#AppVersion}
AppVerName=Media Reader x {#AppVersion}
AppPublisher=xav-art
AppPublisherURL=https://github.com/xav-art/Media-reader-x
AppSupportURL=https://github.com/xav-art/Media-reader-x/issues
VersionInfoVersion={#AppVersion}
; Per-user install into %LOCALAPPDATA%\Programs: no UAC prompt, no admin rights.
PrivilegesRequired=lowest
DefaultDirName={autopf}\Media Reader x
DisableProgramGroupPage=yes
DisableDirPage=auto
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
OutputDir=..\dist
OutputBaseFilename=MediaReaderX-{#AppVersion}-Setup
SetupIconFile=..\src\mediareaderx\assets\app.ico
UninstallDisplayIcon={app}\MediaReaderX.exe
UninstallDisplayName=Media Reader x
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ChangesAssociations=yes
; Close a running copy during upgrades so files are not locked.
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "associate"; Description: "Add Media Reader x to ""Open with"" for HEIC, AVIF, JPEG XL, RAW, MKV, WebM and MOV files"; GroupDescription: "File types:"
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked

[Files]
Source: "..\build\dist\MediaReaderX\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\LICENSE"; DestDir: "{app}"; DestName: "LICENSE.txt"; Flags: ignoreversion
Source: "..\THIRD_PARTY_NOTICES.txt"; DestDir: "{app}"; Flags: ignoreversion

[InstallDelete]
; Remove files from a previous version so stale DLLs never linger.
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{autoprograms}\Media Reader x"; Filename: "{app}\MediaReaderX.exe"; Comment: "View HEIC, AVIF, JPEG XL, RAW and modern video"
Name: "{autodesktop}\Media Reader x"; Filename: "{app}\MediaReaderX.exe"; Tasks: desktopicon

#include "associations.iss"

[Run]
Filename: "{app}\MediaReaderX.exe"; Description: "Launch Media Reader x"; Flags: nowait postinstall skipifsilent

[Code]
// Version 1.0 shipped as "Media Reader" under a different AppId. Remove it
// silently so users never end up with both apps and duplicate "Open with" entries.
const
  LegacyUninstallKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{6B1E3C52-7A4D-4F0B-9C8E-2D5A1F7E9B34}_is1';

procedure RemoveLegacyMediaReader();
var
  UninstallString: String;
  ResultCode: Integer;
begin
  if RegQueryStringValue(HKCU, LegacyUninstallKey, 'UninstallString', UninstallString) then
  begin
    Log('Removing legacy Media Reader 1.0: ' + UninstallString);
    if not Exec(RemoveQuotes(UninstallString), '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART', '',
                SW_HIDE, ewWaitUntilTerminated, ResultCode) then
      Log('Legacy uninstall could not start, code ' + IntToStr(ResultCode))
    else if ResultCode <> 0 then
    begin
      // Usually Media Reader 1.0 is still open. Install anyway, but tell the user how to tidy up.
      Log('Legacy uninstall failed, code ' + IntToStr(ResultCode));
      SuppressibleMsgBox('The old "Media Reader" 1.0 could not be removed. Close it, then uninstall it in ' +
        'Settings > Apps. Media Reader x installs anyway.', mbInformation, MB_OK, IDOK);
    end;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then
    RemoveLegacyMediaReader();
end;
