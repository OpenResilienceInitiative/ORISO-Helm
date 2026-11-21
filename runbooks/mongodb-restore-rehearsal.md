# Rehearse encrypted MongoDB recovery

Use the [rehearsal script](../scripts/rehearse_mongodb_restore.py) with a local Docker daemon that supports `docker image inspect --platform`, Python 3, `age`, `age-keygen` and the MongoDB image already available locally. The Helm chart defaults to MongoDB 5.0. Record the image ID from the rehearsal and confirm the installed image separately before applying the result to an environment.

The script creates two disposable MongoDB containers with networking disabled and no host ports. It seeds a synthetic `consulting_types` database and an unrelated database. It writes the compressed dump stream straight into `age`, removes the source, and restores the encrypted archive into a fresh container. It compares the original BSON values and indexes, and checks that the unrelated database is absent.

## Run and record the rehearsal

Use an output path that does not exist inside a private directory. Keep the resulting encrypted fixture and its receipt only as long as needed to record the result. The script generates and discards its temporary recovery identities.

Run from the repository root after confirming the MongoDB image is present locally.

```bash
evidence_root=$(mktemp -d)
python3 scripts/rehearse_mongodb_restore.py "$evidence_root/result"
```

Use `--image` and `--platform` when rehearsing a different installed image or platform. Keep the source and target on the same image. The rehearsal uses the local image cache.

A passing run verifies that an unsuccessful dump publishes no backup, a different recovery identity cannot authenticate the archive, and a changed copy fails the expected checksum before restore. Record the image, server and client versions, archive checksum, duration and result from the receipt. The archive contains synthetic records.

## Accept the scheduled backup separately

- Confirm the databases in scope and the database operator’s consistency method while writes are active. A scoped fixture dump verifies the recovery format with no concurrent writes. MongoDB’s oplog option requires a full replica-set dump. [MongoDB dump options](https://www.mongodb.com/docs/database-tools/mongodump/#std-option-mongodump.--oplog)
- Confirm the scheduler, off-site destination, recovery-key custody and retention under [Helm #282](https://github.com/OpenResilienceInitiative/ORISO-Helm/issues/282).
- Record a successful scheduled encrypted backup and an authorized restore of application data into an isolated target. Compare the agreed database invariants and record the restore duration.
