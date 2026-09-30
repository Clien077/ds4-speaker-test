@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================================
echo  打包 DS4 手柄扬声器测试工具
echo    A) 文件夹版（推荐，运行时不解压，最稳）
echo    B) 单文件版（好搬运，但要从普通窗口启动）
echo    C) 单文件命令行版
echo ============================================================
echo.

where python >nul 2>nul || (echo [错误] 找不到 python，请先安装 Python 并加入 PATH & pause & exit /b 1)

echo [1/4] 检查依赖...
python -c "import numpy, sounddevice, pywinusb.hid, imageio_ffmpeg, tkinter" || (
  echo [错误] 缺少依赖，请先执行：
  echo     python -m pip install numpy sounddevice pywinusb imageio-ffmpeg
  pause & exit /b 1
)

echo [2/4] 打包 A：文件夹版（主推）...
python -m PyInstaller --noconfirm --clean --onedir --windowed ^
  --name "DS4扬声器测试" ^
  --collect-all pywinusb ^
  --hidden-import sounddevice ^
  --hidden-import _sounddevice_data ^
  ds4_speaker_test.py
if errorlevel 1 ( echo [错误] 打包失败 & pause & exit /b 1 )

echo [3/4] 打包 B/C：单文件版...
rem --runtime-tmpdir . 让单文件版解压到自己所在目录，而不是 %TEMP%
rem （避免低完整性/杀软环境下 "Could not create temporary directory!"）
python -m PyInstaller --noconfirm --onefile --windowed --runtime-tmpdir . ^
  --name "DS4扬声器测试(单文件)" ^
  --collect-all pywinusb ^
  --hidden-import sounddevice ^
  --hidden-import _sounddevice_data ^
  ds4_speaker_test.py
python -m PyInstaller --noconfirm --onefile --console --runtime-tmpdir . ^
  --name "DS4扬声器测试-CLI" ^
  --collect-all pywinusb ^
  --hidden-import sounddevice ^
  --hidden-import _sounddevice_data ^
  ds4_speaker_test.py

echo [4/4] 整理交付目录 release\ ...
if not exist release mkdir release
if exist "dist\DS4扬声器测试" xcopy /E /I /Y /Q "dist\DS4扬声器测试" "release\DS4扬声器测试" >nul
if exist "dist\DS4扬声器测试(单文件).exe" copy /Y "dist\DS4扬声器测试(单文件).exe" "release\" >nul
if exist "dist\DS4扬声器测试-CLI.exe" copy /Y "dist\DS4扬声器测试-CLI.exe" "release\" >nul
if exist "使用说明.txt" copy /Y "使用说明.txt" "release\" >nul

echo.
echo 完成！交付文件在： %~dp0release\
echo     release\DS4扬声器测试\DS4扬声器测试.exe      （推荐：双击即用）
echo     release\DS4扬声器测试(单文件).exe
echo     release\DS4扬声器测试-CLI.exe
echo.
echo 提示：
echo   1) 文件夹版运行时不解压任何文件，最稳、启动也最快；
echo   2) 单文件版若被杀软拦截或报 "Could not create temporary directory!"，
echo      请改用文件夹版，或在普通资源管理器窗口里双击（别从聊天窗口的文件卡片打开）；
echo   3) 需要在命令行里重定向输出/写脚本，用 -CLI 版。
pause
