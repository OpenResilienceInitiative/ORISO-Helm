# Pre-Dev ALTER Runbook: AVV/Legal schema (2026-07-03)

Since 2026-06-30 this repo (ORISO-Helm, `charts/mariadb/sql-schemas/`) is the
canonical home of the MariaDB schema mirrors (ORISO-Database is archived).
This runbook lives next to the mirrors it keeps in sync with.

## Why this is needed

The Pre-Dev cluster runs the services with `SPRING_LIQUIBASE_ENABLED=false`
(forced via configmap). Deploying a new image therefore does NOT run the
Liquibase changesets that are merged on the service `dev` branches. The
AVV/Legal backend (TenantService changesets 0013-0019, AgencyService
changesets 0019-0022) expects tables and columns that the Pre-Dev databases
do not have yet, so the new DPA/DPP endpoints fail with
`Unknown column ...` / missing table errors and return HTTP 500.

This runbook applies the missing schema manually. Every statement is
idempotent (`ADD COLUMN IF NOT EXISTS`, `CREATE TABLE IF NOT EXISTS`,
`CREATE SEQUENCE IF NOT EXISTS`) — the scripts are safe to re-run and
contain NO destructive statements.

How to run (Pre-Dev): open a MariaDB session on the cluster, e.g.

```
kubectl exec -it <mariadb-pod> -n <namespace> -- mariadb -u root -p
```

then paste the script for each database.

## 1. tenantservice database

Source of truth: `ORISO-TenantService/src/main/resources/db/changelog/changeset/`
0013_tenant_admin_controls, 0014_add_tenant_address_description,
0015_add_tenant_dpa, 0016_tenant_dpa_signature,
0017_tenant_dpa_signature_token, 0018_tenant_dpa_version,
0019_tenant_dpa_signature_audit_fields.

