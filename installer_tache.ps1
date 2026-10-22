﻿# Crée une tâche planifiée Windows qui lance le bot chaque jour à 10h.
# Si la machine est éteinte à 10h, la tâche se lance dès qu'elle redémarre.
# Le plus simple : utilise le bouton "Activer" dans le tableau de bord.
# Sinon (PowerShell) :  powershell -ExecutionPolicy Bypass -File .\installer_tache.ps1

param([string]$Python = "")

$ErrorActionPreference = "Stop"
$dossier = Split-Path -Parent $MyInvocation.MyCommand.Path

if (-not $Python) {
    $p = (Get-Command pythonw -ErrorAction SilentlyContinue).Source
    if (-not $p) { $p = (Get-Command python -ErrorAction SilentlyContinue).Source }
    if (-not $p) { $p = (Get-Command py -ErrorAction SilentlyContinue).Source }
    $Python = $p
}
if (-not $Python) { throw "Python introuvable. Installe-le depuis python.org (coche 'Add to PATH')." }

$action   = New-ScheduledTaskAction -Execute $Python -Argument "`"$dossier\bot.py`" run" -WorkingDirectory $dossier
$trigger  = New-ScheduledTaskTrigger -Daily -At 10:00
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -ExecutionTimeLimit (New-TimeSpan -Minutes 15) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 10)

Register-ScheduledTask -TaskName "Binance DCA Bot" -Action $action -Trigger $trigger -Settings $settings `
    -Description "Achat mensuel BTC/ETH et rééquilibrage" -Force | Out-Null

Write-Host "Tâche 'Binance DCA Bot' créée : exécution chaque jour à 10h00."
Write-Host "Pour la supprimer : Unregister-ScheduledTask -TaskName 'Binance DCA Bot'"
