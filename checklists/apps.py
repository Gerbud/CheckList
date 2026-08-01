from django.apps import AppConfig


class ChecklistsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'checklists'
    verbose_name = 'Чек-листы'

    def ready(self):
        from checklists import signals  # noqa: F401
