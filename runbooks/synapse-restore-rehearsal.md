# Rehearse encrypted Synapse recovery

Use the fixture to verify the recovery of a Synapse homeserver with PostgreSQL. It creates a disposable homeserver with a synthetic room, message and attachment. Both image references match those recorded on Dev. The fixture runs on isolated local Docker networks without publishing host ports.

## Run and record the rehearsal

Use a new result path under a private directory. Python 3, `age`, `age-keygen` and the exact locally cached container images are required.

```bash
evidence_root=$(mktemp -d)
python3 scripts/rehearse_synapse_restore.py "$evidence_root/result"
```

## Recovery set

The fixture captures the PostgreSQL custom-format dump and the Synapse media store and signing key while writes from Synapse are stopped. It preserves the server name across both test instances. The script encrypts both archives with an ephemeral age recipient, verifies their checksums and restores them after removing the source. The final check opens the room and reads the message and media through the restored homeserver. It also verifies the public signing-key fingerprint. The database dump excludes one-time encryption-key rows and keeps the table schema. The restore checks that the table is empty before starting Synapse.

The result contains `synapse-postgres.dump.age`, `synapse-media-signing.tar.age` and `receipt.json`. Use the receipt to record image revisions, archive checksums, the elapsed restore time and which checks passed. The ephemeral recovery identity is discarded before the result is published. Keep application content, recovery keys and passwords out of the audit record.

## Scheduled backup acceptance

- Record the actual homeserver configuration, signing-key custody and media alongside the PostgreSQL backup. Confirm that they belong to the same recoverable point. [Synapse backup documentation](https://element-hq.github.io/synapse/latest/usage/administration/backups.html)
- Agree which database roles and other cluster-wide settings need separate recovery. A PostgreSQL database dump covers the selected database. [PostgreSQL 15 backup documentation](https://www.postgresql.org/docs/15/backup-dump.html)
- Verify the scheduled encrypted off-site copy, the retention and recovery-key custody, and a restore of approved application data in an isolated target. Record those checks in [Helm #282](https://github.com/OpenResilienceInitiative/ORISO-Helm/issues/282).
