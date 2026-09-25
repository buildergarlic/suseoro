[CmdletBinding()]
param(
    [string]$PythonPath = '',
    [string]$Version = '2.2.2',
    [string]$TesseractDir = '',
    [string]$IsccPath = '',
    [string]$MakensisPath = '',
    [switch]$PortableZip,
    [switch]$SkipInstaller,
    [switch]$SkipPyInstaller
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$workspace = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$buildRoot = Join-Path $workspace '.build\desktop'
$distRoot = Join-Path $workspace 'dist'
$appRoot = Join-Path $distRoot 'Suseoro'
$uiRoot = Join-Path $workspace 'frontend\dist'
$guideRoot = Join-Path $workspace 'docs'
$guideFiles = @('index.html', 'visual-guide.html', 'user-guide.html', 'quick-start.html', 'school-templates.html')

function Assert-WorkspacePath([string]$Target) {
    $resolved = [IO.Path]::GetFullPath($Target)
    if (-not $resolved.StartsWith($workspace + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Build path is outside the workspace: $resolved"
    }
    $ancestor = $resolved
    while ($ancestor -and $ancestor -ne $workspace) {
        if (Test-Path -LiteralPath $ancestor) {
            $item = Get-Item -LiteralPath $ancestor -Force
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Build cleanup cannot follow a link or junction: $ancestor"
            }
        }
        $ancestor = Split-Path -Parent $ancestor
    }
    return $resolved
}

function Clear-BuildDirectory([string]$Target) {
    $resolved = Assert-WorkspacePath $Target
    if ($resolved -ne $buildRoot -and -not $resolved.StartsWith($buildRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to clear a directory outside the desktop build tree: $resolved"
    }
    if (Test-Path -LiteralPath $resolved) {
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
    New-Item -ItemType Directory -Path $resolved -Force | Out-Null
}

if ($env:OS -ne 'Windows_NT') { throw 'Desktop packaging requires Windows.' }
if ($Version -notmatch '^\d+\.\d+\.\d+$') { throw 'Version must have the form 2.0.0.' }
if (-not $PythonPath) { $PythonPath = Join-Path $workspace 'backend\.venv\Scripts\python.exe' }
if (-not (Test-Path -LiteralPath $PythonPath)) { throw 'Install backend desktop dependencies before building, or supply -PythonPath.' }
Assert-WorkspacePath $appRoot | Out-Null
Assert-WorkspacePath $buildRoot | Out-Null
New-Item -ItemType Directory -Force -Path $distRoot | Out-Null

if (-not $SkipPyInstaller) {
    if (-not (Test-Path -LiteralPath (Join-Path $uiRoot 'index.html'))) {
        throw 'Build frontend/dist first. The installer must contain a complete user interface.'
    }
    foreach ($guide in $guideFiles) {
        if (-not (Test-Path -LiteralPath (Join-Path $guideRoot $guide))) {
            throw "Missing bundled guide: $guide. Run uv run scripts/build-user-guide.py before packaging."
        }
    }
    Clear-BuildDirectory $buildRoot
    $actualVersion = & $PythonPath -c 'from suseoro.simple import VERSION; print(VERSION)'
    if ($LASTEXITCODE -ne 0 -or $actualVersion.Trim() -ne $Version) { throw 'Package and requested build versions differ.' }
    $licenseStage = Join-Path $buildRoot 'licenses'
    $licenseScript = @'
import importlib.metadata as metadata
import pathlib, re, shutil, sys
target = pathlib.Path(sys.argv[1])
target.mkdir(parents=True, exist_ok=True)
count = 0
for distribution in metadata.distributions():
    folder = re.sub(r'[^a-zA-Z0-9_.-]', '_', distribution.metadata.get('Name', 'package') + '-' + distribution.version)
    for relative in distribution.files or []:
        parts = relative.parts
        name = relative.name.lower()
        if not (name.startswith(('license', 'licence', 'copying', 'notice', 'authors')) or any(part.lower() == 'licenses' for part in parts)):
            continue
        source = pathlib.Path(distribution.locate_file(relative))
        if not source.is_file():
            continue
        safe_parts = [part for part in parts if part not in ('.', '..', '/', '\\')]
        destination = target / folder / pathlib.Path(*safe_parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        count += 1
print(f'Bundled {count} installed library license files.')
python_license = pathlib.Path(sys.base_prefix) / 'LICENSE.txt'
if python_license.is_file():
    shutil.copy2(python_license, target / 'Python-LICENSE.txt')
workspace = target.parents[2]
for package in ('react', 'react-dom'):
    license_file = workspace / 'frontend' / 'node_modules' / package / 'LICENSE'
    if not license_file.is_file():
        raise RuntimeError(f'Missing frontend license: {package}')
    shutil.copy2(license_file, target / (package + '-LICENSE.txt'))
'@
    & $PythonPath -c $licenseScript $licenseStage
    if ($LASTEXITCODE -ne 0) { throw 'Library license collection failed.' }
    $arguments = @(
        '-m', 'PyInstaller', '--noconfirm', '--clean', '--onedir', '--windowed',
        '--name', 'Suseoro', '--distpath', $distRoot,
        '--workpath', (Join-Path $buildRoot 'work'), '--specpath', $buildRoot,
        '--paths', (Join-Path $workspace 'backend\src'),
        '--add-data', ($uiRoot + ';ui'),
        '--add-data', ($licenseStage + ';licenses'),
        '--collect-all', 'webview', '--collect-all', 'pythonnet', '--collect-all', 'clr_loader',
        '--collect-all', 'uvicorn', '--collect-all', 'pypdfium2', '--collect-all', 'python_calamine',
        '--collect-submodules', 'suseoro.simple', '--collect-submodules', 'suseoro.ingestion',
        '--hidden-import', 'uvicorn.logging', '--hidden-import', 'uvicorn.loops.asyncio',
        '--hidden-import', 'uvicorn.protocols.http.h11_impl', '--hidden-import', 'uvicorn.lifespan.on',
        '--hidden-import', 'webview.platforms.winforms', '--hidden-import', 'webview.platforms.edgechromium',
        '--exclude-module', 'PyQt5', '--exclude-module', 'PyQt6', '--exclude-module', 'PySide2',
        '--exclude-module', 'PySide6', '--exclude-module', 'cefpython3', '--exclude-module', 'pytest'
    )
    foreach ($guide in $guideFiles) {
        $arguments += @('--add-data', ((Join-Path $guideRoot $guide) + ';help'))
    }
    if ($TesseractDir) {
        $ocrSource = (Resolve-Path -LiteralPath $TesseractDir).Path
        foreach ($required in @('tesseract.exe', 'tessdata\kor.traineddata', 'tessdata\eng.traineddata')) {
            if (-not (Test-Path -LiteralPath (Join-Path $ocrSource $required))) { throw "Missing OCR file: $required" }
        }
        $ocrStage = Join-Path $buildRoot 'portable_tesseract'
        New-Item -ItemType Directory -Force -Path (Join-Path $ocrStage 'tessdata') | Out-Null
        Copy-Item -LiteralPath (Join-Path $ocrSource 'tesseract.exe') -Destination $ocrStage
        Get-ChildItem -LiteralPath $ocrSource -File -Filter '*.dll' | Copy-Item -Destination $ocrStage
        foreach ($language in @('kor', 'eng')) {
            Copy-Item -LiteralPath (Join-Path $ocrSource "tessdata\$language.traineddata") -Destination (Join-Path $ocrStage 'tessdata')
        }
        foreach ($license in @('LICENSE', 'LICENSE.txt', 'COPYING', 'COPYING.txt')) {
            if (Test-Path -LiteralPath (Join-Path $ocrSource $license)) {
                Copy-Item -LiteralPath (Join-Path $ocrSource $license) -Destination $ocrStage
            }
        }
        if (Test-Path -LiteralPath (Join-Path $ocrSource 'doc')) {
            Copy-Item -LiteralPath (Join-Path $ocrSource 'doc') -Destination $ocrStage -Recurse
        }
        # OCR is a separate process. Do not let PyInstaller promote its OpenSSL
        # DLLs into Python's directory: the two runtimes use different versions.
    }
    $arguments += (Join-Path $workspace 'desktop\launcher.py')
    & $PythonPath @arguments
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed.' }
    if ($TesseractDir) {
        Copy-Item -LiteralPath $ocrStage -Destination (Join-Path $appRoot '_internal') -Recurse -Force
    }
    if (-not (Test-Path -LiteralPath (Join-Path $appRoot '_internal\ui\index.html'))) { throw 'Frozen application is missing its UI.' }
    foreach ($guide in $guideFiles) {
        if (-not (Test-Path -LiteralPath (Join-Path $appRoot "_internal\help\$guide"))) {
            throw "Frozen application is missing its guide: $guide"
        }
    }
    foreach ($document in @('LICENSE', 'README.md', 'THIRD_PARTY_NOTICES.md', 'docs\quick-start.md')) {
        $source = Join-Path $workspace $document
        if (-not (Test-Path -LiteralPath $source)) { throw "Missing distribution document: $document" }
        Copy-Item -LiteralPath $source -Destination (Join-Path $appRoot (Split-Path -Leaf $document)) -Force
    }
    $smokeData = Join-Path $buildRoot 'smoke-data'
    $smokeArguments = @('--smoke-test', '--data-dir', ('"' + $smokeData + '"'))
    $smoke = Start-Process -FilePath (Join-Path $appRoot 'Suseoro.exe') -ArgumentList $smokeArguments -WindowStyle Hidden -PassThru
    if (-not $smoke.WaitForExit(30000)) { throw 'The packaged application smoke test did not finish within 30 seconds.' }
    if ($smoke.ExitCode -ne 0) { throw "Packaged application smoke test failed. See $smokeData\desktop.log" }
}

if (-not (Test-Path -LiteralPath (Join-Path $appRoot 'Suseoro.exe'))) { throw 'No packaged Suseoro.exe is available.' }

Copy-Item -LiteralPath (Join-Path $workspace 'desktop\Start-Review.cmd') -Destination $appRoot -Force
Copy-Item -LiteralPath (Join-Path $workspace 'docs\recommendation-batch-guide.md') -Destination $appRoot -Force

if (-not $SkipInstaller) {
    if ($IsccPath) {
        throw 'Official auto-update releases require NSIS. Supply -MakensisPath instead. The archived Inno script is for manual installers only.'
    }
    if (-not $MakensisPath) {
        $nsisCommand = Get-Command makensis.exe -ErrorAction SilentlyContinue
        if ($nsisCommand) { $MakensisPath = $nsisCommand.Source }
        elseif (Test-Path -LiteralPath (Join-Path $workspace '.tools\nsis\nsis-3.12\makensis.exe')) {
            $MakensisPath = Join-Path $workspace '.tools\nsis\nsis-3.12\makensis.exe'
        }
    }
    if ($MakensisPath) {
        & $MakensisPath '/V3' '/WX' '/INPUTCHARSET' 'UTF8' ("/DAPP_VERSION=$Version") ("/DSOURCE_DIR=$appRoot") ("/DOUTPUT_DIR=$distRoot") (Join-Path $workspace 'installer\suseoro.nsi')
    }
    else {
        throw 'NSIS is required for official auto-update installers. Supply -MakensisPath for official NSIS portable. This script never installs a compiler.'
    }
    if ($LASTEXITCODE -ne 0) { throw 'Installer compilation failed.' }
}

if ($PortableZip) {
    $zip = Join-Path $distRoot "Suseoro-Portable-$Version.zip"
    Assert-WorkspacePath $zip | Out-Null
    Compress-Archive -LiteralPath $appRoot -DestinationPath $zip -Force
}

$checksumLines = @()
foreach ($name in @("Suseoro-Setup-$Version.exe", "Suseoro-Portable-$Version.zip", "Suseoro-Guide-$Version.zip")) {
    $file = Join-Path $distRoot $name
    if (Test-Path -LiteralPath $file) {
        $hash = (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLowerInvariant()
        $line = "$hash  $name"
        Set-Content -LiteralPath ($file + '.sha256') -Value $line -Encoding ascii
        $checksumLines += $line
        Write-Output $file
    }
}
Set-Content -LiteralPath (Join-Path $distRoot 'SHA256SUMS') -Value $checksumLines -Encoding ascii
