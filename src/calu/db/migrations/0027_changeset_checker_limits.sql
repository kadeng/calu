-- The changeset checker reviews a whole change before a commit: it reads the files around the
-- diff, follows callers and writes a long answer. Its limits of 8 requests, 24,000 output
-- tokens and 8,000 tokens per answer were those of a step checker with a small surcharge, and
-- a real review ran out of them (PLAN.md D197). Raise them where they are lower; a row whose
-- owner already set a higher value keeps it.

UPDATE checker_types
   SET request_limit       = GREATEST(request_limit, 40),
       output_tokens_limit = GREATEST(output_tokens_limit, 120000),
       max_tokens          = GREATEST(max_tokens, 16000)
 WHERE name = 'changeset';
