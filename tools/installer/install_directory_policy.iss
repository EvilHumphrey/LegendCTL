{ Final destination policy shared by the installer and native policy tests.
  This relies on normal protected Program Files ACLs. It is not an ACL repair
  mechanism for a hierarchy an administrator has made user-writable. }

const
  LegendInvalidAttributes = $FFFFFFFF;
  LegendDirectoryAttribute = $10;
  LegendReparseAttribute = $400;
  LegendUninstallKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{ZDUltimateLegend}_is1';

function LegendGetFileAttributes(FileName: String): LongWord;
  external 'GetFileAttributesW@kernel32.dll stdcall';

function LegendSameDirectory(Left, Right: String): Boolean;
begin
  { Do not resolve attacker-supplied dot segments, short names, device paths,
    or links into an accepted spelling. The normal destination remains valid. }
  Result := CompareText(RemoveBackslashUnlessRoot(Left),
                        RemoveBackslashUnlessRoot(Right)) = 0;
end;

function LegendInstallDirectoryAllowed(SelectedDir, ManagedDir: String): Boolean;
var
  CurrentDir, ParentDir: String;
  Attributes: LongWord;
  ErrorCode: LongInt;
  IsLeaf: Boolean;
begin
  Result := False;
  if not LegendSameDirectory(SelectedDir, ManagedDir) then begin
    Log('LEGENDCTL_INSTALL_PATH_REJECTED');
    Exit;
  end;

  CurrentDir := RemoveBackslashUnlessRoot(ManagedDir);
  IsLeaf := True;
  while CurrentDir <> '' do begin
    Attributes := LegendGetFileAttributes(CurrentDir);
    if Attributes = LegendInvalidAttributes then begin
      ErrorCode := DLLGetLastError;
      { Only a missing app leaf may be created. Access denial and an absent
        parent fail closed; every existing parent must be a real directory. }
      if not (IsLeaf and ((ErrorCode = 2) or (ErrorCode = 3))) then begin
        Log('LEGENDCTL_INSTALL_PATH_UNREADABLE');
        Exit;
      end;
    end else if ((Attributes and LegendDirectoryAttribute) = 0) or
                ((Attributes and LegendReparseAttribute) <> 0) then begin
      Log('LEGENDCTL_INSTALL_PATH_REDIRECTED_OR_NOT_DIRECTORY');
      Exit;
    end;
    ParentDir := ExtractFileDir(CurrentDir);
    if ParentDir = CurrentDir then begin
      Result := True;
      Exit;
    end;
    CurrentDir := ParentDir;
    IsLeaf := False;
  end;
  Log('LEGENDCTL_INSTALL_PATH_NO_ROOT');
end;

function LegendPreviousInstallAtRootAllowed(RootKey: Integer;
  ManagedDir: String): Boolean;
var
  StoredDir: String;
  FoundPath: Boolean;
begin
  Result := True;
  if not RegKeyExists(RootKey, LegendUninstallKey) then
    Exit;
  FoundPath := False;
  if RegValueExists(RootKey, LegendUninstallKey, 'Inno Setup: App Path') then begin
    if not RegQueryStringValue(RootKey, LegendUninstallKey,
      'Inno Setup: App Path', StoredDir) or
      not LegendSameDirectory(StoredDir, ManagedDir) then begin
      Result := False;
      Exit;
    end;
    FoundPath := True;
  end;
  if RegValueExists(RootKey, LegendUninstallKey, 'InstallLocation') then begin
    if not RegQueryStringValue(RootKey, LegendUninstallKey,
      'InstallLocation', StoredDir) or
      not LegendSameDirectory(StoredDir, ManagedDir) then begin
      Result := False;
      Exit;
    end;
    FoundPath := True;
  end;
  Result := FoundPath;
end;

function LegendPreviousInstallAllowed(ManagedDir: String): Boolean;
begin
  { Never overwrite the registration of an installation elsewhere or execute
    its potentially user-writable uninstaller. Migration requires review. }
  Result := LegendPreviousInstallAtRootAllowed(HKLM32, ManagedDir) and
            LegendPreviousInstallAtRootAllowed(HKCU32, ManagedDir);
  if IsWin64 then
    Result := Result and
      LegendPreviousInstallAtRootAllowed(HKLM64, ManagedDir) and
      LegendPreviousInstallAtRootAllowed(HKCU64, ManagedDir);
  if not Result then
    Log('LEGENDCTL_PREVIOUS_INSTALL_PATH_REJECTED');
end;
