CREATE TABLE notes (
    id serial PRIMARY KEY,
    text text NOT NULL,
    status text NOT NULL DEFAULT 'pending',
    task_id text,
    summary text,
    created_at timestamptz NOT NULL DEFAULT now()
)
