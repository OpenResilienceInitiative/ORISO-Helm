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

SET @demo_sequence_topic_value := (SELECT GREATEST(COALESCE(MAX(id), 0), 16) FROM topic);
DO SETVAL(`sequence_topic`, @demo_sequence_topic_value, 0);

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
  WHERE agency_id = @demo_agency_id
    AND postcode_from <= @demo_postcode
    AND postcode_to >= @demo_postcode
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

SET @demo_sequence_postcode_value := (
  SELECT GREATEST(COALESCE(MAX(id), 0), 900000001) FROM agency_postcode_range
);
DO SETVAL(`sequence_agency_postcode_range`, @demo_sequence_postcode_value, 0);

SET @demo_sequence_agency_topic_value := (
  SELECT GREATEST(COALESCE(MAX(id), 0), 900000010) FROM agency_topic
);
DO SETVAL(`sequence_agency_topic`, @demo_sequence_agency_topic_value, 0);
