@echo off
setlocal

rem ============================================================================
rem setup_portable_env.bat
rem WingLightGIS - 便攜式獨立環境自動建置程式
rem 目標：在 ./runtime 建立獨立 64-bit Python 3.11 + GDAL C++ CLI + WhiteboxTools 沙盒
rem 鐵律：絕不修改全域環境變數，完全隔離本機舊版 ArcMap 10.x [Python 2.7]
rem ============================================================================

title WingLightGIS - 環境自動建置程式

echo.
echo ============================================================================
echo           WingLightGIS - 環境建置
echo ============================================================================
echo.

set "TOOLKIT_ROOT=%~dp0"
set "RUNTIME_DIR=%TOOLKIT_ROOT%runtime"
set "BIN_DIR=%TOOLKIT_ROOT%bin"
set "TEMP_DIR=%TOOLKIT_ROOT%workspace_temp"
set "MICROMAMBA_EXE=%TOOLKIT_ROOT%micromamba.exe"

rem 建立專案基礎資料夾
if not exist "%TOOLKIT_ROOT%config" mkdir "%TOOLKIT_ROOT%config"
if not exist "%TEMP_DIR%" mkdir "%TEMP_DIR%"
if not exist "%BIN_DIR%" mkdir "%BIN_DIR%"

rem 檢查 runtime 是否已經建立且完整
if not exist "%RUNTIME_DIR%\python.exe" goto :CHECK_MAMBA
if not exist "%RUNTIME_DIR%\Library\bin\ogr2ogr.exe" goto :RUNTIME_INCOMPLETE
rem 介面套件缺少時只補裝 [上次 pip 步驟失敗的情況]
"%RUNTIME_DIR%\python.exe" -I -c "import customtkinter, tkinterdnd2" >nul 2>nul
if not "%ERRORLEVEL%"=="0" goto :UI_PACKAGES_MISSING

call :ENSURE_LAUNCHER
echo [狀態] 獨立執行環境 [runtime] 已完整存在於:
echo        %RUNTIME_DIR%
echo.
echo 若需要重新安裝，請手動刪除或重新命名 runtime 資料夾後再次執行本腳本。
echo.
pause
endlocal
exit /b 0

:UI_PACKAGES_MISSING
echo [警告] runtime 缺少介面套件 [customtkinter, tkinterdnd2]，直接補裝。
goto :INSTALL_UI_PACKAGES

:RUNTIME_INCOMPLETE
echo [警告] 偵測到未完成的 runtime 資料夾，準備重新建置。

rem ============================================================================
rem 步驟 1/5：檢查與取得 micromamba 管理核心
rem ============================================================================
:CHECK_MAMBA
echo [步驟 1/5] 檢查與取得 micromamba 包管理核心 。

if exist "%MICROMAMBA_EXE%" goto :MAMBA_READY
if exist "%BIN_DIR%\micromamba.exe" (
    set "MICROMAMBA_EXE=%BIN_DIR%\micromamba.exe"
    goto :MAMBA_READY
)

where micromamba.exe >nul 2>nul
if errorlevel 1 goto :DOWNLOAD_MAMBA

for /f "delims=" %%I in ('where micromamba.exe') do (
    set "MICROMAMBA_EXE=%%I"
    goto :MAMBA_READY
)

:DOWNLOAD_MAMBA
echo [下載] 本地未找到 micromamba.exe，開始自動下載微型包管理器 。

set "MAMBA_URL_PRIMARY=https://github.com/mamba-org/micromamba-releases/releases/latest/download/micromamba-win-64.tar.bz2"
set "MAMBA_URL_FALLBACK=https://micro.mamba.pm/api/micromamba/win-64/latest"
set "MAMBA_TAR=%TEMP_DIR%\micromamba.tar.bz2"

rem 優先使用 curl 下載
where curl.exe >nul 2>nul
if errorlevel 1 goto :DOWNLOAD_VIA_POWERSHELL

echo 正在透過 curl 下載 micromamba 壓縮包 。
curl.exe -f -L --connect-timeout 8 --progress-bar --retry 2 "%MAMBA_URL_PRIMARY%" -o "%MAMBA_TAR%"
if exist "%MAMBA_TAR%" goto :EXTRACT_MAMBA

