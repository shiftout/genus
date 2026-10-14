# Deploying genus

Same shape as hoi, dentro and memento: a self-hosted GitHub Actions runner on the
NAS builds the image in place, behind the shared `cloudflared` tunnel.

| | |
|---|---|
| Repo | `shiftout/genus` (private) |
| Host port | none (reachable only through the tunnel; add `8423:8000` to debug) |
| Container | `genus`, internal port 8000 |
| Hostname | `genus.mysynnefo.com` |
| Network | `memento-edge` (external) |
| Data | `/share/Container/genus` → `/data` (`words.db`, `cache.db`) |
| Secrets | `/share/Container/genus/.env` on the NAS, never in git |
| Auth | Cloudflare Access (email OTP) on `genus.mysynnefo.com` |

## Checklist

- [ ] 1. Create the repo `shiftout/genus` (private) and push
- [ ] 2. Create `/share/Container/genus` on the NAS, copy `words.db` and create `.env`
- [ ] 3. Add the tunnel public hostname `genus.mysynnefo.com → genus:8000`
- [ ] 4. Add the Cloudflare Access application for `genus.mysynnefo.com`
- [ ] 5. Deploy the `genus-runner` container in Container Station
- [ ] 6. Re-run the queued deploy job (or push anything)
- [ ] 7. `docker network connect memento-edge sweet_bohr` if it 502s
- [ ] 8. Verify (below)

## One-time setup

### 1. Repo

```bash
cd ~/Developer/genus
git add -A && git commit -m "Initial commit"
gh repo create shiftout/genus --private --source . --push
```

`data/` and `raw/` are gitignored, so the dictionary is not in the repo.

### 2. Data directory, dictionary and secrets

`words.db` (~45 MB) is built on a Mac (the source dumps are ~4.5 GB) and copied over:

```bash
.venv/bin/python scripts/build_db.py --download     # once; writes data/words.db
ssh <nas> 'mkdir -p /share/Container/genus'
scp data/words.db <nas>:/share/Container/genus/words.db
```

Then on the NAS create `/share/Container/genus/.env` (the file must exist, even
if empty, or compose refuses to start):

```
GEMINI_API_KEY=<key from Google AI Studio>
GEMINI_MODEL=gemini-2.5-flash
LLM_DAILY_CAP=100
```

Without a key the app is dictionary-only. `cache.db` is created by the app next
to `words.db`.

**Rotating the key:** create a new key, edit `.env`, then
`docker compose up -d --force-recreate genus` (or re-run the deploy), then delete
the old key in AI Studio.

**Updating the dictionary:** rebuild on the Mac, `scp` the new `words.db`,
`docker restart genus`.

### 3. Tunnel hostname

Zero Trust → Networks → Tunnels → your tunnel → Public Hostname → Add:

- Subdomain `genus`, domain `mysynnefo.com`
- Type `HTTP`, URL `genus:8000`

The origin is the **container name**, resolved over the shared Docker network.

### 4. Cloudflare Access

Zero Trust → Access → Applications → Add → Self-hosted:

- Domain `genus.mysynnefo.com`
- Policy: Allow, include your email address(es), login method One-time PIN
- Set the session duration to the longest allowed

This is the only thing protecting the Gemini quota (as well as the app's own rate
limit and the daily cap), so do it before sharing the URL. Unlike dentro and
memento, genus does not validate the Access JWT itself; it relies on being
unreachable except through the tunnel, which is why no host port is published.

### 5. Attach cloudflared to the shared network

The tunnel container (`sweet_bohr`) is not compose-managed, so its network
membership does not survive being recreated:

```bash
docker network connect memento-edge sweet_bohr
```

If `genus.mysynnefo.com` returns 502, check this first:

```bash
docker inspect sweet_bohr --format '{{json .NetworkSettings.Networks}}' | tr ',' '\n'
```

### 6. Self-hosted runner

One runner per repo. Container Station → Applications → Create, name it
`genus-runner`, paste the same compose as hoi's runner with the repo changed:

```yaml
services:
  github-runner:
    image: myoung34/github-runner:latest
    restart: unless-stopped
    environment:
      REPO_URL: https://github.com/shiftout/genus
      ACCESS_TOKEN: <your-PAT>
      RUNNER_NAME: qnap-nas-genus
      RUNNER_WORKDIR: /runner/_work
      LABELS: self-hosted
      CONFIGURED_ACTIONS_RUNNER_FILES_DIR: /runner-config
      DISABLE_AUTOMATIC_DEREGISTRATION: "true"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - runner_work:/runner/_work
      - runner_config:/runner-config
volumes:
  runner_work:
  runner_config:
```

(The last three settings stop this image crash-looping; see `hoi/DEPLOY.md` and
`dentro/DEPLOY.md` for why.) Verify at repo → Settings → Actions → Runners:
`qnap-nas-genus` should go green "Idle".

## Deploying

Push to `main`. CI runs pytest on a GitHub-hosted runner, then the `deploy` job on
the NAS runs `docker compose up --build -d --remove-orphans`.

Manually: `cd /path/to/genus && docker compose up --build -d`.

## Verifying

```bash
docker exec genus python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8000/healthz').read())"
docker logs genus --tail 50
curl -s -o /dev/null -w '%{http_code}\n' https://genus.mysynnefo.com/healthz   # Access login (302/403), not 200
```

Then, logged in via the browser: type `dentro` → `der Baum`, `δέντρο` → `der Baum`,
`Baun` → suggestions including `Baum`.
