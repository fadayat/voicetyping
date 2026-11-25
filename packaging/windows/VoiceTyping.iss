#ifndef AppVersion
  #define AppVersion "2.3.2"
#endif
#ifndef SourceDir
  #define SourceDir "..\..\dist\VoiceTyping"
#endif
#ifndef OutputDir
  #define OutputDir "..\..\dist"
#endif

[Setup]
AppId={{E9B52FA9-878B-4DE9-ACAE-9B5A1605EE41}
AppName=VoiceTyping
AppVersion={#AppVersion}
AppVerName=VoiceTyping {#AppVersion}
AppPublisher=fadayat
AppPublisherURL=https://github.com/fadayat/voicetyping
AppSupportURL=https://github.com/fadayat/voicetyping/issues
AppUpdatesURL=https://github.com/fadayat/voicetyping/releases
DefaultDirName={localappdata}\Programs\VoiceTyping
DefaultGroupName=VoiceTyping
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
AppMutex=Local\VoiceTyping.Windows.v1
CloseApplications=yes
CloseApplicationsFilter=*.exe,*.dll
RestartApplications=no
AllowNoIcons=yes
LicenseFile=..\..\LICENSE
OutputDir={#OutputDir}
OutputBaseFilename=VoiceTyping-{#AppVersion}-windows-x64-setup
UninstallDisplayIcon={app}\VoiceTyping.exe
UninstallDisplayName=VoiceTyping
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\VoiceTyping"; Filename: "{app}\VoiceTyping.exe"
Name: "{group}\Uninstall VoiceTyping"; Filename: "{uninstallexe}"
Name: "{autodesktop}\VoiceTyping"; Filename: "{app}\VoiceTyping.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\VoiceTyping.exe"; Description: "{cm:LaunchProgram,VoiceTyping}"; Flags: nowait postinstall skipifsilent

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  RunSubKey: String;
  RunValue: String;
  ExePath: String;
  UnquotedCmd: String;
  QuotedCmd: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    RunSubKey := 'Software\Microsoft\Windows\CurrentVersion\Run';
    if RegQueryStringValue(HKCU, RunSubKey, 'VoiceTyping', RunValue) then
    begin
      ExePath := ExpandConstant('{app}\VoiceTyping.exe');
      UnquotedCmd := ExePath + ' --hidden';
      QuotedCmd := '"' + ExePath + '" --hidden';
      if (CompareText(RunValue, UnquotedCmd) = 0) or (CompareText(RunValue, QuotedCmd) = 0) then
      begin
        RegDeleteValue(HKCU, RunSubKey, 'VoiceTyping');
      end;
    end;
  end;
end;
