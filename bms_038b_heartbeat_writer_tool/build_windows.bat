@echo off
setlocal

python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install pyinstaller

pyinstaller --noconfirm --onefile --windowed --name BMS_038B_Heartbeat_Writer bms_038b_heartbeat_writer.py

echo.
echo Build finished.
echo EXE path: dist\BMS_038B_Heartbeat_Writer.exe
pause
