# Stop Da Fei Yu: the app process first, then NapCat (the QQ runtime).
$ErrorActionPreference = 'SilentlyContinue'

$targets = @()
$targets += Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like '*app.py*' }
$targets += Get-CimInstance Win32_Process -Filter "Name='node.exe'" |
    Where-Object { $_.CommandLine -like '*napcat*' }

if ($targets.Count -eq 0) {
    Write-Host 'Nothing is running.'
    exit 0
}

foreach ($item in $targets) {
    Write-Host ("Stopping {0} (PID {1})" -f $item.Name, $item.ProcessId)
    Stop-Process -Id $item.ProcessId -Force
}

Write-Host 'Stopped.'
