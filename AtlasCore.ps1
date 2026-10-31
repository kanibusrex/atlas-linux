<#
  AtlasCore.ps1: Atlas's file operations. No windows or WebView2 here, only files, so the same
  functions run in the window's background workers and can be tested on their own.

  Every function that can take a while is given a shared $State (a synchronized hashtable) holding:
    Events   a queue of progress messages for the window to pass on to the page
    Cancel   set to true by the window to stop the work
    Paused   set to true by the window to pause a copy
  Written for Windows PowerShell 5.1 (.NET Framework), which Atlas runs in; also runs in PowerShell 7.
#>

$script:FDIsWindows = ($env:OS -eq "Windows_NT")

# ---- JSON ---------------------------------------------------------------------------------------
# Windows PowerShell wraps values in its own PSObject layer (a function's output, items added to a list),
# and the fast serializer it offers would try to write that wrapper out instead of what is inside it.
# FDClean, compiled once, unwraps everything first: dictionaries, lists and PowerShell objects become
# plain ones, and only then is it turned into JSON. Without the fast serializer (PowerShell 7), the
# cleaned value goes to ConvertTo-Json.
if (-not ("FDClean" -as [type])) {
  Add-Type -TypeDefinition @'
using System;
using System.Collections;
using System.Collections.Generic;
using System.Management.Automation;
public static class FDClean
{
    public static object Clean(object o) { return Clean(o, 0); }
    private static object Clean(object o, int depth)
    {
        if (o == null || depth > 40) return null;
        PSObject ps = o as PSObject;
        if (ps != null) {
            object b = ps.BaseObject;
            if (b is PSCustomObject) {
                var d = new Dictionary<string, object>();
                foreach (PSPropertyInfo p in ps.Properties) {
                    object v = null;
                    try { v = p.Value; } catch { }
                    d[p.Name] = Clean(v, depth + 1);
                }
                return d;
            }
            o = b;
        }
        if (o is string || o is bool || o is char || o is DateTime || o.GetType().IsPrimitive || o is decimal) return o;
        if (o is Enum) return o.ToString();
        IDictionary dict = o as IDictionary;
        if (dict != null) {
            var d = new Dictionary<string, object>();
            foreach (DictionaryEntry e in dict) d[Convert.ToString(e.Key)] = Clean(e.Value, depth + 1);
            return d;
        }
        IEnumerable list = o as IEnumerable;
        if (list != null) {
            var l = new List<object>();
            foreach (object x in list) l.Add(Clean(x, depth + 1));
            return l;
        }
        return o.ToString();
    }
}
'@
}
# ---- Long paths, and what has a file open ---------------------------------------------------------
# .NET Framework (which Windows PowerShell runs on) cannot reach paths over 260 characters, common on deep
# network shares. FDLong does the same jobs with Windows' own functions and \\?\ paths; the FD helpers
# below use it only when a path is long or .NET says it is, so ordinary paths take the usual route.
if (-not ("FDLong" -as [type])) {
  Add-Type -TypeDefinition @'
using System;
using System.IO;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;
using FILETIME = System.Runtime.InteropServices.ComTypes.FILETIME;

// One file or folder found by FDLong, with the same names as .NET's FileInfo and DirectoryInfo.
public class FDLongItem
{
    public string Name; public string FullName; public bool IsDir; public long Length;
    public FileAttributes Attributes; public DateTime LastWriteTime; public DateTime CreationTime;
    public DateTime LastWriteTimeUtc; public DateTime LastAccessTime;
    public string Extension { get { if (IsDir) return ""; int i = Name.LastIndexOf('.'); return i > 0 ? Name.Substring(i) : ""; } }
}

// Paths too long for .NET Framework (over 260 characters): Windows' own functions, given \\?\ paths.
public static class FDLong
{
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct WIN32_FIND_DATAW
    {
        public uint dwFileAttributes; public FILETIME ftCreationTime; public FILETIME ftLastAccessTime; public FILETIME ftLastWriteTime;
        public uint nFileSizeHigh; public uint nFileSizeLow; public uint dwReserved0; public uint dwReserved1;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 260)] public string cFileName;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 14)] public string cAlternateFileName;
    }
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr FindFirstFileExW(string name, int infoLevel, out WIN32_FIND_DATAW data, int searchOp, IntPtr filter, int flags);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool FindNextFileW(IntPtr h, out WIN32_FIND_DATAW data);
    [DllImport("kernel32.dll")] private static extern bool FindClose(IntPtr h);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern SafeFileHandle CreateFileW(string name, uint access, uint share, IntPtr sec, uint disposition, uint flags, IntPtr template);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)] private static extern bool MoveFileExW(string from, string to, uint flags);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)] private static extern bool DeleteFileW(string name);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)] private static extern bool RemoveDirectoryW(string name);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)] private static extern bool CreateDirectoryW(string name, IntPtr sec);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)] private static extern uint GetFileAttributesW(string name);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)] private static extern bool SetFileAttributesW(string name, uint attrs);
    [DllImport("kernel32.dll", SetLastError = true)] private static extern bool SetFileTime(SafeFileHandle h, IntPtr c, IntPtr a, ref long w);

    private static readonly IntPtr Bad = new IntPtr(-1);
    public static string L(string p)
    {
        if (p.StartsWith(@"\\?\")) return p;
        if (p.StartsWith(@"\\")) return @"\\?\UNC\" + p.Substring(2);
        return @"\\?\" + p;
    }
    private static Exception Err(string what, string p)
    {
        int code = Marshal.GetLastWin32Error();
        return new IOException(what + ": " + new System.ComponentModel.Win32Exception(code).Message + " (" + p + ")");
    }
    private static DateTime Time(FILETIME f) { long t = ((long)(uint)f.dwHighDateTime << 32) | (uint)f.dwLowDateTime; try { return DateTime.FromFileTimeUtc(t); } catch { return DateTime.MinValue; } }

    public static FDLongItem[] List(string dir)
    {
        var list = new List<FDLongItem>();
        WIN32_FIND_DATAW d;
        string full = dir.TrimEnd('\\');
        IntPtr h = FindFirstFileExW(L(full) + @"\*", 1, out d, 0, IntPtr.Zero, 2);
        if (h == Bad) throw Err("The folder could not be opened", dir);
        try {
            do {
                if (d.cFileName == "." || d.cFileName == "..") continue;
                var it = new FDLongItem();
                it.Name = d.cFileName; it.FullName = full + "\\" + d.cFileName;
                it.Attributes = (FileAttributes)d.dwFileAttributes;
                it.IsDir = (d.dwFileAttributes & 0x10) != 0;
                it.Length = it.IsDir ? 0 : (((long)d.nFileSizeHigh << 32) | d.nFileSizeLow);
                it.LastWriteTimeUtc = Time(d.ftLastWriteTime);
                it.LastWriteTime = it.LastWriteTimeUtc.ToLocalTime(); it.CreationTime = Time(d.ftCreationTime).ToLocalTime(); it.LastAccessTime = Time(d.ftLastAccessTime).ToLocalTime();
                list.Add(it);
            } while (FindNextFileW(h, out d));
        } finally { FindClose(h); }
        return list.ToArray();
    }
    // 0 not there, 1 a file, 2 a folder
    public static int Kind(string p)
    {
        uint a = GetFileAttributesW(L(p));
        if (a == 0xFFFFFFFF) return 0;
        return (a & 0x10) != 0 ? 2 : 1;
    }
    public static FileStream OpenRead(string p)
    {
        var h = CreateFileW(L(p), 0x80000000, 7, IntPtr.Zero, 3, 0x08000000, IntPtr.Zero);   // GENERIC_READ, share all, OPEN_EXISTING, sequential
        if (h.IsInvalid) throw Err("The file could not be opened", p);
        return new FileStream(h, FileAccess.Read, 1048576);
    }
    public static FileStream OpenWrite(string p)
    {
        var h = CreateFileW(L(p), 0x40000000, 0, IntPtr.Zero, 2, 0x80, IntPtr.Zero);          // GENERIC_WRITE, CREATE_ALWAYS
        if (h.IsInvalid) throw Err("The file could not be written", p);
        return new FileStream(h, FileAccess.Write, 1048576);
    }
    public static void Move(string from, string to)
    {
        if (!MoveFileExW(L(from), L(to), 2)) throw Err("It could not be moved", from);       // MOVEFILE_COPY_ALLOWED
    }
    public static void DeleteFile(string p)
    {
        SetFileAttributesW(L(p), 0x80);
        if (!DeleteFileW(L(p))) throw Err("It could not be deleted", p);
    }
    public static void DeleteTree(string p)
    {
        foreach (var it in List(p)) {
            if (it.IsDir && (it.Attributes & FileAttributes.ReparsePoint) == 0) DeleteTree(it.FullName);
            else if (it.IsDir) { if (!RemoveDirectoryW(L(it.FullName))) throw Err("It could not be deleted", it.FullName); }
            else DeleteFile(it.FullName);
        }
        SetFileAttributesW(L(p), 0x10);
        if (!RemoveDirectoryW(L(p))) throw Err("It could not be deleted", p);
    }
    public static void MakeDir(string p)
    {
        if (Kind(p) == 2) return;
        string parent = Path.GetDirectoryName(p.TrimEnd('\\'));
        if (!string.IsNullOrEmpty(parent) && parent.Length > 3 && Kind(parent) == 0) MakeDir(parent);
        if (!CreateDirectoryW(L(p), IntPtr.Zero) && Kind(p) != 2) throw Err("The folder could not be made", p);
    }
    public static void SetWriteTimeUtc(string p, DateTime utc)
    {
        using (var h = CreateFileW(L(p), 0x100, 7, IntPtr.Zero, 3, 0x02000000, IntPtr.Zero)) {   // FILE_WRITE_ATTRIBUTES, BACKUP_SEMANTICS
            if (h.IsInvalid) return;
            long t = utc.ToFileTimeUtc();
            SetFileTime(h, IntPtr.Zero, IntPtr.Zero, ref t);
        }
    }
}

// What has a file open: Windows' Restart Manager, as installers use to say "close these programs first".
public static class FDRm
{
    [StructLayout(LayoutKind.Sequential)] private struct RM_UNIQUE_PROCESS { public int dwProcessId; public FILETIME ProcessStartTime; }
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct RM_PROCESS_INFO
    {
        public RM_UNIQUE_PROCESS Process;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 256)] public string strAppName;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 64)] public string strServiceShortName;
        public int ApplicationType; public uint AppStatus; public uint TSSessionId; [MarshalAs(UnmanagedType.Bool)] public bool bRestartable;
    }
    [DllImport("rstrtmgr.dll", CharSet = CharSet.Unicode)] private static extern int RmStartSession(out uint h, int flags, string key);
    [DllImport("rstrtmgr.dll")] private static extern int RmEndSession(uint h);
    [DllImport("rstrtmgr.dll", CharSet = CharSet.Unicode)]
    private static extern int RmRegisterResources(uint h, uint nFiles, string[] files, uint nApps, IntPtr apps, uint nServices, string[] services);
    [DllImport("rstrtmgr.dll")]
    private static extern int RmGetList(uint h, out uint needed, ref uint count, [In, Out] RM_PROCESS_INFO[] info, ref uint reasons);

    // One line per program: process id, then a tab, the kind (app, explorer, service, console...), a tab, and its name.
    public static string[] Who(string[] paths)
    {
        uint h; var outList = new List<string>();
        int r = RmStartSession(out h, 0, Guid.NewGuid().ToString("N"));
        if (r != 0) throw new IOException("Windows' Restart Manager could not start (" + r + ").");
        try {
            r = RmRegisterResources(h, (uint)paths.Length, paths, 0, IntPtr.Zero, 0, null);
            if (r != 0) throw new IOException("The file could not be checked (" + r + ").");
            uint needed = 0, count = 0, reasons = 0;
            r = RmGetList(h, out needed, ref count, null, ref reasons);
            if (r == 234 && needed > 0) {
                var info = new RM_PROCESS_INFO[needed]; count = needed;
                r = RmGetList(h, out needed, ref count, info, ref reasons);
                if (r != 0) throw new IOException("The list of programs could not be read (" + r + ").");
                string[] kinds = { "unknown", "app", "app", "service", "Explorer", "console", "critical" };
                for (int i = 0; i < count; i++) {
                    int k = info[i].ApplicationType; if (k == 1000) k = 6;
                    outList.Add(info[i].Process.dwProcessId + "\t" + (k >= 0 && k < kinds.Length ? kinds[k] : "app") + "\t" + info[i].strAppName + "\t" + info[i].strServiceShortName);
                }
            } else if (r != 0) throw new IOException("The list of programs could not be read (" + r + ").");
        } finally { RmEndSession(h); }
        return outList.ToArray();
    }
}