Note (2026-07-04): the DPA sequence names here are lowercase on purpose — Hibernate
requests the lowercase names at runtime; TenantService changeset 0020 renames any
UPPERCASE leftovers on Liquibase-managed databases (TenantService PR #62).

```sql
-- 0013: tenant admin controls
CREATE SEQUENCE IF NOT EXISTS `tenantservice`.`sequence_tenant_admin_controls`
  INCREMENT BY 1 MINVALUE 1 NOMAXVALUE START WITH 1 CACHE 0;

CREATE TABLE IF NOT EXISTS `tenantservice`.`tenant_admin_controls` (
  id BIGINT NOT NULL,
  controls LONGTEXT NOT NULL,
  update_date DATETIME NOT NULL DEFAULT (UTC_TIMESTAMP),
  PRIMARY KEY (id)
);

-- 0014: tenant address + description (usually already present on Pre-Dev; no-op then)
ALTER TABLE `tenantservice`.`tenant` ADD COLUMN IF NOT EXISTS address VARCHAR(255) NULL;
ALTER TABLE `tenantservice`.`tenant` ADD COLUMN IF NOT EXISTS description TEXT NULL;

-- 0015: DPA content + activation date on tenant
ALTER TABLE `tenantservice`.`tenant` ADD COLUMN IF NOT EXISTS content_dpa LONGTEXT NULL;
ALTER TABLE `tenantservice`.`tenant` ADD COLUMN IF NOT EXISTS dpa_activation_date DATETIME NULL;

-- 0016: DPA signature table + sequence
CREATE SEQUENCE IF NOT EXISTS `tenantservice`.`sequence_tenant_dpa_signature`
  START WITH 100000 INCREMENT BY 1;

CREATE TABLE IF NOT EXISTS `tenantservice`.`tenant_dpa_signature` (
    id bigint NOT NULL,
    tenant_id bigint NOT NULL,
    dpa_version datetime NULL,
    signer_name varchar(255) NULL,
    signer_position varchar(255) NULL,
    signer_is_member tinyint(1) NULL,
    lang varchar(10) NULL,
    signature_status varchar(20) NOT NULL DEFAULT 'PENDING',
    signed_at datetime NULL,
    create_date datetime NOT NULL,
    PRIMARY KEY (id),
    KEY idx_tenant_dpa_signature_tenant (tenant_id),
    CONSTRAINT fk_tenant_dpa_signature_tenant FOREIGN KEY (tenant_id)
        REFERENCES `tenantservice`.`tenant` (id) ON DELETE CASCADE
);

-- 0017: single-use sign-link token columns
ALTER TABLE `tenantservice`.`tenant_dpa_signature` ADD COLUMN IF NOT EXISTS token_hash varchar(64) NULL;
ALTER TABLE `tenantservice`.`tenant_dpa_signature` ADD COLUMN IF NOT EXISTS token_expires_at datetime NULL;

-- 0018: DPA version history table + sequence
CREATE SEQUENCE IF NOT EXISTS `tenantservice`.`sequence_tenant_dpa_version`
  START WITH 100000 INCREMENT BY 1;

CREATE TABLE IF NOT EXISTS `tenantservice`.`tenant_dpa_version` (
    id bigint NOT NULL,
    tenant_id bigint NOT NULL,
    content longtext NULL,
    activation_date datetime NOT NULL,
    create_date datetime NOT NULL,
    PRIMARY KEY (id),
    KEY idx_tenant_dpa_version_tenant (tenant_id),
    CONSTRAINT fk_tenant_dpa_version_tenant FOREIGN KEY (tenant_id)
        REFERENCES `tenantservice`.`tenant` (id) ON DELETE CASCADE
);

-- 0019: DPA signature audit fields (merged 2026-07-03, TenantService PR #52)
ALTER TABLE `tenantservice`.`tenant_dpa_signature` ADD COLUMN IF NOT EXISTS signer_email varchar(255) NULL;
ALTER TABLE `tenantservice`.`tenant_dpa_signature` ADD COLUMN IF NOT EXISTS signer_organisation varchar(255) NULL;
ALTER TABLE `tenantservice`.`tenant_dpa_signature` ADD COLUMN IF NOT EXISTS forwarded_by_user_id varchar(64) NULL;
ALTER TABLE `tenantservice`.`tenant_dpa_signature` ADD COLUMN IF NOT EXISTS source varchar(64) NULL;
```

## 2. agencyservice database

Source of truth: `ORISO-AgencyService/src/main/resources/db/changelog/changeset/`
0019_agency_admin_control, 0020_agency_settings,
0021_agency_topic_legal, 0022_agency_address_contact.

```sql
-- 0019: agency admin control table + sequence
CREATE SEQUENCE IF NOT EXISTS `agencyservice`.`sequence_agency_admin_control`
  INCREMENT BY 1 MINVALUE 0 NOMAXVALUE START WITH 0 CACHE 0;

CREATE TABLE IF NOT EXISTS `agencyservice`.`agency_admin_control` (
  `id` bigint(21) NOT NULL,
  `controls` text COLLATE utf8_unicode_ci NOT NULL,
  `update_date` datetime NOT NULL DEFAULT (UTC_TIMESTAMP),
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8 COLLATE=utf8_unicode_ci;

-- 0020: agency settings
ALTER TABLE `agencyservice`.`agency` ADD COLUMN IF NOT EXISTS `settings` longtext NULL;

-- 0021: department (Fachbereich) privacy policy on agency_topic
ALTER TABLE `agencyservice`.`agency_topic` ADD COLUMN IF NOT EXISTS content_dpp longtext NULL;
ALTER TABLE `agencyservice`.`agency_topic` ADD COLUMN IF NOT EXISTS publication_status varchar(20) NOT NULL DEFAULT 'DRAFT';

-- 0022: agency address / contact columns
ALTER TABLE `agencyservice`.`agency` ADD COLUMN IF NOT EXISTS street varchar(255) NULL;
ALTER TABLE `agencyservice`.`agency` ADD COLUMN IF NOT EXISTS house_number varchar(20) NULL;
ALTER TABLE `agencyservice`.`agency` ADD COLUMN IF NOT EXISTS floor_building varchar(100) NULL;
ALTER TABLE `agencyservice`.`agency` ADD COLUMN IF NOT EXISTS country varchar(100) NULL;
ALTER TABLE `agencyservice`.`agency` ADD COLUMN IF NOT EXISTS phone varchar(30) NULL;
ALTER TABLE `agencyservice`.`agency` ADD COLUMN IF NOT EXISTS phone_secondary varchar(30) NULL;
ALTER TABLE `agencyservice`.`agency` ADD COLUMN IF NOT EXISTS email varchar(255) NULL;
```

### Note on `diocese` (deliberately NOT part of the script)

AgencyService changeset 0013 (active) drops the FK `agency_ibfk_1` and makes
`agency.diocese_id` nullable; changeset 0014 (in the repo, currently commented
out in the master changelog pending data-migration confirmation) drops the
`diocese` table and the `diocese_id` column. The AgencyService code no longer
maps or reads any diocese object, so the schema mirror in this repo reflects
the target state without them. This runbook does NOT drop them on the
live Pre-Dev database — dropping is destructive and not needed for the
legal endpoints to work (the code no longer reads those objects). If you
want the live database to match the mirror exactly, remove them manually
as an optional cleanup after a backup:
`DROP TABLE diocese; ALTER TABLE agency DROP COLUMN diocese_id; DROP SEQUENCE sequence_diocese;`

## 3. Verification

```sql
-- tenantservice: expect 2
SELECT COUNT(*) FROM information_schema.columns
WHERE table_schema = 'tenantservice' AND table_name = 'tenant'
  AND column_name IN ('content_dpa', 'dpa_activation_date');

-- tenantservice: expect 16
SELECT COUNT(*) FROM information_schema.columns
WHERE table_schema = 'tenantservice' AND table_name = 'tenant_dpa_signature';

-- tenantservice: expect 5
SELECT COUNT(*) FROM information_schema.columns
WHERE table_schema = 'tenantservice' AND table_name = 'tenant_dpa_version';

-- tenantservice: expect 3
SELECT COUNT(*) FROM information_schema.tables
WHERE table_schema = 'tenantservice'
  AND table_name IN ('tenant_admin_controls', 'tenant_dpa_signature', 'tenant_dpa_version');

-- tenantservice sequences: expect 2
SELECT COUNT(*) FROM information_schema.tables
WHERE table_schema = 'tenantservice' AND table_type = 'SEQUENCE'
  AND table_name IN ('sequence_tenant_dpa_signature', 'sequence_tenant_dpa_version');

-- agencyservice: expect 8
SELECT COUNT(*) FROM information_schema.columns
WHERE table_schema = 'agencyservice' AND table_name = 'agency'
  AND column_name IN ('settings', 'street', 'house_number', 'floor_building',
                      'country', 'phone', 'phone_secondary', 'email');

-- agencyservice: expect 2
SELECT COUNT(*) FROM information_schema.columns
WHERE table_schema = 'agencyservice' AND table_name = 'agency_topic'
  AND column_name IN ('content_dpp', 'publication_status');

-- agencyservice: expect 1 table + 1 sequence
SELECT COUNT(*) FROM information_schema.tables
WHERE table_schema = 'agencyservice' AND table_name = 'agency_admin_control';
SELECT COUNT(*) FROM information_schema.tables
WHERE table_schema = 'agencyservice' AND table_type = 'SEQUENCE'
  AND table_name = 'sequence_agency_admin_control';
```

After applying, restart is not required — but re-test the legal endpoints
(tenant DPA publish/sign, agency-topic DPP) to confirm the 500s are gone.
