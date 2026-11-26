; Instalador de Windows (Inno Setup 6). Se compila desde la raíz:  iscc /DAppVersion=0.1.0 packaging\iphonephotosync.iss
; Requiere haber generado antes dist\iphonephotosync con PyInstaller.
#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif

[Setup]
AppId={{6F1D2B7A-3C54-4E0B-9B8E-1A5C7D2F9E10}
AppName=iPhonePhotoSync
AppVersion={#AppVersion}
AppPublisher=Jose Carlos Perez
AppPublisherURL=https://github.com/joscarper1/iPhonePhotoSync
DefaultDirName={autopf}\iPhonePhotoSync
DefaultGroupName=iPhonePhotoSync
DisableProgramGroupPage=yes
LicenseFile=..\LICENSE
OutputDir=..\dist
OutputBaseFilename=iphonephotosync-setup
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
UninstallDisplayIcon={app}\iphonephotosync.exe
WizardStyle=modern

[Languages]
Name: "spanish"; MessagesFile: "compiler:Languages\Spanish.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Crear un acceso directo en el escritorio"; Flags: unchecked

[Files]
Source: "..\dist\iphonephotosync\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\iPhonePhotoSync"; Filename: "{app}\iphonephotosync.exe"
Name: "{autodesktop}\iPhonePhotoSync"; Filename: "{app}\iphonephotosync.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\iphonephotosync.exe"; Description: "Abrir iPhonePhotoSync"; Flags: nowait postinstall skipifsilent
