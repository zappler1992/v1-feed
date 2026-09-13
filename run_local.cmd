@echo off
rem v1-feed local runner: build -> commit/push -> ping WebSub. Run by Windows Task Scheduler every 5 minutes.
setlocal
cd /d "%~dp0"
set FEED_BASE_URL=https://zappler1992.github.io/v1-feed
set PYTHONIOENCODING=utf-8
set PY=C:\Users\Yuval\AppData\Local\Programs\Python\Python313\python.exe
if not exist logs mkdir logs
set LOG=logs\run.log
for %%A in ("%LOG%") do if %%~zA gtr 5000000 move /y "%LOG%" logs\run.old.log >nul

echo [%date% %time%] === run start >> "%LOG%"
git pull --rebase --quiet origin main >> "%LOG%" 2>&1
"%PY%" scripts\build_feed.py build >> "%LOG%" 2>&1
if errorlevel 1 (
  echo [%date% %time%] build FAILED >> "%LOG%"
  exit /b 1
)

git add docs state >> "%LOG%" 2>&1
git diff --cached --quiet -- docs/feed.xml docs/news-sitemap.xml docs/sitemap.xml
set FEED_CHANGED=%errorlevel%
git diff --cached --quiet
if errorlevel 1 (
  git -c user.name=v1-feed-bot -c user.email=v1-feed-bot@users.noreply.github.com commit -q -m "feed update (local)" >> "%LOG%" 2>&1
  git push -q origin main >> "%LOG%" 2>&1
  if errorlevel 1 (
    echo [%date% %time%] push FAILED >> "%LOG%"
    exit /b 1
  )
  echo [%date% %time%] pushed >> "%LOG%"
)
if "%FEED_CHANGED%"=="1" "%PY%" scripts\build_feed.py ping >> "%LOG%" 2>&1
echo [%date% %time%] === run end >> "%LOG%"
endlocal
