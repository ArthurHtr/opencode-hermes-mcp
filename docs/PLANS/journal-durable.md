# PLAN — Journal durable des délégations (chemin B)

> Mission : ajouter un **journal durable** des délégations au contrôleur
> `opencode_hermes_mcp` — **modif MINIMALE** : un module d'append + 2 points
> d'appel (début de run / état terminal). Le journal survit aux sessions et
> alimente l'Activity de Helmwire Work (repo `Utility/helmwire-work`,
> consommateur en lecture seule).

## Contexte

- Le contrôleur vit dans `opencode_hermes_mcp/controller.py` (`run()`).
- État MCP actuel : `~/.local/state/opencode-hermes-mcp/turn_<sid>.json` —
  **effacé à la complétion** → pas d'historique. Le journal corrige exactement
  cette lacune.
- Version courante : `pyproject.toml` `version = "0.4.1"`, pin OpenCode
  `1.18.21` (`pin.txt`).
- Cycle de release du repo : tests verts → bump version → tag `v*` (déclenche
  `publish.yml` → PyPI) → MR GitLab. Miroir GitHub public
  `ArthurHtr/opencode-hermes-mcp` (remote `github`).

## Contraintes dures

- **Modif MINIMALE** : pas de refonte du contrôleur. Un module
  `journal.py` + 2 appels dans `run()`. Le reste (SSE, q/p, recovery,
  heuristiques de complétion) reste **inchangé**.
- **Retro-compatibilité totale** : le journal est un ADD. Si l'écriture du
  journal échoue (disk plein, permissions), le run **ne doit PAS échouer** —
  loguer l'erreur et continuer.
- Le chemin du journal : `~/.local/state/opencode-hermes-mcp/delegations.jsonl`
  par défaut, surchargeable via env `OPENCODE_HERMES_MCP_JOURNAL`.
- **NE JAMAIS redémarrer le service `opencode-server`** (des sessions tournent
  dessus, dont celle qui exécute ce plan).
- **NE JAMAIS tuer la session OpenCode en cours** (celle qui exécute ce plan).
- Jamais de lecture en entier d'un fichier > 100 Ko.

## Format du journal (JSONL, une ligne par record)

```json
{"ts": 1787750000123, "kind": "start", "session_id": "ses_...", "directory": "/abs/repo", "agent": "build", "task": "...(tronqué à 500 chars)"}
{"ts": 1787750100456, "kind": "end", "session_id": "ses_...", "directory": "/abs/repo", "state": "completed|error|aborted|timeout", "elapsed_ms": 100333, "files": 12, "additions": 1209, "deletions": 18, "changed_files": ["a.py", "..."]}
```

- `ts` en ms epoch. `state` = l'état terminal du run (mêmes valeurs que le
  `state` du résultat du contrôleur). `changed_files` tronqué à 50 entrées.
- Append **atomique** : ouverture en mode `a`, une écriture complète par ligne
  (flush + fsync), pas de réécriture du fichier.

## Étapes (après CHAQUE étape : tests verts + commit)

### Étape 1 — Module `journal.py`
- `opencode_hermes_mcp/journal.py` :
  - `journal_path() -> Path` (env `OPENCODE_HERMES_MCP_JOURNAL` ou défaut).
  - `append_start(session_id, directory, agent, task)` /
    `append_end(session_id, directory, state, elapsed_ms, files, additions,
    deletions, changed_files)` — toutes deux **swallowent les exceptions**
    (log via `logging`, jamais de raise).
  - `read_journal(path=None) -> list[dict]` (lecture seule, pour les tests et
    le consommateur).
- Tests unitaires (`tests/test_journal.py`) : append start/end, format JSONL
  valide, lecture, non-raise sur chemin non-écrivable, surcharge env.
- `python -m pytest` (ou le runner du repo) vert. Commit.

### Étape 2 — Intégration dans `controller.run()`
- **Début** (après création/résolution de la session, avant le wait) :
  `append_start(...)`.
- **Fin** (chaque état terminal : completed / error / aborted / timeout) :
  `append_end(...)` avec les métriques déjà disponibles dans le résultat.
- **Aucun autre changement** dans le contrôleur.
- Tests : ajouter un test qui vérifie qu'un run produit start+end (mock ou
  run réel minimal si le repo le permet — suivre la convention des tests
  existants). Vert. Commit.

### Étape 3 — Version + docs
- Bump `pyproject.toml` → `0.4.2`.
- README : section « Journal des délégations » (chemin, format, env).
- Commit.

### Étape 4 — Release (cycle propre du repo)
- `tests/run_tests.py` (ou la suite complète) vert.
- MR GitLab (`glab mr create`) — depuis ce repo, hostname `192.168.1.63`.
- Merge du MR.
- **Tag `v0.4.2`** sur main + push (déclenche PyPI `publish.yml`).
  - Si la publication PyPI échoue : **ne pas bloquer** — le signaler dans le
    résumé, ne pas retenter plus d'une fois.
- Push du miroir GitHub : `git push github main --tags` (remote `github`).
- Commit final (si besoin) + push.

## Critère d'acceptation

1. `journal.py` + tests unitaires verts (append, lecture, non-raise, env).
2. `controller.run()` : 2 appels (start/end) — diff minimal, rien d'autre
   modifié.
3. Suite de tests complète verte.
4. Version `0.4.2`, README documenté.
5. MR GitLab créé et mergé, tag `v0.4.2` poussé, miroir GitHub synchronisé.
6. Aucun credential commité.

## Anti-patterns / discipline

- **Modif MINIMALE** — si la tentation de refonte vient, l'arrêter.
- **Le journal ne doit jamais faire échouer un run.**
- **NE JAMAIS redémarrer `opencode-server`** / **NE JAMAIS tuer ta propre
  session OpenCode.**
- Garde anti-boucle : verrou déterminé → écrire immédiatement.
- Livrables au fil de l'eau : commit par étape.
