# مدقق الطابعي - سكربت التنصيب (يحتاج نت مرة وحدة بس)
# يتنصب كلشي داخل %LOCALAPPDATA%\Mudaqqiq بدون صلاحيات أدمن (عدا Tesseract إذا winget طلب).
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}

$Src  = Split-Path -Parent $MyInvocation.MyCommand.Path
$Dest = Join-Path $env:LOCALAPPDATA 'Mudaqqiq'
$Tmp  = Join-Path $Dest 'downloads'
New-Item -ItemType Directory -Force -Path $Dest, $Tmp | Out-Null
$LogFile = Join-Path $Dest 'install-log.txt'
Start-Transcript -Path $LogFile -Force | Out-Null

function Step($msg) { Write-Host ""; Write-Host "==> $msg" -ForegroundColor Cyan }
function Download($url, $out) {
    if (Test-Path $out) { return }
    Write-Host "    Downloading: $url"
    Invoke-WebRequest -Uri $url -OutFile "$out.part" -UseBasicParsing
    Move-Item "$out.part" $out -Force
}
$HasWinget = [bool](Get-Command winget -ErrorAction SilentlyContinue)

# ---------------------------------------------------------- 1) نسخ ملفات البرنامج
Step 'Copying program files'
Copy-Item (Join-Path $Src 'mudaqqiq.py') $Dest -Force
foreach ($a in @('logo.png','logo_white.png','icon.png','icon.ico','version.json')) { if (Test-Path (Join-Path $Src $a)) { Copy-Item (Join-Path $Src $a) $Dest -Force } }

# ---------------------------------------------------------- 2) Python
Step 'Python 3.12'
function Find-Python {
    $c = @("$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
           "$env:ProgramFiles\Python312\python.exe")
    foreach ($p in $c) { if (Test-Path $p) { return $p } }
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) { try { $x = & py -3.12 -c "import sys;print(sys.executable)" 2>$null; if ($x -and (Test-Path $x)) { return $x } } catch {} }
    return $null
}
$Python = Find-Python
if (-not $Python) {
    $inst = Join-Path $Tmp 'python-3.12.7-amd64.exe'
    Download 'https://www.python.org/ftp/python/3.12.7/python-3.12.7-amd64.exe' $inst
    Start-Process $inst -ArgumentList '/quiet InstallAllUsers=0 PrependPath=1 Include_test=0 Include_launcher=1' -Wait
    $Python = Find-Python
}
if (-not $Python) { throw 'Python install failed' }
Write-Host "    $Python"

# ---------------------------------------------------------- 3) Java (نسخة محمولة داخل مجلد البرنامج)
Step 'Java 21'
$JreDir = Join-Path $Dest 'jre'
$Java = Get-ChildItem $JreDir -Recurse -Filter java.exe -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $Java) {
    $zip = Join-Path $Tmp 'jre21.zip'
    Download 'https://api.adoptium.net/v3/binary/latest/21/ga/windows/x64/jre/hotspot/normal/eclipse?project=jdk' $zip
    Expand-Archive $zip -DestinationPath $JreDir -Force
    $Java = Get-ChildItem $JreDir -Recurse -Filter java.exe | Select-Object -First 1
}
$Java = $Java.FullName
Write-Host "    $Java"

# ---------------------------------------------------------- 4) LanguageTool
Step 'LanguageTool (~250MB download)'
$LtRoot = Join-Path $Dest 'LanguageTool'
$Jar = Get-ChildItem $LtRoot -Recurse -Filter languagetool-server.jar -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $Jar) {
    $zip = Join-Path $Tmp 'LanguageTool-stable.zip'
    Download 'https://languagetool.org/download/LanguageTool-stable.zip' $zip
    Expand-Archive $zip -DestinationPath $LtRoot -Force
    $Jar = Get-ChildItem $LtRoot -Recurse -Filter languagetool-server.jar | Select-Object -First 1
}
$LtDir = $Jar.DirectoryName
Write-Host "    $LtDir"

