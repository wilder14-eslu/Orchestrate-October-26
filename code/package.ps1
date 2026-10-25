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

$stagingDir = Join-Path ([System.IO.Path]::GetTempPath()) "orchestrate_package_$(Get-Random)"
$null = New-Item -ItemType Directory -Path $stagingDir -Force

try {
    # 1. Copy README.md
    Copy-Item (Join-Path $repoRoot "README.md") -Destination (Join-Path $stagingDir "README.md")

    # 2. Copy code directory recursively while skipping excluded patterns
    $codeStaging = Join-Path $stagingDir "code"
    $null = New-Item -ItemType Directory -Path $codeStaging -Force

    $excludeDirNames = @("__pycache__", ".pytest_cache", ".venv", "venv", "results")

    Get-ChildItem -Path (Join-Path $repoRoot "code") -Recurse | ForEach-Object {
        $item = $_
        $relPath = $item.FullName.Substring((Join-Path $repoRoot "code").Length).TrimStart("\", "/")

        $segments = $relPath -split "[\\/]"
        $excluded = $false
        foreach ($seg in $segments) {
            if ($seg -in $excludeDirNames -or $seg.EndsWith(".pyc") -or $seg.EndsWith(".zip") -or $seg -eq ".env") {
                $excluded = $true
                break
            }
        }

        # Specifically exclude code/evaluation/results/
        if ($relPath -like "evaluation\results*" -or $relPath -like "evaluation/results*") {
            $excluded = $true
        }

        if (-not $excluded) {
            $destPath = Join-Path $codeStaging $relPath
            if ($item.PSIsContainer) {
                if (-not (Test-Path $destPath)) {
                    $null = New-Item -ItemType Directory -Path $destPath -Force
                }
            } else {
                $parent = Split-Path $destPath
                if (-not (Test-Path $parent)) {
                    $null = New-Item -ItemType Directory -Path $parent -Force
                }
                Copy-Item $item.FullName -Destination $destPath -Force
            }
        }
    }

    # 3. Create zip archive
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [System.IO.Compression.ZipFile]::CreateFromDirectory($stagingDir, $zipPath)

    # 4. Verify and print archive contents and size
    Write-Host "`nSuccessfully created: $zipPath"
    $zipSize = (Get-Item $zipPath).Length
    $zipSizeKB = [math]::Round($zipSize / 1KB, 2)
    $zipSizeMB = [math]::Round($zipSize / 1MB, 2)
    Write-Host "Archive Size: $zipSize bytes ($zipSizeKB KB / $zipSizeMB MB)`n"

    Write-Host "Archive Manifest:"
    Write-Host "------------------------------------------------------------"
    $zip = [System.IO.Compression.ZipFile]::OpenRead($zipPath)
    $entries = $zip.Entries | Sort-Object FullName
    foreach ($entry in $entries) {
        if (-not $entry.FullName.EndsWith("/")) {
            Write-Host ("{0,-60} {1,10} bytes" -f $entry.FullName, $entry.Length)
        }
    }
    $fileCount = ($entries | Where-Object { -not $_.FullName.EndsWith("/") }).Count
    $zip.Dispose()
    Write-Host "------------------------------------------------------------"
    Write-Host "Total Files: $fileCount`n"

} finally {
    if (Test-Path $stagingDir) {
        Remove-Item -Recurse -Force $stagingDir
    }
}
