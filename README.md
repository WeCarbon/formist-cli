# formist-cli

`fm` is the command-line client for [Formist](https://formist.ai). It talks to the
same API as the web app and does only what your account is allowed to do there.

## Install

```sh
pipx install formist-cli
```

Python 3.10 or newer. No third-party dependencies.

## Sign in

```sh
fm login            # opens a browser; the credential is stored at 0600
fm whoami
fm logout
```

On a machine without a browser, use `fm login --no-browser` and paste the code
the page shows. For CI, export `FORMIST_TOKEN` with the `token` field of the
file `fm login` writes; it is used as-is and never written to disk.

Credentials live in `~/.config/formist/` (override with `FORMIST_CONFIG_DIR`),
one file per host, so signing in to one deployment never replaces another.

## Choosing a deployment

`fm` talks to `https://api.formist.ai` by default. `--host` picks another one:

| `--host`     | API                                  |
|--------------|--------------------------------------|
| `formist`    | `https://api.formist.ai` (default)   |
| `evane`      | `https://api.evane.ai`               |
| `evane-ae`   | `https://ae.api.evane.ai`            |
| `formist-cn` | `https://formist-api.we-carbon.com`  |

Anything else is taken as an API origin, for example
`fm --host http://127.0.0.1:8160 spaces` for a local server.

`--host` works before or after the command: `fm --host evane spaces` and
`fm spaces --host evane` are the same.

## Spaces

A space is always addressed by its id, never by its name:

```sh
fm spaces                         # list the spaces you can see, with ids
fm use --space <space-id>         # remember a default space
fm kb search "venue contract" --space <space-id>
```

`fm use` can also remember a host for reads. A command that writes never
inherits a remembered host: name it with `--host`, or the default is used.

## Writes

Commands that change data run as a dry run first and print what they would do.
Add `--apply` (or, for `fm update`, the plan token the dry run printed) to
commit.

## Commands

Run `fm --help` for the full list and `fm <command> --help` for each one.
Commonly used: `spaces`, `use`, `chats`, `chat`, `files`, `kb`, `proposals`,
`messages`, `meeting`, `upload`, `get`, `update`, `export`, `import`, `diff`,
`participants`, `connectors`, `skills-add`, `skills-update`.

## Development

```sh
uv sync
uv run pytest
```
