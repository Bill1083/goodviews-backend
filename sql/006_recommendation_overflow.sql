-- Backs mark_not_interested's replacement quality: instead of falling back
-- to generic top-rated backfill, it can now promote a real scored-but-not-
-- selected candidate from the last full compute. Apply manually via the
-- Supabase SQL editor.

ALTER TABLE user_recommendations
    ADD COLUMN IF NOT EXISTS overflow jsonb NOT NULL DEFAULT '[]';

COMMENT ON COLUMN user_recommendations.overflow IS 'Extra scored-but-not-selected candidates from the last full compute (rank-ordered, same [{"movie_id","reason"}] shape as items), consumed by mark_not_interested so a dismissed movie is replaced by a real algorithmic pick rather than generic popular-movie backfill.';
