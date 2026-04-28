from sqlalchemy import inspect, text

from app.db.database import engine


def ensure_saas_account_tables() -> None:
    if engine.dialect.name != "sqlite":
        return

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    statements: list[str] = []
    index_statements: list[str] = []

    if "users" not in existing_tables:
        statements.append(
            """
            CREATE TABLE users (
                id INTEGER NOT NULL PRIMARY KEY,
                email VARCHAR NOT NULL,
                password_hash VARCHAR NOT NULL,
                display_name VARCHAR,
                status VARCHAR NOT NULL DEFAULT 'active',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        index_statements.extend(
            [
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_email ON users (email)",
                "CREATE INDEX IF NOT EXISTS ix_users_id ON users (id)",
            ]
        )

    if "workspaces" not in existing_tables:
        statements.append(
            """
            CREATE TABLE workspaces (
                id INTEGER NOT NULL PRIMARY KEY,
                name VARCHAR NOT NULL,
                slug VARCHAR NOT NULL,
                owner_user_id INTEGER NOT NULL,
                status VARCHAR NOT NULL DEFAULT 'active',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(owner_user_id) REFERENCES users (id)
            )
            """
        )
        index_statements.extend(
            [
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_workspaces_slug ON workspaces (slug)",
                "CREATE INDEX IF NOT EXISTS ix_workspaces_id ON workspaces (id)",
                "CREATE INDEX IF NOT EXISTS ix_workspaces_owner_user_id ON workspaces (owner_user_id)",
            ]
        )

    if "workspace_members" not in existing_tables:
        statements.append(
            """
            CREATE TABLE workspace_members (
                id INTEGER NOT NULL PRIMARY KEY,
                workspace_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                role VARCHAR NOT NULL DEFAULT 'owner',
                status VARCHAR NOT NULL DEFAULT 'active',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(workspace_id) REFERENCES workspaces (id),
                FOREIGN KEY(user_id) REFERENCES users (id)
            )
            """
        )
        index_statements.extend(
            [
                "CREATE INDEX IF NOT EXISTS ix_workspace_members_id ON workspace_members (id)",
                "CREATE INDEX IF NOT EXISTS ix_workspace_members_workspace_id ON workspace_members (workspace_id)",
                "CREATE INDEX IF NOT EXISTS ix_workspace_members_user_id ON workspace_members (user_id)",
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_workspace_members_workspace_user ON workspace_members (workspace_id, user_id)",
            ]
        )

    if not statements and not index_statements:
        return

    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))


def ensure_usage_event_table() -> None:
    if engine.dialect.name != "sqlite":
        return

    inspector = inspect(engine)
    if "usage_events" in inspector.get_table_names():
        return

    statements = [
        """
        CREATE TABLE usage_events (
            id INTEGER NOT NULL PRIMARY KEY,
            workspace_id INTEGER NOT NULL,
            job_id INTEGER,
            event_type VARCHAR NOT NULL,
            quantity FLOAT NOT NULL DEFAULT 0,
            unit VARCHAR NOT NULL,
            idempotency_key VARCHAR NOT NULL,
            details TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(workspace_id) REFERENCES workspaces (id),
            FOREIGN KEY(job_id) REFERENCES jobs (id),
            CONSTRAINT uq_usage_events_idempotency_key UNIQUE (idempotency_key)
        )
        """,
        "CREATE INDEX IF NOT EXISTS ix_usage_events_id ON usage_events (id)",
        "CREATE INDEX IF NOT EXISTS ix_usage_events_workspace_id ON usage_events (workspace_id)",
        "CREATE INDEX IF NOT EXISTS ix_usage_events_job_id ON usage_events (job_id)",
        "CREATE INDEX IF NOT EXISTS ix_usage_events_event_type ON usage_events (event_type)",
    ]
    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))


def ensure_subscription_table() -> None:
    if engine.dialect.name != "sqlite":
        return

    inspector = inspect(engine)
    if "subscriptions" in inspector.get_table_names():
        return

    statements = [
        """
        CREATE TABLE subscriptions (
            id INTEGER NOT NULL PRIMARY KEY,
            workspace_id INTEGER NOT NULL,
            provider VARCHAR NOT NULL DEFAULT 'mock',
            provider_customer_id VARCHAR,
            provider_subscription_id VARCHAR,
            provider_checkout_id VARCHAR,
            plan_slug VARCHAR NOT NULL DEFAULT 'free',
            status VARCHAR NOT NULL DEFAULT 'inactive',
            current_period_end DATETIME,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(workspace_id) REFERENCES workspaces (id)
        )
        """,
        "CREATE INDEX IF NOT EXISTS ix_subscriptions_id ON subscriptions (id)",
        "CREATE INDEX IF NOT EXISTS ix_subscriptions_workspace_id ON subscriptions (workspace_id)",
        "CREATE INDEX IF NOT EXISTS ix_subscriptions_provider_customer_id ON subscriptions (provider_customer_id)",
        "CREATE INDEX IF NOT EXISTS ix_subscriptions_provider_subscription_id ON subscriptions (provider_subscription_id)",
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_subscriptions_provider_checkout_id ON subscriptions (provider_checkout_id)",
        "CREATE INDEX IF NOT EXISTS ix_subscriptions_plan_slug ON subscriptions (plan_slug)",
        "CREATE INDEX IF NOT EXISTS ix_subscriptions_status ON subscriptions (status)",
    ]
    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))


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
    if "workspace_id" not in existing_columns:
        statements.append("ALTER TABLE niche_definitions ADD COLUMN workspace_id INTEGER")
        statements.append("CREATE INDEX IF NOT EXISTS ix_niche_definitions_workspace_id ON niche_definitions (workspace_id)")
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


def ensure_niche_keyword_workspace_columns() -> None:
    if engine.dialect.name != "sqlite":
        return

    inspector = inspect(engine)
    if "niche_keywords" not in inspector.get_table_names():
        return
    existing_columns = {column["name"] for column in inspector.get_columns("niche_keywords")}

    statements: list[str] = []
    if "workspace_id" not in existing_columns:
        statements.append("ALTER TABLE niche_keywords ADD COLUMN workspace_id INTEGER")
        statements.append("CREATE INDEX IF NOT EXISTS ix_niche_keywords_workspace_id ON niche_keywords (workspace_id)")

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


def ensure_job_workspace_columns() -> None:
    if engine.dialect.name != "sqlite":
        return

    inspector = inspect(engine)
    existing_columns = {column["name"] for column in inspector.get_columns("jobs")}

    statements: list[str] = []
    if "workspace_id" not in existing_columns:
        statements.append("ALTER TABLE jobs ADD COLUMN workspace_id INTEGER")
        statements.append("CREATE INDEX IF NOT EXISTS ix_jobs_workspace_id ON jobs (workspace_id)")
    if "locked_at" not in existing_columns:
        statements.append("ALTER TABLE jobs ADD COLUMN locked_at DATETIME")
        statements.append("CREATE INDEX IF NOT EXISTS ix_jobs_locked_at ON jobs (locked_at)")
    if "locked_by" not in existing_columns:
        statements.append("ALTER TABLE jobs ADD COLUMN locked_by VARCHAR")

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
