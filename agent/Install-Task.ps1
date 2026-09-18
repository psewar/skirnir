# ABGELOEST 2026-09-05: der Heartbeat laeuft im Windows-Dienst OllamaRouterAgent (../agent-go). Nur Rueckfallweg;
# vorher den Dienst stoppen, sonst melden zwei Agenten denselben Knoten.
# Registriert den Router-Agent als Scheduled Task des aktuellen Benutzers (AtLogOn, kein Admin nötig)
# und startet ihn sofort. Erneuter Lauf ersetzt den Task (Neustart, damit Konfig-Änderungen greifen).
$ErrorActionPreference = 'Stop'
$name = 'OllamaRouterAgent'
$script = Join-Path $PSScriptRoot 'ollama-router-agent.ps1'
# Ueber conhost --headless starten: auf Windows 11 ist Windows Terminal der Standard-Konsolenhost, dann bekommt auch ein
# "-WindowStyle Hidden"-Task ein Terminal-Fenster. Wer das Fenster schliesst, beendet den Agenten (Exit 0xC000013A, so
# am 2026-09-05 dreimal auf gpu-desktop passiert: Agent, MQTT-Gesundheit, STT). --headless erzwingt den alten
# fensterlosen conhost.
$action = New-ScheduledTaskAction -Execute "$env:SystemRoot\System32\conhost.exe" -Argument "--headless powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$script`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
# Watchdog: alle 5 min erneut starten. Laeuft der Agent noch, ignoriert der Scheduler den Start (IgnoreNew);
# ist er gestorben (2026-09-05: Prozess endete ohne Log mit 0xC000013A, Ursache unbekannt), kommt er so von selbst zurueck.
$watchdog = New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Minutes 5)
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$existing = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
if ($existing) { Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue; Unregister-ScheduledTask -TaskName $name -Confirm:$false }
# Alte Agent-Instanz hart beenden: Stop-ScheduledTask laesst den Prozess u. U. weiterlaufen, der dann mit der ALTEN
# Konfiguration im Speicher den Mutex haelt und die neue Instanz mit "already running" aussperrt.
# Eigene PID ausschliessen, sonst trifft der Filter dieses Skript selbst.
Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
  Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -like '*ollama-router-agent.ps1*' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 1
Register-ScheduledTask -TaskName $name -Action $action -Trigger @($trigger, $watchdog) -Settings $settings -RunLevel Limited | Out-Null
Start-ScheduledTask -TaskName $name
Start-Sleep -Seconds 4
$info = Get-ScheduledTaskInfo -TaskName $name
"Task $name registriert. State=$((Get-ScheduledTask -TaskName $name).State) LastResult=$($info.LastTaskResult) (267009 = läuft)"
Get-Content (Join-Path $PSScriptRoot 'agent.log') -Tail 3
