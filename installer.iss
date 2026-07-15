#define MyAppName "QSOCapture"
#define MyAppVersion "1.1.0"
#define MyAppPublisher "SQ3RX"
#define MyAppURL "https://github.com/sq3rx/QSOCapture"
#define MyAppExeName "QSOCapture.exe"

; Jeśli podano wersję z linii komend (np. iscc installer.iss /dMyAppVersion=1.2.3),
; to nadpisuje wartość domyślną powyżej.
#ifndef MyAppVersion
  #define MyAppVersion "1.1.0"
#endif

[Setup]
; Unikalny identyfikator aplikacji (GUID) — nie zmieniaj go.
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
OutputBaseFilename=QSOCapture-setup-{#MyAppVersion}
SetupIconFile=
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64
PrivilegesRequired=admin

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "polish"; MessagesFile: "compiler:Languages\Polish.isl"

[Files]
Source: "dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "README.md"; DestDir: "{app}"; Flags: ignoreversion isreadme

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{#MyAppName} (WebView2)"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Utwórz ikonę na pulpicie"; GroupDescription: "Dodatkowe ikony:"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Uruchom {#MyAppName}"; Flags: nowait postinstall skipifsilent

; NOTE: We intentionally do NOT add an [UninstallDelete] section. The
; user's recordings/ (audio files), qsos.db (log database) and config.cfg
; are personal data and must survive an uninstall. Inno Setup only removes
; files it installed (the EXE + README); the user data is left intact so a
; re-install (or manual cleanup) does not destroy recorded QSOs.
