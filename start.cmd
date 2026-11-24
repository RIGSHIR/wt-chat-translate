@echo off
rem 无黑窗启动：优先 pythonw，其次 pyw，最后回落到 python
cd /d "%~dp0"
where pythonw >nul 2>nul && (start "" pythonw "%~dp0main.py" & exit /b 0)
where pyw     >nul 2>nul && (start "" pyw     "%~dp0main.py" & exit /b 0)
echo 未找到 pythonw / pyw，改用 python（会保留黑窗）
python "%~dp0main.py"