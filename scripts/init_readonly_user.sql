-- ==============================================================================
-- DBOps Control Plane: Non-Invasive Read-Only & Backup Role Setup
-- ==============================================================================
-- This script creates a strictly non-invasive role for the DBOps Control Plane.
-- It grants only telemetry views and snapshot read access with ZERO write,
-- update, alter, or drop permissions.
--
-- Supported PostgreSQL Versions: 10, 11, 12, 13, 14, 15, 16, 17+
-- ==============================================================================

-- 1. Create dedicated monitoring user (replace 'ChangeThisStrongPassword123!' with your secret)
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'dbops_monitor') THEN
    CREATE ROLE dbops_monitor WITH LOGIN PASSWORD 'ChangeThisStrongPassword123!';
  END IF;
END
$$;

-- 2. Grant system telemetry inspection roles (PostgreSQL 10+)
-- Allows reading pg_stat_activity, pg_stat_database, locks, and system views
GRANT pg_read_all_stats TO dbops_monitor;

-- 3. Grant table data reading role for non-blocking MVCC backups (PostgreSQL 14+)
-- If on PostgreSQL < 14, grant SELECT on all tables in target schemas instead.
DO $$
BEGIN
  IF EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'pg_read_all_data') THEN
    GRANT pg_read_all_data TO dbops_monitor;
  ELSE
    -- Fallback for PostgreSQL 10-13: Grant select on all existing and future tables in public schema
    EXECUTE 'GRANT USAGE ON SCHEMA public TO dbops_monitor;';
    EXECUTE 'GRANT SELECT ON ALL TABLES IN SCHEMA public TO dbops_monitor;';
    EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO dbops_monitor;';
  END IF;
END
$$;

-- 4. Explicitly ensure default transaction mode for this user is READ ONLY
ALTER ROLE dbops_monitor SET default_transaction_read_only = 'on';

-- 5. Set safety statement timeout to prevent any accidental slow telemetry queries
ALTER ROLE dbops_monitor SET statement_timeout = '15s';

-- ==============================================================================
-- Verification Query: Check that permissions are correctly established
-- ==============================================================================
SELECT 
    r.rolname,
    r.rolsuper,
    r.rolinherit,
    r.rolcreaterole,
    r.rolcreatedb,
    r.rolcanlogin,
    ARRAY(SELECT b.rolname FROM pg_auth_members m JOIN pg_roles b ON (m.roleid = b.oid) WHERE m.member = r.oid) as memberof
FROM pg_roles r
WHERE r.rolname = 'dbops_monitor';

