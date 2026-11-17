@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion

rem ============================================================================
rem 打包綠色免安裝版.bat
rem 從 Git 已提交內容組裝乾淨的 WingLightGIS 綠色免安裝版資料夾與 ZIP 壓縮包。
rem 用法：打包綠色免安裝版.bat [--no-pause] [--skip-tests]
rem ============================================================================

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
set "NO_PAUSE="
set "SKIP_TESTS="
:PARSE_ARGS
if "%~1"=="" goto :ARGS_DONE
if /i "%~1"=="--no-pause" set "NO_PAUSE=1"
if /i "%~1"=="--skip-tests" set "SKIP_TESTS=1"
shift
goto :PARSE_ARGS
:ARGS_DONE

if not exist "%ROOT%\runtime\python.exe" (
    echo [錯誤] 找不到 runtime\python.exe，請先執行 setup_portable_env.bat。
    goto :FAIL
)

rem 檢查工作區是否有未提交變更（git archive 只讀取 HEAD 提交）
git -C "%ROOT%" diff --quiet
if errorlevel 1 (
    echo [警告] 偵測到尚未提交的變更；git archive 將僅打包 HEAD 已提交的檔案。
)

rem 從 core/__init__.py 讀取版本號（不經過 for /f 括號解析，避免路徑或引號觸發 cmd 語法錯誤）
"%ROOT%\runtime\python.exe" -B -c "import pathlib, sys; sys.path.insert(0, r'%ROOT%'); import core; pathlib.Path(r'%TEMP%\wl_gis_ver.txt').write_text(core.__version__, 'ascii')"
if errorlevel 1 (
    echo [錯誤] 無法從 core\__init__.py 讀取 __version__。
    goto :FAIL
)
set /p APP_VERSION=<"%TEMP%\wl_gis_ver.txt"
del /f /q "%TEMP%\wl_gis_ver.txt" >nul 2>nul
if not defined APP_VERSION (
    echo [錯誤] 無法從 core\__init__.py 讀取 __version__。
    goto :FAIL
)

set "DIST_DIR=%ROOT%\dist"
set "STAGE_NAME=WingLightGIS_綠色免安裝版"
set "STAGE_DIR=%DIST_DIR%\%STAGE_NAME%"
set "ZIP_PATH=%DIST_DIR%\WingLightGIS-v%APP_VERSION%.zip"

echo.
echo ============================================================================
echo   WingLightGIS v%APP_VERSION% 綠色免安裝版打包流程
echo ============================================================================
echo.

if defined SKIP_TESTS (
    echo [1/6] 已指定 --skip-tests，略過單元測試。
) else (
    echo [1/6] 執行完整單元測試...
    "%ROOT%\runtime\python.exe" -m unittest discover -s "%ROOT%\tests" -p "test_*.py"
    if errorlevel 1 (
        echo [錯誤] 單元測試未通過，中止打包。
        goto :FAIL
    )
)

echo.
echo [2/6] 建立乾淨發佈目錄（依 .gitattributes export-ignore 排除測試與開發檔）...
if exist "%STAGE_DIR%" rd /s /q "%STAGE_DIR%"
if exist "%ZIP_PATH%" del /f /q "%ZIP_PATH%"
mkdir "%STAGE_DIR%"

pushd "%ROOT%"
git archive --format=tar -o "%DIST_DIR%\stage_src.tar" HEAD
if errorlevel 1 (
    popd
    echo [錯誤] git archive 匯出失敗。
    goto :FAIL
)
popd

"%ROOT%\runtime\python.exe" -B -c "import tarfile, pathlib; dst=pathlib.Path(r'%STAGE_DIR%'); t=tarfile.open(r'%DIST_DIR%\stage_src.tar'); t.extractall(dst, filter='data'); t.close()"
set "EXTRACT_RC=%ERRORLEVEL%"
del /f /q "%DIST_DIR%\stage_src.tar" >nul 2>nul
if not "%EXTRACT_RC%"=="0" (
    echo [錯誤] 解壓 git archive 失敗。
    goto :FAIL
)

