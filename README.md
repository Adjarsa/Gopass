# Gopass

Un seul pass pour réserver des cours de sport et de bien-être dans tous les studios partenaires, partout en Algérie.

Ce dépôt contient la plateforme complète : une API Python (FastAPI), une base PostgreSQL et le site trilingue (français, anglais, arabe), servis par un seul service.

## Ce que fait la plateforme

- **Membres** : compte, formules à crédits, catalogue de cours filtrable, réservation, annulation, QR code, historique, suppression du compte.
- **Studios** : planning hebdomadaire récurrent, places ouvertes à Gopass, exceptions par date, cours ponctuels, validation des arrivées par code, relevé mensuel.
- **Administration** : création des studios et de leurs gérants, formules et prix, prix des cours en crédits, recherche de membres, ajustement de crédits avec motif, paiements en espèces, relevés par studio, messages reçus.
- **Paiement** : en ligne par Chargily Pay (cartes CIB et Edahabia), ou en espèces enregistré par l'administrateur.

## Structure

```
app/core.py     configuration, modèles de la base, mots de passe et sessions
app/logic.py    règles de gestion : crédits, séances, réservations, annulations, paiements, relevés, e-mails
app/api.py      routes de l'API
app/main.py     démarrage, données initiales, service du site
web/index.html  le site (une seule page, sans étape de compilation)
web/vendor/     générateur de QR codes (licence MIT)
tests/          tests automatiques des règles
```

## Lancer en local

```bash
pip install -r requirements.txt -r requirements-dev.txt
ADMIN_EMAIL=moi@exemple.dz ADMIN_PASSWORD=motdepasse1 SEED_DEMO=1 DEMO_PASSWORD=motdepasse1 \
  uvicorn app.main:app --reload
```

Le site est sur http://localhost:8000. Sans `DATABASE_URL`, une base SQLite locale `gopass.db` est créée.
Avec `SEED_DEMO=1`, six studios fictifs sont ajoutés ; leurs gérants se connectent avec `studio1@demo.gopass` à `studio6@demo.gopass` et le mot de passe `DEMO_PASSWORD`.

Tests : `python -m pytest tests`

## Déployer sur Railway

1. Créer un projet Railway à partir de ce dépôt GitHub.
2. Ajouter une base **PostgreSQL** au projet.
3. Dans les variables du service web, renseigner au minimum :
   - `DATABASE_URL` : référence vers la variable `DATABASE_URL` de la base PostgreSQL ;
   - `SECRET_KEY` : une longue chaîne aléatoire (obligatoire, le service refuse de démarrer sans) ;
   - `PUBLIC_URL` : l'adresse publique du site, par exemple `https://www.votre-domaine.dz` ;
   - `ADMIN_EMAIL` et `ADMIN_PASSWORD` : le compte administrateur créé au premier démarrage.
4. Générer un domaine public pour le service, puis brancher votre nom de domaine.
5. Ouvrir le site, se connecter avec le compte administrateur : l'administration est à l'adresse `/#admin`.

La commande de démarrage et le contrôle de santé (`/health`) sont dans `railway.json`. Les tables sont créées automatiquement au premier démarrage. La liste complète des variables est dans `.env.example`.

**Ce dépôt est public : aucune clé ni aucun mot de passe ne doit y être écrit.** Tout se saisit dans les variables Railway.

## Brancher Chargily Pay

1. Ouvrir un compte Chargily Pay et récupérer la **clé secrète**.
2. Dans Railway : `CHARGILY_SECRET_KEY` = cette clé. En mode test, laisser `CHARGILY_BASE_URL` par défaut ; pour la production, y mettre l'adresse « live » indiquée dans la documentation Chargily.
3. Dans le tableau de bord Chargily, déclarer l'adresse de notification : `https://votre-domaine/api/webhooks/chargily`.

Le site redirige le membre vers la page de paiement Chargily. Les crédits ne sont ajoutés qu'à la réception de la notification `checkout.paid`, dont la signature est vérifiée. Une notification reçue deux fois ne crédite qu'une fois.

Sans `CHARGILY_SECRET_KEY`, le paiement en ligne est désactivé et le site indique de payer en espèces.

> L'appel à Chargily a été écrit d'après sa documentation publique et testé avec un faux serveur. Il n'a pas encore été essayé avec un vrai compte : faites un paiement en mode test avant d'ouvrir au public.

## Premiers pas après le déploiement

1. **Formules** : trois formules sont créées avec des prix indicatifs (10 crédits pour 4 000 DA, 25 pour 9 500 DA, 45 pour 16 000 DA). Corrigez-les dans l'administration avant d'ouvrir.
2. **Studios** : créez chaque studio et son gérant dans l'administration, puis transmettez-lui son e-mail et son mot de passe provisoire.
3. **Planning** : le gérant se connecte, crée ses cours récurrents et indique son tarif habituel par séance. Le prix en crédits est calculé automatiquement ; vous pouvez le corriger dans l'administration.

## Règles appliquées côté serveur

- Crédits d'un cours = tarif de référence ÷ `CREDIT_VALUE_DZD`, arrondi, minimum 1.
- Une réservation exige un compte, un solde suffisant et une place Gopass libre ; une seule place par membre et par séance.
- Deux réservations simultanées ne peuvent pas prendre la dernière place (verrou en base, vérifié sur PostgreSQL).
- Annulation par le membre plus de `CANCEL_HOURS` heures avant : crédits rendus. Ensuite : crédits perdus.
- Annulation d'une séance ou suppression d'une série par le studio : réservations annulées et remboursées.
- Les places Gopass ne dépassent jamais la capacité et ne descendent jamais sous les réservations prises.
- Un code de réservation n'est valable qu'une fois, dans le studio du cours, le jour du cours.
- Une formule dure `PLAN_DURATION_DAYS` jours. À l'échéance, les crédits restants expirent. Un renouvellement avant l'échéance ajoute les nouveaux crédits aux crédits restants.
- Reversement au studio : `PAYOUT_RATE` du tarif de référence pour chaque présence, absence ou annulation tardive.
- Chaque mouvement de crédits et chaque action d'administration est journalisé.

## Ce qui n'est pas encore fait

- Vérification de l'adresse e-mail à l'inscription et **mot de passe oublié**.
- Notifications WhatsApp. Les e-mails (réservation, annulation par le studio, paiement) partent seulement si SMTP est configuré.
- Limitation des tentatives de connexion.
- Migrations de schéma : les tables sont créées au démarrage, mais une modification ultérieure des modèles demandera un outil comme Alembic.
- Pages légales (conditions générales, mentions légales) : à rédiger avec un juriste. La page Confidentialité décrit le fonctionnement réel, elle ne remplace pas un texte juridique.
- Remboursement d'une formule payée : à traiter à la main dans le tableau de bord Chargily.
- L'administration est en français uniquement.
