; Inno Setup script for modpack-dl.
; Built by CI:  ISCC.exe /DMyAppVersion=1.2.3 installer\modpack-dl.iss
; Expects dist\modpack-dl.exe (PyInstaller --onefile) to exist next to the installer folder.

#define MyAppName "Modpack Downloader"
#ifndef MyAppVersion
  #define MyAppVersion "0.0.0-dev"
#endif
#define MyAppExe "modpack-dl.exe"

[Setup]
AppId={{ECB17FB2-FA8C-412D-81CD-96D795BF35B8}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
DefaultDirName={autopf}\ModpackDl
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; per-user install: no admin rights needed, PATH goes to the user's environment
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ChangesEnvironment=yes
OutputDir=..\installer-out
OutputBaseFilename=modpack-dl-setup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayName={#MyAppName}
WizardStyle=modern

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "addtopath"; Description: "Add modpack-dl to PATH (use it from any terminal)"; Flags: checkedonce
Name: "mrpackmenu"; Description: "Add ""Download mods (modpack-dl)"" to the right-click menu of .mrpack files"; Flags: checkedonce

[Files]
Source: "..\dist\{#MyAppExe}"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\Modpack Downloader (terminal)"; Filename: "{cmd}"; Parameters: "/k ""cd /d %USERPROFILE% && ""{app}\{#MyAppExe}"" --help"""; WorkingDir: "{%USERPROFILE}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"

[Registry]
; Right-click a .mrpack -> download its mods next to the file. cmd /k keeps the window open to read the result.
Root: HKA; Subkey: "Software\Classes\SystemFileAssociations\.mrpack\shell\ModpackDl"; ValueType: string; ValueName: ""; ValueData: "Download mods (modpack-dl)"; Flags: uninsdeletekey; Tasks: mrpackmenu
Root: HKA; Subkey: "Software\Classes\SystemFileAssociations\.mrpack\shell\ModpackDl\command"; ValueType: string; ValueName: ""; ValueData: "cmd.exe /k """"{app}\{#MyAppExe}"" ""%1"""""; Tasks: mrpackmenu

[Code]
const
  EnvKey = 'Environment';

function GetUserPath(): string;
begin
  if not RegQueryStringValue(HKCU, EnvKey, 'Path', Result) then
    Result := '';
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Paths, Dir: string;
begin
  if (CurStep = ssPostInstall) and WizardIsTaskSelected('addtopath') then
  begin
    Dir := ExpandConstant('{app}');
    Paths := GetUserPath();
    if Pos(';' + Lowercase(Dir) + ';', ';' + Lowercase(Paths) + ';') = 0 then
    begin
      if (Paths <> '') and (Paths[Length(Paths)] <> ';') then
        Paths := Paths + ';';
      RegWriteExpandStringValue(HKCU, EnvKey, 'Path', Paths + Dir);
    end;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Paths, Dir: string;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    Dir := ExpandConstant('{app}');
    Paths := ';' + GetUserPath() + ';';
    if StringChangeEx(Paths, ';' + Dir + ';', ';', True) > 0 then
    begin
      while (Length(Paths) > 0) and (Paths[1] = ';') do
        Delete(Paths, 1, 1);
      while (Length(Paths) > 0) and (Paths[Length(Paths)] = ';') do
        Delete(Paths, Length(Paths), 1);
      RegWriteExpandStringValue(HKCU, EnvKey, 'Path', Paths);
    end;
  end;
end;