# ---------------------------------------------------------- 5) Tesseract OCR
Step 'Tesseract OCR'
function Find-Tess {
    $c = @("$env:ProgramFiles\Tesseract-OCR\tesseract.exe", "${env:ProgramFiles(x86)}\Tesseract-OCR\tesseract.exe",
           "$env:LOCALAPPDATA\Programs\Tesseract-OCR\tesseract.exe")
    foreach ($p in $c) { if (Test-Path $p) { return $p } }
    $t = Get-Command tesseract -ErrorAction SilentlyContinue; if ($t) { return $t.Source }
    return $null
}
$Tess = Find-Tess
if (-not $Tess -and $HasWinget) {
    Write-Host '    Installing via winget (accept the permission prompt if shown)'
    winget install --id UB-Mannheim.TesseractOCR -e --silent --accept-package-agreements --accept-source-agreements | Out-Host
    $Tess = Find-Tess
}
if (-not $Tess) {
    $inst = Join-Path $Tmp 'tesseract-setup.exe'
    Download 'https://digi.bib.uni-mannheim.de/tesseract/tesseract-ocr-w64-setup-5.4.0.20240606.exe' $inst
    Write-Host '    Opening Tesseract installer - finish it with default settings'
    Start-Process $inst -Wait
    $Tess = Find-Tess
}
if (-not $Tess) { throw 'Tesseract install failed' }
Write-Host "    $Tess"
$TessData = Join-Path $Dest 'tessdata'
New-Item -ItemType Directory -Force -Path $TessData | Out-Null
foreach ($l in @('eng', 'ara', 'osd')) {
    Download "https://github.com/tesseract-ocr/tessdata_best/raw/main/$l.traineddata" (Join-Path $TessData "$l.traineddata")
}

# ---------------------------------------------------------- 6) مكتبات بايثون
Step 'Python libraries'
$Venv = Join-Path $Dest 'venv'
if (-not (Test-Path (Join-Path $Venv 'Scripts\python.exe'))) { & $Python -m venv $Venv }
$VPy  = Join-Path $Venv 'Scripts\python.exe'
$VPyw = Join-Path $Venv 'Scripts\pythonw.exe'
& $VPy -m pip install --upgrade pip --quiet
& $VPy -m pip install pymupdf pytesseract pillow requests --quiet
if ($LASTEXITCODE -ne 0) { throw 'pip install failed' }

# ---------------------------------------------------------- 7) الإعدادات
Step 'Saving settings'
$Browser = @("${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe", "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
             "$env:ProgramFiles\Google\Chrome\Application\chrome.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
$Logo = Join-Path $Dest 'logo.png'
$cfgPath = Join-Path $Dest 'config.json'
$OldLogo = $null
if (Test-Path $cfgPath) { try { $OldLogo = (Get-Content $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json).logo } catch {} }
$cfg = [ordered]@{
    tesseract = $Tess; tessdata = $TessData; java = $Java
    languagetool_dir = $LtDir; browser = "$Browser"; logo = ''
}
if ($OldLogo) { $cfg.logo = $OldLogo } elseif (Test-Path $Logo) { $cfg.logo = $Logo }
$json = $cfg | ConvertTo-Json
[IO.File]::WriteAllText($cfgPath, $json, (New-Object Text.UTF8Encoding $false))

# ---------------------------------------------------------- 8) اختصار على سطح المكتب
Step 'Desktop shortcut'
$Desktop = [Environment]::GetFolderPath('Desktop')
$Script = Join-Path $Dest 'mudaqqiq.py'
# مشغّل احتياطي (.bat) داخل مجلد البرنامج
$Runner = Join-Path $Dest 'run.bat'
[IO.File]::WriteAllText($Runner, "@echo off`r`nstart """" ""$VPyw"" ""$Script""`r`n", (New-Object Text.ASCIIEncoding))
try {
    $ws = New-Object -ComObject WScript.Shell
    $lnk = $ws.CreateShortcut((Join-Path $Desktop 'Mudaqqiq Altabaay.lnk'))   # اسم إنكليزي: WScript ما يقبل عربي
    $lnk.TargetPath = $VPyw
    $lnk.Arguments = '"' + $Script + '"'
    $lnk.WorkingDirectory = $Dest
    $lnk.Description = 'Mudaqqiq - offline booklet proofreading'
    if (Test-Path (Join-Path $Dest 'icon.ico')) { $lnk.IconLocation = (Join-Path $Dest 'icon.ico') }
    $lnk.Save()
    Write-Host '    Desktop shortcut: Mudaqqiq Altabaay'
} catch {
    Copy-Item $Runner (Join-Path $Desktop 'Mudaqqiq Altabaay.bat') -Force
    Write-Host '    Desktop launcher: Mudaqqiq Altabaay.bat'
}

# ---------------------------------------------------------- 9) فحص سريع
Step 'Self-test'
& $VPy -c "import pymupdf, pytesseract, PIL, requests; print('    Python libraries OK')"
& $Tess --version | Select-Object -First 1 | ForEach-Object { Write-Host "    $_" }
$ErrorActionPreference = 'Continue'
cmd /c "`"$Java`" -version 2>&1" | Select-Object -First 1 | ForEach-Object { Write-Host "    $_" }

Remove-Item $Tmp -Recurse -Force -ErrorAction SilentlyContinue
Stop-Transcript | Out-Null
Write-Host ''
Write-Host 'DONE. Open "Mudaqqiq Altabaay" from the Desktop. It works offline from now on.' -ForegroundColor Green
