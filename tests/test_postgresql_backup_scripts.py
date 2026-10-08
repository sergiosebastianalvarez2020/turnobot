from pathlib import Path

from scripts import backup_postgresql, restore_postgresql, verify_backup_postgresql


def test_build_pg_env_from_url_masks_password():
    env = backup_postgresql.build_pg_env(
        "postgresql://turnobot:super-secret@db.example.com:5432/turnobot"
    )

    assert env["PGHOST"] == "db.example.com"
    assert env["PGPORT"] == "5432"
    assert env["PGDATABASE"] == "turnobot"
    assert env["PGUSER"] == "turnobot"
    assert env["PGPASSWORD"] == "super-secret"
    assert "super-secret" not in backup_postgresql.sanitize_pg_env(env)["PGPASSWORD"]


def test_build_pg_dump_command_uses_custom_compressed_dump():
    cmd = backup_postgresql.build_pg_dump_command(
        "postgresql://turnobot:super-secret@db.example.com:5432/turnobot",
        Path("/tmp/turnobot-pg-20260930-010101.dump"),
    )

    assert cmd[0] == "pg_dump"
    assert "--format=c" in cmd
    assert "--compress=9" in cmd
    assert "--dbname=postgresql://turnobot@db.example.com:5432/turnobot" in cmd
    assert "--file=/tmp/turnobot-pg-20260930-010101.dump" in cmd


def test_build_pg_restore_command_uses_target_database():
    cmd = restore_postgresql.build_pg_restore_command(
        Path("/tmp/turnobot-pg-20260930-010101.dump"), "turnobot_restore_test"
    )

    assert cmd[0] == "pg_restore"
    assert "--clean" in cmd
    assert "--if-exists" in cmd
    assert "--dbname=turnobot_restore_test" in cmd
    assert str(Path("/tmp/turnobot-pg-20260930-010101.dump")) in cmd


def test_verify_backup_uses_restore_list_command():
    cmd = verify_backup_postgresql.build_restore_listing_command(
        Path("/tmp/turnobot-pg-20260930-010101.dump")
    )

    assert cmd[0] == "pg_restore"
    assert "--list" in cmd
    assert cmd[-1].replace("\\", "/").endswith("/turnobot-pg-20260930-010101.dump")
