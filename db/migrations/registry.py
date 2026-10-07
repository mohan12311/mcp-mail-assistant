"""Immutable ordered migration registry."""

from db.migrations.v0001_initial import MIGRATION as V0001
from db.migrations.v0002_mail_transport import MIGRATION as V0002
from db.migrations.v0003_outbound_send_ledger import MIGRATION as V0003
from db.migrations.v0004_reply_threading import MIGRATION as V0004
from db.migrations.v0005_attachment_processing import MIGRATION as V0005
from db.migrations.v0006_data_consistency import MIGRATION as V0006
from db.migrations.v0007_approval_security import MIGRATION as V0007
from db.migrations.v0008_slack_conversation import MIGRATION as V0008
from db.migrations.v0009_user_preferences import MIGRATION as V0009


MIGRATIONS = (V0001, V0002, V0003, V0004, V0005, V0006, V0007, V0008, V0009)

if tuple(migration.version for migration in MIGRATIONS) != tuple(
    range(1, len(MIGRATIONS) + 1)
):
    raise RuntimeError("migration versions must be contiguous and start at 1")
