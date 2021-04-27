# Generated by Django 2.2.19 on 2021-04-01 12:46

from django.db import migrations
from labs.models import get_short_uuid


# Move IDS for cases, events, and categories to strings from integers
def change_ids(apps, schema_editor):
    Timeline = apps.get_model("labs", "Timeline")
    for timeline in Timeline.objects.all():
        for event in timeline.timeline['events']:
            if type(event['id']) is int:
                event['id'] = get_short_uuid()

        for case in timeline.timeline['cases']:
            if type(case['id']) is int:
                case['id'] = get_short_uuid()

        if 'categories' in timeline.timeline:
            for cat in timeline.timeline['categories']:
                if ('id' in cat and type(cat['id']) is int) or ('id' not in cat):
                    cat['id'] = get_short_uuid()

        timeline.save()


def add_categories(apps, schema_editor):
    Timeline = apps.get_model("labs", "Timeline")

    for timeline in Timeline.objects.all():
        for event in timeline.timeline['events']:
            if 'categories' not in event:
                event['categories'] = []
        for case in timeline.timeline['cases']:
            if 'categories' not in case:
                case['categories'] = []
        if 'categories' not in timeline.timeline:
            timeline.timeline['categories'] = []

        timeline.save()


class Migration(migrations.Migration):
    dependencies = [
        ('labs', '0006_auto_20210401_1246'),
    ]

    operations = [
        migrations.RunPython(change_ids, migrations.RunPython.noop),
        migrations.RunPython(add_categories, migrations.RunPython.noop),
    ]