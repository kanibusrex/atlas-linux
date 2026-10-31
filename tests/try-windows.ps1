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
  $atlas = Start-Process dist\Atlas\Atlas.exe -ArgumentList "C:\Windows" -PassThru
  Start-Sleep 25
  Shot "1-started"
  if ($atlas.HasExited) { throw "Atlas closed by itself (exit code $($atlas.ExitCode))" }

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
} finally {
  "---- atlas.log"
  Get-Content $env:ATLAS_LOG -ErrorAction SilentlyContinue
}
