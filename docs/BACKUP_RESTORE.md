# Backup and disaster recovery

Everything here runs on the production host from the repository root.

| | |
|---|---|
| **What is backed up** | The whole `whatsapp_ai_bot` database: customers, conversations, messages, AI logs, the ingested knowledge base, model pricing, `alembic_version` |
| **What is not** | Redis (see [What is deliberately not backed up](#what-is-deliberately-not-backed-up)), the `knowledge/` markdown on disk, TLS certificates, Docker secrets |
| **Where** | The `backups` Docker volume, mounted at `/backups` in the `backup` service |
| **Schedule** | Daily at 02:00 UTC |
| **Retention** | 14 daily, 8 weekly, 12 monthly |
| **RPO** (worst-case data loss) | 24 hours |
| **RTO** (time to restore) | ~5 minutes for a typical database |

---

## The five commands

```bash
./scripts/restore.sh --list              # what can I restore from?
./scripts/restore.sh --verify latest     # rehearsal, touches nothing
./scripts/restore.sh latest              # restore the newest backup
./scripts/restore.sh daily/whatsapp_ai_bot-20260731T0200Z.dump
docker compose -f docker-compose.prod.yml exec backup /scripts/backup.sh   # take one now
```

---

## How it works

### Layout

```
/backups/
  daily/     whatsapp_ai_bot-20260731T0200Z.dump  + .sha256
  weekly/    hard links to the Sunday daily
  monthly/   hard links to the 1st-of-month daily
  pre-restore/   safety dumps taken before a restore, never pruned
  state/
    last_success       unix timestamp, read by the healthcheck
    last_result.json   outcome of the most recent run
    last_verified.json outcome of the most recent restore drill
    last_drill         unix timestamp of the last drill
    backup.log
```

Weekly and monthly entries are **hard links**, not copies. The same bytes
appear in each tier and each tier prunes on its own clock; the data survives
until the last link to it is removed. A monthly backup therefore costs an
inode, not another full dump.

### Format

`pg_dump --format=custom --compress=9`. Custom format rather than plain SQL
because it can be listed without unpacking, restored selectively, and is
compressed in-stream.

### Verification at write time

Every dump is checked before it is accepted:

1. It is written to `.partial` and only renamed after it passes — a truncated
   file from a full disk never appears in `daily/` looking like a real backup.
2. `pg_restore --list` must parse it.
3. The listing must contain `users`, `conversations` and `messages`. A dump of
   the wrong database, or one taken before migrations ran, parses perfectly
   and is worthless.
4. It must be larger than 2 KB.
5. A `.sha256` is written alongside it, checked before any restore.

If any check fails the file is deleted, `last_result.json` records the error,
and `last_success` is deliberately **not** touched.

### Restore drill

Weekly, the scheduler runs `verify-restore.sh latest`, which:

- verifies the checksum,
- creates a scratch database on the same server,
- restores the newest dump into it,
- counts rows in `users`, `conversations`, `messages`, `documents`,
  `document_chunks`,
- reads `alembic_version`,
- drops the scratch database.

This is the difference between having backups and knowing they work. Run it by
hand against a specific file before you rely on it:

```bash
docker compose -f docker-compose.prod.yml exec backup \
  /scripts/verify-restore.sh monthly/whatsapp_ai_bot-20260701T0200Z.dump
```

### Health monitoring

The `backup` service's Docker healthcheck does not check that the process is
alive — it checks the **age of the last success**. A backup container running
happily while producing nothing for three days is the failure that matters,
and a liveness check cannot see it.

- Unhealthy once the newest success is older than `BACKUP_MAX_AGE_HOURS`
  (default 30 — one missed nightly run is tolerated, two is not).
- A fresh deployment gets a `BACKUP_GRACE_HOURS` window (default 26) before
  the absence of any backup counts as a failure.

```bash
docker compose -f docker-compose.prod.yml ps backup
docker inspect --format '{{.State.Health.Status}}' "$(docker compose -f docker-compose.prod.yml ps -q backup)"
```

---

## Recovery procedures

### 1. Restore the most recent backup

The ordinary case: bad data, a mistaken bulk delete, a corrupted table.

```bash
./scripts/restore.sh latest
```

The script:

1. Resolves the file and verifies its checksum **before stopping anything**.
2. Asks you to type the database name.
3. Stops `app` and `worker`. `nginx`, `db` and `redis` stay up.
4. Takes a safety dump of the current state into `/backups/pre-restore/`.
5. Restores with `--clean --if-exists --single-transaction`, so a failure
   halfway rolls back rather than leaving half a schema.
6. Prints row counts and the schema revision.
7. Runs `migrate` in case the dump predates the running image.
8. Starts `app` and `worker` and waits for `/health/ready`.

**Data loss window:** everything written since the backup was taken. Meta
retries webhook deliveries for a while, so some in-flight customer messages
redeliver on their own once the app is back.

### 2. Restore a specific older backup

```bash
./scripts/restore.sh --list
./scripts/restore.sh weekly/whatsapp_ai_bot-20260727T0200Z.dump
```

Verify it first if you are unsure it is good:

```bash
./scripts/restore.sh --verify weekly/whatsapp_ai_bot-20260727T0200Z.dump
```

### 3. Undo a restore

Every restore writes a safety dump first, and its path is printed at the end.

```bash
./scripts/restore.sh /backups/pre-restore/whatsapp_ai_bot-before-restore-20260731T0312Z.dump
```

These are never pruned. Delete them by hand once you are satisfied.

### 4. Total host loss — rebuild from nothing

The backups live in a Docker volume on the same host, so **this only works if
you have been copying them off the box.** See
[Off-site copies](#off-site-copies) — without that step, losing the host loses
the backups too.

On the new host:

```bash
# 1. Code and secrets
git clone https://github.com/mohamedshhahat1/whatsapp-ai-bot.git
cd whatsapp-ai-bot
./scripts/init-secrets.sh          # restore the same secret values

# 2. TLS
DOMAIN=bot.example.com CERTBOT_EMAIL=ops@example.com ./scripts/init-letsencrypt.sh

# 3. Bring up the database and the backup service only
docker compose -f docker-compose.prod.yml up -d db redis backup

# 4. Copy the off-site archive into the volume
docker compose -f docker-compose.prod.yml cp \
  ./whatsapp_ai_bot-20260731T0200Z.dump backup:/backups/daily/
docker compose -f docker-compose.prod.yml exec backup sh -c \
  'cd /backups/daily && sha256sum *.dump > /dev/null'

# 5. Rehearse, then restore
./scripts/restore.sh --verify latest
docker compose -f docker-compose.prod.yml up -d
./scripts/restore.sh latest

# 6. Re-ingest the knowledge base (it is in the dump, but re-running is safe
#    and confirms the markdown on disk matches what is in the database)
docker compose -f docker-compose.prod.yml exec app python scripts/ingest_knowledge.py

# 7. Repoint DNS, then re-verify the webhook callback in the Meta dashboard
```

### 5. The database is up but the data is wrong

Do not restore first. Take a dump of the current state so the evidence
survives, then investigate:

```bash
docker compose -f docker-compose.prod.yml exec backup /scripts/backup.sh
```

---

## Off-site copies

**This is not automated, and it is the largest remaining gap in the recovery
story.** The `backups` volume lives on the same host as the database it
protects. It survives `docker compose down -v`, a dropped table and a corrupt
page. It does not survive the host: a failed disk, a deleted VM or a ransomed
server takes the database and every backup of it at the same moment.

Pull them somewhere else, from a machine that is not the production host:

```bash
# rsync over ssh, nightly, from a backup host
rsync -avz --delete \
  prod:/var/lib/docker/volumes/whatsapp-ai-bot_backups/_data/monthly/ \
  /srv/offsite/whatsapp-ai-bot/monthly/
```

Or push to object storage from the host:

```bash
docker compose -f docker-compose.prod.yml exec -T backup \
  sh -c 'cat $(ls -1t /backups/daily/*.dump | head -n 1)' \
  | aws s3 cp - "s3://your-bucket/whatsapp-ai-bot/$(date -u +%F).dump"
```

Pull is safer than push: a compromised production host cannot delete backups
it has no credentials to reach.

---

## What is deliberately not backed up

**Redis.** It holds the Celery queue, rate-limit counters, the reply
idempotency cache and the dead-letter list. All of it is either
reconstructable or intentionally short-lived, and restoring a stale queue
would replay old webhook deliveries against a restored database — the one
thing worse than losing them. Redis keeps AOF persistence so it survives a
restart; it is not part of disaster recovery.

**The `knowledge/` markdown.** It is in git, and mounted read-only from the
host. The ingested chunks and embeddings *are* in the dump.

**TLS certificates.** Let's Encrypt reissues in seconds;
`init-letsencrypt.sh` handles it.

**Docker secrets.** `./secrets/` is not in the repository and is not in the
dump. Store them in your password manager. Losing them means new API
credentials from Meta and OpenAI, and every operator's dashboard key rotating.

---

## Configuration

All set on the `backup` service in `docker-compose.prod.yml`.

| Variable | Default | Meaning |
|---|---|---|
| `BACKUP_HOUR` | `2` | UTC hour of the daily run |
| `RETAIN_DAILY` | `14` | Daily backups kept |
| `RETAIN_WEEKLY` | `8` | Weekly backups kept |
| `RETAIN_MONTHLY` | `12` | Monthly backups kept |
| `RESTORE_DRILL_ENABLED` | `true` | Run the weekly restore rehearsal |
| `RESTORE_DRILL_DAYS` | `7` | Days between drills |
| `BACKUP_MAX_AGE_HOURS` | `30` | Healthcheck goes red past this |
| `BACKUP_GRACE_HOURS` | `26` | Grace period on a fresh deployment |
| `BACKUP_MIN_BYTES` | `2048` | Dumps smaller than this are rejected |

Retention prunes by **count, not age**. An age rule silently empties the
directory if the scheduler has been down, and the point of retention is to
still have something after a bad week.

---

## Testing this before you need it

Do this once, now, on a staging host — not during an incident.

```bash
# 1. Take a backup
docker compose -f docker-compose.prod.yml exec backup /scripts/backup.sh

# 2. Confirm it is restorable
./scripts/restore.sh --verify latest

# 3. Break something on purpose
docker compose -f docker-compose.prod.yml exec db \
  psql -U postgres -d whatsapp_ai_bot -c 'DELETE FROM messages;'

# 4. Restore, and confirm the messages are back
./scripts/restore.sh latest
```

If step 4 does not bring the rows back, you do not have backups — you have
files.