echo 主要載點連線異常，切換至備用載點 。
curl.exe -f -L --connect-timeout 8 --progress-bar --retry 2 "%MAMBA_URL_FALLBACK%" -o "%MAMBA_TAR%"
if exist "%MAMBA_TAR%" goto :EXTRACT_MAMBA

:DOWNLOAD_VIA_POWERSHELL
echo 正在透過 PowerShell 下載 micromamba 壓縮包 。
powershell -NoProfile -Command "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; (New-Object System.Net.WebClient).DownloadFile('%MAMBA_URL_PRIMARY%', '%MAMBA_TAR%')"
if exist "%MAMBA_TAR%" goto :EXTRACT_MAMBA

powershell -NoProfile -Command "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; (New-Object System.Net.WebClient).DownloadFile('%MAMBA_URL_FALLBACK%', '%MAMBA_TAR%')"

:EXTRACT_MAMBA
if not exist "%MAMBA_TAR%" goto :MAMBA_DOWNLOAD_FAILED

echo 正在解壓縮 micromamba.exe 。
tar.exe -xf "%MAMBA_TAR%" -C "%TOOLKIT_ROOT%" Library/bin/micromamba.exe 2>nul
if exist "%TOOLKIT_ROOT%Library\bin\micromamba.exe" (
    move "%TOOLKIT_ROOT%Library\bin\micromamba.exe" "%MICROMAMBA_EXE%" >nul
    rd /s /q "%TOOLKIT_ROOT%Library" 2>nul
    goto :EXTRACT_CLEANUP
)

tar.exe -xf "%MAMBA_TAR%" -C "%TOOLKIT_ROOT%" bin/micromamba.exe 2>nul
if exist "%TOOLKIT_ROOT%bin\micromamba.exe" (
    move "%TOOLKIT_ROOT%bin\micromamba.exe" "%MICROMAMBA_EXE%" >nul
)

:EXTRACT_CLEANUP
if exist "%MAMBA_TAR%" del /f /q "%MAMBA_TAR%" 2>nul

:MAMBA_READY
if not exist "%MICROMAMBA_EXE%" goto :MAMBA_DOWNLOAD_FAILED

echo [狀態] micromamba 核心定位完成: "%MICROMAMBA_EXE%"
echo.

rem ============================================================================
rem 步驟 2/5：使用 micromamba 建立獨立 ./runtime 環境
rem ============================================================================
echo [步驟 2/5] 使用 micromamba 建立獨立 ./runtime 環境 。
echo 正在自 conda-forge 下載並安裝獨立執行環境 [Python 3.11, GDAL CLI, WhiteboxTools, NumPy, OpenPyXL, Folium, Pillow]
echo 此過程約需 2~4 分鐘 [視網路頻寬與電腦效能而定]，請耐心等候。
echo.

"%MICROMAMBA_EXE%" create -p "%RUNTIME_DIR%" -c conda-forge --yes ^
    python=3.11 ^
    gdal=3.13 ^
    whitebox_tools=2.4 ^
    numpy=2.4 ^
    openpyxl=3.1 ^
    folium=0.20 ^
    pillow=13.0 ^
    "libblas=*=*openblas" ^
    pip

if not "%ERRORLEVEL%"=="0" goto :ENV_CREATE_FAILED

echo.
rem ============================================================================
rem 步驟 3/5：安裝額外 UI 視覺與相容性套件
rem ============================================================================
:INSTALL_UI_PACKAGES
echo [步驟 3/5] 安裝額外 UI 視覺與相容性套件 [customtkinter, tkinterdnd2] 。
"%RUNTIME_DIR%\python.exe" -m pip install customtkinter==6.0.0 tkinterdnd2==0.7.0 darkdetect==0.8.0 --no-warn-script-location
if not "%ERRORLEVEL%"=="0" goto :PIP_INSTALL_FAILED

echo.
rem ============================================================================
rem 步驟 4/5：清理執行期用不到的開發檔案 [靜態庫、除錯符號、說明文件、標頭檔]
rem 保留 Library\share\proj、Library\share\gdal 與 Library\bin 的 DLL
rem ============================================================================
echo [步驟 4/5] 清理執行期用不到的開發檔案 [靜態庫、除錯符號、說明文件、標頭檔] 。
for /r "%RUNTIME_DIR%\Library\lib" %%F in (*.lib) do if /i "%%~xF"==".lib" del /f /q "%%F"
for /r "%RUNTIME_DIR%" %%F in (*.pdb) do if /i "%%~xF"==".pdb" del /f /q "%%F"
if exist "%RUNTIME_DIR%\Library\share\doc" rd /s /q "%RUNTIME_DIR%\Library\share\doc"
if exist "%RUNTIME_DIR%\Library\share\man" rd /s /q "%RUNTIME_DIR%\Library\share\man"
if exist "%RUNTIME_DIR%\Library\share\bash-completion" rd /s /q "%RUNTIME_DIR%\Library\share\bash-completion"
if exist "%RUNTIME_DIR%\Library\include" rd /s /q "%RUNTIME_DIR%\Library\include"
if exist "%RUNTIME_DIR%\include" rd /s /q "%RUNTIME_DIR%\include"

