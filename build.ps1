$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw 'Falta .venv. Sigue los pasos del README para preparar Python.'
}

Push-Location -LiteralPath $projectRoot
$previousTemp = $env:TEMP
$previousTmp = $env:TMP
try {
    & $pythonPath -B -m unittest discover -s tests -q
    if ($LASTEXITCODE -ne 0) { throw 'Fallaron las pruebas. Se conserva el ejecutable anterior.' }

    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
    $stagingPath = Join-Path $projectRoot ".build-tmp\release-$stamp"
    $tempPath = Join-Path $stagingPath 'temp'
    New-Item -ItemType Directory -Path $tempPath -Force | Out-Null
    $env:TEMP = $tempPath
    $env:TMP = $tempPath
    & $pythonPath -m PyInstaller --noconfirm --workpath (Join-Path $stagingPath 'build') --distpath (Join-Path $stagingPath 'dist') 'TFT AUTO-ACCEPT.spec'
    if ($LASTEXITCODE -ne 0) { throw 'Falló PyInstaller. Se conserva el ejecutable anterior.' }

    $stagedExe = Join-Path $stagingPath 'dist\TFT AUTO-ACCEPT.exe'
    & $pythonPath -B tests\smoke_exe.py $stagedExe
    if ($LASTEXITCODE -ne 0) { throw 'Falló el arranque del EXE. Se conserva el ejecutable anterior.' }

    $distPath = Join-Path $projectRoot 'dist'
    New-Item -ItemType Directory -Path $distPath -Force | Out-Null
    $destination = Join-Path $distPath 'TFT AUTO-ACCEPT.exe'
    if (Test-Path -LiteralPath $destination) {
        $backupPath = Join-Path $distPath 'backups'
        New-Item -ItemType Directory -Path $backupPath -Force | Out-Null
        $backupFile = Join-Path $backupPath "TFT AUTO-ACCEPT-$stamp.exe"
        Copy-Item -LiteralPath $destination -Destination $backupFile
        if ((Get-FileHash -LiteralPath $destination).Hash -ne (Get-FileHash -LiteralPath $backupFile).Hash) {
            throw 'No se pudo verificar la copia de respaldo. Se conserva el ejecutable anterior.'
        }
        Write-Output "Respaldo: $backupFile"
    }
    Copy-Item -LiteralPath $stagedExe -Destination $destination -Force
    if ((Get-FileHash -LiteralPath $stagedExe).Hash -ne (Get-FileHash -LiteralPath $destination).Hash) {
        throw 'El ejecutable copiado no coincide con el verificado.'
    }
    Write-Output "Ejecutable verificado: $destination"
} finally {
    $env:TEMP = $previousTemp
    $env:TMP = $previousTmp
    Pop-Location
}
