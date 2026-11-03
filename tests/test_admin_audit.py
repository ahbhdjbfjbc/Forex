import json

from src import admin_audit


def test_admin_audit_persists_result_without_command_arguments(tmp_path, monkeypatch):
    monkeypatch.setattr(admin_audit, "get_data_dir", lambda: tmp_path)
    admin_audit.record_admin_action("123456", "/apply", "succeeded")
    path = tmp_path / "admin_audit.jsonl"
    record = json.loads(path.read_text(encoding="utf-8").strip())
    assert record["actor_user_id"] == "123456"
    assert record["action"] == "/apply"
    assert record["result"] == "succeeded"
    assert "timestamp" in record
    assert "SECRET" not in path.read_text(encoding="utf-8")


def test_admin_audit_redacts_invalid_action_and_actor(tmp_path, monkeypatch):
    monkeypatch.setattr(admin_audit, "get_data_dir", lambda: tmp_path)
    admin_audit.record_admin_action("@username", "/scan private-token=SECRET", "unknown")
    record = json.loads((tmp_path / "admin_audit.jsonl").read_text(encoding="utf-8").strip())
    assert record["actor_user_id"] == "unknown"
    assert record["action"] == "unknown"
    assert record["result"] == "unknown"
