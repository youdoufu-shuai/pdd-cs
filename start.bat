@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"
title 拼多多智能客服 - 一键启动

echo ============================================================
echo    拼多多多店铺智能客服 - 一键启动
echo ============================================================
echo.

rem ---------- 1. 找 Python ----------
set "PY_CMD="
where py >nul 2>nul && set "PY_CMD=py -3"
if not defined PY_CMD (
  where python >nul 2>nul && set "PY_CMD=python"
)
if not defined PY_CMD (
  echo [错误] 没有找到 Python。
  echo 请先安装 Python 3.10 或更高版本：https://www.python.org/downloads/
  echo 安装时记得勾选 "Add Python to PATH"，然后重新双击本文件。
  pause
  exit /b 1
)
%PY_CMD% -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul
if errorlevel 1 (
  echo [错误] Python 版本过低，需要 3.10 以上。
  %PY_CMD% --version
  pause
  exit /b 1
)
echo [OK] Python：%PY_CMD%
%PY_CMD% --version

if /I "%~1"=="--check" (
  echo [OK] start.bat 自检通过
  exit /b 0
)

rem ---------- 2. 虚拟环境 + 依赖 ----------
if not exist ".venv\Scripts\python.exe" (
  echo.
  echo [1/3] 首次运行：创建虚拟环境（约 10 秒）...
  %PY_CMD% -m venv .venv
  if errorlevel 1 (
    echo [错误] 创建虚拟环境失败，请检查磁盘空间或 Python 安装。
    pause
    exit /b 1
  )
)
set "VPY=%CD%\.venv\Scripts\python.exe"

echo [2/3] 检查依赖（首次需要下载，请稍等）...
"%VPY%" -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 (
  echo [提示] 直连下载失败，改用国内镜像重试...
  "%VPY%" -m pip install --disable-pip-version-check -q -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
  if errorlevel 1 (
    echo [错误] 依赖安装失败，请检查网络后重试。
    pause
    exit /b 1
  )
)

rem ---------- 3. 配置 ----------
if not exist "config\config.yaml" (
  echo [3/3] 生成默认配置...
  "%VPY%" -m pdd_cs setup --yes
)
if not exist "config\secrets" mkdir "config\secrets"

rem ---------- 启动 ----------
set "PORT=38617"
set "PORT_FILE=%TEMP%\pdd_cs_port.txt"
"%VPY%" -c "from pdd_cs.config import Config; open(r'%PORT_FILE%','w').write(str(Config.load().server.port))" >nul 2>nul
if exist "%PORT_FILE%" (
  set /p PORT=<"%PORT_FILE%"
  del "%PORT_FILE%" >nul 2>nul
)

echo.
echo 面板地址：http://127.0.0.1:!PORT!
echo 启动后会自动打开浏览器；如果没打开，请手动复制上面的地址。
echo.
echo 按 Ctrl+C 可以停止程序。
echo.
"%VPY%" -m pdd_cs run --open

echo.
echo 程序已退出。如果启动失败，请查看上面的错误信息，或运行：
echo   .venv\Scripts\python.exe -m pdd_cs doctor
pause
