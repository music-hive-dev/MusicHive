; Inno Setup script for the Windows installer. build.sh passes AppVersion,
; SourceDir (the PyInstaller folder) and OutputDir.

[Setup]
AppId={{937B6BF2-9989-456D-B43B-DA4DD15D6A01}
AppName=MusicHive
AppVersion={#AppVersion}
AppVerName=MusicHive {#AppVersion}
AppPublisher=MusicHive
AppPublisherURL=https://github.com/music-hive-dev/MusicHive
DefaultDirName={autopf}\MusicHive
DefaultGroupName=MusicHive
DisableProgramGroupPage=yes
; Installing just for yourself needs no administrator.
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename=MusicHive-{#AppVersion}-windows-x64-setup
SetupIconFile=musichive.ico
UninstallDisplayIcon={app}\MusicHive.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[InstallDelete]
; Files from an older version must not mix with the new ones.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; The id matches the one main.py gives itself, so the taskbar keeps them together.
Name: "{autoprograms}\MusicHive"; Filename: "{app}\MusicHive.exe"; AppUserModelID: "MusicHive.MusicHive"
Name: "{autodesktop}\MusicHive"; Filename: "{app}\MusicHive.exe"; AppUserModelID: "MusicHive.MusicHive"; Tasks: desktopicon

[Run]
Filename: "{app}\MusicHive.exe"; Description: "{cm:LaunchProgram,MusicHive}"; Flags: nowait postinstall skipifsilent
