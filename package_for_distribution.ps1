# Package ArcTrigger for Distribution
# This script creates a clean distribution package

$version = Read-Host "Enter version (MV1.0 or MV1.1)"
$packageName = "ArcTrigger_$version"

# Create package directory
$packageDir = "dist\$packageName"
if (Test-Path $packageDir) {
    Remove-Item $packageDir -Recurse -Force
}
New-Item -ItemType Directory -Path $packageDir | Out-Null

# Copy executable
$exeName = "ArcTrigger00_$version.exe"
if (Test-Path "dist\$exeName") {
    Copy-Item "dist\$exeName" "$packageDir\ArcTrigger.exe"
    Write-Host "✅ Copied executable: $exeName"
} else {
    Write-Host "❌ Executable not found: dist\$exeName"
    exit 1
}

# Copy deployment guide
if (Test-Path "DEPLOYMENT.md") {
    Copy-Item "DEPLOYMENT.md" "$packageDir\DEPLOYMENT.md"
    Write-Host "✅ Copied deployment guide"
}

# Create README for the package
$readme = @"
# ArcTrigger $version

## Quick Start

1. Ensure **Interactive Brokers TWS is installed and running** (must be logged in)
2. Run `ArcTrigger.exe`
3. The application will create necessary files automatically

## Requirements

- Windows 10/11 (64-bit)
- TWS (Trader Workstation) installed and running
- TWS API enabled (Settings → API → Enable ActiveX and Socket Clients)

## Optional: Polygon API Key

If you need Polygon data features, create a `.env` file in this folder:
```
POLYGON_API_KEY=your_key_here
```

## Support

See DEPLOYMENT.md for detailed information.

Version: $version
"@

$readme | Out-File "$packageDir\README.txt" -Encoding UTF8
Write-Host "✅ Created README.txt"

# Create logs directory (empty, will be populated at runtime)
New-Item -ItemType Directory -Path "$packageDir\logs" | Out-Null
Write-Host "✅ Created logs directory"

Write-Host "`n✅ Package created: $packageDir"
Write-Host "`nContents:"
Get-ChildItem $packageDir | Select-Object Name, Length | Format-Table -AutoSize

Write-Host "`n📦 Ready to share! Zip the folder: $packageDir"
