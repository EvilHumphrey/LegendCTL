; Inno Setup script for the ZD Ultimate Legend wrapper.
;
; Produces an admin/Program Files installer alongside the portable ZIP. The
; install dir sits under Program Files, whose ACLs are admin-only-write, which
; blocks DLL planting / binary tamper. No code signing yet (a later release
; will add SignTool directives + signtool invocation in build_release.ps1).
; The installer
; LicenseFile directive is intentionally omitted: the project is MIT-licensed,
; which requires no click-through acceptance screen.
;
; Build invocation (driven by tools/build_release.ps1):
;   set ZDUL_VERSION=2.0.0
;   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" tools\installer\inno_setup_zd_wrapper.iss

#if GetEnv("ZDUL_VERSION") == ""
  #error "ZDUL_VERSION environment variable must be set. Use tools/build_release.ps1 to build, or set $env:ZDUL_VERSION manually before running ISCC.exe directly."
#endif

[Setup]
AppId={{ZDUltimateLegend}
AppName=ZD Ultimate Legend Wrapper
AppVersion={#GetEnv("ZDUL_VERSION")}
AppPublisher=EvilHumphrey
; Installed under Program Files so the
; target inherits admin-only-write ACLs — a non-admin process can't plant a DLL
; or tamper with the shipped binaries. Admin is required and intentionally NOT
; command-line-overridable (no PrivilegesRequiredOverridesAllowed): allowing a
; downgrade to a user-writable path would reintroduce the very risk this fixes.
DefaultDirName={autopf}\ZDUltimateLegend
DefaultGroupName=ZD Ultimate Legend Wrapper
PrivilegesRequired=admin
OutputDir=..\..\dist
OutputBaseFilename=ZDUltimateLegend-v{#GetEnv("ZDUL_VERSION")}-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Files]
; Bundle the entire PyInstaller-built folder.
Source: "..\..\dist\ZDUltimateLegend-v{#GetEnv("ZDUL_VERSION")}\*"; \
    DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\ZD Ultimate Legend Wrapper"; Filename: "{app}\ZD Ultimate Legend.exe"
Name: "{group}\Uninstall ZD Ultimate Legend Wrapper"; Filename: "{uninstallexe}"
Name: "{userdesktop}\ZD Ultimate Legend Wrapper"; Filename: "{app}\ZD Ultimate Legend.exe"; \
    Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; \
    GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Run]
; The installer runs elevated (admin), but the wrapper itself must NOT —
; runasoriginaluser launches it back in the original (non-elevated) user context.
Filename: "{app}\ZD Ultimate Legend.exe"; \
    Description: "Launch ZD Ultimate Legend Wrapper"; \
    Flags: nowait postinstall skipifsilent runasoriginaluser
