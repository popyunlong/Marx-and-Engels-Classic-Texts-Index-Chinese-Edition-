#define AppName "马恩文集全集检索程序"
#define AppVersion "1.1.0"
#define AppPublisher "Marx Search"
#define AppExeName "马恩文集全集检索程序.exe"

[Setup]
AppId={{C4C572A8-1AAE-4DB9-8D81-2E5DE5F33E12}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=..\release\installer
OutputBaseFilename=marx-search-full-setup-{#AppVersion}
Compression=lzma
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64
SetupIconFile=..\marx_multisize.ico
UninstallDisplayIcon={app}\{#AppExeName}
DiskSpanning=yes
DiskSliceSize=max

[Files]
Source: "..\release\current\program\{#AppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\release\current\assets\config\*"; DestDir: "{app}\config"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\release\current\assets\data\*"; DestDir: "{app}\data"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\release\current\assets\pdfs\*"; DestDir: "{app}\pdfs"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"

[Run]
Filename: "{app}\{#AppExeName}"; Description: "启动完整资料版"; Flags: nowait postinstall skipifsilent