echo.
rem ============================================================================
rem 步驟 5/5：驗證沙盒環境隔離度與工具鏈
rem ============================================================================
echo [步驟 5/5] 驗證沙盒環境隔離度與 GDAL/OGR C++ 原生指令相容性 。

if not exist "%RUNTIME_DIR%\python.exe" goto :VERIFY_FAILED_PYTHON
if not exist "%RUNTIME_DIR%\Library\bin\ogr2ogr.exe" goto :VERIFY_FAILED_GDAL
"%RUNTIME_DIR%\python.exe" -I -c "import customtkinter, tkinterdnd2" >nul 2>nul
if not "%ERRORLEVEL%"=="0" goto :PIP_INSTALL_FAILED

echo ----------------------------------------------------------------------------
echo 正在執行沙盒環境版本驗證測試:
"%RUNTIME_DIR%\python.exe" --version
"%RUNTIME_DIR%\Library\bin\ogr2ogr.exe" --version
"%RUNTIME_DIR%\Library\bin\gdalwarp.exe" --version
echo ----------------------------------------------------------------------------

call :ENSURE_LAUNCHER

echo.
echo ============================================================================
echo   【建置完成】WingLightGIS 獨立便攜環境已完全配置完成。
echo   日後直接點兩下 WingLightGIS.exe 開啟。
echo   [完全無須開啟 QGIS 或 ArcMap，亦絕不更動 Windows 系統環境變數]
echo ============================================================================
echo.
pause
endlocal
exit /b 0

rem ----------------------------------------------------------------------------
rem 沒有 WingLightGIS.exe 時用 launcher\build_launcher.bat 產生 [已存在就不重編，避免覆寫正在執行的啟動器]
rem ----------------------------------------------------------------------------
:ENSURE_LAUNCHER
if exist "%TOOLKIT_ROOT%WingLightGIS.exe" exit /b 0
echo [啟動器] 正在產生 WingLightGIS.exe 。
call "%TOOLKIT_ROOT%launcher\build_launcher.bat" --no-pause
exit /b 0

rem ----------------------------------------------------------------------------
rem 錯誤處理常式
rem ----------------------------------------------------------------------------
:MAMBA_DOWNLOAD_FAILED
echo.
echo ============================================================================
echo 【錯誤】無法自動下載 micromamba.exe。
echo 請依循以下手動離線步驟進行：
echo 1. 至瀏覽器下載 micromamba Windows 64-bit 版本：
echo    https://github.com/mamba-org/micromamba-releases/releases
echo 2. 將解壓縮出的 micromamba.exe 放置於專案根目錄：
echo    %TOOLKIT_ROOT%micromamba.exe
echo 3. 重新執行本 setup_portable_env.bat
echo ============================================================================
echo.
pause
endlocal
exit /b 1

:ENV_CREATE_FAILED
echo.
echo ============================================================================
echo 【錯誤】micromamba 環境建立失敗。
echo 請檢查網路連線或防火牆設定後重試。
echo ============================================================================
echo.
pause
endlocal
exit /b 1

:PIP_INSTALL_FAILED
echo.
echo ============================================================================
echo 【錯誤】customtkinter 或 tkinterdnd2 安裝失敗。
echo 請檢查網路連線或防火牆設定後再重新執行本腳本，將自動補裝。
echo ============================================================================
echo.
pause
endlocal
exit /b 1

:VERIFY_FAILED_PYTHON
echo 【錯誤】找不到 %RUNTIME_DIR%\python.exe。
pause
endlocal
exit /b 1

:VERIFY_FAILED_GDAL
echo 【錯誤】找不到 %RUNTIME_DIR%\Library\bin\ogr2ogr.exe。
pause
endlocal
exit /b 1
