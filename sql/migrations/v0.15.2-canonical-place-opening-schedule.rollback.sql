BEGIN;

SET LOCAL lock_timeout = '5s';

DROP TABLE IF EXISTS public.travel_canonical_place_opening_schedule RESTRICT;

COMMIT;
