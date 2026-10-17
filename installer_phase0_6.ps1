# Phase 0, action 0.6 : fins de ligne identiques partout (LF)
# Ajoute .editorconfig et .gitattributes dans biblio, baobab-administration et baobab-user.
# Aucun fichier existant n'est modifié ni converti.
# À lancer depuis le dossier biblio (celui qui contient manage.py) :
#     powershell -ExecutionPolicy Bypass -File .\installer_phase0_6.ps1
$ErrorActionPreference = "Stop"

if (-not (Test-Path "manage.py")) {
    throw "Lancez ce script depuis le dossier biblio (celui qui contient manage.py)."
}
$biblio = (Get-Location).Path
$projet = Split-Path $biblio -Parent
$backup = Join-Path $biblio "_sauvegarde_phase0_6"
$utf8 = New-Object System.Text.UTF8Encoding($false)

$dossiers = @(
    @{ nom = "biblio"; chemin = $biblio },
    @{ nom = "baobab-administration"; chemin = (Join-Path $projet "baobab-administration") },
    @{ nom = "baobab-user"; chemin = (Join-Path $projet "baobab-user") }
)

# Écrit le fichier dans le dossier ; si un fichier différent existe déjà, il est d'abord sauvegardé.
function Write-FileIn($dossier, [string]$fichier, [string]$content) {
    $full = Join-Path $dossier.chemin $fichier
    $label = "créé      : "
    if (Test-Path $full) {
        $old = [System.IO.File]::ReadAllText($full)
        if ($old.Replace("`r`n", "`n") -eq $content.Replace("`r`n", "`n")) {
            Write-Host ("inchangé  : " + $dossier.nom + "\" + $fichier)
            return
        }
        $saved = Join-Path (Join-Path $backup $dossier.nom) $fichier
        New-Item -ItemType Directory -Force (Split-Path $saved -Parent) | Out-Null
        Copy-Item $full $saved -Force
        $label = "remplacé  : "
    }
    [System.IO.File]::WriteAllText($full, $content, $utf8)
    Write-Host ($label + $dossier.nom + "\" + $fichier)
}

$editorconfig = @'
# https://editorconfig.org : mêmes réglages dans tous les éditeurs.
root = true

[*]
charset = utf-8
end_of_line = lf
insert_final_newline = true
trim_trailing_whitespace = true

[*.py]
indent_style = space
indent_size = 4

[*.{js,jsx,css,html}]
indent_style = space
indent_size = 2

[*.md]
trim_trailing_whitespace = false

'@

$gitattributes = @'
# Fins de ligne : LF dans le dépôt, quel que soit le système (Windows inclus).
* text=auto eol=lf

# Fichiers binaires : jamais convertis.
*.png binary
*.jpg binary
*.jpeg binary
*.gif binary
*.ico binary
*.webp binary
*.woff binary
*.woff2 binary
*.xlsx binary
*.zip binary

'@

foreach ($d in $dossiers) {
    if (-not (Test-Path $d.chemin)) {
        Write-Host ("ignoré    : " + $d.nom + " (dossier introuvable à côté de biblio)")
        continue
    }
    Write-FileIn $d ".editorconfig" $editorconfig
    Write-FileIn $d ".gitattributes" $gitattributes
}

Write-Host ""
Write-Host "Terminé. Pour appliquer les fins de ligne LF aux fichiers déjà suivis par git,"
Write-Host "dans chaque dossier qui contient un .git (par exemple baobab-administration) :"
Write-Host "    git add --renormalize ."
Write-Host "    git status     # ne doit lister que des changements de fins de ligne"
Write-Host "    git commit -m `"Fins de ligne LF (.gitattributes, .editorconfig)`""
