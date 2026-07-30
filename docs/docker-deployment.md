# Docker deployment support

Mimo Console remains a NoneBot plugin. Docker support adds an optional,
privilege-separated deployment backend instead of giving the NoneBot container
access to the Docker socket.

## Compatibility contract

The first preview supports:

- Linux Docker Engine and Docker Compose v2.
- Docker Compose 2.24.4 or newer (`!reset` override support).
- A standard NoneBot project with `pyproject.toml`.
- `uv.lock` as the writable, reproducible dependency format.
- A Dockerfile that builds the project from its lock file.
- One configured Compose service per Mimo Console instance.
- Registry plugins with validated package and module names.
- GitHub HTTPS source plugins whose import package uses the
  `nonebot_plugin_*` convention and whose dependencies are plain PyPI
  requirements.
- A single administrator-configured dotenv file per instance, with masked reads,
  atomic writes and bounded backups.

The existing local `nb-cli` behavior stays the default. Docker mode is opt-in
and never installs packages into the running NoneBot interpreter.

PDM, requirements-only projects, non-GitHub repositories, repositories that
require local/VCS dependencies, Kubernetes and cross-host management are
outside the first preview.

## Trust boundary

The NoneBot container:

- hosts Mimo Console and reports NoneBot runtime state;
- submits structured package operations over a Unix socket;
- never mounts `/var/run/docker.sock`;
- never supplies project paths, Compose files or shell commands.

The host agent:

- loads the only allowed project path and Compose service from administrator
  configuration;
- validates every package and operation;
- persists operation state and audit output;
- updates a staged project copy;
- resolves dependencies inside a restricted UV tool container, not on the host;
- excludes runtime dotenv files, tokens, private keys and secret directories from
  the staged dependency/build workspace;
- builds a uniquely tagged image;
- verifies the image before deployment;
- replaces only the configured service;
- restores the previous project files and image on failure.
- reads and updates only the configured environment file, never a path supplied
  by the NoneBot container.

## Operation lifecycle

Operations use these states:

`queued -> preparing -> locking -> building -> verifying -> deploying ->
health_checking -> succeeded`

Failures before deployment leave the current container untouched. Failures
after deployment enter `rolling_back` and end in `rolled_back` or `failed`.

The plugin receives an operation ID immediately. The browser can reconnect
after the managed NoneBot container restarts and retrieve the persisted result.

## Required Compose convention

The managed service uses a persistent Mimo override file:

```yaml
services:
  bot:
    image: example/nonebot:mimo-initial
    build: !reset null
```

The agent updates the managed service image and applies `build: !reset null`
while preserving every other administrator-configured field. Rollback restores
the exact previous override snapshot. All deploy and rollback commands include
both the project's Compose file and the override.

## Security invariants

- Unix-socket transport with a token stored in a mounted secret.
- Constant-time token comparison.
- No arbitrary command endpoint.
- Strict package-name and operation allowlists.
- Canonical path containment checks.
- One mutating operation per instance.
- Redacted logs and bounded output.
- Explicit build, deploy and health-check timeouts.
- No Docker socket inside a NoneBot container.
- Mimo Console self-update is pinned to its allowlisted HTTPS Git repository and
  follows the same staged build, verification and rollback transaction.
- Environment values are bounded, newline-free and backed up before replacement;
  secret placeholders never overwrite an existing secret.
