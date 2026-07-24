Option Explicit

Dim shell, fileSystem, scriptDirectory, runner, command
Set shell = CreateObject("WScript.Shell")
Set fileSystem = CreateObject("Scripting.FileSystemObject")

scriptDirectory = fileSystem.GetParentFolderName(WScript.ScriptFullName)
runner = fileSystem.BuildPath(scriptDirectory, "run_automation_queue.ps1")
command = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File """ & runner & """"

shell.Run command, 0, True