'@
}
$script:FDJss = $null
try {
  Add-Type -AssemblyName System.Web.Extensions -ErrorAction Stop
  $script:FDJss = New-Object System.Web.Script.Serialization.JavaScriptSerializer
  $script:FDJss.MaxJsonLength = [int]::MaxValue
  $script:FDJss.RecursionLimit = 100
} catch { $script:FDJss = $null }
function ConvertTo-FDJson {
  param($Value)
  $clean = [FDClean]::Clean($Value)
  if ($script:FDJss) {
    try { return $script:FDJss.Serialize($clean) } catch { }
  }
  return (ConvertTo-Json -InputObject $clean -Depth 40 -Compress)
}

function Get-FDEpoch {
  param([datetime]$When)
  if ($When.Year -lt 1971) { return 0 }
  return [long]([DateTimeOffset]$When.ToUniversalTime()).ToUnixTimeMilliseconds()
}

function Send-FDEvent {
  param($State, $Message)
  if ($State -and $State.Events) { $State.Events.Enqueue((ConvertTo-FDJson $Message)) }
}

# ---- The FD helpers: .NET for ordinary paths, FDLong for long ones ------------------------------------
$script:FDLongAt = 240
$script:FDSep = [string][System.IO.Path]::DirectorySeparatorChar
function Test-FDLong { param([string]$P) return ($script:FDIsWindows -and $P.Length -ge $script:FDLongAt) }
function Test-FDTooLong { param($Err) $e = $Err; while ($e) { if ($e -is [System.IO.PathTooLongException]) { return $true }; $e = $e.InnerException }; return $false }
# What is inside a folder: FileInfo and DirectoryInfo, or FDLongItem for a long path.
function Get-FDChildren {
  param([string]$Dir)
  if (Test-FDLong $Dir) { return , [FDLong]::List($Dir) }
  try { return , (New-Object System.IO.DirectoryInfo($Dir)).GetFileSystemInfos() }
  catch {
    if ($script:FDIsWindows -and (Test-FDTooLong $_.Exception)) { return , [FDLong]::List($Dir) }
    throw
  }
}
function Test-FDDirItem { param($Item) return (($Item -is [System.IO.DirectoryInfo]) -or ($Item -is [FDLongItem] -and $Item.IsDir)) }
# 0 not there, 1 a file, 2 a folder
function Get-FDKind {
  param([string]$P)
  if (Test-FDLong $P) { return [FDLong]::Kind($P) }
  try {
    if ([System.IO.Directory]::Exists($P)) { return 2 }
    if ([System.IO.File]::Exists($P)) { return 1 }
    return 0
  } catch { if ($script:FDIsWindows) { return [FDLong]::Kind($P) }; return 0 }
}
function Test-FDItem { param([string]$P) return ((Get-FDKind $P) -ne 0) }
function Open-FDRead {
  param([string]$P)
  if (Test-FDLong $P) { return [FDLong]::OpenRead($P) }
  return (New-Object System.IO.FileStream($P, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, ([System.IO.FileShare]::ReadWrite -bor [System.IO.FileShare]::Delete), 1048576))
}
function Open-FDWrite {
  param([string]$P)
  if (Test-FDLong $P) { return [FDLong]::OpenWrite($P) }
  return (New-Object System.IO.FileStream($P, [System.IO.FileMode]::Create, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None, 1048576))
}
function Move-FDPath {
  param([string]$From, [string]$To, [bool]$IsDir)
  if ((Test-FDLong $From) -or (Test-FDLong $To)) { [FDLong]::Move($From, $To); return }
  if ($IsDir) { [System.IO.Directory]::Move($From, $To) } else { [System.IO.File]::Move($From, $To) }
}
function Remove-FDFile {
  param([string]$P)
  if (Test-FDLong $P) { [FDLong]::DeleteFile($P); return }
  [System.IO.File]::SetAttributes($P, [System.IO.FileAttributes]::Normal)
  [System.IO.File]::Delete($P)
}
function Remove-FDTree {
  param([string]$P)
  if (Test-FDLong $P) { [FDLong]::DeleteTree($P); return }
  try {
    Get-ChildItem -LiteralPath $P -Recurse -Force -ErrorAction SilentlyContinue | ForEach-Object { try { $_.Attributes = 'Normal' } catch { } }
    [System.IO.Directory]::Delete($P, $true)
  } catch {
    if ($script:FDIsWindows -and (Test-FDTooLong $_.Exception)) { [FDLong]::DeleteTree($P); return }
    throw
  }
}
function New-FDDir {
  param([string]$P)
  if (Test-FDLong $P) { [FDLong]::MakeDir($P); return }
  [void][System.IO.Directory]::CreateDirectory($P)
}
function Set-FDWriteTime {
  param([string]$P, [datetime]$Utc)
  if (Test-FDLong $P) { [FDLong]::SetWriteTimeUtc($P, $Utc); return }
  [System.IO.File]::SetLastWriteTimeUtc($P, $Utc)
}
function Join-FDPath { param([string]$A, [string]$B) if ($A.EndsWith("\") -or $A.EndsWith("/")) { return $A + $B }; return $A + [System.IO.Path]::DirectorySeparatorChar + $B }

# One file or folder, in a compact form: n name, d folder, s size, m modified, c created, a attributes
# (h hidden, r read-only, y system, l link), so a big folder stays small to send.
function ConvertTo-FDEntry {
  param($Item)
  $isDir = Test-FDDirItem $Item
  $at = $Item.Attributes
  $flags = ""
  if ($at -band [System.IO.FileAttributes]::Hidden) { $flags += "h" }
  if ($at -band [System.IO.FileAttributes]::ReadOnly) { $flags += "r" }
  if ($at -band [System.IO.FileAttributes]::System) { $flags += "y" }
  if ($at -band [System.IO.FileAttributes]::ReparsePoint) { $flags += "l" }
  $size = 0
  if (-not $isDir) { try { $size = [long]$Item.Length } catch { $size = 0 } }
  $m = 0; $c = 0
  try { $m = Get-FDEpoch $Item.LastWriteTime } catch { }
  try { $c = Get-FDEpoch $Item.CreationTime } catch { }
  return @{ n = $Item.Name; d = $isDir; s = $size; m = $m; c = $c; a = $flags }
}

# ---- A folder's contents ----------------------------------------------------------------------
# A server on its own (\\server) lists its shares instead.
# A typed path, the way Windows reads one: %APPDATA% and the other environment names, ~ for your own
# folder, and shell: names such as shell:startup or shell:downloads.
function Resolve-FDPath {
  param([string]$Path)
  $p = ([string]$Path).Trim().Trim('"')
  if ($p -match '^(?i)shell:([A-Za-z ]+)(\\.*)?$') {
    $name = $Matches[1].ToLowerInvariant().Replace(" ", ""); $rest = $Matches[2]
    $home2 = [Environment]::GetFolderPath("UserProfile")
    $map = @{
      "desktop" = [Environment]::GetFolderPath("Desktop"); "personal" = [Environment]::GetFolderPath("MyDocuments"); "documents" = [Environment]::GetFolderPath("MyDocuments")
      "downloads" = (Join-Path $home2 "Downloads"); "mypictures" = [Environment]::GetFolderPath("MyPictures"); "pictures" = [Environment]::GetFolderPath("MyPictures")
      "appdata" = [Environment]::GetFolderPath("ApplicationData"); "localappdata" = [Environment]::GetFolderPath("LocalApplicationData")
      "startup" = [Environment]::GetFolderPath("Startup"); "commonstartup" = [Environment]::GetFolderPath("CommonStartup")
      "programs" = [Environment]::GetFolderPath("Programs"); "commonprograms" = [Environment]::GetFolderPath("CommonPrograms"); "startmenu" = [Environment]::GetFolderPath("StartMenu")
      "sendto" = [Environment]::GetFolderPath("SendTo"); "recent" = [Environment]::GetFolderPath("Recent"); "templates" = [Environment]::GetFolderPath("Templates")
      "fonts" = [Environment]::GetFolderPath("Fonts"); "profile" = $home2; "windows" = [Environment]::GetFolderPath("Windows"); "system" = [Environment]::GetFolderPath("System")
      "programfiles" = [Environment]::GetFolderPath("ProgramFiles"); "programfilesx86" = [Environment]::GetFolderPath("ProgramFilesX86"); "commonappdata" = [Environment]::GetFolderPath("CommonApplicationData")
      "cache" = [Environment]::GetFolderPath("InternetCache"); "cookies" = [Environment]::GetFolderPath("Cookies"); "history" = [Environment]::GetFolderPath("History")
      "temp" = [System.IO.Path]::GetTempPath()
    }
    if ($map.ContainsKey($name) -and $map[$name]) { $p = ([string]$map[$name]).TrimEnd('\') + [string]$rest }
  }
  if ($p -eq "~") { $p = [Environment]::GetFolderPath("UserProfile") }
  elseif ($p -match '^~[\\/]') { $p = [Environment]::GetFolderPath("UserProfile") + $p.Substring(1) }
  if ($p.IndexOf('%') -ge 0) { $p = [Environment]::ExpandEnvironmentVariables($p) }
  if ($p -match '^[A-Za-z]:$') { $p += '\' }
  return $p
}

function Get-FDList {
  param([string]$Path)
  $p = Resolve-FDPath $Path
  if ($p.IndexOf('%') -ge 0 -and $p -match '%[^%\\]+%') { return @{ path = $p; error = ("Windows has no setting called " + $Matches[0] + ", so that folder is not known.") } }
  if ($p -match '^\\\\[^\\]+\\?$') { return (Get-FDShares $p) }
  try {
    $kind = Get-FDKind $p
    if ($kind -eq 1 -and $p -match '(?i)\.zip$') { return (Get-FDZipList $p "") }
    if ($kind -ne 2) {
      $z = Find-FDZipPart $p
      if ($z) { return (Get-FDZipList $z.zip $z.inner) }
      return @{ path = $p; error = "This folder could not be found. It may have been moved or deleted, or the drive is not connected." }
    }
    $full = $p
    if (-not (Test-FDLong $p)) { try { $full = [System.IO.Path]::GetFullPath($p) } catch { $full = $p } }
    $entries = New-Object System.Collections.Generic.List[object]
    foreach ($it in (Get-FDChildren $full)) {
      try { $entries.Add((ConvertTo-FDEntry $it)) } catch { }
    }
    $parent = ""
    try { $parent = [System.IO.Path]::GetDirectoryName($full.TrimEnd('\', '/')) } catch { $parent = "" }
    if (-not $parent -and $full.Length -gt 3 -and $full -match '^[A-Za-z]:') { $parent = $full.Substring(0, 3) }
    if (-not $parent -and $p -match '^(\\\\[^\\]+)\\[^\\]+\\?$') { $parent = $Matches[1] }
    return @{ path = $full; parent = $parent; entries = $entries.ToArray(); long = (Test-FDLong $full) }
  } catch [System.UnauthorizedAccessException] {
    return @{ path = $p; error = "You do not have permission to open this folder." }
  } catch {
    $msg = $_.Exception.Message
    if ($_.Exception.InnerException) { $msg = $_.Exception.InnerException.Message }
    return @{ path = $p; error = $msg }
  }
}

# ---- Drives and the usual places ----------------------------------------------------------------
function Get-FDDrives {
  $drives = New-Object System.Collections.Generic.List[object]
  foreach ($d in [System.IO.DriveInfo]::GetDrives()) {
    $info = @{ path = $d.Name; type = [string]$d.DriveType; ready = $false; label = ""; total = 0; free = 0; unc = "" }
    try {
      if ($d.IsReady) {
        $info.ready = $true
        $info.label = [string]$d.VolumeLabel
        $info.total = [long]$d.TotalSize
        $info.free = [long]$d.AvailableFreeSpace
      }
    } catch { }
    # a mapped drive's own address (\\server\share), from Windows' record of mapped drives
    if ($script:FDIsWindows -and $info.type -eq "Network") {
      try {
        $k = Get-ItemProperty -LiteralPath ("HKCU:\Network\" + $d.Name.Substring(0, 1)) -ErrorAction Stop
        $info.unc = [string]$k.RemotePath
        $info.reconnect = $true
      } catch { }
      if (-not $info.unc) { $info.unc = Get-FDMappedPath $d.Name.Substring(0, 1) }
    }
    $drives.Add($info)
  }
  $places = New-Object System.Collections.Generic.List[object]
  $home2 = [Environment]::GetFolderPath("UserProfile")
  $want = @(
    @("Desktop", [Environment]::GetFolderPath("Desktop")),
    @("Documents", [Environment]::GetFolderPath("MyDocuments")),
    @("Downloads", (Join-Path $home2 "Downloads")),
    @("Pictures", [Environment]::GetFolderPath("MyPictures")),
    @("Home", $home2)
  )
  if ($env:OneDriveCommercial) { $want += , @("OneDrive", $env:OneDriveCommercial) }
  elseif ($env:OneDrive) { $want += , @("OneDrive", $env:OneDrive) }
  foreach ($w in $want) {
    if ($w[1] -and (Test-Path -LiteralPath $w[1])) { $places.Add(@{ name = $w[0]; path = $w[1] }) }
  }
  return @{ drives = $drives.ToArray(); places = $places.ToArray(); user = [Environment]::UserName; machine = [Environment]::MachineName }
}

# ---- Mapping network drives ----------------------------------------------------------------------
# Mapped as you, the same as Explorer's "Map network drive": the drive shows in Explorer and every other app.
function Get-FDMappedPath {
  param([string]$Letter)
  try {
    $net = New-Object -ComObject WScript.Network
    $all = $net.EnumNetworkDrives()
    for ($i = 0; $i -lt $all.Count - 1; $i += 2) {
      if (([string]$all.Item($i)).TrimEnd(":").ToUpper() -eq $Letter.ToUpper()) { return [string]$all.Item($i + 1) }
    }
  } catch { }
  return ""
}
function Get-FDNetText {
  # "Exception calling "MapNetworkDrive" with "3" argument(s): "The network name cannot be found."" -> the reason only
  param([string]$Text)
  $t = $Text -replace '^Exception calling "[^"]+" with "\d+" argument\(s\): ', ''
  $t = $t.Trim().Trim('"').Trim()
  return $t
}
function Get-FDDriveLetters {
  # the letters free to map: not a drive now, and not a mapped drive that is only disconnected
  $used = @{}
  foreach ($d in [System.IO.DriveInfo]::GetDrives()) { $used[$d.Name.Substring(0, 1).ToUpper()] = $true }
  $remembered = @{}
  if ($script:FDIsWindows) {
    try { foreach ($k in Get-ChildItem -LiteralPath "HKCU:\Network" -ErrorAction Stop) { $l = $k.PSChildName.ToUpper(); $used[$l] = $true; $remembered[$l] = [string](Get-ItemProperty -LiteralPath $k.PSPath).RemotePath } } catch { }
  }
  $free = New-Object System.Collections.Generic.List[string]
  foreach ($c in [char[]]"ZYXWVUTSRQPONMLKJIHGFED") { if (-not $used[[string]$c]) { $free.Add([string]$c) } }
  $gone = New-Object System.Collections.Generic.List[object]
  foreach ($l in $remembered.Keys) {
    $here = $false; foreach ($d in [System.IO.DriveInfo]::GetDrives()) { if ($d.Name.Substring(0, 1).ToUpper() -eq $l) { $here = $true } }
    if (-not $here) { $gone.Add(@{ letter = $l; unc = $remembered[$l] }) }
  }
  return @{ free = $free.ToArray(); remembered = $gone.ToArray() }
}
function Connect-FDDrive {
  param([string]$Letter, [string]$Path, [bool]$Reconnect)
  $Letter = $Letter.Trim().TrimEnd(":", "\").ToUpper()
  if ($Letter -notmatch '^[A-Z]$') { throw "Pick a drive letter." }
  $Path = $Path.Trim().Trim('"').Replace("/", "\").TrimEnd("\")
  if ($Path -notmatch '^\\\\[^\\]+\\[^\\]+') { throw "The folder needs to be a network share, such as \\fileserver\Departments." }
  if (-not $script:FDIsWindows) { throw "Drives can only be mapped on Windows." }
  try {
    $net = New-Object -ComObject WScript.Network
    $net.MapNetworkDrive($Letter + ":", $Path, $Reconnect)
  } catch {
    throw (Get-FDNetText $_.Exception.Message)
  }
  return @{ path = $Letter + ":\"; letter = $Letter; unc = $Path }
}
function Disconnect-FDDrive {
  # Force closes files left open on the drive; the drive stops coming back at sign-in either way
  param([string]$Letter, [bool]$Force)
  $Letter = $Letter.Trim().TrimEnd(":", "\").ToUpper()
  if ($Letter -notmatch '^[A-Z]$') { throw "That is not a drive letter." }
  if (-not $script:FDIsWindows) { throw "Drives can only be disconnected on Windows." }
  try {
    $net = New-Object -ComObject WScript.Network
    $net.RemoveNetworkDrive($Letter + ":", $Force, $true)
  } catch {
    $why = Get-FDNetText $_.Exception.Message
    # ERROR_OPEN_FILES (2401) or ERROR_DEVICE_IN_USE (2404), however Windows words them
    $code = 0; $ex = $_.Exception; while ($ex) { if ($ex.HResult -band 0xFFFF) { $c = $ex.HResult -band 0xFFFF; if ($c -eq 2401 -or $c -eq 2404) { $code = $c } }; $ex = $ex.InnerException }
    if (-not $Force -and ($code -or $why -match 'files open|open files|in use|pending|outstanding')) { return @{ busy = $true; why = $why } }
    throw $why
  }
  return @{ ok = $true }
}

# ---- A server's shares ----------------------------------------------------------------------------
function Get-FDShares {
  param([string]$Server)
  $srv = ([string]$Server).TrimEnd('\')
  if (-not $script:FDIsWindows) { return @{ path = $srv; error = "Shares can only be listed on Windows." } }
  $lines = @()
  try { $lines = @(& net.exe view $srv /all 2>&1) } catch { return @{ path = $srv; error = $_.Exception.Message } }
  $entries = New-Object System.Collections.Generic.List[object]
  $inTable = $false
  foreach ($ln in $lines) {
    $s = [string]$ln
    if ($s -match '^-{5,}') { $inTable = $true; continue }
    if (-not $inTable) { continue }
    if ($s -match '^\S.*\bcommand completed\b' -or -not $s.Trim()) { continue }
    # "Name   Disk   Comment": share names can hold spaces, the type column is Disk, Print or IPC
    if ($s -match '^(.+?)\s{2,}(Disk|Print|IPC)\b') {
      if ($Matches[2] -eq "Disk") { $entries.Add(@{ n = $Matches[1].Trim(); d = $true; s = 0; m = 0; c = 0; a = "share" }) }
    }
  }
  if (-not $entries.Count) {
    $joined = (($lines | ForEach-Object { [string]$_ }) -join " ").Trim()
    return @{ path = $srv; error = $(if ($joined) { $joined } else { "No shares were found on " + $srv + "." }) }
  }
  return @{ path = $srv; parent = ""; entries = $entries.ToArray(); shares = $true }
}

# ---- Searching ----------------------------------------------------------------------------------
# Walks the folder and those inside it, sending matches in batches as they are found. Folders that
# cannot be opened are passed over. The query is words (all must appear in the name) or a wildcard
# (*.pdf); "ext", "type", "after", "minSize" and "maxSize" narrow it further.
function Invoke-FDSearch {
  param([string]$Root, [string]$Query, $Options, $State)
  $max = 5000
  if ($Options -and $Options.max) { $max = [int]$Options.max }
  $q = ([string]$Query).Trim()
  $wild = ($q -match '[\*\?]')
  $words = @()
  if (-not $wild) { $words = @($q.ToLowerInvariant() -split '\s+' | Where-Object { $_ }) }
  $exts = @()
  if ($Options -and $Options.ext) { $exts = @(([string]$Options.ext).ToLowerInvariant() -split '[,;\s]+' | Where-Object { $_ } | ForEach-Object { if ($_.StartsWith(".")) { $_ } else { "." + $_ } }) }
  $type = $(if ($Options -and $Options.type) { [string]$Options.type } else { "" })
  $after = 0; $minS = -1; $maxS = -1
  if ($Options -and $Options.after) { $after = [long]$Options.after }
  if ($Options -and $null -ne $Options.minSize -and "$($Options.minSize)" -ne "") { $minS = [long]$Options.minSize }
  if ($Options -and $null -ne $Options.maxSize -and "$($Options.maxSize)" -ne "") { $maxS = [long]$Options.maxSize }
  $recurse = -not ($Options -and $Options.recurse -eq $false)
  $hidden = ($Options -and $Options.hidden -eq $true)

  $found = 0; $scanned = 0; $batch = New-Object System.Collections.Generic.List[object]
  $lastSend = [DateTime]::UtcNow
  $stack = New-Object System.Collections.Generic.Stack[string]
  $stack.Push($Root)
  while ($stack.Count -gt 0) {
    if ($State -and $State.Cancel) { break }
    $dir = $stack.Pop()
    $items = $null
    try { $items = Get-FDChildren $dir } catch { continue }
    foreach ($it in $items) {
      if ($State -and $State.Cancel) { break }
      $scanned++
      $isDir = Test-FDDirItem $it
      $at = $it.Attributes
      if (-not $hidden -and (($at -band [System.IO.FileAttributes]::Hidden) -or ($at -band [System.IO.FileAttributes]::System))) { continue }
      # links to other folders are not followed, so a loop cannot trap the search
      if ($isDir -and $recurse -and -not ($at -band [System.IO.FileAttributes]::ReparsePoint)) { $stack.Push($it.FullName) }
      $name = $it.Name
      $ok = $true
      if ($wild) { $ok = ($name -like $q) }
      elseif ($words.Count) {
        $low = $name.ToLowerInvariant()
        foreach ($w in $words) { if ($low.IndexOf($w) -lt 0) { $ok = $false; break } }
      }
      if (-not $ok) { continue }
      if ($type -eq "folder" -and -not $isDir) { continue }
      if ($type -eq "file" -and $isDir) { continue }
      if ($exts.Count -and ($isDir -or ($exts -notcontains $it.Extension.ToLowerInvariant()))) { continue }
      $e = ConvertTo-FDEntry $it
      if ($after -gt 0 -and $e.m -lt $after) { continue }
      if (-not $isDir -and $minS -ge 0 -and $e.s -lt $minS) { continue }
      if (-not $isDir -and $maxS -ge 0 -and $e.s -gt $maxS) { continue }
      $e.p = $it.FullName
      $batch.Add($e)
      $found++
      if ($found -ge $max) { break }
    }
    $now = [DateTime]::UtcNow
    if ($batch.Count -ge 200 -or ($batch.Count -gt 0 -and ($now - $lastSend).TotalMilliseconds -gt 250)) {
      Send-FDEvent $State @{ kind = "search"; items = $batch.ToArray(); scanned = $scanned; dir = $dir }
      $batch.Clear()
      $lastSend = $now
    } elseif (($now - $lastSend).TotalMilliseconds -gt 500) {
      Send-FDEvent $State @{ kind = "search"; items = @(); scanned = $scanned; dir = $dir }
      $lastSend = $now
    }
    if ($found -ge $max) { break }
  }
  if ($batch.Count) { Send-FDEvent $State @{ kind = "search"; items = $batch.ToArray(); scanned = $scanned; dir = "" } }
  return @{ found = $found; scanned = $scanned; capped = ($found -ge $max); cancelled = [bool]($State -and $State.Cancel) }
}

# ---- Folder sizes --------------------------------------------------------------------------------
# The total size and file count of each folder given, sent one by one as each is finished, and as a
# running total while a big one is being added up.
function Measure-FDFolder {
  param([string]$Path, $State, [string]$Tag)
  $bytes = [long]0; $files = 0; $dirs = 0; $denied = 0
  $stack = New-Object System.Collections.Generic.Stack[string]
  $stack.Push($Path)
  $last = [DateTime]::UtcNow
  while ($stack.Count -gt 0) {
    if ($State -and $State.Cancel) { break }
    $d = $stack.Pop()
    $items = $null
    try { $items = Get-FDChildren $d } catch { $denied++; continue }
    foreach ($it in $items) {
      if (Test-FDDirItem $it) {
        if (-not ($it.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) { $stack.Push($it.FullName); $dirs++ }
      } else {
        try { $bytes += [long]$it.Length } catch { }
        $files++
      }
    }
    if ($Tag -and ([DateTime]::UtcNow - $last).TotalMilliseconds -gt 400) {
      Send-FDEvent $State @{ kind = "size"; path = $Path; bytes = $bytes; files = $files; dirs = $dirs; partial = $true }
      $last = [DateTime]::UtcNow
    }
  }
  return @{ path = $Path; bytes = $bytes; files = $files; dirs = $dirs; denied = $denied; partial = [bool]($State -and $State.Cancel) }
}
function Get-FDSizes {
  param($Paths, $State)
  $out = New-Object System.Collections.Generic.List[object]
  foreach ($p in @($Paths)) {
    if ($State -and $State.Cancel) { break }
    $r = Measure-FDFolder ([string]$p) $State "live"
    $r.kind = "size"
    Send-FDEvent $State $r
    $out.Add($r)
  }
  return @{ sizes = $out.ToArray() }
}

# ---- Names ---------------------------------------------------------------------------------------
$script:FDBadChars = [System.IO.Path]::GetInvalidFileNameChars()
function Test-FDName {
  param([string]$Name)
  $n = [string]$Name
  if (-not $n.Trim()) { return "A name cannot be empty." }
  if ($n.IndexOfAny($script:FDBadChars) -ge 0 -or $n.IndexOfAny([char[]]'<>:"/\|?*') -ge 0) { return 'A name cannot contain any of these: \ / : * ? " < > |' }
  if ($n.EndsWith(".") -or $n.EndsWith(" ")) { return "A name cannot end with a dot or a space." }
  if ($n -match '^(?i)(CON|PRN|AUX|NUL|COM\d|LPT\d)(\..*)?$') { return ("Windows keeps the name " + $n + " for itself.") }
  return ""
}
# A free name in a folder: "Report.docx", then "Report (2).docx", "Report (3).docx"...
function Get-FDFreeName {
  param([string]$Dir, [string]$Name)
  $target = Join-FDPath $Dir $Name
  $tk = Get-FDKind $target
  if ($tk -eq 0) { return $Name }
  $base = [System.IO.Path]::GetFileNameWithoutExtension($Name)
  $ext = [System.IO.Path]::GetExtension($Name)
  if ($tk -eq 2) { $base = $Name; $ext = "" }
  if ($base -match '^(.*) \((\d+)\)$') { $base = $Matches[1] }
  for ($i = 2; $i -lt 10000; $i++) {
    $cand = $base + " (" + $i + ")" + $ext
    if (-not (Test-FDItem (Join-FDPath $Dir $cand))) { return $cand }
  }
  return ($base + " (" + [guid]::NewGuid().ToString("N").Substring(0, 6) + ")" + $ext)
}

function New-FDFolder {
  param([string]$Dir, [string]$Name)
  $bad = Test-FDName $Name
  if ($bad) { return @{ error = $bad } }
  $n = Get-FDFreeName $Dir $Name
  $full = Join-FDPath $Dir $n
  New-FDDir $full
  return @{ path = $full; name = $n }
}
function New-FDFile {
  param([string]$Dir, [string]$Name)
  $bad = Test-FDName $Name
  if ($bad) { return @{ error = $bad } }
  $n = Get-FDFreeName $Dir $Name
  $full = Join-FDPath $Dir $n
  $fs = Open-FDWrite $full
  $fs.Close()
  return @{ path = $full; name = $n }
}

# Renames one or many. Names are checked first, and nothing is renamed if any new name is not allowed
# or would clash with a file that is not itself being renamed. Many are renamed in two steps (each to
# a temporary name, then to its new one), so names can be swapped or shifted along without clashing.
function Rename-FDItems {
  param($Items)
  $list = @($Items)
  $problems = New-Object System.Collections.Generic.List[object]
  $from = @{}
  foreach ($it in $list) { $from[([string]$it.path).ToLowerInvariant()] = $true }
  $targets = @{}
  foreach ($it in $list) {
    $p = [string]$it.path; $nn = [string]$it.name
    $bad = Test-FDName $nn
    if ($bad) { $problems.Add(@{ path = $p; error = $bad }); continue }
    if (-not (Test-FDItem $p)) { $problems.Add(@{ path = $p; error = "It is no longer there." }); continue }
    $dir = [System.IO.Path]::GetDirectoryName($p.TrimEnd('\', '/'))
    $t = Join-FDPath $dir $nn
    $tl = $t.ToLowerInvariant()
    if ($targets.ContainsKey($tl)) { $problems.Add(@{ path = $p; error = ("Two items would both be called " + $nn + ".") }); continue }
    $targets[$tl] = $true
    if ((Test-FDItem $t) -and -not $from.ContainsKey($tl) -and $tl -ne $p.ToLowerInvariant()) {
      $problems.Add(@{ path = $p; error = ("Something called " + $nn + " is already there.") })
    }
  }
  if ($problems.Count) { return @{ done = 0; problems = $problems.ToArray() } }
  $moves = New-Object System.Collections.Generic.List[object]
  foreach ($it in $list) {
    $p = [string]$it.path; $nn = [string]$it.name
    $dir = [System.IO.Path]::GetDirectoryName($p.TrimEnd('\', '/'))
    if ([System.IO.Path]::GetFileName($p.TrimEnd('\', '/')) -ceq $nn) { continue }
    $moves.Add(@{ from = $p; tmp = (Join-FDPath $dir (".fdren-" + [guid]::NewGuid().ToString("N").Substring(0, 10))); to = (Join-FDPath $dir $nn); dir = ((Get-FDKind $p) -eq 2) })
  }
  $done = New-Object System.Collections.Generic.List[object]
  try {
    foreach ($m in $moves) {
      Move-FDPath $m.from $m.tmp $m.dir
      $done.Add($m)
    }
    foreach ($m in $moves) {
      Move-FDPath $m.tmp $m.to $m.dir
      $m.finished = $true
    }
  } catch {
    $why = $_.Exception.Message
    # put back whatever was moved, so a failure leaves the folder as it was
    foreach ($m in $done) {
      try {
        $now = $(if ($m.finished) { $m.to } else { $m.tmp })
        Move-FDPath $now $m.from $m.dir
      } catch { }
    }
    return @{ done = 0; problems = @(@{ path = ""; error = ("Nothing was renamed: " + $why) }) }
  }
  return @{ done = $moves.Count; problems = @(); renamed = @($moves | ForEach-Object { @{ from = $_.from; to = $_.to } }) }
}

# ---- Deleting ------------------------------------------------------------------------------------
# To the Recycle Bin where Windows has one (local drives). Network folders have no Recycle Bin, so the
# page asks first and passes Permanent.
function Test-FDRecyclable {
  param([string]$Path)
  if ($Path.StartsWith("\\")) { return $false }
  try {
    $root = [System.IO.Path]::GetPathRoot($Path)
    $d = New-Object System.IO.DriveInfo($root)
    return ($d.DriveType -eq [System.IO.DriveType]::Fixed -or $d.DriveType -eq [System.IO.DriveType]::Removable)
  } catch { return $false }
}
function Remove-FDItems {
  param($Paths, [bool]$Permanent, $State)
  $vb = $false
  if ($script:FDIsWindows -and -not $Permanent) {
    try { Add-Type -AssemblyName Microsoft.VisualBasic -ErrorAction Stop; $vb = $true } catch { $vb = $false }
  }
  $removed = 0; $problems = New-Object System.Collections.Generic.List[object]
  foreach ($p in @($Paths)) {
    if ($State -and $State.Cancel) { break }
    $p = [string]$p
    try {
      $kind = Get-FDKind $p
      $isDir = ($kind -eq 2)
      if ($kind -eq 0) { continue }
      if ($vb -and (Test-FDRecyclable $p) -and (Test-FDLong $p)) {
        $problems.Add(@{ path = $p; error = "Its path is too long for the Recycle Bin. Delete it permanently instead (Shift+Delete)." }); continue
      } elseif ($vb -and (Test-FDRecyclable $p)) {
        $ui = [Microsoft.VisualBasic.FileIO.UIOption]::OnlyErrorDialogs
        $rc = [Microsoft.VisualBasic.FileIO.RecycleOption]::SendToRecycleBin
        if ($isDir) { [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory($p, $ui, $rc) }
        else { [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile($p, $ui, $rc) }
      } elseif (-not $Permanent) {
        $problems.Add(@{ path = $p; error = "This has no Recycle Bin, so it was not deleted." }); continue
      } else {
        if ($isDir) { Remove-FDTree $p } else { Remove-FDFile $p }
      }
      $removed++
    } catch [System.OperationCanceledException] {
      break
    } catch {
      $problems.Add(@{ path = $p; error = $_.Exception.Message })
    }
  }
  return @{ removed = $removed; problems = $problems.ToArray() }
}

# ---- Copying and moving ---------------------------------------------------------------------------
# Items already there by the same name, before a copy or move starts, so the page can ask what to do.
function Get-FDConflicts {
  param($Sources, [string]$Dest)
  $names = New-Object System.Collections.Generic.List[string]
  foreach ($s in @($Sources)) {
    $n = [System.IO.Path]::GetFileName(([string]$s).TrimEnd('\', '/'))
    if (Test-FDItem (Join-FDPath $Dest $n)) { $names.Add($n) }
  }
  return @{ conflicts = $names.ToArray() }
}

# Copies (or moves) items into a folder, reporting progress in $State as it goes: bytes and files done
# and to do, and the file being copied. $Policy says what to do when a name is already there:
# "replace", "skip" or "keep" (keep both: the new one gets a free name such as "Report (2).docx").
# A move within one drive is a rename, so it is instant; across drives it is a copy, then the
# originals are removed once every file has copied.
# $Dests, when given, is a folder for each source in turn (comparing folders copies each file to its own
# matching place), and those folders are made if they are not there.
function Invoke-FDCopy {
  param($Sources, [string]$Dest, [bool]$Move, [string]$Policy, $State, $Dests = $null)
  if (-not $Policy) { $Policy = "keep" }
  $State.Phase = "counting"
  $plan = New-Object System.Collections.Generic.List[object]
  $total = [long]0; $count = 0
  $problems = New-Object System.Collections.Generic.List[object]
  $quick = New-Object System.Collections.Generic.List[object]
  # (set apart from $(...): PowerShell would turn a list of one into a plain string)
  $srcList = @($Sources)
  $destList = $null
  if ($Dests) { $destList = @($Dests) }
  $sep = [System.IO.Path]::DirectorySeparatorChar

  for ($si = 0; $si -lt $srcList.Count; $si++) {
    if ($State.Cancel) { break }
    $src = [string]$srcList[$si]
    $destFull = $(if ($destList) { [string]$destList[$si] } else { $Dest })
    if (-not (Test-FDLong $destFull)) { try { $destFull = [System.IO.Path]::GetFullPath($destFull) } catch { } }
    $destFull = $destFull.TrimEnd('\', '/')
    if ($destFull -match '^[A-Za-z]:$') { $destFull += '\' }
    if ($destList -and -not (Test-FDItem $destFull)) {
      try { New-FDDir $destFull } catch { $problems.Add(@{ path = $destFull; error = $_.Exception.Message }); continue }
    }
    $srcFull = $src.TrimEnd('\', '/')
    $kind = Get-FDKind $srcFull
    $isDir = ($kind -eq 2)
    if ($kind -eq 0) { $problems.Add(@{ path = $src; error = "It is no longer there." }); continue }
    $name = [System.IO.Path]::GetFileName($srcFull)
    $parent = [System.IO.Path]::GetDirectoryName($srcFull)
    if ($isDir -and (($destFull.TrimEnd('\', '/') + $sep).StartsWith($srcFull + $sep, [StringComparison]::OrdinalIgnoreCase))) {
      $problems.Add(@{ path = $src; error = "A folder cannot be copied into itself." }); continue
    }
    $sameFolder = ($parent.TrimEnd('\', '/') -ieq $destFull.TrimEnd('\', '/'))
    if ($Move -and $sameFolder) { continue }
    $targetName = $name
    $exists = Test-FDItem (Join-FDPath $destFull $name)
    if ($exists) {
      if ($Policy -eq "skip" -and -not $isDir) { continue }
      if ($Policy -eq "keep" -or $sameFolder) { $targetName = Get-FDFreeName $destFull $name }
    }
    $target = Join-FDPath $destFull $targetName
    # a move within one drive, to a name not taken: renamed at once
    $sameVol = ([System.IO.Path]::GetPathRoot($srcFull) -ieq [System.IO.Path]::GetPathRoot($destFull))
    if ($Move -and $sameVol -and -not (Test-FDItem $target)) {
      $quick.Add(@{ from = $srcFull; to = $target; dir = $isDir }); continue
    }
    if ($isDir) {
      $plan.Add(@{ kind = "dir"; to = $target })
      $stack = New-Object System.Collections.Generic.Stack[object]
      $stack.Push(@($srcFull, $target))
      while ($stack.Count -gt 0) {
        if ($State.Cancel) { break }
        $pair = $stack.Pop()
        $items = $null
        try { $items = Get-FDChildren $pair[0] } catch { $problems.Add(@{ path = $pair[0]; error = $_.Exception.Message }); continue }
        foreach ($it in $items) {
          $t = Join-FDPath $pair[1] $it.Name
          if (Test-FDDirItem $it) {
            $plan.Add(@{ kind = "dir"; to = $t })
            if (-not ($it.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) { $stack.Push(@($it.FullName, $t)) }
          } else {
            $plan.Add(@{ kind = "file"; from = $it.FullName; to = $t; size = [long]$it.Length; mtime = $it.LastWriteTimeUtc })
            $total += [long]$it.Length; $count++
          }
        }
      }
    } else {
      $len = [long]0; $mt = [DateTime]::UtcNow
      $fi = $null
      foreach ($x in (Get-FDChildren $parent)) { if ($x.Name -ieq $name) { $fi = $x; break } }
      if ($fi) { $len = [long]$fi.Length; $mt = $fi.LastWriteTimeUtc }
      $plan.Add(@{ kind = "file"; from = $srcFull; to = $target; size = $len; mtime = $mt })
      $total += $len; $count++
    }
    if ($Move) { $plan.Add(@{ kind = "remove"; from = $srcFull; dir = $isDir }) }
  }

  $State.BytesTotal = $total; $State.FilesTotal = $count
  $State.BytesDone = [long]0; $State.FilesDone = 0
  $State.Phase = "copying"

  foreach ($q in $quick) {
    try { Move-FDPath $q.from $q.to $q.dir } catch { $problems.Add(@{ path = $q.from; error = $_.Exception.Message }) }
  }

  $buf = New-Object byte[] (1048576)
  $failedUnder = New-Object System.Collections.Generic.List[string]
  foreach ($step in $plan) {
    if ($State.Cancel) { break }
    if ($step.kind -eq "dir") {
      try { New-FDDir $step.to } catch { $problems.Add(@{ path = $step.to; error = $_.Exception.Message }) }
      continue
    }
    if ($step.kind -eq "remove") {
      # the originals go only if every file under them copied
      $bad = $false
      foreach ($f in $failedUnder) { if ($f.StartsWith($step.from, [StringComparison]::OrdinalIgnoreCase)) { $bad = $true; break } }
      if ($bad -or $State.Cancel) { continue }
      try {
        if ($step.dir) { Remove-FDTree $step.from } else { Remove-FDFile $step.from }
      } catch { $problems.Add(@{ path = $step.from; error = ("Copied, but the original could not be removed: " + $_.Exception.Message) }) }
      continue
    }
    $State.Current = $step.from
    $tmp = $step.to + ".fdpart"
    $ok = $false
    try {
      if ($Policy -eq "skip" -and (Test-FDItem $step.to)) { $State.FilesDone++; $State.BytesDone += $step.size; continue }
      $in = Open-FDRead $step.from
      try {
        $out = Open-FDWrite $tmp
        try {
          while ($true) {
            while ($State.Paused -and -not $State.Cancel) { Start-Sleep -Milliseconds 150 }
            if ($State.Cancel) { break }
            $n = $in.Read($buf, 0, $buf.Length)
            if ($n -le 0) { break }
            $out.Write($buf, 0, $n)
            $State.BytesDone += $n
          }
        } finally { $out.Close() }
      } finally { $in.Close() }
      if ($State.Cancel) { try { Remove-FDFile $tmp } catch { }; break }
      if (Test-FDItem $step.to) { Remove-FDFile $step.to }
      Move-FDPath $tmp $step.to $false
      try { Set-FDWriteTime $step.to $step.mtime } catch { }
      $ok = $true
    } catch {
      try { if (Test-FDItem $tmp) { Remove-FDFile $tmp } } catch { }
      $problems.Add(@{ path = $step.from; error = $_.Exception.Message })
      $failedUnder.Add($step.from)
    }
    if ($ok) { $State.FilesDone++ }
  }
  $State.Phase = "done"
  return @{ filesDone = [int]$State.FilesDone; filesTotal = $count; bytesDone = [long]$State.BytesDone; bytesTotal = $total; moved = $quick.Count; problems = $problems.ToArray(); cancelled = [bool]$State.Cancel }
}

# ---- Reading a file for the preview pane ----------------------------------------------------------
# Text files are read here (the first part of a big one); pictures, PDFs and the rest are served to the
# page by the window directly.
function Read-FDText {
  param([string]$Path, [int]$Max = 262144)
  $fs = Open-FDRead $Path
  try {
    $len = $fs.Length
    $n = [int][Math]::Min([long]$Max, $len)
    $buf = New-Object byte[] $n
    $got = 0
    while ($got -lt $n) { $r = $fs.Read($buf, $got, $n - $got); if ($r -le 0) { break }; $got += $r }
  } finally { $fs.Close() }
  # mostly zero bytes: not text
  $zeros = 0
  for ($i = 0; $i -lt [Math]::Min($got, 4096); $i++) { if ($buf[$i] -eq 0) { $zeros++ } }
  $enc = New-Object System.Text.UTF8Encoding($false)
  if ($got -ge 2 -and $buf[0] -eq 0xFF -and $buf[1] -eq 0xFE) { $enc = [System.Text.Encoding]::Unicode }
  elseif ($got -ge 2 -and $buf[0] -eq 0xFE -and $buf[1] -eq 0xFF) { $enc = [System.Text.Encoding]::BigEndianUnicode }
  elseif ($zeros -gt 8) { return @{ binary = $true; size = $len } }
  $text = $enc.GetString($buf, 0, $got)
  if ($text.Length -gt 0 -and $text[0] -eq [char]0xFEFF) { $text = $text.Substring(1) }
  return @{ text = $text; size = $len; cut = ($len -gt $got) }
}

# ---- Details for the properties panel ---------------------------------------------------------------
function Get-FDDetails {
  param([string]$Path)
  $it = Get-Item -LiteralPath $Path -Force
  $r = ConvertTo-FDEntry $it
  $r.path = $it.FullName
  try { $r.accessed = Get-FDEpoch $it.LastAccessTime } catch { }
  $r.attributes = [string]$it.Attributes
  try { $r.owner = (Get-Acl -LiteralPath $Path).Owner } catch { $r.owner = "" }
  if ($it -is [System.IO.FileInfo]) {
    try {
      $vi = $it.VersionInfo
      if ($vi -and $vi.FileVersion) { $r.version = [string]$vi.FileVersion; $r.product = [string]$vi.ProductName; $r.company = [string]$vi.CompanyName }
    } catch { }
  } else {
    try { $r.items = @($it.GetFileSystemInfos()).Count } catch { }
  }
  return $r
}

# ---- File hashes ---------------------------------------------------------------------------------------
# SHA-256, SHA-1 and MD5 in one read of the file (as ThreatLocker and VirusTotal show them: capitals),
# with progress for a big one.
function Get-FDHash {
  param([string]$Path, $State)
  $algs = @([System.Security.Cryptography.SHA256]::Create(), [System.Security.Cryptography.SHA1]::Create(), [System.Security.Cryptography.MD5]::Create())
  $fs = Open-FDRead $Path
  try {
    $total = $fs.Length; $done = [long]0
    $buf = New-Object byte[] (1048576)
    $last = [DateTime]::UtcNow
    while ($true) {
      if ($State -and $State.Cancel) { return @{ cancelled = $true } }
      $n = $fs.Read($buf, 0, $buf.Length)
      if ($n -le 0) { break }
      foreach ($a in $algs) { [void]$a.TransformBlock($buf, 0, $n, $null, 0) }
      $done += $n
      if (([DateTime]::UtcNow - $last).TotalMilliseconds -gt 300) { Send-FDEvent $State @{ kind = "hash"; done = $done; total = $total }; $last = [DateTime]::UtcNow }
    }
    foreach ($a in $algs) { [void]$a.TransformFinalBlock((New-Object byte[] 0), 0, 0) }
    $hex = { param($h) (($h | ForEach-Object { $_.ToString("X2") }) -join "") }
    return @{ sha256 = (& $hex $algs[0].Hash); sha1 = (& $hex $algs[1].Hash); md5 = (& $hex $algs[2].Hash); size = $total }
  } finally { $fs.Close(); foreach ($a in $algs) { $a.Dispose() } }
}

# ---- Comparing two folders -------------------------------------------------------------------------------
# Everything inside each folder, by its path within it. Files are the same when their sizes match and their
# modified times are within 2 seconds (FAT drives and some servers keep times to 2 seconds). A folder that is
# only on one side is listed once, not with everything inside it.
function Get-FDTree {
  param([string]$Root, $State, [ref]$Scanned)
  $map = @{}
  $stack = New-Object System.Collections.Generic.Stack[string]
  $stack.Push("")
  while ($stack.Count -gt 0) {
    if ($State -and $State.Cancel) { break }
    $rel = $stack.Pop()
    $dir = $(if ($rel) { Join-FDPath $Root $rel } else { $Root })
    $items = $null
    try { $items = Get-FDChildren $dir } catch { continue }
    foreach ($it in $items) {
      $r = $(if ($rel) { $rel + $script:FDSep + $it.Name } else { $it.Name })
      $isDir = Test-FDDirItem $it
      $map[$r.ToLowerInvariant()] = @{ rel = $r; d = $isDir; s = $(if ($isDir) { [long]0 } else { [long]$it.Length }); m = $it.LastWriteTimeUtc }
      $Scanned.Value++
      if ($isDir -and -not ($it.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) { $stack.Push($r) }
    }
    if ($State -and ($Scanned.Value % 2000) -lt 20) { Send-FDEvent $State @{ kind = "compare"; scanned = $Scanned.Value } }
  }
  return $map
}
function Compare-FDFolders {
  param([string]$Left, [string]$Right, $State, [int]$Max = 20000)
  $scanned = 0
  $leftTree = Get-FDTree $Left $State ([ref]$scanned)
  $rightTree = Get-FDTree $Right $State ([ref]$scanned)
  $keys = New-Object System.Collections.Generic.List[string]
  foreach ($k in $leftTree.Keys) { $keys.Add($k) }
  foreach ($k in $rightTree.Keys) { if (-not $leftTree.ContainsKey($k)) { $keys.Add($k) } }
  $keys.Sort([StringComparer]::OrdinalIgnoreCase)
  $items = New-Object System.Collections.Generic.List[object]
  $counts = @{ same = 0; onlyLeft = 0; onlyRight = 0; newerLeft = 0; newerRight = 0; different = 0; kinds = 0 }
  $skip = $null
  $epoch = { param($d) Get-FDEpoch ($d.ToLocalTime()) }
  foreach ($k in $keys) {
    if ($skip -and $k.StartsWith($skip)) { continue }
    $skip = $null
    $li = $leftTree[$k]; $ri = $rightTree[$k]
    $st = ""
    if ($li -and -not $ri) { $st = "onlyLeft"; if ($li.d) { $skip = $k + $script:FDSep } }
    elseif ($ri -and -not $li) { $st = "onlyRight"; if ($ri.d) { $skip = $k + $script:FDSep } }
    elseif ($li.d -and $ri.d) { continue }
    elseif ($li.d -ne $ri.d) { $st = "kinds" }
    else {
      $dt = ($li.m - $ri.m).TotalSeconds
      if ([Math]::Abs($dt) -le 2 -and $li.s -eq $ri.s) { $counts.same++; continue }
      if ([Math]::Abs($dt) -gt 2) { $st = $(if ($dt -gt 0) { "newerLeft" } else { "newerRight" }) } else { $st = "different" }
    }
    $counts[$st]++
    if ($items.Count -lt $Max) {
      $one = $(if ($li) { $li } else { $ri })
      $items.Add(@{ rel = $one.rel; st = $st; d = [bool]$one.d
        ls = $(if ($li) { $li.s } else { $null }); lm = $(if ($li) { & $epoch $li.m } else { $null })
        rs = $(if ($ri) { $ri.s } else { $null }); rm = $(if ($ri) { & $epoch $ri.m } else { $null }) })
    }
  }
  return @{ left = $Left; right = $Right; items = $items.ToArray(); counts = $counts; scanned = $scanned; capped = ($items.Count -ge $Max); cancelled = [bool]($State -and $State.Cancel) }
}

# ---- Zip files ---------------------------------------------------------------------------------------------
$script:FDZipReady = $false
function Initialize-FDZip {
  if ($script:FDZipReady) { return }
  Add-Type -AssemblyName System.IO.Compression
  try { Add-Type -AssemblyName System.IO.Compression.FileSystem } catch { }
  $script:FDZipReady = $true
}
# A path that goes inside a zip file (C:\Files\Report.zip\Images): the zip, and where inside it.
function Find-FDZipPart {
  param([string]$Path)
  $sp = $script:FDSep
  $p = $(if ($sp -eq "\") { $Path.Replace("/", "\") } else { $Path })
  $at = 0
  while ($true) {
    $i = $p.IndexOf(".zip" + $sp, $at, [StringComparison]::OrdinalIgnoreCase)
    if ($i -lt 0) { return $null }
    $zip = $p.Substring(0, $i + 4)
    if ((Get-FDKind $zip) -eq 1) { return @{ zip = $zip; inner = $p.Substring($i + 5).Trim([char]$sp).Replace($sp, "/") } }
    $at = $i + 5
  }
}
function Open-FDZip {
  param([string]$Zip, [bool]$Write = $false)
  Initialize-FDZip
  if ($Write) { return (New-Object System.IO.Compression.ZipArchive((Open-FDWrite $Zip), [System.IO.Compression.ZipArchiveMode]::Create, $false)) }
  return (New-Object System.IO.Compression.ZipArchive((Open-FDRead $Zip), [System.IO.Compression.ZipArchiveMode]::Read, $false))
}
# What is at one place inside a zip, listed like a folder. Entries carry "z" in their flags.
function Get-FDZipList {
  param([string]$Zip, [string]$Inner)
  $pre = $(if ($Inner) { $Inner.Trim("/") + "/" } else { "" })
  $za = $null
  try { $za = Open-FDZip $Zip } catch { return @{ path = $Zip; error = ("This zip file could not be opened: " + $_.Exception.Message) } }
  try {
    $dirs = @{}; $out = New-Object System.Collections.Generic.List[object]
    foreach ($e in $za.Entries) {
      $fn = $e.FullName.Replace("\", "/")
      if ($pre -and -not $fn.StartsWith($pre, [StringComparison]::OrdinalIgnoreCase)) { continue }
      $rest = $fn.Substring($pre.Length)
      if (-not $rest) { continue }
      $slash = $rest.IndexOf("/")
      if ($slash -ge 0) {
        $dn = $rest.Substring(0, $slash)
        if ($dn -and -not $dirs.ContainsKey($dn.ToLowerInvariant())) { $dirs[$dn.ToLowerInvariant()] = $true; $out.Add(@{ n = $dn; d = $true; s = 0; m = 0; c = 0; a = "z" }) }
        continue
      }
      $m = 0; try { $m = [long]$e.LastWriteTime.ToUnixTimeMilliseconds() } catch { }
      $out.Add(@{ n = $rest; d = $false; s = [long]$e.Length; m = $m; c = 0; a = "z"; packed = [long]$e.CompressedLength })
    }
    $here = $Zip + $(if ($Inner) { $script:FDSep + $Inner.Trim("/").Replace("/", $script:FDSep) } else { "" })
    $parent = $(if ($Inner) { [System.IO.Path]::GetDirectoryName($here) } else { [System.IO.Path]::GetDirectoryName($Zip) })
    return @{ path = $here; parent = $parent; entries = $out.ToArray(); zip = $Zip; inner = $Inner.Trim("/") }
  } finally { $za.Dispose() }
}
# Puts files and folders in a new zip file in the folder given, named after the first of them.
function New-FDZip {
  param($Paths, [string]$Dir, [string]$Name, $State)
  Initialize-FDZip
  $list = New-Object System.Collections.Generic.List[object]
  $total = [long]0
  foreach ($p in @($Paths)) {
    $p = ([string]$p).TrimEnd("\")
    $base = [System.IO.Path]::GetFileName($p)
    if ((Get-FDKind $p) -eq 2) {
      $stack = New-Object System.Collections.Generic.Stack[object]
      $stack.Push(@($p, $base))
      $list.Add(@{ dir = $true; rel = $base + "/" })
      while ($stack.Count -gt 0) {
        $pair = $stack.Pop()
        foreach ($it in (Get-FDChildren $pair[0])) {
          $rel = $pair[1] + "/" + $it.Name
          if (Test-FDDirItem $it) { $list.Add(@{ dir = $true; rel = $rel + "/" }); $stack.Push(@($it.FullName, $rel)) }
          else { $list.Add(@{ dir = $false; rel = $rel; from = $it.FullName; size = [long]$it.Length; m = $it.LastWriteTime }); $total += [long]$it.Length }
        }
      }
    } elseif ((Get-FDKind $p) -eq 1) {
      $fi = $null; foreach ($x in (Get-FDChildren ([System.IO.Path]::GetDirectoryName($p)))) { if ($x.Name -ieq $base) { $fi = $x } }
      $list.Add(@{ dir = $false; rel = $base; from = $p; size = $(if ($fi) { [long]$fi.Length } else { 0 }); m = $(if ($fi) { $fi.LastWriteTime } else { Get-Date }) })
      if ($fi) { $total += [long]$fi.Length }
    }
  }
  $zn = Get-FDFreeName $Dir $Name
  $zp = Join-FDPath $Dir $zn
  $State.BytesTotal = $total; $State.BytesDone = [long]0
  $za = Open-FDZip $zp $true
  $buf = New-Object byte[] (1048576)
  $ok = $false
  try {
    foreach ($it in $list) {
      if ($State.Cancel) { break }
      if ($it.dir) { [void]$za.CreateEntry($it.rel); continue }
      $e = $za.CreateEntry($it.rel, [System.IO.Compression.CompressionLevel]::Optimal)
      try { $e.LastWriteTime = [DateTimeOffset]$it.m } catch { }
      $out = $e.Open(); $in = Open-FDRead $it.from
      try {
        while ($true) { $n = $in.Read($buf, 0, $buf.Length); if ($n -le 0) { break }; $out.Write($buf, 0, $n); $State.BytesDone += $n }
      } finally { $in.Close(); $out.Close() }
      Send-FDEvent $State @{ kind = "zip"; done = [long]$State.BytesDone; total = $total }
    }
    $ok = -not $State.Cancel
  } finally { $za.Dispose() }
  if (-not $ok) { try { Remove-FDFile $zp } catch { }; return @{ cancelled = $true } }
  return @{ path = $zp; name = $zn; files = @($list | Where-Object { -not $_.dir }).Count }
}
# Takes files out of a zip: everything (into a new folder named after the zip), or what is selected inside it.
# Nothing is written outside the folder chosen, whatever names the zip holds; a name already there keeps both.
function Expand-FDZip {
  param([string]$Zip, [string]$Dest, $Entries, [string]$Inner, $State)
  $za = Open-FDZip $Zip
  $files = 0; $problems = New-Object System.Collections.Generic.List[object]
  try {
    if (-not $Dest) {
      $folder = Get-FDFreeName ([System.IO.Path]::GetDirectoryName($Zip)) ([System.IO.Path]::GetFileNameWithoutExtension($Zip))
      $Dest = Join-FDPath ([System.IO.Path]::GetDirectoryName($Zip)) $folder
    }
    New-FDDir $Dest
    $destRoot = $Dest.TrimEnd("\", "/") + [System.IO.Path]::DirectorySeparatorChar
    $pre = $(if ($Inner) { $Inner.Trim("/") + "/" } else { "" })
    $want = @(@($Entries) | Where-Object { $_ } | ForEach-Object { ($pre + ([string]$_).Trim("/")).ToLowerInvariant() })
    $total = [long]0
    foreach ($e in $za.Entries) { $total += [long]$e.Length }
    $State.BytesTotal = $total; $State.BytesDone = [long]0
    $buf = New-Object byte[] (1048576)
    foreach ($e in $za.Entries) {
      if ($State.Cancel) { break }
      $fn = $e.FullName.Replace("\", "/")
      $low = $fn.ToLowerInvariant()
      if ($want.Count) {
        $hit = $false
        foreach ($w in $want) { if ($low -eq $w -or $low.StartsWith($w + "/")) { $hit = $true; break } }
        if (-not $hit) { continue }
      }
      $rel = $(if ($pre -and $fn.StartsWith($pre, [StringComparison]::OrdinalIgnoreCase)) { $fn.Substring($pre.Length) } else { $fn })
      if (-not $rel) { continue }
      $target = Join-FDPath $Dest ($rel.Replace("/", [string][System.IO.Path]::DirectorySeparatorChar))
      $full = $target
      if (-not (Test-FDLong $target)) { $full = [System.IO.Path]::GetFullPath($target) }
      if (-not $full.StartsWith($destRoot, [StringComparison]::OrdinalIgnoreCase)) { $problems.Add(@{ path = $fn; error = "Its name points outside the folder, so it was left out." }); continue }
      if ($fn.EndsWith("/")) { New-FDDir $full; continue }
      try {
        New-FDDir ([System.IO.Path]::GetDirectoryName($full))
        if (Test-FDItem $full) { $full = Join-FDPath ([System.IO.Path]::GetDirectoryName($full)) (Get-FDFreeName ([System.IO.Path]::GetDirectoryName($full)) ([System.IO.Path]::GetFileName($full))) }
        $in = $e.Open(); $out = Open-FDWrite $full
        try { while ($true) { $n = $in.Read($buf, 0, $buf.Length); if ($n -le 0) { break }; $out.Write($buf, 0, $n); $State.BytesDone += $n } }
        finally { $in.Close(); $out.Close() }
        try { Set-FDWriteTime $full $e.LastWriteTime.UtcDateTime } catch { }
        $files++
      } catch { $problems.Add(@{ path = $fn; error = $_.Exception.Message }) }
    }
  } finally { $za.Dispose() }
  return @{ dest = $Dest; files = $files; problems = $problems.ToArray(); cancelled = [bool]$State.Cancel }
}
# One file from a zip, taken out to a temporary folder so it can be opened.
function Expand-FDZipEntry {
  param([string]$Zip, [string]$Entry)
  $za = Open-FDZip $Zip
  try {
    $e = $null
    foreach ($x in $za.Entries) { if ($x.FullName.Replace("\", "/") -ieq $Entry.Trim("/")) { $e = $x; break } }
    if (-not $e) { return @{ error = "That file is no longer in the zip." } }
    $dir = Join-Path (Join-Path ([System.IO.Path]::GetTempPath()) "Atlas") ([guid]::NewGuid().ToString("N").Substring(0, 8))
    New-FDDir $dir
    $to = Join-FDPath $dir $e.Name
    $in = $e.Open(); $out = Open-FDWrite $to
    try { $in.CopyTo($out) } finally { $in.Close(); $out.Close() }
    return @{ path = $to }
  } finally { $za.Dispose() }
}

# ---- What is using a file ----------------------------------------------------------------------------------
# Windows' Restart Manager names the programs that have files open on this PC. For a folder, the files in
# it (up to 400) are checked. On a network share only the server knows who has a file open from elsewhere.
function Get-FDUsers {
  param($Paths)
  $files = New-Object System.Collections.Generic.List[string]
  $network = $false
  foreach ($p in @($Paths)) {
    $p = [string]$p
    if ($p.StartsWith("\\")) { $network = $true }
    else {
      try { if ((New-Object System.IO.DriveInfo([System.IO.Path]::GetPathRoot($p))).DriveType -eq [System.IO.DriveType]::Network) { $network = $true } } catch { }
    }
    if ((Get-FDKind $p) -eq 2) {
      $stack = New-Object System.Collections.Generic.Stack[string]; $stack.Push($p)
      while ($stack.Count -gt 0 -and $files.Count -lt 400) {
        $d = $stack.Pop()
        try { foreach ($it in (Get-FDChildren $d)) { if (Test-FDDirItem $it) { $stack.Push($it.FullName) } elseif ($files.Count -lt 400) { $files.Add($it.FullName) } } } catch { }
      }
    } else { $files.Add($p) }
  }
  $progs = New-Object System.Collections.Generic.List[object]
  if ($files.Count -and $script:FDIsWindows) {
    foreach ($line in [FDRm]::Who($files.ToArray())) {
      $f = $line.Split("`t")
      $owner = ""
      try { $pr = Get-CimInstance Win32_Process -Filter ("ProcessId=" + [int]$f[0]) -ErrorAction Stop; $o = Invoke-CimMethod -InputObject $pr -MethodName GetOwner -ErrorAction Stop; if ($o.User) { $owner = $(if ($o.Domain) { $o.Domain + "\" + $o.User } else { $o.User }) } } catch { }
      $exe = ""
      try { $exe = (Get-Process -Id ([int]$f[0]) -ErrorAction Stop).Path } catch { }
      $progs.Add(@{ pid = [int]$f[0]; kind = $f[1]; name = $f[2]; service = $(if ($f.Count -gt 3) { $f[3] } else { "" }); owner = $owner; exe = $exe })
    }
  }
  return @{ programs = $progs.ToArray(); checked = $files.Count; network = $network }
}

# ---- Who has access -----------------------------------------------------------------------------------------
# NTFS permissions in plain words, read only. Rights are named the way Explorer's Security tab names them.
function ConvertTo-FDRights {
  param([long]$Mask)
  $has = { param($x) (($Mask -band $x) -eq $x) }
  if ((& $has 0x10000000) -or (& $has 0x1F01FF)) { return "Full control" }
  if ((& $has 0x1301BF)) { return "Change" }
  $read = (& $has 0x1200A9) -or (& $has 0x80000000) -or (& $has 0x20089)
  $write = (& $has 0x116) -or (& $has 0x40000000)
  if ($read -and $write) { return "Read and write" }
  if ($read) { return "Read" }
  if ($write) { return "Write only" }
  if (& $has 0x1) { return "See what is in it" }
  return "Special"
}
function ConvertTo-FDApplies {
  param($Rule, [bool]$IsDir)
  if (-not $IsDir) { return "This file" }
  $ci = ($Rule.InheritanceFlags -band [System.Security.AccessControl.InheritanceFlags]::ContainerInherit) -ne 0
  $oi = ($Rule.InheritanceFlags -band [System.Security.AccessControl.InheritanceFlags]::ObjectInherit) -ne 0
  $io = ($Rule.PropagationFlags -band [System.Security.AccessControl.PropagationFlags]::InheritOnly) -ne 0
  $np = ($Rule.PropagationFlags -band [System.Security.AccessControl.PropagationFlags]::NoPropagateInherit) -ne 0
  $what = $(if ($ci -and $oi) { "subfolders and files" } elseif ($ci) { "subfolders" } elseif ($oi) { "files" } else { "" })
  if ($np -and $what) { $what = $what.Replace("subfolders and files", "folders and files directly inside").Replace("subfolders", "folders directly inside").Replace("files", "files directly inside") }
  if (-not $what) { return "This folder only" }
  if ($io) { return ($what.Substring(0, 1).ToUpper() + $what.Substring(1) + " only") }
  return ("This folder, " + $what)
}
function Get-FDAccess {
  param([string]$Path)
  if (Test-FDLong $Path) { return @{ path = $Path; error = "This path is too long for Windows to read its permissions from here. Open a folder nearer the top of it." } }
  $isDir = ((Get-FDKind $Path) -eq 2)
  $acl = Get-Acl -LiteralPath $Path
  $rules = $acl.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier])
  $me = [System.Security.Principal.WindowsIdentity]::GetCurrent()
  $mine = @{}
  $mine[$me.User.Value] = $true
  foreach ($g in $me.Groups) { $mine[$g.Value] = $true }
  $allow = [long]0; $deny = [long]0; $via = New-Object System.Collections.Generic.List[string]
  $out = New-Object System.Collections.Generic.List[object]
  foreach ($r in $rules) {
    $sid = $r.IdentityReference.Value
    $who = $sid
    try { $who = $r.IdentityReference.Translate([System.Security.Principal.NTAccount]).Value } catch { }
    $mask = [long]$r.FileSystemRights
    $applies = ConvertTo-FDApplies $r $isDir
    $isDeny = ($r.AccessControlType -eq [System.Security.AccessControl.AccessControlType]::Deny)
    $out.Add(@{ who = $who; sid = $sid; deny = $isDeny; rights = (ConvertTo-FDRights $mask); mask = $mask; inherited = [bool]$r.IsInherited; applies = $applies })
    $inheritOnly = ($r.PropagationFlags -band [System.Security.AccessControl.PropagationFlags]::InheritOnly) -ne 0
    if ($mine.ContainsKey($sid) -and -not $inheritOnly) {
      if ($isDeny) { $deny = $deny -bor $mask } else { $allow = $allow -bor $mask; if ($via -notcontains $who) { $via.Add($who) } }
    }
  }
  $eff = $allow -band (-bnot $deny)
  $owner = ""
  try { $owner = $acl.Owner } catch { }
  # the share's own permissions, when the server will say
  $share = $null; $shareError = ""
  $unc = $Path
  if ($Path -match '^([A-Za-z]):') {
    try { $k = Get-ItemProperty -LiteralPath ("HKCU:\Network\" + $Matches[1]) -ErrorAction Stop; $unc = [string]$k.RemotePath + $Path.Substring(2) } catch { $unc = $Path }
  }
  if ($unc -match '^\\\\([^\\]+)\\([^\\]+)') {
    $srv = $Matches[1]; $shn = $Matches[2]
    try {
      $ss = Get-CimInstance -ClassName Win32_LogicalShareSecuritySetting -ComputerName $srv -Filter ("Name='" + $shn.Replace("'", "''") + "'") -OperationTimeoutSec 8 -ErrorAction Stop
      $sd = (Invoke-CimMethod -InputObject $ss -MethodName GetSecurityDescriptor -ErrorAction Stop).Descriptor
      $se = New-Object System.Collections.Generic.List[object]
      foreach ($ace in @($sd.DACL)) {
        $t = $ace.Trustee
        $nm = $(if ($t.Domain) { $t.Domain + "\" + $t.Name } else { $t.Name })
        $m = [long]$ace.AccessMask
        $rt = $(if (($m -band 2032127) -eq 2032127) { "Full control" } elseif (($m -band 1245631) -eq 1245631) { "Change" } elseif (($m -band 1179817) -eq 1179817) { "Read" } else { "Special" })
        $se.Add(@{ who = $nm; rights = $rt; deny = ([int]$ace.AceType -eq 1) })
      }
      $share = @{ name = ("\\" + $srv + "\" + $shn); entries = $se.ToArray() }
    } catch { $shareError = ("The share's own permissions (\\" + $srv + "\" + $shn + ") could not be read: only the server's administrators can see them.") }
  }
  return @{ path = $Path; dir = $isDir; owner = $owner; protected = [bool]$acl.AreAccessRulesProtected; entries = $out.ToArray()
    me = @{ user = $me.Name; rights = $(if ($eff) { ConvertTo-FDRights $eff } else { "None" }); denied = [bool]$deny; via = $via.ToArray() }; share = $share; shareError = $shareError }
}
# The members of a group in Active Directory (read only). Windows' own groups, such as BUILTIN\Administrators,
# belong to each computer, not to the directory.
function ConvertTo-FDLdap { param([string]$S) return ($S.Replace("\", "\5c").Replace("*", "\2a").Replace("(", "\28").Replace(")", "\29").Replace([string][char]0, "\00")) }
function Get-FDGroupMembers {
  param([string]$Name)
  $dom = ""; $acct = $Name
  if ($Name.Contains("\")) { $dom = $Name.Split("\")[0]; $acct = $Name.Split("\")[1] }
  $builtinDomain = (@("BUILTIN", "NT AUTHORITY", "NT SERVICE", "APPLICATION PACKAGE AUTHORITY", "CREATOR OWNER", ([Environment]::MachineName).ToUpperInvariant()) -contains $dom.ToUpperInvariant())
  $builtinName = ((-not $dom) -and (@("Everyone", "CREATOR OWNER", "SYSTEM") -contains $acct))
  if ($builtinDomain -or $builtinName) {
    return @{ group = $Name; builtin = $true; note = "This is one of Windows' own groups. It belongs to each computer rather than to the directory, so its members are not listed here." }
  }
  $root = New-Object System.DirectoryServices.DirectoryEntry
  $s = New-Object System.DirectoryServices.DirectorySearcher($root)
  $s.Filter = "(&(objectCategory=group)(sAMAccountName=" + (ConvertTo-FDLdap $acct) + "))"
  [void]$s.PropertiesToLoad.AddRange(@("distinguishedName", "description", "member"))
  $g = $s.FindOne()
  if (-not $g) { return @{ group = $Name; notGroup = $true; note = "This is a person or a computer, not a group." } }
  $dn = [string]$g.Properties["distinguishedname"][0]
  $m = New-Object System.DirectoryServices.DirectorySearcher($root)
  $m.Filter = "(memberOf=" + (ConvertTo-FDLdap $dn) + ")"
  $m.PageSize = 500; $m.SizeLimit = 2000
  [void]$m.PropertiesToLoad.AddRange(@("displayName", "sAMAccountName", "objectClass", "mail", "userAccountControl", "title", "department"))
  $out = New-Object System.Collections.Generic.List[object]
  foreach ($r in $m.FindAll()) {
    $cls = @($r.Properties["objectclass"])
    $kind = $(if ($cls -contains "computer") { "computer" } elseif ($cls -contains "group") { "group" } else { "person" })
    $uac = 0; if ($r.Properties["useraccountcontrol"].Count) { $uac = [int]$r.Properties["useraccountcontrol"][0] }
    $out.Add(@{ name = [string]($r.Properties["displayname"] | Select-Object -First 1); login = [string]($r.Properties["samaccountname"] | Select-Object -First 1)
      kind = $kind; mail = [string]($r.Properties["mail"] | Select-Object -First 1); title = [string]($r.Properties["title"] | Select-Object -First 1)
      disabled = (($uac -band 2) -ne 0) })
  }
  $desc = ""; if ($g.Properties["description"].Count) { $desc = [string]$g.Properties["description"][0] }
  return @{ group = $Name; description = $desc; members = $out.ToArray(); capped = ($out.Count -ge 2000)
    note = $(if ($acct -ieq "Domain Users") { "Everyone with an account belongs to Domain Users, usually without being listed as a member." } else { "" }) }
}
