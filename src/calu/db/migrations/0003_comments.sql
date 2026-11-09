-- calu schema, version 3: user comments on selections in diffs and files.
--
-- A comment is relayed to Claude as a user message and kept here, so that it can later serve as
-- training or evaluation data for the checker agents (README "comment on a selection"). The
-- comment itself is stored as the JSON of `calu.comments.model.Comment`; the columns next to it
-- are the ones an operator or a query filters by.

CREATE TABLE comments (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id  uuid NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    turn        integer,
    kind        text NOT NULL CHECK (kind IN ('diff', 'file')),
    path        text NOT NULL,
    start_line  integer NOT NULL,
    end_line    integer NOT NULL,
    ref         text,
    comment     text NOT NULL,
    content     jsonb NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX comments_by_session ON comments (session_id, created_at);

GRANT SELECT, INSERT ON comments TO calu_session;
GRANT SELECT ON comments TO calu_operator;
