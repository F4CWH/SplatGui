; Installeur de SPLAT!Gui (Inno Setup 6) — à compiler avec build_installer.ps1, qui
; recompile l'application (PyInstaller) puis passe la version : ISCC /DAppVersion=x.y.z SplatGui.iss
;
; Installation par utilisateur (%LOCALAPPDATA%\Programs\SPLAT!Gui), sans droits administrateur :
; l'application écrit ses données (réglages, résultats, relief, DLL…) à côté de l'exécutable.
;
; Deux éditions, de même AppId (l'une peut remplacer l'autre) :
;   - SPLAT!Gui-Setup-<version>.exe : l'application seule (SPLAT! s'installe ensuite par la
;     fenêtre Pré-requis) ;
;   - SPLAT!Gui-Setup-<version>-complet.exe (ISCC /DComplet) : avec les exécutables SPLAT! x64 et
;     x86, leurs DLL MSYS2 / MinGW et gnuplot, pris dans le dossier du projet (bin\, deps\ et
;     gnuplot\, tels que les installe la fenêtre Pré-requis). Code source de ces exécutables
;     (GNU GPL v2) : https://github.com/F4CWH/splat

#ifndef AppVersion
  #define AppVersion "1.2.1"
#endif
#define AppName "SPLAT!Gui"
#define AppExe "SPLAT!Gui.exe"
#define DistDir "..\dist\SPLAT!Gui"

