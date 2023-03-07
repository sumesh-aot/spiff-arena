from __future__ import annotations

from spiffworkflow_backend.models.db import db
from spiffworkflow_backend.models.db import SpiffworkflowBaseDBModel


# delta algorithm <- just to save it for when we want to try to implement it:
#   a = {"hey": { "hey2": 2, "hey3": 3, "hey6": 7 }, "hey30": 3, "hey40": 4}
#   b = {"hey": { "hey2": 4, "hey5": 3 }, "hey20": 2, "hey30": 3}
#   a_keys = set(a.keys())
#   b_keys = set(b.keys())
#   removed = a_keys - b_keys
#   added_keys = b_keys - a_keys
#   keys_present_in_both = a_keys & b_keys
#   changed = {}
#   for key_in_both in keys_present_in_both:
#       if a[key_in_both] != b[key_in_both]:
#           changed[key_in_both] = b[key_in_both]
#   added = {}
#   for added_key in added_keys:
#       added[added_key] = b[added_key]
#   final_tuple = [added, removed, changed]
class JsonDataModel(SpiffworkflowBaseDBModel):
    __tablename__ = "json_data"
    id: int = db.Column(db.Integer, primary_key=True)

    # this is a sha256 hash of spec and serializer_version
    hash: str = db.Column(db.String(255), nullable=False, index=True, unique=True)
    data: dict = db.Column(db.JSON, nullable=False)