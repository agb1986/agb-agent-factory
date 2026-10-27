# Deploying the factory

The factory runs on the CasaOS host as one Docker image with a data volume. Keys arrive at run time from an env file; none is built into the image. This page covers build, first run, where the traces live, rotating a key, running one review from the host, importing the compose file into CasaOS, and upgrading. The files are `Dockerfile`, `.dockerignore`, `compose.yaml` and `.env.example`; the convention is in [CONVENTIONS.md](CONVENTIONS.md#deployment).

## What the image holds

- `python:3.14-slim`, a non-root user `factory` (uid 10001) and the project installed from `uv.lock` into `/opt/venv` with the `jev` and `api` extras. The build stage is separate, so uv and the compiler caches stay out of the final image.
- `git` and `ripgrep`, which the reviewers' tools call, plus the `agents/` manifests and the `fixtures/` sources. The fixtures are built into `/app/fixtures/repos/` at image build time, so `mixed` and the others are there to review.
- The entrypoint is `factory`, so `docker run IMAGE review ...` is `factory review ...`. The default command is `--help`.
- `/app/data` is a volume and `FACTORY_TRACE_DB=/app/data/traces.duckdb`, so the trace database is on the volume and nowhere else.
- `TYPESAFE_LOG_LEVEL=warning`. The TypeSafe SDK logs each request id at INFO; the image and `compose.yaml` both pin WARNING so logs carry none. Never set it to `debug`, which logs request bodies, and the body is the diff under review.

No sandbox image and no Docker CLI are in it. See [Sandbox containers](#sandbox-containers).

## Build

```sh
docker build -t agb-agent-factory .
docker run --rm agb-agent-factory --version     # factory 0.1.0
```

or `docker compose build`, which tags `agb-agent-factory:latest`. The first build needs network for the base images, apt and the locked wheels; after that the layers cache, and a source change reinstalls nothing.

## First run

```sh
cp .env.example .env && chmod 600 .env     # then edit it
docker compose run --rm factory --version
docker compose run --rm factory review /app/fixtures/repos/mixed \
  --base base --head head --no-triage --allow-unpublished
```

`.env` holds two keys and is git-ignored, and `.dockerignore` keeps it out of the build context.

| Variable | Needed for | Where it comes from |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | every review | The Anthropic Console, API keys. On the machine that already has it, `agb sys secrets` holds it; paste the value into `.env` on the host. |
| `TYPESAFE_API_KEY` | the triage step, which `--no-triage` skips | The Typesafe dashboard. Absent is fine: a review without triage works, and with triage on, a missing key degrades to a `jev_error` that does not change the exit code. |

The GitHub workflow `live.yml` reads the same two names from repository secrets, and neither is set there yet; that is separate from this file. The env file is the only place the CasaOS host needs them.

`--allow-unpublished` is needed on a fresh data volume. A review resolves published agent versions from the trace database, and a new database has none; `factory publish-check <agent>` publishes one once its evals pass, after which the flag is not needed.

The first review of `mixed` costs about seven cents and exits 1, because the planted defects make it "do not ship". That is the expected result, not a failure of the deployment.

## Where the traces live

In the named volume `agb-agent-factory_factory-data`, mounted at `/app/data`, as `traces.duckdb`. Each run is one row with kind, tokens, cost and decision. The volume survives `docker compose down` and an image upgrade. To read it:

```sh
docker compose run --rm --entrypoint factory factory cost --help        # cost by pipeline run
docker run --rm -v agb-agent-factory_factory-data:/d python:3.14-slim ls -l /d
```

To keep the database in a host directory instead, replace the volume line in `compose.yaml` with `./data:/app/data` and make the directory writable for the image's user: `mkdir data && sudo chown 10001 data`. DuckDB allows one writer at a time, so run one factory process against a database; the API serialises its own requests for that reason.

## Rotate a key

1. Create the new key in the provider's console.
2. Edit `.env` on the host and replace the value. `chmod 600 .env` stays.
3. For a one-shot `run`, the next run reads it. For the API, `docker compose --profile api up -d --force-recreate api`, because a running container keeps the environment it started with.
4. Revoke the old key in the console.
5. Run `python scripts/scan_image.py agb-agent-factory --env ANTHROPIC_API_KEY` from a checkout with the old key exported, to confirm no image ever held it. It prints findings by path and never the value.

The image does not change when a key does.

## Run one review from the host

Repositories are mounted read-only at `/repos`. Point `REVIEW_REPOS` at the directory that holds them (the default is `./repos`), then:

```sh
REVIEW_REPOS=/srv/repos docker compose run --rm factory review /repos/shop \
  --base main --head my-branch --json > review.json
```

stdout is the JSON report and stderr the text report. The exit code is the factory's: 0 ship, 1 do not ship or error, 2 usage, 3 not found, 4 missing or rejected key. Add `--no-triage` without a `TYPESAFE_API_KEY`, `--budget-usd N` to cap the spend (default 5.0), and `--reviewers code-reviewer,security-reviewer` for a subset.

Without Compose: `docker run --rm --env-file .env -v factory-data:/app/data -v /srv/repos:/repos:ro agb-agent-factory review /repos/shop --base main --head my-branch`.

The mount is read-only and git runs inside as another uid, so the image sets `safe.directory '*'` system-wide; reviews only read.

## Import into CasaOS

CasaOS reads a Compose file. In the CasaOS UI, open **App Store**, **Custom Install**, and import `compose.yaml` (paste it or point at the file). Three things to know:

- `env_file: .env` resolves relative to the compose file, so put `.env` next to it on the host, or replace that line with the two `environment:` entries filled in by the importer. Do not paste real keys into a compose file that is stored anywhere else.
- Build the image on the host first (`docker compose build`) or load it with `docker save | docker load`, because the file names a local image and CasaOS does not build from a repository.
- The `factory` service is a one-shot: it prints its help and exits, with `restart: "no"`. Run reviews from a shell as above. The `api` service is behind a profile and stays out of CasaOS unless you start it with `--profile api`.

## Upgrade

```sh
git pull
docker compose build
docker compose --profile api up -d --force-recreate api    # only if you run the API
docker compose run --rm factory --version
```

The volume, and with it every trace, is untouched. CI scans the image of every pull request; to scan the one you built, run `python scripts/scan_image.py agb-agent-factory`. Roll back by checking out the previous tag and building again.

## The HTTP trigger

`docker compose --profile api up -d api` starts a small FastAPI app on `127.0.0.1:8000`. `POST /review` takes the arguments of `factory review` as JSON (`repo_path`, `base`, `head`, `reviewers`, `allow_unpublished`, `no_triage`, `budget_usd`) and returns the report. `GET /healthz` returns `{"status": "ok"}`.

```sh
curl -s localhost:8000/review -H 'content-type: application/json' \
  -d '{"repo_path": "/repos/shop", "base": "main", "head": "my-branch", "no_triage": true}'
```

A review that finishes returns 200 with `{"exit_code": 0 or 1, "report": ...}`, and "do not ship" is exit code 1 inside a 200. A usage error is 422, an unknown repository, ref or agent 404, a missing key 503, any other failure 500 with the factory's error envelope.

**It has no authentication.** Anyone who can reach the port can spend your Anthropic key and read any repository mounted at `/repos`. The compose file therefore publishes it on `127.0.0.1` only. Do not change that to `0.0.0.0` or put it behind a port forward; if it has to be reached from another machine, put an authenticating proxy in front of it first. Requests run one at a time.

## Resource limits

The factory container has `cpus: 2`, `mem_limit: 1g` and `pids_limit: 256`, a read-only root, a 256 MB `/tmp`, no Linux capabilities and `no-new-privileges`. Those are sized for a review panel, which is a few threads waiting on the network. The fix loop's sandbox containers use their own limits, set in `tools/sandbox.py`: 1 CPU, 512 MB and 128 pids each, no network. They are siblings of the factory container, not children, so they are not charged to its limits; budget the host for the factory plus one sandbox.

## Sandbox containers

The fix loop (`factory fix`, #24) runs every model-written command and the test suite in a throwaway `docker run`. The reviews this deployment serves do not, so **the default image has no Docker CLI and the container has no Docker socket**, and `factory fix` run from it exits 4 because it cannot reach Docker.

To run the fix loop from this image, the decision of [ADR 0001](decisions/0001-fixer-runtime.md) stands: sibling containers started through the host's socket, not Docker in Docker. To turn it on:

1. Build with `WITH_DOCKER_CLI: "1"` in `compose.yaml` (or `--build-arg WITH_DOCKER_CLI=1`) to add the `docker` client.
2. Uncomment the socket mount in `compose.yaml`.
3. Mount the work tree at the same path inside and outside the container, because `docker run -v` resolves a path on the host, not in the container. `factory fix` clones the repository into a temporary directory and mounts the clone, so pass `--work-dir` a directory mounted at the same path on both sides; the default, the container's `/tmp`, is a tmpfs the host cannot see.

Mounting `/var/run/docker.sock` gives the factory container root-equivalent control of the host: anything that can run a command in it can start a privileged container. That is why it is off, and why it should stay off on any host where the factory reads repositories you do not trust. The factory's own container still drops its capabilities and runs as uid 10001, but the socket makes that moot.

### The sandbox image has only the standard library and pytest

`factory-sandbox:0.1` (`tools/sandbox.Dockerfile`) is `python:3.12-slim` with `git`, `pytest` and `uv`, and the sandbox has no network. A target repository whose tests import anything else cannot be fixed in it. For such a repository, build a derived image on the host, installing the dependencies from the target's lockfile at build time, and tag it over the name the sandbox uses:

```dockerfile
FROM factory-sandbox:0.1
COPY uv.lock pyproject.toml /deps/
RUN cd /deps && uv export --frozen --no-emit-project -o req.txt && uv pip install --system -r req.txt
```

Or tag it under a name of its own and set `FACTORY_SANDBOX_IMAGE` to that name, in `.env` for the compose service or in the environment of one `factory fix` run; unset or blank, the sandbox uses `factory-sandbox:0.1`, so this deployment works unchanged. The host's Docker daemon resolves the name, so the image must be on the host, not in the factory container.

## Checks

CI builds the image on every pull request, runs `scripts/scan_image.py` over its layers and configuration for `sk-ant-` and `whk_` strings, runs `--version` in it, and proves the scan fails by planting a key in a derived image. The scan needs no secret. It lists one exception by path: the `hardcoded-secret` fixture holds a made-up `whk_` token on purpose, and that single file is counted and ignored. Any other match fails the job.
