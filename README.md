# biblio : backend BAOBAB

API REST (Django REST Framework), interfaces web Django et base PostgreSQL de la plateforme.
Voir le `README.md` à la racine du projet pour l'ensemble (architecture, rôles, démarrage, déploiement).

## Démarrer

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env     # puis renseigner SECRET_KEY et la base de données
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

Toutes les variables d'environnement sont décrites dans `.env.example`.

## Applications (`apps/`)

| Application | Rôle |
|---|---|
| `accounts` | Comptes utilisateurs, connexion, limitation des tentatives de connexion |
| `organizations` | Structures, statut, modules, personnel et rôles, politique de prêt |
| `catalog` | Catalogue : livres, exemplaires, catégories, emplacements |
| `members` | Adhérents des bibliothèques |
| `circulation` | Prêts, retours, renouvellements |
| `coworking` | Espaces, ressources, réservations, passages, paiements |
| `imports` | Import du catalogue depuis un fichier CSV ou XLSX |
| `audit` | Journal d'audit des actions |
| `api` | API REST : vues, permissions, limitation de débit, routes |
| `staff` | Interface web Django du personnel (`/staff/`) |
| `reader` | Interface web Django du lecteur (`/reader/`) |

## API

Préfixe : `/api/v1/`.

| Route | Contenu |
|---|---|
| `auth/` | `login`, `register` (lecteurs), `refresh`, `me`, `password` |
| `platform/` | Administration de la plateforme (structures, audit global) |
| `staff/<organization_id>/` | Personnel d'une structure : catalogue, membres, prêts, équipe, paramètres, audit |
| `staff/<organization_id>/imports/` | Import du catalogue |
| `staff/<organization_id>/coworking/` | Module coworking côté personnel |
| `reader/` | Espace lecteur : bibliothèques, catalogue, prêts, coworking |
| `schema/`, `docs/` | Schéma OpenAPI et documentation Swagger |

Chaque route du personnel vérifie que l'utilisateur appartient à la structure demandée et que celle-ci est active.

## Commandes de gestion

```powershell
python manage.py create_staff --organization <identifiant-court> --email <e-mail> --first-name <prénom> --last-name <nom>
python manage.py register_member ...    # inscrire un membre (voir --help)
python manage.py checkout_copy ...      # prêter un exemplaire (voir --help)
python manage.py return_copy ...        # enregistrer un retour (voir --help)
python manage.py list_loans ...         # lister les prêts (voir --help)
```

## Tests

```powershell
python manage.py test --keepdb --settings=config.settings_test
```

893 tests. `config/settings_test.py` réduit le coût du hachage des mots de passe : il ne doit servir
qu'aux tests. Laissez `DATABASE_URL` absente pour tester sur la base locale.

## Fichiers utiles

- `build.sh` : étapes de build sur Render.
- `.python-version` : version de Python.
- `scripts/smoke_prod.ps1` : vérification d'un déploiement.
- `schema.yml` : schéma OpenAPI généré.
