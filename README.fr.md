[English](README.md) · [简体中文](README.zh-CN.md) · **Français** · [العربية](README.ar.md)

# formist-cli

**`fm` est un client en ligne de commande pour les déploiements [Formist](https://formist.ai)
et [Evane](https://evane.ai), qui passe exactement par la même API et la même connexion que
l'application web.**

![Un terminal exécutant fm health, fm spaces et fm kb search sur un déploiement Formist](docs/screenshot.png)

<sub>Données fictives, générées par le vrai code de `fm` avec `python3 scripts/render_screenshot.py`.</sub>

---

## Le problème

Faire tourner un déploiement Formist ou Evane au quotidien, c'est lui poser sans arrêt des
questions urgentes : cette région est-elle en bonne santé, un connecteur est-il bloqué, la
migration d'hier s'est-elle appliquée partout, à qui appartient tel espace parmi des dizaines.
Chaque question suppose normalement d'ouvrir l'application web, de trouver la bonne page
d'administration, ou d'écrire un script ad hoc contre l'API en gérant soi-même la connexion.
Ni l'un ni l'autre ne se scripte, et les deux sont lents au moment précis où il faut aller vite.

## L'idée

`fm` se connecte de la même façon que l'application web, donc il ne peut faire que ce que
votre propre compte peut déjà faire là-bas, rien de plus. Chaque commande affiche le
déploiement, l'espace et l'identifiant qu'elle a résolus avant de s'exécuter, pour que ce qui
est à l'écran soit l'information complète, pas une supposition. Une commande qui modifie des
données montre toujours d'abord ce qu'elle ferait ; valider est une étape à part, explicite.

- **Un seul client pour chaque déploiement.** `--host` bascule entre Formist, Evane, Evane EAU,
  Formist Chine, ou n'importe quelle origine personnalisée, comme un serveur de développement
  local, avec le même binaire et les mêmes commandes.
- **La provenance avant l'action.** Chaque commande annonce le déploiement et l'espace qu'elle
  a résolus, et d'où ils viennent : une option, une valeur par défaut mémorisée, ou rien du
  tout. Une mauvaise cible se repère en lisant l'écran, pas par ses conséquences.
- **Simulation par défaut.** Une commande qui modifie des données affiche d'abord ce qu'elle
  ferait et attend `--apply`. `fm update`, qui applique un lot de changements en une fois,
  revérifie que le plan correspond toujours avant de valider, pour qu'une lecture périmée ne
  devienne jamais une écriture erronée.
- **Un identifiant par déploiement.** `fm login` enregistre un identifiant OAuth avec les
  droits `0600`, un fichier par déploiement, de sorte que se connecter à un environnement de
  test ne touche jamais la production. `FORMIST_TOKEN` en fournit un pour la CI : utilisé tel
  quel, jamais écrit sur disque, jamais rafraîchi.
- **Aucune dépendance surprise.** Uniquement la bibliothèque standard, Python 3.10 ou plus,
  une installation en une seconde.

## Installation

```sh
pipx install formist-cli
```

Python 3.10 ou plus récent. Aucune dépendance tierce.

## Utilisation

### Connexion

```sh
fm login            # opens a browser; the credential is stored at 0600
fm whoami
fm logout
```

Sur une machine sans navigateur, utilisez `fm login --no-browser` et collez le code affiché
par la page. Pour la CI, exportez `FORMIST_TOKEN` avec le champ `token` du fichier que
`fm login` écrit ; il est utilisé tel quel et jamais écrit sur disque.

Les identifiants vivent dans `~/.config/formist/` (remplaçable par `FORMIST_CONFIG_DIR`), un
fichier par déploiement, donc se connecter à l'un ne remplace jamais l'autre.

Les actions d'administration de la plateforme demandent un droit supplémentaire :
`fm login --admin` le demande, et le serveur ne l'accorde que si votre compte le possède déjà.
Sans cette option, même le jeton d'un administrateur de la plateforme ne fonctionne que comme
un utilisateur ordinaire.

### Choisir un déploiement

`fm` s'adresse par défaut à `https://api.formist.ai`. `--host` en choisit un autre :

| `--host`     | API                                  |
|--------------|---------------------------------------|
| `formist`    | `https://api.formist.ai` (défaut)    |
| `evane`      | `https://api.evane.ai`               |
| `evane-ae`   | `https://ae.api.evane.ai`            |
| `formist-cn` | `https://formist-api.we-carbon.com`  |

Toute autre valeur est prise comme une origine d'API, par exemple
`fm --host 127.0.0.1:8160 spaces` pour un serveur local. `--host` fonctionne avant ou après la
commande : `fm --host evane spaces` et `fm spaces --host evane` reviennent au même.

### Espaces

Un espace se désigne toujours par son identifiant, jamais par son nom, puisque les noms ne
sont pas uniques :

```sh
fm spaces                         # every space, platform-admin only
fm use --space <space-id>         # remember a default space
fm kb search "venue contract" --space <space-id>
```

`fm use` peut aussi mémoriser un déploiement, pour la lecture seulement. Une commande
d'écriture n'hérite jamais d'un déploiement mémorisé : nommez-le avec `--host`, sinon la
valeur par défaut (`formist`) est utilisée.

### Écritures

La plupart des commandes qui modifient des données s'exécutent d'abord en simulation et
affichent ce qu'elles feraient ; ajoutez `--apply` pour valider. `fm update`, qui applique un
lot de changements de façon transactionnelle, fonctionne un peu différemment : la simulation
affiche un jeton de plan, et `--apply <jeton>` relance la simulation pour confirmer que le plan
correspond toujours avant de valider. Quelques actions d'administration irréversibles, comme
`fm spaces delete`, n'ont aucune simulation et le précisent dans leur propre `--help`.

### Commandes

Lancez `fm --help` pour la liste complète et `fm <commande> --help` pour chacune. La surface se
divise en deux : les commandes qui fonctionnent avec les droits de votre propre compte, comme
dans l'application web, et celles qui demandent en plus les droits d'administration de la
plateforme, revérifiés par le serveur à chaque appel.

Usage courant, droits de votre propre compte : `chats`, `chat`, `kb`, `get`, `export`,
`import`, `diff`, `upload`, `skill-attach`, `entity-fields`, `skill-library-install`,
`meeting`, `project`, `connector`, `skills-add`, `skills-update`.

Administration de la plateforme, droit supplémentaire requis : `spaces`, `users`, `proposals`,
`audit-log`, `connectors`, `logs`, `space-plan`, `usage-tokens`, `alerts`, `settings`,
`messages`, `whatsapp`, `update`, `airtable`, `participants`, `instance`.

`fm health` est public, aucune connexion requise, sauf si `--admin` demande la vue
multirégion ; `fm doctor` ne demande pas non plus de droit d'administration. `fm files` mélange
les deux : lire et réévaluer relèvent de l'usage courant, fusionner deux entrées en double
relève de l'administration de la plateforme.

## FAQ

**Ai-je besoin des droits d'administration de la plateforme pour utiliser cet outil ?**
Non. Se connecter avec un compte ordinaire exécute les commandes de niveau utilisateur
exactement comme le ferait l'application web pour ce compte. Une commande d'administration
est refusée localement, avec le droit manquant nommé, avant même l'envoi d'une requête,
chaque fois que `fm login` a déjà enregistré que l'identifiant en est dépourvu ; sinon, c'est
le serveur qui tranche en dernier ressort, comme pour l'application web.

**Puis-je le pointer vers un serveur de développement local ?**
Oui : `--host` accepte n'importe quelle origine d'API, pas seulement les quatre déploiements
nommés, par exemple `fm --host 127.0.0.1:8160 health`.

**Puis-je l'utiliser en CI, sans navigateur ?**
Oui : exportez `FORMIST_TOKEN` avec le champ `token` du fichier que `fm login` écrit. Il est
relu à chaque appel, utilisé tel quel, et `fm` ne l'écrit jamais sur disque ni ne le rafraîchit.

**Qu'est-ce qui empêche une écriture d'atterrir sur le mauvais déploiement ?**
`fm use` ne mémorise un déploiement que pour la lecture ; une écriture résout toujours
`--host` depuis la ligne de commande, avec `formist` comme valeur par défaut si rien n'est
donné. Il n'existe pas d'« environnement courant » qu'une écriture pourrait hériter en
silence.

**`fm` peut-il faire quelque chose que mon compte ne pourrait pas faire dans l'application
web ?**
Non. Il appelle la même API avec le même identifiant, et le serveur applique chaque droit à
chaque appel ; `fm` n'est qu'une autre porte d'entrée vers elle, pas un autre ensemble de
droits.

## Développement

```sh
uv sync
uv run pytest
```

`docs/screenshot.png` est généré à partir d'une vraie sortie de `fm`, contre un serveur de
test local, jamais dessiné à la main :

```sh
python3 scripts/render_screenshot.py
```

## Licence

[MIT](LICENSE) © 2026 WeCarbon
