Option Explicit
Dim shell, fso, root, python, app
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(WScript.ScriptFullName)
python = fso.BuildPath(root, ".venv\Scripts\pythonw.exe")
app = fso.BuildPath(root, "app.py")
If Not fso.FileExists(python) Then
    python = "D:\dev\TrancyDesktop\.venv\Scripts\pythonw.exe"
End If
If Not fso.FileExists(python) Then
    python = "D:\ProjectsPycharm\MultiProjects\.venv\Scripts\pythonw.exe"
End If
If Not fso.FileExists(python) Then
    MsgBox "Python environment not found. See desktop/README.md.", 16, "Trancy Desktop"
    WScript.Quit 1
End If
shell.CurrentDirectory = root
shell.Run Chr(34) & python & Chr(34) & " -B " & Chr(34) & app & Chr(34), 0, False
