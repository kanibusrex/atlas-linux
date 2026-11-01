# Starts the built Atlas.exe on a Windows CI machine and takes pictures of the screen, so the build can be
# looked at without a Windows PC: try\1-started.png, try\2-handed-over.png and try\atlas.log.
$ErrorActionPreference = "Stop"
New-Item -ItemType Directory -Force try | Out-Null
$env:ATLAS_LOG = (Resolve-Path try).Path + "\atlas.log"
$env:ATLAS_DEBUG = "1"
Add-Type -AssemblyName System.Windows.Forms, System.Drawing

function Shot($name) {
  $b = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
  $bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height
  $g = [System.Drawing.Graphics]::FromImage($bmp)
  $g.CopyFromScreen($b.Location, [System.Drawing.Point]::Empty, $b.Size)
  $bmp.Save((Join-Path (Resolve-Path try) "$name.png"))
  $g.Dispose(); $bmp.Dispose()
}

try {
  # the built app's own Python reaching GitHub (it has no certificates of its own; see atlas_update.py)
  $check = Start-Process dist\Atlas\Atlas.exe -ArgumentList "--check-update" -Wait -PassThru
  Get-Content $env:ATLAS_LOG -ErrorAction SilentlyContinue | Select-String "Atlas \S+ (couldn't reach GitHub|: the newest release)"
  if ($check.ExitCode -ne 0) { throw "The built Atlas couldn't reach GitHub to look for updates." }

  $atlas = Start-Process dist\Atlas\Atlas.exe -ArgumentList "C:\Windows" -PassThru
  Start-Sleep 25
  Shot "1-started"
  if ($atlas.HasExited) { throw "Atlas closed by itself (exit code $($atlas.ExitCode))" }
  if (-not (Select-String -Path $env:ATLAS_LOG -SimpleMatch "The page is up and talking to Atlas" -Quiet)) { throw "Atlas's window opened, but its page never reached Atlas." }

  # a second Atlas hands its folder to the first (a new tab there), and leaves
  $t = [Diagnostics.Stopwatch]::StartNew()
  $second = Start-Process dist\Atlas\Atlas.exe -ArgumentList "C:\Users" -PassThru
  if (-not $second.WaitForExit(30000)) { throw "The second Atlas didn't hand over its folder and leave." }
  "The second Atlas left after $($t.ElapsedMilliseconds) ms"
  Start-Sleep 5
  Shot "2-handed-over"

  $atlas.CloseMainWindow() | Out-Null
  if (-not $atlas.WaitForExit(15000)) { $atlas.Kill(); throw "Atlas didn't close when asked." }
  "Atlas closed"

  # Documents marked as downloaded from the internet (Mark of the Web, which stops Explorer's preview pane
  # showing them). Atlas shows them itself: the pictures should show the documents, and the mark must stay.
  New-Item -ItemType Directory -Force try\motw | Out-Null
  Copy-Item tests\samples\* try\motw
  $saved = "$env:LOCALAPPDATA\Atlas\page.json"
  $page = Get-Content $saved -Raw | ConvertFrom-Json
  $page | Add-Member -Force pvPos "right"
  $page | Add-Member -Force preview $true
  [IO.File]::WriteAllText($saved, ($page | ConvertTo-Json -Depth 30))
  foreach ($name in "sample.pdf", "sample.docx") {
    $file = (Resolve-Path "try\motw\$name").Path
    Set-Content -LiteralPath $file -Stream Zone.Identifier -Value "[ZoneTransfer]`r`nZoneId=3"
    $atlas = Start-Process dist\Atlas\Atlas.exe -ArgumentList "`"$file`"" -PassThru
    Start-Sleep 20
    Shot "3-marked-$name"
    if ($atlas.HasExited) { throw "Atlas closed by itself previewing $name" }
    $atlas.CloseMainWindow() | Out-Null
    if (-not $atlas.WaitForExit(15000)) { $atlas.Kill() }
    if (-not (Get-Content -LiteralPath $file -Stream Zone.Identifier -ErrorAction SilentlyContinue)) { throw "Previewing $name took its mark away" }
    "$name previewed, and still marked"
  }
} finally {
  "---- atlas.log"
  Get-Content $env:ATLAS_LOG -ErrorAction SilentlyContinue
  "---- Windows' record of programs that failed, from the last few minutes"
  Get-WinEvent -FilterHashtable @{ LogName = "Application"; StartTime = (Get-Date).AddMinutes(-10) } -ErrorAction SilentlyContinue |
    Where-Object { $_.ProviderName -in ".NET Runtime", "Application Error" } | ForEach-Object { $_.Message; "" }
}
