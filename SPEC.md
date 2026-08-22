# SPEC — Package + installers pour opencode-hermes-mcp

Tu es dans le repo `~/gitlab/opencode-hermes-mcp` (branche `feat/initial-build`).
Le code de production est déjà seedé (server.py, controller.py, client.py,
models.py, smoke_client.py, tests/run_tests.py). Ta mission : en faire un
package installable + les scripts install/uninstall/upgrade. **Ne modifie PAS
la logique de server.py / controller.py / client.py / models.py** — ce sont
des sémantiques API validées en production contre OpenCode 1.18.18.

## Contexte (ce que le système fait)

```
Hermes (LLM) --MCP stdio--> server.py (FastMCP, 6 tools) --HTTP+SSE--> OpenCode server :4096
```

- Le process MCP est spawné par Hermes via `mcp_servers.opencode.command`
  dans `~/.hermes/config.yaml` → pointe sur un launcher bash qui lit les
  creds du serveur OpenCode et exec `.venv/bin/python server.py`.
- Le serveur OpenCode tourne en service systemd user `opencode-server`
  (binaire `opencode serve --hostname 127.0.0.1 --port 4096`), creds
  HTTP basic dans `~/.config/hermes/opencode-server.json`
  (format: `{"base_url": "http://127.0.0.1:4096", "username": "...", "password": "..."}`).
- Le LLM utilisé par OpenCode = endpoint Unsloth (OpenAI-compatible),
  config dans `~/.config/opencode/opencode.json` (provider `unsloth`,
  npm `@ai-sdk/openai-compatible`, apiKey `{file:secrets/unsloth-api-key}`
  → fichier `~/.config/opencode/secrets/unsloth-api-key`, mode 600).

## Version pin

**OpenCode binaire : PINNÉ à `1.18.18`** (version validée du controller).
Le controller n'est PAS validé contre d'autres versions. Définis une
constante `OPENCODE_VERSION="1.18.18"` en tête des scripts (install +
upgrade). L'upgrade du binaire est un flag explicite `--binary` (voir plus
bas), jamais le comportement par défaut.

## Livrables

### 1. `pyproject.toml`
- package nom `opencode-hermes-mcp`, version `0.1.0`
- `requires-python = ">=3.11"`
- dépendance : `mcp==1.12.4` (seule dépendance externe ; le reste est stdlib)
- les modules sont des fichiers plats au root du repo (server.py etc.) —
  pas besoin de package python structuré ; `pip install -e .` doit juste
  résoudre la dépendance `mcp`. (Un packaging flat/`py-modules` simple est
  acceptable ; ne restructure PAS les imports des 4 fichiers existants.)

### 2. `scripts/install.sh` — idempotent, spécifique Unsloth
Comportement (flags : `--yes` pour non-interactif, `--port N` défaut 4096,
`--skip-binary`) :
1. **Prompts** (sauf `--yes` + env) : base URL LLM (défaut
   `https://ai.helmwire.com/v1`), clé API Unsloth (lecture masquée, défaut
   env `UNSLOTH_API_KEY`), modèle (défaut `unsloth/unsloth/Qwen3.8-27B-GGUF`).
   Les valeurs peuvent aussi venir des env `OPENCODE_LLM_BASE_URL`,
   `UNSLOTH_API_KEY`, `OPENCODE_LLM_MODEL`.
2. **Binaire OpenCode** : installer la version PINNÉE via le script officiel
   `curl -fsSL https://opencode.ai/install | bash -s -- --version 1.18.18`
   (pose `~/.opencode/bin/opencode`). Si le binaire existe déjà ET
   `opencode --version` == 1.18.18 → skip (message). `--skip-binary` force
   le skip.
3. **Venv** : `python3 -m venv $REPO/.venv` (ou réutiliser si présent) +
   installation de `mcp==1.12.4` (préférer `uv pip install --python
   $REPO/.venv/bin/python` si `uv` existe, sinon pip).
4. **Config OpenCode** : écrire `~/.config/opencode/opencode.json` —
   provider `unsloth` avec baseURL + apiKey `{file:secrets/unsloth-api-key}`
   + le modèle demandé (limits context 183040 / output 65536, reasoning +
   tool_call true, temperature 0.2 topP 0.9) + `"model": "<provider>/<model>"`.
   Écrire le secret `~/.config/opencode/secrets/unsloth-api-key` (chmod 600).
   **Si ces fichiers existent déjà, ne les écraser PAS** (message + skip),
   sauf flag `--force-config`.
5. **Creds serveur** : `~/.config/hermes/opencode-server.json` — si absent,
   générer username `opencode` + password aléatoire (secrets.token_urlsafe(16)),
   base_url `http://127.0.0.1:<port>`. chmod 600.
6. **Launchers** (écris-les dans `~/.local/bin/`, chmod +x) :
   - `opencode-mcp-launch.sh` : lit `opencode-server.json`, exporte
     `OPENCODE_SERVER_URL/USERNAME/PASSWORD`, exec
     `$REPO/.venv/bin/python $REPO/server.py`. (Le launcher actuel en
     production est `~/.local/bin/opencode-mcp-launch.sh` — reproduis sa
     logique, en pointant sur `$REPO`.)
   - `opencode-server-launch.sh` : lit les creds, exec
     `~/.opencode/bin/opencode serve --hostname 127.0.0.1 --port <port>`
     depuis `$HOME`.
