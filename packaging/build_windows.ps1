# Builds dist\CanAnalyser-windows.zip from a clean virtual environment.
# Run from the repository root:  powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
$ErrorActionPreference = "Stop"
function Check { if ($LASTEXITCODE -ne 0) { throw "Build step failed (exit code $LASTEXITCODE)" } }

python -m venv .venv-build; Check
.venv-build\Scripts\python -m pip install --upgrade pip; Check
.venv-build\Scripts\python -m pip install . pyinstaller; Check

.venv-build\Scripts\pyinstaller --noconfirm --clean packaging\cananalyser.spec; Check

# Databases go next to the .exe so colleagues can see and add files.
Copy-Item -Recurse -Force databases dist\CanAnalyser\databases
Copy-Item -Force packaging\README-users.txt dist\CanAnalyser\README.txt

Compress-Archive -Force -Path dist\CanAnalyser -DestinationPath dist\CanAnalyser-windows.zip
Write-Host "Built dist\CanAnalyser-windows.zip"
