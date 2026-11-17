@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1

where python >nul 2>nul
if errorlevel 1 (
    echo.
    echo 未找到 Python。请先安装 Python 3.10 或更高版本，并勾选 Add Python to PATH。
    echo.
    pause
    exit /b 1
)

echo 正在扫描本文件夹下的小说，请稍候...
echo.
python novel_merger.py --root "%~dp0" --output "合并结果"
set "CODE=%ERRORLEVEL%"

echo.
if not "%CODE%"=="0" (
    echo 程序结束，返回码：%CODE%
    echo 如果提示输出文件夹已存在，请先改名/移走“合并结果”，
    echo 或在命令行运行：python novel_merger.py --overwrite
) else (
    echo 处理完成，结果在“合并结果”文件夹中。
)
echo.
pause
exit /b %CODE%
