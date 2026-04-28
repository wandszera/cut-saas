"""initial schema

Revision ID: 20260424_0001
Revises:
Create Date: 2026-04-24 00:00:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260424_0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("password_hash", sa.String(), nullable=False),
        sa.Column("display_name", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_users_email"), "users", ["email"], unique=True)
    op.create_index(op.f("ix_users_id"), "users", ["id"], unique=False)

    op.create_table(
        "workspaces",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column("owner_user_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_workspaces_id"), "workspaces", ["id"], unique=False)
    op.create_index(op.f("ix_workspaces_owner_user_id"), "workspaces", ["owner_user_id"], unique=False)
    op.create_index(op.f("ix_workspaces_slug"), "workspaces", ["slug"], unique=True)

    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=True),
        sa.Column("source_type", sa.String(), nullable=False),
        sa.Column("source_value", sa.Text(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("video_path", sa.String(), nullable=True),
        sa.Column("audio_path", sa.String(), nullable=True),
        sa.Column("transcript_path", sa.String(), nullable=True),
        sa.Column("result_path", sa.String(), nullable=True),
        sa.Column("detected_niche", sa.String(), nullable=True),
        sa.Column("niche_confidence", sa.String(), nullable=True),
        sa.Column("transcript_insights", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_jobs_id"), "jobs", ["id"], unique=False)
    op.create_index(op.f("ix_jobs_workspace_id"), "jobs", ["workspace_id"], unique=False)

    op.create_table(
        "niche_definitions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("keywords_json", sa.Text(), nullable=False),
        sa.Column("weights_json", sa.Text(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("llm_notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_niche_definitions_id"), "niche_definitions", ["id"], unique=False)
    op.create_index(op.f("ix_niche_definitions_slug"), "niche_definitions", ["slug"], unique=True)
    op.create_index(op.f("ix_niche_definitions_workspace_id"), "niche_definitions", ["workspace_id"], unique=False)

    op.create_table(
        "niche_keywords",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=True),
        sa.Column("niche", sa.String(), nullable=False),
        sa.Column("keyword", sa.String(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("occurrences", sa.Integer(), nullable=False),
        sa.Column("distinct_jobs", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_niche_keywords_id"), "niche_keywords", ["id"], unique=False)
    op.create_index(op.f("ix_niche_keywords_keyword"), "niche_keywords", ["keyword"], unique=False)
    op.create_index(op.f("ix_niche_keywords_niche"), "niche_keywords", ["niche"], unique=False)
    op.create_index(op.f("ix_niche_keywords_workspace_id"), "niche_keywords", ["workspace_id"], unique=False)

    op.create_table(
        "workspace_members",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "user_id", name="uq_workspace_members_workspace_user"),
    )
    op.create_index(op.f("ix_workspace_members_id"), "workspace_members", ["id"], unique=False)
    op.create_index(op.f("ix_workspace_members_user_id"), "workspace_members", ["user_id"], unique=False)
    op.create_index(op.f("ix_workspace_members_workspace_id"), "workspace_members", ["workspace_id"], unique=False)

    op.create_table(
        "candidates",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("mode", sa.String(), nullable=False),
        sa.Column("start_time", sa.Float(), nullable=False),
        sa.Column("end_time", sa.Float(), nullable=False),
        sa.Column("duration", sa.Float(), nullable=False),
        sa.Column("heuristic_score", sa.Float(), nullable=True),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("opening_text", sa.Text(), nullable=True),
        sa.Column("closing_text", sa.Text(), nullable=True),
        sa.Column("full_text", sa.Text(), nullable=True),
        sa.Column("hook_score", sa.Float(), nullable=True),
        sa.Column("clarity_score", sa.Float(), nullable=True),
        sa.Column("closure_score", sa.Float(), nullable=True),
        sa.Column("emotion_score", sa.Float(), nullable=True),
        sa.Column("duration_fit_score", sa.Float(), nullable=True),
        sa.Column("transcript_context_score", sa.Float(), nullable=True),
        sa.Column("llm_score", sa.Float(), nullable=True),
        sa.Column("llm_why", sa.Text(), nullable=True),
        sa.Column("llm_title", sa.Text(), nullable=True),
        sa.Column("llm_hook", sa.Text(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("is_favorite", sa.Boolean(), nullable=False),
        sa.Column("editorial_notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_candidates_id"), "candidates", ["id"], unique=False)
    op.create_index(op.f("ix_candidates_job_id"), "candidates", ["job_id"], unique=False)

    op.create_table(
        "clips",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("mode", sa.String(), nullable=False),
        sa.Column("start_time", sa.Float(), nullable=False),
        sa.Column("end_time", sa.Float(), nullable=False),
        sa.Column("duration", sa.Float(), nullable=False),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("reason", sa.String(), nullable=True),
        sa.Column("text", sa.String(), nullable=True),
        sa.Column("headline", sa.String(), nullable=True),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("hashtags", sa.String(), nullable=True),
        sa.Column("suggested_filename", sa.String(), nullable=True),
        sa.Column("render_preset", sa.String(), nullable=True),
        sa.Column("publication_status", sa.String(), nullable=False),
        sa.Column("subtitles_burned", sa.Boolean(), nullable=False),
        sa.Column("output_path", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_clips_id"), "clips", ["id"], unique=False)
    op.create_index(op.f("ix_clips_job_id"), "clips", ["job_id"], unique=False)

    op.create_table(
        "job_steps",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("step_name", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_job_steps_id"), "job_steps", ["id"], unique=False)
    op.create_index(op.f("ix_job_steps_job_id"), "job_steps", ["job_id"], unique=False)
    op.create_index(op.f("ix_job_steps_step_name"), "job_steps", ["step_name"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_job_steps_step_name"), table_name="job_steps")
    op.drop_index(op.f("ix_job_steps_job_id"), table_name="job_steps")
    op.drop_index(op.f("ix_job_steps_id"), table_name="job_steps")
    op.drop_table("job_steps")
    op.drop_index(op.f("ix_clips_job_id"), table_name="clips")
    op.drop_index(op.f("ix_clips_id"), table_name="clips")
    op.drop_table("clips")
    op.drop_index(op.f("ix_candidates_job_id"), table_name="candidates")
    op.drop_index(op.f("ix_candidates_id"), table_name="candidates")
    op.drop_table("candidates")
    op.drop_index(op.f("ix_workspace_members_workspace_id"), table_name="workspace_members")
    op.drop_index(op.f("ix_workspace_members_user_id"), table_name="workspace_members")
    op.drop_index(op.f("ix_workspace_members_id"), table_name="workspace_members")
    op.drop_table("workspace_members")
    op.drop_index(op.f("ix_niche_keywords_workspace_id"), table_name="niche_keywords")
    op.drop_index(op.f("ix_niche_keywords_niche"), table_name="niche_keywords")
    op.drop_index(op.f("ix_niche_keywords_keyword"), table_name="niche_keywords")
    op.drop_index(op.f("ix_niche_keywords_id"), table_name="niche_keywords")
    op.drop_table("niche_keywords")
    op.drop_index(op.f("ix_niche_definitions_workspace_id"), table_name="niche_definitions")
    op.drop_index(op.f("ix_niche_definitions_slug"), table_name="niche_definitions")
    op.drop_index(op.f("ix_niche_definitions_id"), table_name="niche_definitions")
    op.drop_table("niche_definitions")
    op.drop_index(op.f("ix_jobs_workspace_id"), table_name="jobs")
    op.drop_index(op.f("ix_jobs_id"), table_name="jobs")
    op.drop_table("jobs")
    op.drop_index(op.f("ix_workspaces_slug"), table_name="workspaces")
    op.drop_index(op.f("ix_workspaces_owner_user_id"), table_name="workspaces")
    op.drop_index(op.f("ix_workspaces_id"), table_name="workspaces")
    op.drop_table("workspaces")
    op.drop_index(op.f("ix_users_id"), table_name="users")
    op.drop_index(op.f("ix_users_email"), table_name="users")
    op.drop_table("users")
