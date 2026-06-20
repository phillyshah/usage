"""Shared test setup: force OFFLINE_MODE with an isolated temp data dir before
any app module imports the settings singleton."""
import os
import tempfile

os.environ.setdefault("OFFLINE_MODE", "true")
os.environ.setdefault("LOCAL_DATA_DIR", tempfile.mkdtemp(prefix="usage-test-"))
os.environ.setdefault("ANTHROPIC_API_KEY", "")
