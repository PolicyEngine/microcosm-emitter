# Development instructions

- Use uv, Python 3.13+, pytest, and Ruff. Keep workflows as single-command calls to Bash scripts when shell logic has multiple lines.
- The generic runtime must not import telemetry or Microcosm. The telemetry client must not import service implementation, database, or authentication modules.
- Publish only the `microcosm-emitter` distribution. Keep the generic host and required telemetry library internally separate under `microcosm_emitter`; do not introduce standalone telemetry packages or optional telemetry installation.
- Python databases use SQLAlchemy declarative ORM and Alembic exclusively. No handwritten SQL, direct-driver persistence, create_all(), schema repair, or pre-Alembic adoption. TypeScript database work, if introduced, must use Drizzle ORM and Drizzle Kit migrations.
- Tests use temporary databases created through real migrations, synthetic credentials, and loopback network services. Never use ambient credentials or contact the production collector in tests.
- Preserve the existing collector protocol; do not add a general hosted events API.
- Push feature branches and open PRs; do not push directly to main. Do not automatically merge or monitor queued checks.
