-- Run once as a Postgres superuser, before the exporter first starts. Only needed
-- when you bring your own Postgres; the bundled stack (examples/stack) creates the
-- role and database for you.
--
--   psql -h <your-postgres> -U postgres -f 000_bootstrap.sql
--
-- Replace the password first, and use the same value in AXIMOTE_PG_DSN. The
-- exporter applies the other sql/ files at every start, so only the role and
-- database are created here. A dedicated role keeps a car logger away from any
-- other data on the same server.
--
-- Local-time analytics (trips by weekday, first departure of the day, charge
-- cycles) use the database setting aximote.tz. Set it to your own zone:

\set aximote_password 'CHANGEME'
\set aximote_tz 'UTC'

CREATE ROLE aximote WITH LOGIN PASSWORD :'aximote_password';

CREATE DATABASE aximote OWNER aximote;

ALTER DATABASE aximote SET aximote.tz = :'aximote_tz';