if exist "%STAGE_DIR%\config\WingLightGIS.json" del /f /q "%STAGE_DIR%\config\WingLightGIS.json"
if exist "%STAGE_DIR%\workspace_temp" rd /s /q "%STAGE_DIR%\workspace_temp"
if exist "%STAGE_DIR%\outputs" rd /s /q "%STAGE_DIR%\outputs"

echo.
echo [3/6] 複製獨立沙盒 runtime 環境...
robocopy "%ROOT%\runtime" "%STAGE_DIR%\runtime" /E /NFL /NDL /NJH /NJS /NC /NS /XD __pycache__ include doc man bash-completion /XF *.pdb *.lib *.pyc
if errorlevel 8 (
    echo [錯誤] 複製 runtime 失敗。
    goto :FAIL
)

echo.
echo [4/6] 編譯原生啟動器 WingLightGIS.exe 並整理授權文件...
set "LAUNCHER_OUT=%STAGE_DIR%\WingLightGIS.exe"
call "%STAGE_DIR%\launcher\build_launcher.bat" --no-pause
if errorlevel 1 (
    echo [錯誤] WingLightGIS.exe 編譯失敗。
    goto :FAIL
)

"%ROOT%\runtime\python.exe" -B "%ROOT%\tools\collect_licenses.py" "%STAGE_DIR%"
if errorlevel 1 (
    echo [錯誤] 第三方授權收集失敗。
    goto :FAIL
)
if exist "%STAGE_DIR%\LICENSE" del /f /q "%STAGE_DIR%\LICENSE"

echo.
echo [5/6] 對組裝後成品執行自我測試 (--self-test)...
"%STAGE_DIR%\runtime\python.exe" -B "%STAGE_DIR%\main.py" --self-test "%DIST_DIR%\selftest_report.json"
if errorlevel 1 (
    echo [錯誤] 發佈資料夾自我測試失敗！請檢查 %DIST_DIR%\selftest_report.json。
    goto :FAIL
)

rem 清理任何意外產生的 __pycache__ 或暫存
for /d /r "%STAGE_DIR%" %%D in (__pycache__) do if exist "%%D" rd /s /q "%%D"
if exist "%STAGE_DIR%\workspace_temp" rd /s /q "%STAGE_DIR%\workspace_temp"

echo.
echo [6/6] 壓縮為綠色免安裝 ZIP 並統計檔案大小...
set "WL_STAGE_DIR=%STAGE_DIR%"
set "WL_ZIP_PATH=%ZIP_PATH%"
powershell -NoProfile -Command "Add-Type -AssemblyName System.IO.Compression.FileSystem; $src = $env:WL_STAGE_DIR; $dst = $env:WL_ZIP_PATH; [System.IO.Compression.ZipFile]::CreateFromDirectory($src, $dst, [System.IO.Compression.CompressionLevel]::Optimal, $true); $zipMB = (Get-Item $dst).Length / 1MB; $rawBytes = (Get-ChildItem $src -Recurse -File | Measure-Object -Property Length -Sum).Sum; $rawMB = $rawBytes / 1MB; Write-Host ('ZIP 壓縮包: {0} ({1:N1} MB)' -f $dst, $zipMB); Write-Host ('解壓縮後大小: {0:N1} MB ({1:N0} bytes)' -f $rawMB, $rawBytes)"
if errorlevel 1 (
    echo [錯誤] ZIP 壓縮失敗。
    goto :FAIL
)

echo.
echo ============================================================================
echo   【打包完成】
echo   發佈資料夾：%STAGE_DIR%
echo   ZIP 壓縮包：%ZIP_PATH%
echo ============================================================================
echo.
if not defined NO_PAUSE pause
endlocal
exit /b 0

:FAIL
echo.
if not defined NO_PAUSE pause
endlocal
exit /b 1
