# Packaging script for submission: builds code.zip containing README.md and code/
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path "$PSScriptRoot\..").Path
$zipPath = Join-Path $repoRoot "code.zip"

Write-Host "Building code.zip from repository root: $repoRoot"

if (Test-Path $zipPath) {
    Remove-Item -Force $zipPath
}

Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem

$fileStream = [System.IO.File]::Create($zipPath)
$archive = New-Object System.IO.Compression.ZipArchive($fileStream, [System.IO.Compression.ZipArchiveMode]::Create)

$excludeDirNames = @("__pycache__", ".pytest_cache", ".venv", "venv", "results")

try {
    # 1. Add README.md
    $readmePath = Join-Path $repoRoot "README.md"
    if (Test-Path $readmePath) {
        $entryName = "README.md"
        $null = [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($archive, $readmePath, $entryName, [System.IO.Compression.CompressionLevel]::Optimal)
    } else {
        throw "README.md not found at repository root: $readmePath"
    }

    # 2. Add files in code/ recursively while skipping excluded patterns
    $codeDir = Join-Path $repoRoot "code"
    Get-ChildItem -Path $codeDir -Recurse -File | ForEach-Object {
        $file = $_
        $relPath = $file.FullName.Substring($repoRoot.Length).TrimStart("\", "/")

        $segments = $relPath -split "[\\/]"
        $excluded = $false
        foreach ($seg in $segments) {
            if ($seg -in $excludeDirNames -or $seg.EndsWith(".pyc") -or $seg.EndsWith(".zip") -or $seg -eq ".env") {
                $excluded = $true
                break
            }
        }

        # Specifically exclude code/evaluation/results/
        if ($relPath -like "code\evaluation\results*" -or $relPath -like "code/evaluation/results*") {
            $excluded = $true
        }

        if (-not $excluded) {
            # Convert all path separators to forward slashes for Linux compatibility
            $entryName = $relPath.Replace("\", "/")
            $null = [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($archive, $file.FullName, $entryName, [System.IO.Compression.CompressionLevel]::Optimal)
        }
    }
}
finally {
    $archive.Dispose()
    $fileStream.Dispose()
}

# 3. Verification step: verify no entry contains '\'
Write-Host "`nVerifying archive entries in: $zipPath"
$verifyStream = [System.IO.File]::OpenRead($zipPath)
$verifyArchive = New-Object System.IO.Compression.ZipArchive($verifyStream, [System.IO.Compression.ZipArchiveMode]::Read)

$hasBackslash = $false
$invalidEntries = @()
$entries = $verifyArchive.Entries | Sort-Object FullName

foreach ($entry in $entries) {
    if ($entry.FullName.Contains("\")) {
        $hasBackslash = $true
        $invalidEntries += $entry.FullName
    }
}

if ($hasBackslash) {
    $verifyArchive.Dispose()
    $verifyStream.Dispose()
    Write-Error "ERROR: Found $($invalidEntries.Count) zip entries containing backslashes ('\'):"
    foreach ($inv in $invalidEntries) {
        Write-Host "  $inv" -ForegroundColor Red
    }
    exit 1
}

# 4. Print manifest and sizes
$zipSize = (Get-Item $zipPath).Length
$zipSizeKB = [math]::Round($zipSize / 1KB, 2)
$zipSizeMB = [math]::Round($zipSize / 1MB, 2)
Write-Host "Successfully created: $zipPath"
Write-Host "Archive Size: $zipSize bytes ($zipSizeKB KB / $zipSizeMB MB)`n"

Write-Host "Archive Manifest (Forward-slash paths verified):"
Write-Host "------------------------------------------------------------"
foreach ($entry in $entries) {
    Write-Host ("{0,-60} {1,10} bytes" -f $entry.FullName, $entry.Length)
}
Write-Host "------------------------------------------------------------"
Write-Host "Total Files: $($entries.Count)`n"

$verifyArchive.Dispose()
$verifyStream.Dispose()
Write-Host "Backslash check: PASSED (0 backslashes found across $($entries.Count) entries)." -ForegroundColor Green
