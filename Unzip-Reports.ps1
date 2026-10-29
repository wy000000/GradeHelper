# Unzip all zip files under D:\AI\ReportProj\Reports
# Contents are extracted directly to the Reports folder (no per-zip subfolder)

$sourceDir = ".\"

# Check if directory exists
if (-not (Test-Path $sourceDir)) {
    Write-Error "Directory not found: $sourceDir"
    exit 1
}

# Get all zip files (@() ensures always an array)
$zipFiles = @(Get-ChildItem $sourceDir -Filter *.zip -File)

if ($zipFiles.Count -eq 0) {
    Write-Host "No zip files found."
    exit 0
}

Write-Host "Found $($zipFiles.Count) zip files, extracting contents directly to $sourceDir`n"

$success = 0
$failed = 0

foreach ($zip in $zipFiles) {
    try {
        # Extract directly to Reports folder (no same-named subfolder)
        Expand-Archive -Path $zip.FullName -DestinationPath $sourceDir -Force

        Write-Host "[OK] $($zip.Name)"
        $success++
    }
    catch {
        Write-Warning "[FAIL] $($zip.Name) : $_"
        $failed++
    }
}

Write-Host "`n=============================="
Write-Host "Extraction complete: $success succeeded, $failed failed"
Write-Host "=============================="