param(
    [string]$Python = "$PSScriptRoot\..\.venv\Scripts\python.exe",
    [string]$Iscc = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
)
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) { throw "Python not found: $Python" }
if (-not (Test-Path -LiteralPath $Iscc -PathType Leaf)) { throw "Inno Setup compiler not found: $Iscc" }
$appOutput = Join-Path $projectRoot 'build\installer-app'
if (Test-Path -LiteralPath $appOutput) {
    throw "Build output already exists: $appOutput. Use a clean output directory to avoid packaging stale files."
}
Push-Location $projectRoot
try {
    & $Python -m PyInstaller --noconfirm --distpath $appOutput --workpath 'build\pyinstaller' 'packaging\scribe.spec'
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed.' }
    & $Iscc 'packaging\scribe.iss'
    if ($LASTEXITCODE -ne 0) { throw 'Installer compilation failed.' }
} finally {
    Pop-Location
}
