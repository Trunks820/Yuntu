BEGIN;

SET LOCAL lock_timeout = '5s';

CREATE TABLE IF NOT EXISTS public.travel_canonical_place_opening_schedule (
    place_id BIGINT PRIMARY KEY REFERENCES public.travel_canonical_place(place_id) ON DELETE CASCADE,
    season_rules JSONB NOT NULL,
    CONSTRAINT ck_canonical_place_opening_rules_array CHECK (jsonb_typeof(season_rules) = 'array')
);

COMMENT ON TABLE public.travel_canonical_place_opening_schedule IS
    'Current offline opening facts for a canonical place';

COMMENT ON COLUMN public.travel_canonical_place_opening_schedule.season_rules IS
    'Inclusive seasonal and effective date bounds, weekly closures, times, holiday rules and per-field sources, verification dates and confidence. Empty array means checked without usable facts.';

COMMIT;
