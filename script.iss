; Script for Gui_AkgXtract - Clean Per-User Setup
#define MyAppName "Gui_AkgXtract"
#ifndef MyAppVersion
  #define MyAppVersion "1.1.2.1"
#endif
#define MyAppPublisher "Medoomem"
#define MyAppExeName "guiextract.exe"

[Setup]
AppId={{8024821E-9D5D-40DE-9804-C12DAF652032}
VersionInfoVersion={#MyAppVersion}
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription="Universal game archive extractor"
VersionInfoCopyright="Copyright (C) 2024 Medoomem"
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
; Install to User's local folder (No Admin rights required)
DefaultDirName={userpf}\{#MyAppName}
PrivilegesRequired=lowest
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\{#MyAppExeName}
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Output settings
OutputDir=Output
OutputBaseFilename=Gui_AkgXtract_Setup
SetupIconFile=dist\guiextract\downloader_icon.ico
Compression=lzma2/max
InternalCompressLevel=ultra
SolidCompression=yes
WizardStyle=modern

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; 1. The main executable
Source: "dist\guiextract\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion
; 2. Include everything else (DLLs, backend folder, etc.)
Source: "dist\guiextract\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent runasoriginaluser