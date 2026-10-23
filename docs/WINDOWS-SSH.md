# SSH from Windows

Ateable uses OpenSSH to send app source, settings and control messages to a Linux worker. Downloads and output files stay on that worker. SSH host-key verification stays enabled, and pairing keys travel through stdin rather than command-line arguments.

On Windows, Ateable automatically prefers the OpenSSH client included with Git for Windows when it finds a normal system or per-user installation. Otherwise it uses `ssh` from PATH. It never installs a client or edits PATH or your SSH configuration.

This choice addresses a real acceptance-test failure: Windows built-in OpenSSH 9.5p2 stalled with redirected stdin in a background process. Git for Windows OpenSSH 10.5p1 completed the same strict, noninteractive stdin test. This records the tested combinations; other client versions still need validation.

Before automatic worker installation, Ateable performs a Windows-only input check using the worker's POSIX shell. It sends a harmless marker and requires the exact reply within 12 seconds. A stalled or incorrect reply stops setup before package installation or source upload and explains how to choose a working client.

To select a different client, set `ATEABLE_SSH_EXECUTABLE` to the absolute path of an existing SSH executable, then restart Ateable. For example, in PowerShell before launching Ateable from that session:

```powershell
$env:ATEABLE_SSH_EXECUTABLE = 'C:\Program Files\Git\usr\bin\ssh.exe'
```

If pairing appears to hang before package installation, check that the VM is reachable and package downloads are available. On affected Windows clients, install [Git for Windows from its official distribution](https://gitforwindows.org/) or choose a working SSH client with the variable above. The app stays in the background; no SSH terminal window is opened.
