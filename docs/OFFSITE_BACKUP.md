# Off-Site Backups

Backups that live on the same server as the database are not backups. They
protect against `DROP TABLE`. They do not protect against the disk failing,
the provider suspending the account, ransomware, or somebody running
`docker compose down -v` on the wrong terminal. Every one of those takes the
database and the backups together.

This system replicates every verified backup to object storage in a different
failure domain, encrypted, checksummed, and provably restorable.

---

## How it fits together

```
02:00  backup.sh
         |-- pg_dump --format=custom --compress=9   (local, verified)
         |-- promote to weekly/monthly tiers        (hard links)
         |-- prune local tiers by count
         |-- write state/last_success
         |
         +-- backup-upload.sh          <-- off-site replication
               |-- encrypt (age or OpenSSL AES-256)
               |-- sha256 over the CIPHERTEXT
               |-- storage_put via lib/storage.sh
               |-- read-back verify (object is listable)
               +-- prune remote tiers by count

03:00  backup-verify-remote.sh   downloads + checksums the newest object
weekly restore-drill.sh          proves a backup actually restores
always backup-metrics.sh         exports state as Prometheus metrics
```

The upload runs **after** the local backup is marked successful, and an upload
failure does **not** fail the backup. A local dump that verified is a real
backup even when the network is down; conflating the two would throw away a
good dump because of a DNS blip. Off-site failure has its own state file and
its own alert (`OffsiteUploadMissing`).

---

## Choosing a provider

Set `BACKUP_REMOTE_PROVIDER` to one of `s3`, `b2`, `gcs`, `azure`, or `none`.
The default is `none`, which disables off-site replication entirely and logs a
warning on every run.

All four providers implement the same four verbs (`put`, `get`, `list`,
`delete`) in `scripts/lib/storage.sh`. Nothing downstream branches on the
provider, so the restore path is identical whichever you pick. That matters:
the day you need a restore is not the day to discover the code was only ever
exercised against one backend.

### AWS S3

```bash
BACKUP_REMOTE_PROVIDER=s3
BACKUP_REMOTE_BUCKET=my-company-wa-backups
BACKUP_S3_REGION=eu-west-1
BACKUP_S3_ACCESS_KEY_ID_FILE=/run/secrets/backup_s3_key_id
BACKUP_S3_SECRET_ACCESS_KEY_FILE=/run/secrets/backup_s3_secret
BACKUP_S3_STORAGE_CLASS=STANDARD_IA
```

`STANDARD_IA` is a good default: backups are written once and read almost
never, which is exactly the access pattern infrequent-access tiers are priced
for. Do **not** use Glacier or Deep Archive for the daily tier -- restores take
hours, and the restore you need urgently is always the most recent one.

### Backblaze B2

```bash
BACKUP_REMOTE_PROVIDER=b2
BACKUP_REMOTE_BUCKET=my-company-wa-backups
BACKUP_S3_REGION=us-west-004
BACKUP_S3_ACCESS_KEY_ID_FILE=/run/secrets/backup_s3_key_id
BACKUP_S3_SECRET_ACCESS_KEY_FILE=/run/secrets/backup_s3_secret
```

B2 goes through its **S3-compatible API**, not the native `b2` CLI. One code
path instead of two, and the credentials keep the same shape. The endpoint is
derived from the region automatically; override `BACKUP_S3_ENDPOINT` only if
Backblaze changes the pattern.

Use the *keyID* as the access key and the *applicationKey* as the secret. Scope
the application key to the single bucket.

### Google Cloud Storage

```bash
BACKUP_REMOTE_PROVIDER=gcs
BACKUP_REMOTE_BUCKET=my-company-wa-backups
BACKUP_GCS_CREDENTIALS_FILE=/run/secrets/backup_gcs_key.json
BACKUP_GCS_PROJECT=my-project-id
```

The service account needs `roles/storage.objectAdmin` on that bucket only.
`objectAdmin` rather than `objectCreator` because retention pruning has to
delete.

### Azure Blob Storage

```bash
BACKUP_REMOTE_PROVIDER=azure
BACKUP_REMOTE_BUCKET=wa-backups          # the CONTAINER name
BACKUP_AZURE_ACCOUNT=mystorageaccount
BACKUP_AZURE_SAS_TOKEN_FILE=/run/secrets/backup_azure_sas
```

A SAS token is preferred over an account key: it can be scoped to one container
and expired independently. `BACKUP_AZURE_ACCOUNT_KEY` is supported as a
fallback but grants access to the entire storage account.

**Set a calendar reminder for the SAS expiry.** An expired token fails every
upload, and the only thing standing between that and silent data loss is the
`OffsiteUploadMissing` alert.

---

## Encryption

Encryption is **mandatory**. `backup-upload.sh` refuses to upload plaintext and
exits non-zero rather than falling back. This is not configurable.

The reasoning: off-site means the bytes now live somewhere you do not control,
on hardware you cannot inspect, under an account that can be compromised
independently of your server. A Postgres dump here contains every customer
phone number and the full text of every message they ever sent. A leaked
bucket would be a reportable data breach.

Two mechanisms, in order of preference:

**age** (preferred)

```bash
age-keygen -o backup-identity.txt          # keep this OFF the server
grep 'public key' backup-identity.txt      # -> age1...

BACKUP_AGE_RECIPIENT=age1qz...
```

Public-key encryption, so the **server never holds the key that can decrypt
its own backups**. An attacker who fully owns the server still cannot read the
archive. Restoring requires the identity file, which lives in your password
manager.

