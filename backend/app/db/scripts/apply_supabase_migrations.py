import os
import sys
from pathlib import Path
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

# Load .env from project root or backend
backend_dir = Path(__file__).resolve().parent.parent.parent.parent
dotenv_path = backend_dir.parent / ".env"
if dotenv_path.exists():
    load_dotenv(dotenv_path)
else:
    load_dotenv()


def run_sql_file(engine, sql_path: Path) -> bool:
    """Execute a .sql file using SQLAlchemy engine."""
    print(f"Applying {sql_path.name}...")
    try:
        content = sql_path.read_text(encoding="utf-8")
        # Split or execute in autocommit connection
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(text(content))
        print(f"✓ Successfully applied {sql_path.name}")
        return True
    except Exception as e:
        print(f"✗ Error applying {sql_path.name}: {e}", file=sys.stderr)
        return False


def main():
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        print("ERROR: DATABASE_URL environment variable not set", file=sys.stderr)
        sys.exit(1)

    print(f"Connecting to database...")
    engine = create_engine(database_url)

    migrations_dir = Path(__file__).resolve().parent.parent / "migrations" / "supabase"
    if not migrations_dir.is_dir():
        print(f"Migrations directory not found: {migrations_dir}", file=sys.stderr)
        sys.exit(1)

    sql_files = sorted(migrations_dir.glob("*.sql"))
    if not sql_files:
        print("No migration files found.")
        return

    for sql_file in sql_files:
        success = run_sql_file(engine, sql_file)
        if not success:
            print(f"Migration halted at {sql_file.name}.")
            sys.exit(1)

    print("All Supabase migrations (01~13) applied successfully.")


if __name__ == "__main__":
    main()
