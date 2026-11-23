Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "C:\Users\monte\OneDrive\Desktop\jarvis"
WshShell.Run """C:\Users\monte\AppData\Local\Programs\Python\Python312\pythonw.exe"" ""C:\Users\monte\OneDrive\Desktop\jarvis\server.py"" --saludo", 0, False