7. **systemd** : écrire `~/.config/systemd/user/opencode-server.service`
   (Type=simple, ExecStart le launcher serveur, Restart=always, RestartSec=3,
   TimeoutStopSec=30, WantedBy=default.target) + `systemctl --user daemon-reload`
   + `enable --now`.
8. **Hermes config** : patcher `~/.hermes/config.yaml` (python3 + yaml,
   sauvegarde `.bak` avant) :
   - `mcp_servers.opencode = {command: ~/.local/bin/opencode-mcp-launch.sh,
     enabled: true, timeout: 14400, connect_timeout: 30,
     supports_parallel_tool_calls: false}`
   - `timeouts.tools.sequential_call = 14400` et
     `timeouts.tools.concurrent_batch = 14400` (clé imbriquée `timeouts.tools.*`)
   Idempotent : ne pas dupliquer si l'entrée existe.
9. **Vérification finale** : attendre la santé du serveur
   (`curl -u user:pass http://127.0.0.1:<port>/global/health`, retry ~30 s),
   puis lancer `$REPO/.venv/bin/python $REPO/smoke_client.py` et exiger la
   sortie « tool surface OK ». Afficher un résumé final (ce qui a été
   installé, ce qui a été skip, rappel : nouveau session Hermes requis pour
   charger le MCP).

### 3. `scripts/uninstall.sh`
- stop + disable + remove du service systemd `opencode-server`
- supprimer les 2 launchers `~/.local/bin/opencode-*-launch.sh`
- supprimer le venv `$REPO/.venv`
- retirer `mcp_servers.opencode` de `~/.hermes/config.yaml` (backup .bak)
- supprimer `~/.config/hermes/opencode-server.json`
- **NE PAS toucher** : le clone git, `~/.config/opencode/opencode.json`,
  le secret Unsloth, le binaire OpenCode — sauf flag `--purge` qui en plus
  supprime la config OpenCode + secret + (avec `--purge-binary`) le binaire.
- Afficher clairement ce qui a été retiré / conservé.

### 4. `scripts/upgrade.sh`
Par défaut (mise à jour du controller, PAS du binaire) :
1. `git pull` dans le repo (les versions à jour = toujours le repo distant)
2. rafraîchir les dépendances du venv (`pip install -e .` / uv)
3. `systemctl --user restart opencode-server`
4. re-lancer `smoke_client.py` (exiger OK)
Flag `--binary [VERSION]` : monter le binaire OpenCode (défaut : dernière
version via le script officiel sans `--version`) — avec un Avertissement
explicite avant : « le controller est validé pour 1.18.18 ; après un
upgrade du binaire, re-valider le controller (tests/run_tests.py) ».

### 5. Fixes de portabilité (les seuls changements de code autorisés)
- `smoke_client.py` : le chemin du repo doit être relatif au fichier
  (`os.path.dirname(os.path.abspath(__file__))`) — déjà le cas pour le venv,
  mais le `directory` de test `/home/arthur/gitlab/erdos-moser-equation`
  doit devenir `$OPENCODE_MCP_TEST_DIR` avec défaut `/tmp/oc-mcp-test/gitrepo`
  (le smoke doit tourner sur n'importe quelle machine).
- `interaction_test.py` et `repro.py` : ne les copie PAS dans le repo
  (scripts de debug ad-hoc) — s'ils sont déjà présents, supprime-les.
- `tests/run_tests.py` : même principe — chemins relatifs au repo,
  `directory` de test via env. Lis-le avant de toucher : il contient la
  suite d'intégration ; ne casse PAS ses tests, rends-le seulement
  portable (chemins + env).
- Ajoute un `.gitignore` propre (.venv/, __pycache__/, *.pyc, *.bak).

### 6. `README.md` (remplace celui auto-généré)
- architecture (le schéma 3 couches), prérequis (Hermes installé, python3
  >=3.11, accès réseau), installation en 2 commandes, usage (Hermes délègue
  via opencode_run), upgrade/uninstall, la règle du pin 1.18.18 + procédure
  de re-validation après upgrade binaire, les 3 timeouts (controller 3600 /
  MCP 14400 / hermes tools 14400) en un paragraphe.

## Contraintes de validation (à faire toi-même, sans LLM)

- `bash -n` sur chaque script.
- `shellcheck` si dispo (sinon skip, ne l'installe pas).
- Créer le venv du repo + `$REPO/.venv/bin/python smoke_client.py` → doit
  afficher « tool surface OK » (le serveur OpenCode tourne déjà sur cette
  machine, creds dans ~/.config/hermes/opencode-server.json).
- **NE PAS exécuter install.sh en mode réel sur cette machine** (le
  basculement live est fait par le superviseur après le MR). Tu peux tester
  les parties sûres (génération de fichiers en mode dry-run si tu ajoutes un
  flag `--dry-run`, ou en pointant les cibles vers un tmpdir via env
  `OPENCODE_MCP_HOME` si tu conçois les scripts ainsi).
- `git add -A && git commit` à la fin (message clair). **Ne fais PAS de
  push ni de MR** — le superviseur s'en charge.

## Défauts Unsloth (à coder en dur comme défauts, pas comme seules valeurs)

- base URL : `https://ai.helmwire.com/v1`
- modèle : `unsloth/unsloth/Qwen3.8-27B-GGUF` (context 183040, output 65536)
- provider name affiché : `Unsloth — ai.helmwire.com`