**OpenSSL passphrase** (fallback)

```bash
BACKUP_ENCRYPTION_PASSPHRASE_FILE=/run/secrets/backup_passphrase
```

AES-256-CBC with PBKDF2 at 200,000 iterations. Works everywhere with no extra
binary, but the server holds the key, so an attacker who owns the server can
decrypt the backups.

> **Store the key somewhere other than the server it protects.** A key that
> only exists on the machine you are recovering from is not a key, it is a
> souvenir. Losing it means the backups are permanently unreadable -- there is
> no recovery path and no support ticket that fixes it.

The SHA-256 checksum is computed over the **ciphertext**, because the
ciphertext is what has to survive the round trip. A plaintext checksum would
only match after a corrupted upload by coincidence.

---

## Retention

| Tier | Local default | Remote default | Variable |
|---|---|---|---|
| Daily | 14 | 30 | `RETAIN_DAILY` / `RETAIN_REMOTE_DAILY` |
| Weekly | 8 | 12 | `RETAIN_WEEKLY` / `RETAIN_REMOTE_WEEKLY` |
| Monthly | 12 | 24 | `RETAIN_MONTHLY` / `RETAIN_REMOTE_MONTHLY` |

Remote retention is longer than local because remote storage is cheap and the
scenario it covers -- discovering corruption weeks after it happened -- needs
depth rather than freshness.

Pruning is **by count, newest-first**, never by age. An age-based rule empties
the bucket after any outage long enough to stop backups, which is precisely
when the older copies are the only ones you have left.

---

## Restoring

### From off-site, into a scratch database first

```bash
# Fetch and decrypt the newest daily backup.
docker compose -f docker-compose.prod.yml exec backup \
  /app/scripts/backup-download.sh latest
# -> /backups/restore/whatsapp_ai_bot-20260731T0200Z.dump
```

`backup-download.sh` verifies the checksum before decrypting, then confirms the
result parses as a `pg_dump` archive. If either fails it says so plainly rather
than handing a corrupt file to `pg_restore` and letting it die halfway through
a table.

Other tiers and specific objects:

```bash
./scripts/backup-download.sh latest --tier monthly
./scripts/backup-download.sh daily/whatsapp_ai_bot-20260701T0200Z.dump.enc
./scripts/backup-download.sh latest --output /tmp/check.dump
```

### Into production

Use `scripts/restore.sh`, which handles stopping the application, restoring,
and running migrations. **Restore into a scratch database and inspect it before
you touch production.** `restore-drill.sh` does exactly this automatically, and
reading its most recent report is the fastest way to know whether a restore
will work before you need it to.

---

## Verification

Three independent layers, because each catches something the others cannot:

| Layer | What it proves | When |
|---|---|---|
| `backup.sh` inline checks | The dump parses, contains the expected tables, and is above a size floor | Every backup |
| `backup-verify-remote.sh` | The remote bytes download and match their checksum | Daily |
| `restore-drill.sh` | A backup actually restores into a clean database and the app boots against it | Weekly |

The second layer matters more than it looks. Listing a bucket proves nothing:
*"the upload job has been writing zero-byte objects for three weeks"* is a real
and common failure that a listing reports as perfectly healthy. Only
downloading the bytes catches it.

```bash
./scripts/backup-verify-remote.sh              # newest daily
./scripts/backup-verify-remote.sh --all        # every object, every tier
./scripts/backup-verify-remote.sh --tier monthly
```

---

## Health checks and metrics

`backup-metrics.sh` renders the state directory as Prometheus
textfile-collector metrics:

| Metric | Meaning |
|---|---|
| `backup_last_status` | 1 if the last local backup succeeded |
| `backup_last_success_timestamp_seconds` | When the last good backup finished |
| `backup_last_size_bytes` | Size of the newest dump |
| `backup_local_bytes_total` | Disk consumed by all local tiers |
| `backup_files_count{tier=...}` | Retained files per tier |
| `backup_offsite_last_status` | 1 if the last upload succeeded |
| `backup_last_offsite_upload_timestamp_seconds` | When bytes last reached off-site storage |
| `backup_remote_verify_status` | 1 if the last remote verification passed |
| `restore_drill_status` | 1 if the last restore drill passed |
| `restore_drill_last_success_timestamp_seconds` | When a restore was last *proven* |

Alerting on these is covered in `docs/ALERTING.md`. The important ones are
age-based rather than liveness-based: a backup container running happily while
producing nothing is the failure that actually happens, and liveness cannot
see it.

---

## What this does not protect against

Stated plainly, because a false sense of coverage is its own risk:

- **Logical corruption replicated before anyone notices.** If a bug corrupts
  data on Monday and nobody notices until Friday, the daily tier may hold five
  corrupted copies. The weekly and monthly tiers exist for this; retention
  depth is your only defence.
- **Losing the encryption key.** Unrecoverable. No support ticket fixes it.
- **A compromised server with a passphrase-based key.** An attacker who owns
  the server can decrypt the backups. Use `age` if this is in your threat
  model.
- **Point-in-time recovery.** These are nightly snapshots, not WAL archiving.
  Worst-case data loss is roughly 24 hours. If that is unacceptable, add
  continuous WAL shipping -- this system does not do it.
- **Provider account compromise.** Credentials scoped to one bucket limit the
  blast radius, but a stolen key can still delete the bucket. Enable object
  versioning and a delete-protection or object-lock policy at the provider if
  your threat model includes this.
