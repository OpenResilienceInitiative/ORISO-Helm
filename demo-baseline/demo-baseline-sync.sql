-- ORISO demo/initial-delivery baseline sync.
-- Idempotent by design: rerunning this script must not create duplicate
-- agency_topic rows or duplicate postcode coverage rows.

SET @demo_agency_id := 246;
SET @demo_tenant_id := 21;
SET @demo_postcode := '88885';

USE consultingtypeservice;

INSERT INTO topic (
  id,
  tenant_id,
  name,
  description,
  status,
  create_date,
  update_date,
  internal_identifier,
  fallback_agency_id,
  fallback_url,
  welcome_message,
  send_next_step_message,
  titles_short,
  titles_long,
  titles_welcome,
  titles_dropdown,
  slug
)
VALUES (
  2,
  1,
  '{"de": "Kinder und Jugendliche", "en": "Children and young people"}',
  '{"de": "Wenn der Alltag zu viel wird - hier gibt es Hilfe und ein offenes Ohr.", "en": "Support and a listening ear when everyday life becomes too much."}',
  'ACTIVE',
  UTC_TIMESTAMP(),
  UTC_TIMESTAMP(),
  'children-youth',
  NULL,
  NULL,
  NULL,
  0,
  '{"de": "Kinder und Jugendliche", "en": "Children and young people"}',
  '{"de": "Wenn der Alltag zu viel wird - hier gibt es Hilfe und ein offenes Ohr.", "en": "Support and a listening ear when everyday life becomes too much."}',
  'Kinder und Jugendliche',
  'Kinder und Jugendliche',
  'children-youth-counselling'
),
(
  10,
  1,
  '{"de": "Eltern und Familie", "en": "Parents and family"}',
  '{"de": "Ob Erziehungsfragen, Konflikte oder familiaere Krisen - hier finden Sie verstaendnisvolle Begleitung.", "en": "Support for parenting questions, conflict, and family pressure."}',
  'ACTIVE',
  UTC_TIMESTAMP(),
  UTC_TIMESTAMP(),
  'parents-family',
  NULL,
  NULL,
  NULL,
  0,
  '{"de": "Eltern und Familie", "en": "Parents and family"}',
  '{"de": "Ob Erziehungsfragen, Konflikte oder familiaere Krisen - hier finden Sie verstaendnisvolle Begleitung.", "en": "Support for parenting questions, conflict, and family pressure."}',
  'Eltern und Familie',
  'Eltern und Familie',
  'parents-and-family'
)
ON DUPLICATE KEY UPDATE
  name = VALUES(name),
  description = VALUES(description),
  status = VALUES(status),
  update_date = UTC_TIMESTAMP(),
  internal_identifier = VALUES(internal_identifier),
  titles_short = VALUES(titles_short),
  titles_long = VALUES(titles_long),
  titles_welcome = VALUES(titles_welcome),
  titles_dropdown = VALUES(titles_dropdown),
  slug = VALUES(slug);

-- MariaDB requires SETVAL's next_value argument to be an integer literal; a user
-- variable or subquery raises ERROR 1064. We pass the reserved baseline id as the
-- last-USED value (is_used = 1), so the next NEXTVAL returns id + increment and
-- skips past the reserved topic ids -- otherwise the next sequence-driven topic
-- INSERT would collide with the demo rows (ERROR 1062 Duplicate entry). SETVAL never
-- lowers a sequence, so this is a no-op on environments that already advanced past it.
DO SETVAL(`sequence_topic`, 16, 1);

USE agencyservice;

UPDATE agency
SET
  tenant_id = @demo_tenant_id,
  name = 'Caritasverband Wismar',
  consulting_type = 1,
  is_offline = 0,
  is_external = 0,
  delete_date = NULL,
  update_date = UTC_TIMESTAMP()
WHERE id = @demo_agency_id;

INSERT INTO agency_postcode_range (
  id,
  tenant_id,
  agency_id,
  postcode_from,
  postcode_to,
  create_date,
  update_date
)
SELECT
  900000001,
  @demo_tenant_id,
  @demo_agency_id,
  '00000',
  '99999',
  UTC_TIMESTAMP(),
  UTC_TIMESTAMP()
WHERE EXISTS (
  SELECT 1 FROM agency WHERE id = @demo_agency_id
)
AND NOT EXISTS (
  SELECT 1
  FROM agency_postcode_range
  -- postcode_from/postcode_to are utf8mb3_unicode_ci columns, but @demo_postcode
  -- carries the client's connection collation (utf8mb3_general_ci under the gate's
  -- mariadb client). Two IMPLICIT collations of the same charset raise
  -- ERROR 1267 (Illegal mix of collations), so normalise the variable to the
  -- column's charset and collation. Postcodes are ASCII, so CONVERT is lossless.
  WHERE agency_id = @demo_agency_id
    AND postcode_from <= CONVERT(@demo_postcode USING utf8mb3) COLLATE utf8mb3_unicode_ci
    AND postcode_to >= CONVERT(@demo_postcode USING utf8mb3) COLLATE utf8mb3_unicode_ci
);

INSERT INTO agency_topic (
  id,
  agency_id,
  topic_id,
  create_date,
  update_date,
  publication_status,
  publication_status_imprint
)
SELECT
  900000002,
  @demo_agency_id,
  2,
  UTC_TIMESTAMP(),
  UTC_TIMESTAMP(),
  'DRAFT',
  'DRAFT'
WHERE EXISTS (
  SELECT 1 FROM agency WHERE id = @demo_agency_id
)
ON DUPLICATE KEY UPDATE
  agency_id = VALUES(agency_id),
  topic_id = VALUES(topic_id),
  update_date = UTC_TIMESTAMP();

INSERT INTO agency_topic (
  id,
  agency_id,
  topic_id,
  create_date,
  update_date,
  publication_status,
  publication_status_imprint
)
SELECT
  900000010,
  @demo_agency_id,
  10,
  UTC_TIMESTAMP(),
  UTC_TIMESTAMP(),
  'DRAFT',
  'DRAFT'
WHERE EXISTS (
  SELECT 1 FROM agency WHERE id = @demo_agency_id
)
ON DUPLICATE KEY UPDATE
  agency_id = VALUES(agency_id),
  topic_id = VALUES(topic_id),
  update_date = UTC_TIMESTAMP();

-- MariaDB requires SETVAL's next_value argument to be an integer literal (a user
-- variable or subquery raises ERROR 1064). We pass the reserved baseline row ids as
-- the last-USED value (is_used = 1), so the next NEXTVAL returns id + increment and
-- skips past the reserved ids -- otherwise the following app INSERT would collide with
-- the demo rows (ERROR 1062 Duplicate entry). SETVAL never lowers a sequence, so this
-- is a no-op on environments that already advanced beyond these ids.
DO SETVAL(`sequence_agency_postcode_range`, 900000001, 1);
DO SETVAL(`sequence_agency_topic`, 900000010, 1);
