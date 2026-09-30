@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 不带参数 -> 打开图形界面；带参数 -> 命令行模式（例如 run.bat --list / run.bat --file 音乐.mp3）
python ds4_speaker_test.py %*
if errorlevel 1 pause
