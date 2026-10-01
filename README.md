# CS Desk

Staff ticketing app with PostgreSQL persistence, editable client and concern lookups, ticket history, filtering, exports and Google Sheets snapshot import with conflict handling.

## Free hosting setup

Use the existing Supabase database and Render's Free Docker web service. No purchased domain is needed. `render.yaml` selects the free web service plan and creates no paid database. Free services have usage limits and cold starts; this configuration does not provide guaranteed availability or unlimited storage.

1. In Supabase **Authentication → Users**, create accounts for approved staff using their `@findme.com.ph` addresses. Keep public sign-ups disabled. Confirm the accounts under your organization's account-verification process. Each staff member needs their own password; do not use the database password as a login password.
2. In Render choose **New → Blueprint** and connect this repository. Review the service plan: **Free**.
3. Enter the prompted environment variables privately in Render:
   - `DATABASE_URL`: Supabase Session pooler URI with the database password URL-encoded, and `?sslmode=require` appended.
   - `SUPABASE_URL`: project URL from Supabase project settings.
   - `SUPABASE_PUBLISHABLE_KEY`: Supabase publishable key (or legacy anon key), never a service-role key.
   - `CS_STAFF_DOMAIN`: already set to `findme.com.ph` by the blueprint.
4. Deploy. The server uses Render's assigned HTTPS origin automatically. Use `/healthz` for health checks.
5. Test login with a confirmed staff account; verify other domains cannot enter. Check existing tickets, filters, validation records, CSV export and a designated test ticket's create/close/reopen history. The UI records the signed-in email, not a freely selected operator name.

The website requires login for all ticket and lookup data, including API routes and exports. Authentication is validated against Supabase on requests. Sessions use Secure/HttpOnly cookies and last at most one hour before requiring sign-in again. All admitted staff have the same ticket and validation editing privileges. This is an internal staff app, not a customer self-service portal.

## Database

Existing tickets have been copied from the local app into Supabase and verified by the migration tool. Application startup requires a migrated database. It never silently initializes a hosted database from a missing snapshot.

Ticket data, SQLite files, source spreadsheet snapshots, passwords, and connection strings are excluded from this repository and the Docker image. Tables use row-level security with no public API policies; the backend connects using the database server role.

## Google Sheets synchronization

The prior 15-minute import runs on the local computer against SQLite. It has NOT been redirected to this hosted database. Before declaring the hosted site current, configure that importer securely with the PostgreSQL connection, import a fresh complete snapshot, verify conflicts, and stop using the independent local app for edits. A cloud scheduler with authorized Google access is needed if sheet synchronization must continue while the PC is off. Outbound app-to-Sheets reporting is also not connected yet.

## Tests

`python -m unittest test_auth.py` verifies domain checks, verified identity, cookie settings, anonymous API denial, cross-origin write denial and audit-actor enforcement. Local full workflow tests require the original private source fixtures and are intentionally not bundled in this repository. A live authenticated PostgreSQL workflow check is required before team rollout.

## Operations

Keep independent database backups and monitor the Supabase free storage allowance. Disabling public signup in Supabase is part of setup; email-domain matching alone is not an account-provisioning process. Disable users in Supabase when access should end. Keep any previous local database as a backup, not as a second independently edited source.
