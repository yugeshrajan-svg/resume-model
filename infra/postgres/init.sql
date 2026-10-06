-- Shared initialization for Docker and the native PostgreSQL helper.
-- Run as the bootstrap superuser against the existing screening database.
-- These passwords are for the localhost development services only.
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'screening_system') THEN
        CREATE ROLE screening_system LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
            BYPASSRLS PASSWORD 'system';
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'screening_app') THEN
        CREATE ROLE screening_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
            NOBYPASSRLS PASSWORD 'app';
    END IF;
END
$$;

ALTER DATABASE screening OWNER TO screening_system;
GRANT ALL ON SCHEMA public TO screening_system;
