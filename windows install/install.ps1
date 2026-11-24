$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectDir
if (-not (Test-Path ".venv\Scripts\python.exe")) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw "No se pudo crear el entorno virtual" }
}
& ".venv\Scripts\python.exe" -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "No se pudieron instalar las dependencias" }
if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    # MLX is available on Apple silicon. Windows connects to an existing engine.
    Add-Content ".env" "`nLOCAL_IA_MODE=external`nLOCAL_IA_EXTERNAL_URL=http://127.0.0.1:11434"
}
Write-Host 'Configura LOCAL_IA_MODEL en .env con un modelo instalado en tu motor (por ejemplo, Ollama).'
Write-Host 'Inicia: .venv\Scripts\python.exe -m core.cli serve'
Write-Host 'Conversa: .venv\Scripts\python.exe -m core.cli chat'
