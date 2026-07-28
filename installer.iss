#define MyAppName "QSOCapture"
#define MyAppPublisher "SQ3RX"
#define MyAppURL "https://github.com/sq3rx/QSOCapture"
#define MyAppExeName "QSOCapture.exe"

; If a version is passed on the command line (e.g. iscc installer.iss /dMyAppVersion=1.2.3),
; it overrides the default value below.
#ifndef MyAppVersion
  #define MyAppVersion "0.6.0beta"
#endif

; Optional suffix in the output filename to tell builds apart (e.g.
; "-Win7" for the legacy Windows 7/8 build). Empty by default.
#ifndef MyAppNameSuffix
  #define MyAppNameSuffix ""
#endif

; Application folder name (without .exe). PyInstaller onedir produces a folder
; named e.g. "QSOCapture-portable-0.5.0beta" containing the EXE and all
; supporting DLLs. CI must pass /dMyExeName="QSOCapture-portable-0.5.0beta".
#ifndef MyExeName
  #define MyExeName "QSOCapture"
#endif

[Setup]
; Unique application identifier (GUID) — do not change it.
AppId={{8F3C9A1E-2B4D-4E7A-9C6F-1A2B3C4D5E6F}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
AllowNoIcons=yes
LicenseFile=
OutputDir=installer
OutputBaseFilename=QSOCapture{#MyAppNameSuffix}-setup-{#MyAppVersion}
SetupIconFile=icon.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64os
PrivilegesRequired=admin

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
; The portable build in dist/ is now a folder (onedir) carrying the version
; (e.g. QSOCapture-portable-0.5.0beta/). The EXE inside is always named
; QSOCapture.exe (stable name), so we copy the entire folder contents.
Source: "dist\{#MyExeName}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "README.md"; DestDir: "{app}"; Flags: ignoreversion isreadme
Source: "icon.ico"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop icon"; GroupDescription: "Additional icons:"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Run {#MyAppName}"; Flags: nowait postinstall skipifsilent

; NOTE: We intentionally do NOT add an [UninstallDelete] section. The
; user's recordings/ (audio files), qsos.db (log database) and config.cfg
; are personal data and must survive an uninstall. Inno Setup only removes
; files it installed (the EXE + README); the user data is left intact so a
; re-install (or manual cleanup) does not destroy recorded QSOs.