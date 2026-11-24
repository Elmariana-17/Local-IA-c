# installers/win/install.ps1
$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent $PSScriptRoot
$Python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $Python) { Write-Error "Python no encontrado, instala Python 3.10+"; exit 1 }

Write-Host "[1/3] Creando entorno virtual..."
Set-Location $ProjectDir
python -m venv .venv
& "$ProjectDir\.venv\Scripts\pip.exe" install -r requirements.txt

Write-Host "[2/3] Creando script de inicio..."
$VbsPath = Join-Path $ProjectDir "start-hidden.vbs"
@"
Set WshShell = CreateObject("WScript.Shell")
WshShell.Run """$ProjectDir\.venv\Scripts\python.exe"" -m uvicorn core.main:app --host 127.0.0.1 --port 8001", 0, False
"@ | Out-File -FilePath $VbsPath -Encoding ASCII

Write-Host "[3/3] Registrando tarea programada..."
$Action = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "`"$VbsPath`"" -WorkingDirectory $ProjectDir
$Trigger = New-ScheduledTaskTrigger -AtLogOn
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
Register-ScheduledTask -TaskName "LocalAIBridge" -Action $Action -Trigger $Trigger -Settings $Settings -Force

Start-ScheduledTask -TaskName "LocalAIBridge"
Write-Host " Instalación completada. API en http://127.0.0.1:8001"