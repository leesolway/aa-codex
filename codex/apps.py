from django.apps import AppConfig


class CodexConfig(AppConfig):
    name = "codex"
    verbose_name = "Corporation Codex"

    def ready(self):
        try:
            from . import auth_hooks  # noqa: F401
        except Exception:
            pass
