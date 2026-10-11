param(
    [Parameter(Mandatory)][string]$BaseUrl,
    [Parameter(Mandatory)][string]$Email,
    [Parameter(Mandatory)][string]$Password,
    [Parameter(Mandatory)][string]$OrgId,
    [switch]$CheckThrottle
)

$api = $BaseUrl.TrimEnd("/") + "/api/v1"
$failed = 0

function Call($Method, $Path, $Body = $null, $Headers = @{}) {
    $req = @{ Uri = "$api/$Path"; Method = $Method; Headers = $Headers; UseBasicParsing = $true }
    if ($Body) { $req.Body = ($Body | ConvertTo-Json); $req.ContentType = "application/json" }
    try {
        $r = Invoke-WebRequest @req
        $json = $null
        try { $json = $r.Content | ConvertFrom-Json } catch {}
        return @{ Status = [int]$r.StatusCode; Json = $json }
    } catch {
        $status = 0
        if ($_.Exception.Response) { $status = [int]$_.Exception.Response.StatusCode }
        return @{ Status = $status; Json = $null }
    }
}

function Check($Label, $Ok) {
    if ($Ok) { Write-Host "[OK]    $Label" -ForegroundColor Green }
    else { Write-Host "[ECHEC] $Label" -ForegroundColor Red; $script:failed++ }
}

$ping = Call GET "ping/"
Check "ping" ($ping.Status -eq 200 -and $ping.Json.status -eq "ok")

$login = Call POST "auth/login/" @{ email = $Email; password = $Password }
Check "connexion staff" ($login.Status -eq 200 -and [bool]$login.Json.access)
if (-not $login.Json.access) { exit 1 }
$auth = @{ Authorization = "Bearer $($login.Json.access)" }

Check "profil (auth/me)" ((Call GET "auth/me/" $null $auth).Status -eq 200)

$dash = Call GET "staff/$OrgId/dashboard/" $null $auth
Check "tableau de bord staff" ($dash.Status -eq 200)
if ($dash.Json) { Write-Host ("        prets en cours : " + $dash.Json.loans.active + ", en retard : " + $dash.Json.loans.overdue) }

Check "liste des livres" ((Call GET "staff/$OrgId/books/?page_size=3" $null $auth).Status -eq 200)
Check "autre structure refusee (403)" ((Call GET "staff/999999999/books/" $null $auth).Status -eq 403)
Check "acces anonyme refuse (401)" ((Call GET "staff/$OrgId/books/").Status -eq 401)

try {
    $docs = Invoke-WebRequest -Uri "$api/docs/" -UseBasicParsing
    Check "documentation /api/v1/docs/" ($docs.StatusCode -eq 200)
} catch { Check "documentation /api/v1/docs/" $false }

if ($CheckThrottle) {
    Write-Host "Test du debit : 12 connexions ratees avec des X-Forwarded-For differents..."
    $got429 = $false
    for ($i = 1; $i -le 12; $i++) {
        $h = @{ "X-Forwarded-For" = "203.0.113.$i" }
        $r = Call POST "auth/login/" @{ email = "personne@example.com"; password = "mauvais" } $h
        if ($r.Status -eq 429) { $got429 = $true; Write-Host "        429 recu a la tentative $i"; break }
    }
    Check "limite de connexion non contournable avec X-Forwarded-For" $got429
    Write-Host "        Attendez 1 minute avant de vous reconnecter normalement."
}

if ($failed -gt 0) { Write-Host "$failed verification(s) en echec." -ForegroundColor Red; exit 1 }
Write-Host "Tout est bon." -ForegroundColor Green