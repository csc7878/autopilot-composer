@echo off
REM ============================================================
REM  AutoPilot Composer —— 一键推送到 GitHub（本地运行版）
REM  适用：WorkBuddy GitHub 连接器无写入权限（403）时的兜底方案。
REM  使用方法：
REM    1. 在 github.com 新建空仓库 autopilot-composer（public，不要勾 README）
REM    2. 生成 PAT：GitHub -> 右上角头像 -> Settings
REM       -> Developer settings -> Personal access tokens
REM       -> Tokens (classic) -> Generate new token (classic)
REM       -> 勾选 repo 权限（整组）-> Generate，复制 ghp_xxx
REM    3. 在本文件夹（_apc_repo）空白处按住 Shift 右键 -> 在此处打开命令窗口
REM       直接双击本文件也行，然后按提示粘贴 PAT
REM  说明：Token 仅用于本次推送，推送完成后会自动从远程地址中清除。
REM ============================================================
setlocal
set REPO=autopilot-composer
set OWNER=csc7878

set /p GH_PAT=请输入 GitHub Personal Access Token (ghp_xxx): 
if "%GH_PAT%"=="" (
  echo [错误] 未输入 Token，已取消。
  pause
  exit /b 1
)

echo [1/5] 初始化 git 仓库...
git init -q
git config user.name "csc7878"
git config user.email "woshicsc001@126.com"

echo [2/5] 添加文件并提交...
git add -A
git add -f scripts/breakpoint.json
git commit -q -m "Initial commit: AutoPilot Composer 3.1.0" || echo (无新提交或已提交)

echo [3/5] 设置远程仓库（临时带 Token）...
git branch -M main
git remote remove origin >nul 2>&1
git remote add origin https://%GH_PAT%@github.com/%OWNER%/%REPO%.git

echo [4/5] 推送到 main 分支...
git push -u origin main

if %errorlevel%==0 (
  echo.
  echo [5/5] 清理远程地址中的 Token...
  git remote set-url origin https://github.com/%OWNER%/%REPO%.git
  echo [完成] 已推送到 https://github.com/%OWNER%/%REPO%
) else (
  echo.
  echo [失败] 请检查：Token 是否带 repo 权限、网络是否可访问 github.com。
  git remote set-url origin https://github.com/%OWNER%/%REPO%.git >nul 2>&1
)
pause
