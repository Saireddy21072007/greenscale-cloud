@echo off
REM One-command start for Windows. Creates a virtualenv on first run.
setlocal

if not exist .venv (
    echo Creating virtual environment...
    python -m venv .venv
    call .venv\Scripts\activate.bat
    python -m pip install --upgrade pip
    pip install -r requirements.txt
    echo Training the runtime predictor...
    python scripts\train_model.py
) else (
    call .venv\Scripts\activate.bat
)

echo.
echo GreenScale Cloud 2.0  ->  http://localhost:5000
echo.
python run.py

endlocal
