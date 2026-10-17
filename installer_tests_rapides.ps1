# Phase 0, action 0.4 (préparation) : accélérer les tests
# Ajoute config\settings_test.py (réglages de test seulement : hachage des mots de passe rapide).
# Aucun fichier existant n'est modifié ; la production n'est pas concernée.
# À lancer depuis le dossier biblio (celui qui contient manage.py) :
#     powershell -ExecutionPolicy Bypass -File .\installer_tests_rapides.ps1
$ErrorActionPreference = "Stop"

if (-not (Test-Path "manage.py")) {
    throw "Lancez ce script depuis le dossier biblio (celui qui contient manage.py)."
}
if (-not (Test-Path "config\settings.py")) {
    throw "config\settings.py est introuvable : ce script ne correspond pas à votre projet."
}
$root = (Get-Location).Path
$backup = Join-Path $root "_sauvegarde_tests_rapides"
$utf8 = New-Object System.Text.UTF8Encoding($false)

# Écrit le fichier ; si un fichier différent existe déjà, il est d'abord copié dans _sauvegarde_tests_rapides.
function Write-ProjectFile([string]$relative, [string]$content) {
    $full = Join-Path $root $relative
    $dir = Split-Path $full -Parent
    New-Item -ItemType Directory -Force $dir | Out-Null
    $label = "créé      : "
    if (Test-Path $full) {
        $old = [System.IO.File]::ReadAllText($full)
        if ($old.Replace("`r`n", "`n") -eq $content.Replace("`r`n", "`n")) {
            Write-Host ("inchangé  : " + $relative)
            return
        }
        $saved = Join-Path $backup $relative
        New-Item -ItemType Directory -Force (Split-Path $saved -Parent) | Out-Null
        Copy-Item $full $saved -Force
        $label = "remplacé  : "
    }
    [System.IO.File]::WriteAllText($full, $content, $utf8)
    Write-Host ($label + $relative)
}

Write-ProjectFile "config\settings_test.py" @'
"""Réglages utilisés uniquement pour accélérer les tests (jamais en production).

Usage : python manage.py test --keepdb --settings=config.settings_test

Seul le hachage des mots de passe change : même algorithme (PBKDF2-SHA256), mais avec très peu
d'itérations. Chaque test crée plusieurs comptes ; avec les réglages normaux, chaque création
coûte environ une seconde. Tout le reste vient de config.settings.
"""
from django.contrib.auth.hashers import PBKDF2PasswordHasher

from .settings import *  # noqa: F401,F403


class FastPBKDF2PasswordHasher(PBKDF2PasswordHasher):
    iterations = 1000


PASSWORD_HASHERS = ["config.settings_test.FastPBKDF2PasswordHasher"]

'@

Write-Host ""
Write-Host "Terminé. Pour mesurer le gain sur les 43 tests de circulation :"
Write-Host "  Measure-Command { python manage.py test apps.circulation --keepdb --settings=config.settings_test } | Select-Object TotalSeconds"
