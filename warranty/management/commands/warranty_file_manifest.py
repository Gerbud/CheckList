from pathlib import PurePosixPath

from django.core.management.base import BaseCommand

from warranty.models import WarrantyAttachment


class Command(BaseCommand):
    help = 'Печатает безопасный список относительных путей файлов Bitrix для rsync --files-from.'

    def handle(self, *args, **options):
        for value in WarrantyAttachment.objects.order_by('source_path').values_list('source_path', flat=True):
            path = PurePosixPath(str(value).removeprefix('/upload/'))
            if path.is_absolute() or '..' in path.parts or not path.parts:
                continue
            self.stdout.write(path.as_posix())
