@echo off
cd /d C:\claw-code\projects\options_trading_ai
start /min "" C:\claw-code\.venv\Scripts\python.exe market_monitor.py --loop --interval-seconds 900
