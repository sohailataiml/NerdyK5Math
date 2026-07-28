-- Runs once on first container start (docker-entrypoint-initdb.d).
--
-- The integration tests drop and recreate every table to exercise migrations
-- from empty. Pointing them at the development database means running the test
-- suite silently destroys whatever you were looking at — and would destroy
-- something worse if TEST_DATABASE_URL were ever set to a real environment.
-- They get their own database instead, and tests/test_migrations.py refuses to
-- run against one whose name does not end in `_test`.
CREATE DATABASE tutor_test OWNER tutor;
