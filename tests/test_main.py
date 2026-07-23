import importlib
import sys

def test_main_wires_sqlite_service_without_global_messages(
  monkeypatch,
  tmp_path
):
  database_path = tmp_path / "min-chat.db"
  monkeypatch.setenv("DEEPSEEK_API_KEY","test-only-key")
  monkeypatch.setenv("CHAT_DB_PATH", str(database_path))

  sys.modules.pop("main",None)
  main = importlib.import_module("main")

  paths = {route.path for route in main.app.routes}
  assert not hasattr(main, "messages")
  assert database_path.exists()
  assert "/conversations/" in paths
  assert "/conversations/{conversation_id}/chat/" in paths