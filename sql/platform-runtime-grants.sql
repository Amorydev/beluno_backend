-- Run as `migrator` after Alembic's platform schema migration and after the
-- Procrastinate schema bootstrap. Domain DML is intentionally not granted.

REVOKE ALL ON SCHEMA public FROM PUBLIC;

DO $$
DECLARE
    schema_name text;
BEGIN
    FOREACH schema_name IN ARRAY ARRAY[
        'iam', 'groups', 'plans', 'sync_audit', 'finance', 'decisions',
        'schedule_places', 'coordination', 'bookings', 'media_memories',
        'engagement', 'search_export', 'billing', 'analytics_ops', 'jobs'
    ]
    LOOP
        EXECUTE format('REVOKE ALL ON SCHEMA %I FROM PUBLIC', schema_name);
        EXECUTE format('GRANT USAGE ON SCHEMA %I TO api_runtime, worker_runtime, scheduler_runtime, support_readonly', schema_name);
        EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM PUBLIC', schema_name);
        EXECUTE format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM PUBLIC', schema_name);
        EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE migrator IN SCHEMA %I REVOKE ALL ON TABLES FROM PUBLIC', schema_name);
        EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE migrator IN SCHEMA %I REVOKE ALL ON SEQUENCES FROM PUBLIC', schema_name);
    END LOOP;
END;
$$;

-- The job queue is the only direct-table exception in Phase 2. It contains
-- no user domain data; all future domain mutations flow through API services.
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA jobs TO worker_runtime, scheduler_runtime;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA jobs TO worker_runtime, scheduler_runtime;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA jobs TO worker_runtime, scheduler_runtime;

-- The API enqueues jobs inside its own domain transaction so a committed state
-- change and its follow-up job are atomic. It may only insert through the
-- versioned defer function; it cannot read job arguments or alter queue state.
GRANT INSERT ON jobs.procrastinate_jobs, jobs.procrastinate_events TO api_runtime;
GRANT SELECT (id) ON jobs.procrastinate_jobs TO api_runtime;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA jobs TO api_runtime;
GRANT EXECUTE ON FUNCTION jobs.procrastinate_defer_jobs_v1(jobs.procrastinate_job_to_defer_v1[])
    TO api_runtime;
