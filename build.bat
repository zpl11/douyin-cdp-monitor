@echo off
REM 构建 CDP Monitor 的 Windows 可执行文件
echo [1/3] 安装依赖...
pip install -r requirements.txt

echo [2/3] 打包为 exe...
pyinstaller --onefile --windowed --name="CDP-Monitor" ^
    --add-data "config.json;." ^
    main.py

echo [3/3] 完成！
echo 可执行文件位于 dist\CDP-Monitor.exe
pause
