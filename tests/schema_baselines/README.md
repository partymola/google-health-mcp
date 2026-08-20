# Schema baselines

One `.sql` file per schema a release shipped, named `X.Y.Z.sql` for that
release. `TestTheMigrationLockstep` builds each one into a database, opens it
through `db.get_db`, and asserts it ends up with exactly the current `SCHEMA`.

Add one at each release whose schema differs from the newest file here. Never
edit one in place. Deleting one drops support for databases that old.

Why each of those matters: AGENTS.md, "Seams the suite does not cross".
