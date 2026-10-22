@echo off
chcp 65001 >nul
cd /d "%~dp0"
title DCA Bot - tableau de bord
where python >nul 2>nul
if %errorlevel%==0 (python app.py) else (py app.py)
if errorlevel 1 pause
