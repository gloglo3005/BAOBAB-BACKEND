# Phase 0, action 0.3 : apps\circulation devient un vrai paquet Python
# Ajoute apps\circulation\__init__.py (fichier vide). Sans lui, "manage.py test" ne parcourt pas
# ce dossier : ses 43 tests (35 + 6 + 2) ne sont jamais découverts par un lancement normal.
# À lancer depuis le dossier biblio (celui qui contient manage.py) :
#     powershell -ExecutionPolicy Bypass -File .\installer_phase0_3.ps1
$ErrorActionPreference = "Stop"

if (-not (Test-Path "manage.py")) {
    throw "Lancez ce script depuis le dossier biblio (celui qui contient manage.py)."
}
if (-not (Test-Path "apps\circulation\models.py")) {
    throw "apps\circulation\models.py est introuvable : ce script ne correspond pas à votre projet."
}
$root = (Get-Location).Path
$backup = Join-Path $root "_sauvegarde_phase0_3"
$utf8 = New-Object System.Text.UTF8Encoding($false)

# Écrit le fichier ; si un fichier différent existe déjà, il est d'abord copié dans _sauvegarde_phase0_3.
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

Write-ProjectFile "apps\circulation\__init__.py" ""

Write-Host ""
Write-Host "Terminé. Vérification du nombre de tests découverts (sans base de données) :"
Write-Host '  python -c "import os,django,unittest;os.environ.setdefault(''DJANGO_SETTINGS_MODULE'',''config.settings'');django.setup();print(unittest.defaultTestLoader.discover(''.'',top_level_dir=''.'').countTestCases())"'
Write-Host "Résultat attendu : 893 (850 avant cette action)."
