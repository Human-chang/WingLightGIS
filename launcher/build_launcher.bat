@echo off
setlocal

rem ============================================================================
rem build_launcher.bat
rem 產生 WingLightGIS.exe 啟動器 [原始碼 launcher\WingLightGIS.cs]
rem 用 Windows 內建的 .NET Framework 4 編譯器 csc.exe，不必另外安裝
rem 用法：build_launcher.bat [--no-pause]
rem 輸出到專案資料夾的 WingLightGIS.exe [測試時可用環境變數 LAUNCHER_OUT 指定別的位置]
rem ============================================================================

set "LAUNCHER_DIR=%~dp0"
for %%I in ("%~dp0..") do set "TOOLKIT_ROOT=%%~fI"
if not defined LAUNCHER_OUT set "LAUNCHER_OUT=%TOOLKIT_ROOT%\WingLightGIS.exe"
set "NO_PAUSE="
if /i "%~1"=="--no-pause" set "NO_PAUSE=1"

set "CSC=%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if not exist "%CSC%" set "CSC=%WINDIR%\Microsoft.NET\Framework\v4.0.30319\csc.exe"
if not exist "%CSC%" goto :NO_CSC

"%CSC%" /nologo /target:winexe /optimize+ /codepage:65001 /win32icon:"%TOOLKIT_ROOT%\assets\app_icon.ico" /win32manifest:"%LAUNCHER_DIR%WingLightGIS.manifest" /reference:System.Windows.Forms.dll /out:"%LAUNCHER_OUT%" "%LAUNCHER_DIR%WingLightGIS.cs"
if errorlevel 1 goto :BUILD_FAILED

echo 已產生 %LAUNCHER_OUT%
call :END_PAUSE
endlocal
exit /b 0

:NO_CSC
echo [錯誤] 找不到 .NET Framework 4 的編譯器 csc.exe，沒辦法產生 WingLightGIS.exe。
echo        Windows 10 與 11 都有內建 .NET Framework 4.8，可能是被關掉了，請在 Windows 設定裡把它開啟後再試一次。
call :END_PAUSE
endlocal
exit /b 1

:BUILD_FAILED
echo [錯誤] WingLightGIS.exe 編譯失敗，請看上面的訊息。
echo        如果 WingLightGIS.exe 正在執行，請先關掉再試一次。
call :END_PAUSE
endlocal
exit /b 1

:END_PAUSE
if not defined NO_PAUSE pause
exit /b 0
