' Launches run_local.cmd with no visible window (used by Task Scheduler)
Set sh = CreateObject("WScript.Shell")
dir = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\"))
sh.Run "cmd.exe /c """ & dir & "run_local.cmd""", 0, True
