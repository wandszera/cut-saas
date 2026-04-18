from sqlalchemy import inspect, text

from app.db.database import engine


def ensure_niche_definitions_table() -> None:
    if engine.dialect.name != "sqlite":
        return

    inspector = inspect(engine)
    if "niche_definitions" not in inspector.get_table_names():
        create_statement = """
        CREATE TABLE niche_definitions (
            id INTEGER NOT NULL PRIMARY KEY,
            name VARCHAR NOT NULL,
            slug VARCHAR NOT NULL,
            description TEXT,
            keywords_json TEXT NOT NULL DEFAULT '[]',
            weights_json TEXT NOT NULL DEFAULT '{}',
            source VARCHAR NOT NULL DEFAULT 'custom',
            status VARCHAR NOT NULL DEFAULT 'pending',
            llm_notes TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
        index_statements = [
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_niche_definitions_slug ON niche_definitions (slug)",
            "CREATE INDEX IF NOT EXISTS ix_niche_definitions_id ON niche_definitions (id)",
        ]
        with engine.begin() as connection:
            connection.execute(text(create_statement))
            for statement in index_statements:
                connection.execute(text(statement))


def ensure_niche_definition_columns() -> None:
    if engine.dialect.name != "sqlite":
        return

    ensure_niche_definitions_table()
    inspector = inspect(engine)
    existing_columns = {column["name"] for column in inspector.get_columns("niche_definitions")}

    statements: list[str] = []
    if "description" not in existing_columns:
        statements.append("ALTER TABLE niche_definitions ADD COLUMN description TEXT")
    if "keywords_json" not in existing_columns:
        statements.append("ALTER TABLE niche_definitions ADD COLUMN keywords_json TEXT NOT NULL DEFAULT '[]'")
    if "weights_json" not in existing_columns:
        statements.append("ALTER TABLE niche_definitions ADD COLUMN weights_json TEXT NOT NULL DEFAULT '{}'")
    if "source" not in existing_columns:
        statements.append("ALTER TABLE niche_definitions ADD COLUMN source VARCHAR NOT NULL DEFAULT 'custom'")
    if "status" not in existing_columns:
        statements.append("ALTER TABLE niche_definitions ADD COLUMN status VARCHAR NOT NULL DEFAULT 'pending'")
    if "llm_notes" not in existing_columns:
        statements.append("ALTER TABLE niche_definitions ADD COLUMN llm_notes TEXT")
    if "created_at" not in existing_columns:
        statements.append("ALTER TABLE niche_definitions ADD COLUMN created_at DATETIME DEFAULT CURRENT_TIMESTAMP")
    if "updated_at" not in existing_columns:
        statements.append("ALTER TABLE niche_definitions ADD COLUMN updated_at DATETIME DEFAULT CURRENT_TIMESTAMP")

    if not statements:
        return

    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))


def ensure_job_insights_columns() -> None:
    if engine.dialect.name != "sqlite":
        return

    inspector = inspect(engine)
    existing_columns = {column["name"] for column in inspector.get_columns("jobs")}

    statements: list[str] = []
    if "transcript_insights" not in existing_columns:
        statements.append("ALTER TABLE jobs ADD COLUMN transcript_insights TEXT")

    if not statements:
        return

    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))


def ensure_candidate_editorial_columns() -> None:
    if engine.dialect.name != "sqlite":
        return

    inspector = inspect(engine)
    existing_columns = {column["name"] for column in inspector.get_columns("candidates")}

    statements: list[str] = []
    if "heuristic_score" not in existing_columns:
        statements.append(
            "ALTER TABLE candidates ADD COLUMN heuristic_score FLOAT"
        )
    if "is_favorite" not in existing_columns:
        statements.append(
            "ALTER TABLE candidates ADD COLUMN is_favorite BOOLEAN NOT NULL DEFAULT 0"
        )
    if "editorial_notes" not in existing_columns:
        statements.append(
            "ALTER TABLE candidates ADD COLUMN editorial_notes TEXT"
        )
    if "updated_at" not in existing_columns:
        statements.append(
            "ALTER TABLE candidates ADD COLUMN updated_at DATETIME"
        )
    if "transcript_context_score" not in existing_columns:
        statements.append(
            "ALTER TABLE candidates ADD COLUMN transcript_context_score FLOAT"
        )
    if "llm_score" not in existing_columns:
        statements.append(
            "ALTER TABLE candidates ADD COLUMN llm_score FLOAT"
        )
    if "llm_why" not in existing_columns:
        statements.append(
            "ALTER TABLE candidates ADD COLUMN llm_why TEXT"
        )
    if "llm_title" not in existing_columns:
        statements.append(
            "ALTER TABLE candidates ADD COLUMN llm_title TEXT"
        )
    if "llm_hook" not in existing_columns:
        statements.append(
            "ALTER TABLE candidates ADD COLUMN llm_hook TEXT"
        )

    if not statements:
        return

    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))


def ensure_clip_editorial_columns() -> None:
    if engine.dialect.name != "sqlite":
        return

    inspector = inspect(engine)
    existing_columns = {column["name"] for column in inspector.get_columns("clips")}

    statements: list[str] = []
    if "headline" not in existing_columns:
        statements.append("ALTER TABLE clips ADD COLUMN headline VARCHAR")
    if "description" not in existing_columns:
        statements.append("ALTER TABLE clips ADD COLUMN description VARCHAR")
    if "hashtags" not in existing_columns:
        statements.append("ALTER TABLE clips ADD COLUMN hashtags VARCHAR")
    if "suggested_filename" not in existing_columns:
        statements.append("ALTER TABLE clips ADD COLUMN suggested_filename VARCHAR")
    if "render_preset" not in existing_columns:
        statements.append("ALTER TABLE clips ADD COLUMN render_preset VARCHAR")
    if "publication_status" not in existing_columns:
        statements.append("ALTER TABLE clips ADD COLUMN publication_status VARCHAR NOT NULL DEFAULT 'draft'")

    if not statements:
        return

    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))
