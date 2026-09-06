' run-hidden.vbs -- launch a console program with NO window at all.
'
' Task Scheduler flashes a console for every console-subsystem action
' (powershell.exe included) before -WindowStyle Hidden can take effect,
' so a 5-minute watchdog tick blinks a cmd window at the operator all
' day. wscript.exe is GUI-subsystem: a command launched through this
' shim never creates a console. Waits for the child and propagates its
' exit code, so sequential task actions and LastTaskResult behave
' exactly as before.
'
' Usage (as a scheduled-task action):
'   Execute:  wscript.exe
'   Argument: "<path>\run-hidden.vbs" <exe> [args...]

Option Explicit
Dim shell, cmd, i, arg
If WScript.Arguments.Count < 1 Then WScript.Quit 87 ' ERROR_INVALID_PARAMETER
cmd = ""
For i = 0 To WScript.Arguments.Count - 1
    arg = WScript.Arguments(i)
    If InStr(arg, " ") > 0 Then arg = """" & arg & """"
    If i > 0 Then cmd = cmd & " "
    cmd = cmd & arg
Next
Set shell = CreateObject("WScript.Shell")
WScript.Quit shell.Run(cmd, 0, True)
