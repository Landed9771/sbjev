' Keeps the sbjev server running in the background with no console window.
' install.ps1 runs it at login (Startup folder) and registers it for sbjev:// links,
' so the userscript's "Start server" button can launch it. Double-clicking works too.
' If the server crashes it is restarted after 5 s; if the port is taken (a server you
' started by hand), it checks again every minute. Output is appended to server.log.
Option Explicit
Dim sh, fso, dir, me_, wmi, procs, p, others, code, logf, py, f
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
me_ = LCase(WScript.ScriptFullName)

' only one supervisor at a time (a second launch, e.g. from a sbjev:// link, just exits)
Set wmi = GetObject("winmgmts:\\.\root\cimv2")
Set procs = wmi.ExecQuery("SELECT CommandLine FROM Win32_Process WHERE Name='wscript.exe'")
others = 0
For Each p In procs
    If Not IsNull(p.CommandLine) Then
        If InStr(LCase(p.CommandLine), me_) > 0 Then others = others + 1
    End If
Next
If others > 1 Then WScript.Quit 0

' the Python the installer set up (python.txt), else whatever "python" is on PATH
py = "python"
If fso.FileExists(dir & "\python.txt") Then
    Set f = fso.OpenTextFile(dir & "\python.txt", 1)
    If Not f.AtEndOfStream Then py = Trim(f.ReadLine)
    f.Close
    If py = "" Then py = "python"
End If

sh.CurrentDirectory = dir
logf = dir & "\server.log"
Do
    ' keep the log from growing forever
    If fso.FileExists(logf) Then
        If fso.GetFile(logf).Size > 5000000 Then fso.DeleteFile logf
    End If
    code = sh.Run("cmd /c echo [%date% %time%] starting>> server.log & """ & py & """ sbjev.py --serve >> server.log 2>&1", 0, True)
    If code = 3 Then
        WScript.Sleep 60000   ' port already in use: someone else is serving
    Else
        WScript.Sleep 5000    ' crashed or was killed: restart shortly
    End If
Loop
