"""Insert ~20 FAKE registry records so the mock registry checker has something to match.

Run (after `alembic upgrade head`): python -m scripts.seed_registry

Includes a record whose name differs slightly from the demo doctor's (fuzzy name match) and an
inactive registration, to exercise the admin evidence badges.
"""

from __future__ import annotations

from app.db import SessionLocal
from app.models.registry import RegistryRecord

# (reg_number, council, full_name, reg_year, is_active)
RECORDS = [
    ("MH-2015-12345", "Maharashtra Medical Council", "Dr. Priya Sharma", 2015, True),
    ("GJ-2018-67890", "Gujarat Medical Council", "Dr. Arjun Patel", 2018, True),
    ("KA-2020-11111", "Karnataka Medical Council", "Sneha Kumar", 2020, True),  # near-match name
    ("DL-2012-99999", "Delhi Medical Council", "Dr. Ramesh Kapoor", 2012, False),  # inactive
    ("TN-2010-22001", "Tamil Nadu Medical Council", "Dr. Lakshmi Narayanan", 2010, True),
    ("TN-2016-22002", "Tamil Nadu Medical Council", "Dr. Karthik Subramanian", 2016, True),
    ("KL-2014-33001", "Kerala Medical Council", "Dr. Anjali Menon", 2014, True),
    ("KL-2019-33002", "Kerala Medical Council", "Dr. Thomas George", 2019, True),
    ("WB-2011-44001", "West Bengal Medical Council", "Dr. Sourav Banerjee", 2011, True),
    ("WB-2017-44002", "West Bengal Medical Council", "Dr. Ritika Das", 2017, True),
    ("UP-2013-55001", "Uttar Pradesh Medical Council", "Dr. Vivek Mishra", 2013, True),
    ("UP-2021-55002", "Uttar Pradesh Medical Council", "Dr. Neha Srivastava", 2021, True),
    ("RJ-2009-66001", "Rajasthan Medical Council", "Dr. Mahesh Rathore", 2009, True),
    ("RJ-2018-66002", "Rajasthan Medical Council", "Dr. Pooja Shekhawat", 2018, True),
    ("MP-2015-77001", "Madhya Pradesh Medical Council", "Dr. Alok Tiwari", 2015, True),
    ("PB-2012-88001", "Punjab Medical Council", "Dr. Harpreet Singh", 2012, True),
    ("TS-2020-99001", "Telangana State Medical Council", "Dr. Sai Kiran Reddy", 2020, True),
    ("AP-2016-99002", "Andhra Pradesh Medical Council", "Dr. Divya Lakshmi", 2016, True),
    ("NMC-2022-10001", "NMC", "Dr. Aarav Khanna", 2022, True),
    ("NMC-2019-10002", "NMC", "Dr. Meera Joshi", 2019, True),
]


def seed_registry() -> int:
    """Add any missing fake records. Returns how many were inserted."""
    inserted = 0
    with SessionLocal() as db:
        existing = {(r.reg_number, r.council, r.full_name) for r in db.query(RegistryRecord).all()}
        for reg_number, council, full_name, reg_year, is_active in RECORDS:
            if (reg_number, council, full_name) in existing:
                continue
            db.add(
                RegistryRecord(
                    reg_number=reg_number,
                    council=council,
                    full_name=full_name,
                    reg_year=reg_year,
                    is_active=is_active,
                )
            )
            inserted += 1
        db.commit()
    return inserted


if __name__ == "__main__":
    print(f"Inserted {seed_registry()} fake registry record(s).")