[Setup]
AppId={{6F2B8C41-3D7A-4E59-9B1F-5A0C2E7D8F34}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=F4CWH
; Code source (GNU GPL) : lien affiché dans « Programmes et fonctionnalités ».
AppPublisherURL=https://github.com/F4CWH/SplatGui
AppSupportURL=https://github.com/F4CWH/SplatGui
AppUpdatesURL=https://github.com/F4CWH/SplatGui
DefaultDirName={autopf}\SPLAT!Gui
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
#ifdef Complet
OutputBaseFilename=SPLAT!Gui-Setup-{#AppVersion}-complet
#else
OutputBaseFilename=SPLAT!Gui-Setup-{#AppVersion}
#endif
SetupIconFile=..\icons\splat_icon.ico
; SPLAT! et SPLAT!Gui : GNU GPL version 2 ou ultérieure (texte à accepter avant l'installation).
LicenseFile=..\LICENSE
AppCopyright=Splat! (KD2BD) / SPLAT!Gui (F4CWH) — GNU GPL v2 ou ultérieure
UninstallDisplayIcon={app}\{#AppExe}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern

[Languages]
Name: "french"; MessagesFile: "compiler:Languages\French.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#DistDir}\{#AppExe}"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#DistDir}\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#DistDir}\lib\*"; DestDir: "{app}\lib"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#DistDir}\geojson\*"; DestDir: "{app}\geojson"; Flags: ignoreversion recursesubdirs createallsubdirs skipifsourcedoesntexist
Source: "{#DistDir}\icons\*"; DestDir: "{app}\icons"; Flags: ignoreversion recursesubdirs createallsubdirs skipifsourcedoesntexist
; Bibliothèque d'antennes : modèles fournis, sans écraser ceux que l'utilisateur a modifiés.
Source: "{#DistDir}\antennas\*"; DestDir: "{app}\antennas"; Flags: onlyifdoesntexist recursesubdirs createallsubdirs skipifsourcedoesntexist
#ifdef Complet
; SPLAT! et ses DLL : deps\<arch> et gnuplot\bin sont ajoutés au PATH d'exécution par l'application
; au démarrage suivant (install.json).
Source: "..\bin\x64\*"; DestDir: "{app}\bin\x64"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\deps\x64\*.dll"; DestDir: "{app}\deps\x64"; Flags: ignoreversion
Source: "..\bin\x86\*"; DestDir: "{app}\bin\x86"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\deps\x86\*.dll"; DestDir: "{app}\deps\x86"; Flags: ignoreversion
Source: "..\gnuplot\*"; DestDir: "{app}\gnuplot"; Flags: ignoreversion recursesubdirs createallsubdirs
#endif

[InstallDelete]
; Mise à jour : bibliothèques de la version précédente (modules supprimés ou renommés).
Type: filesandordirs; Name: "{app}\lib"
#ifdef Complet
; gnuplot d'une version précédente (installé par l'assistant ou par la fenêtre Pré-requis).
Type: filesandordirs; Name: "{app}\gnuplot"
#endif
; Exécutable des versions 1.1.0 et antérieures (renommé SPLAT!Gui.exe).
Type: files; Name: "{app}\SplatGui.exe"
; Raccourcis « Splat!Gui » des versions précédentes : supprimés pour être recréés sous le nom SPLAT!Gui.
Type: files; Name: "{group}\{#AppName}.lnk"
Type: files; Name: "{group}\{cm:UninstallProgram,{#AppName}}.lnk"
Type: files; Name: "{autodesktop}\{#AppName}.lnk"

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[Code]
{ Dossiers de travail (résultats, sites .qth, paramètres ITM .lrp) : choisis sur une page de
  l'assistant (par défaut dans le dossier d'installation, ou ceux de l'installation précédente),
  créés, puis transmis à l'application par le fichier install.json du dossier d'installation,
  qu'elle applique à ses réglages au démarrage suivant. }
var
  FoldersPage: TInputDirWizardPage;
  FoldersBase: String;     { dossier d'installation ayant servi aux valeurs par défaut affichées }

{ Dossier I (0 : résultats, 1 : sites, 2 : paramètres ITM) : nom du sous-dossier par défaut. }
function FolderName(I: Integer): String;
begin
  case I of
    0: Result := 'runs';
    1: Result := 'qth';
  else
    Result := 'lrp';
  end;
end;

{ Clé dans les réglages de l'application (settings.json). }
function FolderKey(I: Integer): String;
begin
  Result := FolderName(I) + '_dir';
end;

{ Clé de la valeur mémorisée pour la réinstallation suivante. }
function FolderData(I: Integer): String;
begin
  Result := 'Folder_' + FolderName(I);
end;

procedure InitializeWizard;
var
  I: Integer;
begin
  FoldersPage := CreateInputDirPage(wpSelectDir, 'Dossiers de travail',
    'Où SPLAT!Gui doit-il enregistrer ses fichiers ?',
    'Résultats des calculs, fichiers de sites et paramètres ITM. Ces dossiers peuvent se trouver hors du ' +
    'dossier d''installation (autre disque, dossier partagé…) ; ils restent modifiables dans Fichier → Réglages.',
    False, '');
  FoldersPage.Add('Résultats des calculs (runs) :');
  FoldersPage.Add('Fichiers de sites (.qth) :');
  FoldersPage.Add('Paramètres ITM (.lrp) :');
  for I := 0 to 2 do
    FoldersPage.Values[I] := GetPreviousData(FolderData(I), '');
  FoldersBase := '';
end;

{ Valeurs par défaut : sous-dossiers du dossier d'installation, mises à jour si celui-ci change
  (tant que l'utilisateur ne les a pas modifiées). }
procedure CurPageChanged(CurPageID: Integer);
var
  I: Integer;
  App: String;
begin
  if CurPageID = FoldersPage.ID then
  begin
    App := ExpandConstant('{app}');
    for I := 0 to 2 do
      if (FoldersPage.Values[I] = '') or
         ((FoldersBase <> '') and (CompareText(FoldersPage.Values[I], AddBackslash(FoldersBase) + FolderName(I)) = 0)) then
        FoldersPage.Values[I] := AddBackslash(App) + FolderName(I);
    FoldersBase := App;
  end;
end;

function FolderValue(I: Integer): String;
begin
  Result := Trim(FoldersPage.Values[I]);
  if Result = '' then                                  { installation silencieuse : page non affichée }
    Result := AddBackslash(ExpandConstant('{app}')) + FolderName(I);
end;

procedure RegisterPreviousData(PreviousDataKey: Integer);
var
  I: Integer;
begin
  for I := 0 to 2 do
    SetPreviousData(PreviousDataKey, FolderData(I), FolderValue(I));
end;

function JsonString(S: String): String;
begin
  StringChangeEx(S, '\', '\\', True);
  StringChangeEx(S, '"', '\"', True);
  Result := '"' + S + '"';
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  I: Integer;
  Lines: TArrayOfString;
begin
  if CurStep = ssPostInstall then
  begin
    SetArrayLength(Lines, 5);
    Lines[0] := '{';
    for I := 0 to 2 do
    begin
      ForceDirectories(FolderValue(I));
      Lines[I + 1] := '  ' + JsonString(FolderKey(I)) + ': ' + JsonString(FolderValue(I));
      if I < 2 then
        Lines[I + 1] := Lines[I + 1] + ',';
    end;
    Lines[4] := '}';
    SaveStringsToUTF8File(ExpandConstant('{app}\install.json'), Lines, False);
  end;
end;

{ Désinstallation : les fichiers installés sont retirés ; les données créées par l'application
  (réglages, profils, résultats, relief, exécutables SPLAT!, DLL) sont supprimées sur demande,
  et conservées lors d'une désinstallation silencieuse. }
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if (CurUninstallStep = usPostUninstall) and DirExists(ExpandConstant('{app}')) then
    if not UninstallSilent and (MsgBox('Supprimer aussi les données de SPLAT!Gui (réglages, profils, résultats, ' +
        'relief, exécutables SPLAT! et DLL) présentes dans :' + #13#10 + ExpandConstant('{app}') + ' ?',
        mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES) then
      DelTree(ExpandConstant('{app}'), True, True, True);
end;
