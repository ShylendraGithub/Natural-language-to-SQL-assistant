-- A dedicated, least-privilege role for the NL->SQL assistant on PostgreSQL.
-- This role is the real security boundary: even if the LLM, the validator and the
-- application all misbehave, the database still refuses anything but SELECT.
-- Run as a superuser / the database owner. Adjust names to your setup.

CREATE ROLE nl2sql_reader LOGIN PASSWORD 'change-me';

GRANT CONNECT ON DATABASE mydb TO nl2sql_reader;
GRANT USAGE   ON SCHEMA public TO nl2sql_reader;

-- Grant SELECT only on the tables the assistant is allowed to see.
-- Do NOT use "ALL TABLES IN SCHEMA": new tables (and PII tables) would be exposed silently.
GRANT SELECT ON customers, products, orders, order_items TO nl2sql_reader;

-- Belt and braces: every session of this role is read-only, time-boxed and capped.
ALTER ROLE nl2sql_reader SET default_transaction_read_only = on;
ALTER ROLE nl2sql_reader SET statement_timeout = '5s';
ALTER ROLE nl2sql_reader SET idle_in_transaction_session_timeout = '10s';
ALTER ROLE nl2sql_reader CONNECTION LIMIT 5;

-- Sensitive columns: grant column-level SELECT instead of table-level, e.g.
--   GRANT SELECT (customer_id, name, city, country, signup_date) ON customers TO nl2sql_reader;
-- (the e-mail column stays invisible to the assistant).
--
-- Multi-tenant data: add row-level security so one tenant's question can never read another's rows.
--   ALTER TABLE orders ENABLE ROW LEVEL SECURITY;
--   CREATE POLICY tenant_isolation ON orders FOR SELECT TO nl2sql_reader
--       USING (tenant_id = current_setting('app.tenant_id')::int);

-- Connection URL for the assistant (SQLAlchemy + psycopg 3):
--   postgresql+psycopg://nl2sql_reader:change-me@localhost:5432/mydb
