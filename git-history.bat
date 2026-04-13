@echo off
setlocal
set PYTHONPATH=%~dp0;%PYTHONPATH%
python -m git_history %*
