-- ORISO demo baseline drift check.
-- Returns zero rows when the environment is demo-ready.

SELECT 'missing topic 2: Kinder und Jugendliche' AS drift
WHERE NOT EXISTS (
  SELECT 1
  FROM consultingtypeservice.topic
  WHERE id = 2
    AND status = 'ACTIVE'
    AND JSON_UNQUOTE(JSON_EXTRACT(name, '$.de')) = 'Kinder und Jugendliche'
);

SELECT 'missing topic 10: Eltern und Familie' AS drift
WHERE NOT EXISTS (
  SELECT 1
  FROM consultingtypeservice.topic
  WHERE id = 10
    AND status = 'ACTIVE'
    AND JSON_UNQUOTE(JSON_EXTRACT(name, '$.de')) = 'Eltern und Familie'
);

SELECT 'missing visible online agency for postcode 88885 topic 2 consultingType 1' AS drift
WHERE NOT EXISTS (
  SELECT 1
  FROM agencyservice.agency a
  JOIN agencyservice.agency_postcode_range apr ON apr.agency_id = a.id
  JOIN agencyservice.agency_topic at ON at.agency_id = a.id
  WHERE a.consulting_type = 1
    AND a.is_offline = 0
    AND a.delete_date IS NULL
    AND apr.postcode_from <= '88885'
    AND apr.postcode_to >= '88885'
    AND at.topic_id = 2
);

SELECT 'missing visible online agency for postcode 88885 topic 10 consultingType 1' AS drift
WHERE NOT EXISTS (
  SELECT 1
  FROM agencyservice.agency a
  JOIN agencyservice.agency_postcode_range apr ON apr.agency_id = a.id
  JOIN agencyservice.agency_topic at ON at.agency_id = a.id
  WHERE a.consulting_type = 1
    AND a.is_offline = 0
    AND a.delete_date IS NULL
    AND apr.postcode_from <= '88885'
    AND apr.postcode_to >= '88885'
    AND at.topic_id = 10
);

SELECT CONCAT('duplicate agency_topic rows for agency ', agency_id, ' topic ', topic_id) AS drift
FROM agencyservice.agency_topic
WHERE agency_id = 246
  AND topic_id IN (2, 10)
GROUP BY agency_id, topic_id
HAVING COUNT(*) > 1;
